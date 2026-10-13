# -*- coding: utf-8 -*-
"""状态/模型/IDE/档案相关 API：/api/state、/api/ides、/api/models、/api/profiles、/api/codex/sync。"""
import json
import os
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

from .. import state
from ..catalog import (CODEX_CATALOG, codex_catalog_exists, restart_codex,
                       sync_codex_catalog)
from ..ides import (IDE_ADAPTERS, ide_adapter, ide_backup_exists, ide_backup_status, ide_enabled,
                    ide_inject_disabled_reason, ide_write_allowed,
                    ide_write_globally_blocked, set_ide_enabled,
                    sync_all_ide_catalogs)
from ..log import _log
from ..models import _natural_key, models_view
from ..net import (_urlopen, auto_sync_info, close_quietly, ensure_auto_profiles,
                   fetch_openrouter_profiles, read_limited)
from ..paths import PROFILES_PATH, WBRELAY_VERSION
from ..profiles import load_profiles, parse_catalog_profiles, save_profiles
from ..redact import redact
from ..routing import (_route_models_set, route_table, supplier_cooled,
                       supplier_cooldown_reason, supplier_health)
from ..settings import (_mask_key, get_keys, get_suppliers,
                        group_models_enabled_map, load_settings,
                        models_enabled_map, set_group_models_enabled)
from ..state import _cooldowns
from ..usage import stats_view


# 用户提交的 catalog URL 导入：拉取超时（响应体上限见 net.MAX_UPSTREAM_JSON_BYTES）
CATALOG_URL_TIMEOUT_SEC = 30


class ApiStateMixin:
    # ---- state ----
    def _api_state(self):
        raw = load_settings()
        sups = []
        for s in get_suppliers():
            raw_key = s.get("api_key") or ""
            sups.append({
                "id": s["id"], "name": s["name"], "base_url": s["base_url"],
                "wire_api": s["wire_api"], "models": list(s["models"]),
                "enabled": s["enabled"], "timeout_sec": s["timeout_sec"],
                "key_set": bool(raw_key),
                "masked": _mask_key(raw_key),
                "cooling": supplier_cooled(s["id"]),
                "cool_until": _cooldowns.get(s["id"], 0.0),
                "cool_reason": supplier_cooldown_reason(s["id"]),
                "health": supplier_health(s["id"]),
            })
        keys = [{"id": k.get("id") or "", "name": k.get("name") or "",
                 "masked": (k.get("key", "")[:4] + "******" + k.get("key", "")[-4:])
                 if len(k.get("key", "")) > 8 else "******",
                 "enabled": k.get("enabled", True) is not False}
                for k in get_keys()]
        return self._json(200, {
            "suppliers": sups,
            "api_keys": keys,
            "port": state._SERVER_PORT,
            "version": WBRELAY_VERSION,
            "stats": stats_view(),
        })

    def _api_key_current(self):
        """按需取回**网关自身**签发的 Key（只读，只回第一个）。

        边界（2026-10-14 按所有者要求新增，用于「其他 IDE」卡片上的复制按钮）：
          · **上游供应商的 `api_key` 仍然一律不回显**（只回 `key_set` / 掩码）；
          · 这里只回本网关自己签发的本机 Key —— 它本来就明文躺在 `settings.json` 里，
            掩码只防「页面/截图泄露」，而复制按钮**不把明文写进 DOM**；
          · 仍受 `/api/*` 的本机门禁（Host / Origin）与（开启时的）admin token 约束
            （路由层已加 `_admin_ok()` 校验）。
        """
        keys = get_keys()
        first = keys[0] if keys else {}
        return self._json(200, {"id": first.get("id") or "",
                                "name": first.get("name") or "",
                                "key": first.get("key") or ""})

    def _api_ides(self):
        """列出所有 IDE 适配器状态。"""
        out = []
        detected_count = 0
        for adp in IDE_ADAPTERS:
            detected = bool(adp["detect"]())
            if detected:
                detected_count += 1
            item = {
                "id": adp["id"],
                "name": adp["name"],
                "detected": detected,
                "enabled": bool(ide_enabled(adp["id"])),
                "restartable": bool(adp.get("restartable", False)),
                "config_paths": adp["config_paths"](),
                "owner": adp["owner"](),
            }
            item.update(ide_backup_status(adp["id"]))
            out.append(item)
        return self._json(200, {"ides": out, "detected": detected_count})

    def _api_ide_toggle(self, ide_id):
        """切换 IDE 接管开关：开=立即备份+注入；关=立即恢复。"""
        adp = ide_adapter(ide_id)
        if adp is None:
            return self._err(404, "unknown ide: %s" % ide_id)
        blocked = ide_write_globally_blocked()
        if blocked:
            return self._err(403, blocked)
        payload = self._payload() or {}
        on = bool(payload.get("enabled", False))
        if on and not adp["detect"]():
            return self._err(400, "%s 未检测到，请先安装" % adp["name"])
        if not set_ide_enabled(ide_id, on):
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        if on:
            try:
                if not adp["backup"]() or not adp["inject"](state._SERVER_PORT):
                    set_ide_enabled(ide_id, False)
                    return self._err(500, "%s 接管未完成（备份或配置写入失败）" % adp["name"])
                adp["sync_catalog"]()
            except Exception as exc:
                set_ide_enabled(ide_id, False)
                return self._err(500, "%s 接管失败: %s" % (adp["name"], exc))
        else:
            try:
                if ide_backup_exists(ide_id):
                    if adp["restore"]() is False:
                        return self._err(500, "%s 恢复未完成，备份已保留" % adp["name"])
            except Exception as exc:
                return self._err(500, "%s 恢复失败: %s" % (adp["name"], exc))
        return self._json(200, {"ok": True, "enabled": on})

    def _api_ide_restart(self, ide_id):
        """同步 catalog + 重启指定 IDE。"""
        adp = ide_adapter(ide_id)
        if adp is None:
            return self._err(404, "unknown ide: %s" % ide_id)
        if not ide_write_allowed(ide_id):
            return self._err(403, ide_inject_disabled_reason() or "IDE 接管已禁用")
        if not adp.get("restartable", False):
            return self._err(400, "%s 是命令行工具，请重启终端会话后生效" % adp["name"])
        try:
            adp["sync_catalog"]()
        except Exception as exc:
            _log("[WBRelay] %s catalog 同步失败: %s" % (ide_id, exc))
        killed, launched = adp["restart"]()
        return self._json(200, {
            "ok": True, "killed": killed or "(无)", "launched": launched,
        })

    # ---- 模型配置（拉取缓存 + 全局启停） ----
    def _api_models(self):
        qs = parse_qs(urlparse(self.path).query)
        refresh = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
        view = models_view(refresh=refresh)
        # 自动补缺：当前已知模型中档案缺失的，从 OpenRouter 同步（源有 24h 缓存，不频繁联网）
        added = 0
        try:
            all_ids = [m["id"] for sup in (view.get("suppliers") or {}).values()
                       for m in (sup.get("models") or [])]
            if refresh:
                # ② 重新拉取时强制更新档案库源（即使无缺失模型也刷新同步状态）
                fetch_openrouter_profiles(force=True)
            added = ensure_auto_profiles(all_ids)
        except Exception:
            pass
        view["models_enabled"] = models_enabled_map()          # 兼容旧前端（全局启停）
        view["group_models_enabled"] = group_models_enabled_map()   # 组级启用（新）
        view["route_table"] = route_table()                    # 各供应商可路由清单
        view["route_models"] = sorted(_route_models_set(), key=_natural_key)
        view["profiles"] = {"count": len(load_profiles()), "auto": auto_sync_info(),
                            "added": added}
        if refresh:
            try:
                sync_all_ide_catalogs()
            except Exception as exc:
                _log("[WBRelay] IDE catalog 同步失败（模型刷新）: %s" % exc)
        return self._json(200, view)

    def _save_models_enabled(self):
        payload = self._payload()
        if payload is None:
            return self._err(400, "invalid json")
        gem = payload.get("group_models_enabled")
        if gem is None and isinstance(payload.get("models_enabled"), dict):
            # 兼容旧全局启停提交：显式启用的模型应用到所有组
            gem = {}
            for sup in get_suppliers():
                base = sup["base_url"].rstrip("/")
                if base:
                    gem.setdefault(base, {})
            for mid, v in payload["models_enabled"].items():
                if v is True:
                    for base in gem:
                        gem[base][mid] = True
        if not isinstance(gem, dict):
            return self._err(400, "group_models_enabled must be an object")
        if not set_group_models_enabled(gem):
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        try:
            sync_all_ide_catalogs()
        except Exception as exc:
            _log("[WBRelay] IDE catalog 同步失败（模型启停已保存）: %s" % exc)
        return self._json(200, {"ok": True,
                                "group_models_enabled": group_models_enabled_map()})

    # ---- 模型档案库 ----
    def _api_profiles(self):
        """当前模型档案库状态。"""
        prof = load_profiles()
        try:
            mtime = os.path.getmtime(PROFILES_PATH)
            updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        except OSError:
            updated = ""
        models = [{"id": k, "reasoning_efforts": v.get("reasoning_efforts") or [],
                   "context_window": v.get("context_window"),
                   "max_output_tokens": v.get("max_output_tokens"),
                   "supports_vision": bool(v.get("supports_vision")),
                   "prompt_price": v.get("prompt_price") or 0,
                   "completion_price": v.get("completion_price") or 0,
                   "cache_read_price": v.get("cache_read_price") or 0}
                  for k, v in sorted(prof.items()) if not k.startswith("_")]
        info = auto_sync_info()
        return self._json(200, {"count": len(prof), "updated_at": updated, "models": models,
                                "auto": info})

    def _api_profiles_auto(self):
        """手动立即从网络同步：强拉 OpenRouter，补全当前档案缺失的所有已知模型。"""
        all_ids = []
        for s in get_suppliers():
            all_ids.extend(s.get("models") or [])
        view = models_view(refresh=False)
        for sup in (view.get("suppliers") or {}).values():
            for m in (sup.get("models") or []):
                all_ids.append(m["id"])
        src = fetch_openrouter_profiles(force=True)
        if not src:
            return self._err(502, "OpenRouter 拉取失败（网络不通或源不可用），请稍后再试")
        prof = load_profiles()
        merged = dict(prof)
        added = 0
        for m in set(all_ids):
            if m and m not in merged and m in src:
                merged[m] = src[m]
                added += 1
        if not save_profiles(merged):
            return self._err(503, "模型档案未保存：只读模式或写盘失败", "server_error")
        return self._json(200, {"ok": True, "added": added, "total": len(merged),
                                "detail": "OpenRouter 全量 %d 个模型" % len(src)})

    def _api_profiles_import(self):
        """导入模型档案：source=external（本地 catalog 文件）| source=url（网络 catalog）。合并覆盖同名。"""
        payload = self._payload()
        source = str((payload or {}).get("source") or "").strip()
        new_profiles = {}
        detail = ""
        if source in ("external", "upstream"):
            path = str(load_settings().get("upstream_catalog_path") or "").strip()
            if not path or not os.path.exists(path):
                return self._err(404, "未配置外部模型 catalog 路径（settings.upstream_catalog_path）")
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                # 异常原文可能夹带路径/凭据：脱敏后再回显
                return self._err(500, "读取 catalog 失败：%s" % redact(exc, 200))
            new_profiles = parse_catalog_profiles(data)
            detail = "外部 catalog 文件（%d 个）" % len(new_profiles)
        elif source == "url":
            url = str((payload or {}).get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return self._err(400, "url 必须以 http(s):// 开头")
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                # 边界：timeout=30s + 响应体上限（超限不整体读进内存）+ 句柄一定关闭
                with _urlopen(req, CATALOG_URL_TIMEOUT_SEC) as resp:
                    data = json.loads(read_limited(resp).decode("utf-8", "replace"))
            except Exception as exc:
                # 不把带凭据的原文回显给客户端：先脱敏
                return self._err(500, "拉取失败：%s" % redact(exc, 200))
            new_profiles = parse_catalog_profiles(data)
            if not new_profiles:
                return self._err(400, "URL 内容不是可识别的模型 catalog（需含 models 数组，每项含 slug）")
            detail = "URL catalog（%d 个）" % len(new_profiles)
        else:
            return self._err(400, "source 必须是 external 或 url")
        if not new_profiles:
            return self._err(400, "未解析到任何模型档案")
        cur = load_profiles()
        before = len(cur)
        merged = dict(cur)
        merged.update(new_profiles)
        if not save_profiles(merged):
            return self._err(503, "模型档案未保存：只读模式或写盘失败", "server_error")
        return self._json(200, {"ok": True, "imported": len(new_profiles),
                                "total": len(merged), "added": len(merged) - before,
                                "detail": detail})

    def _api_codex_sync(self):
        """同步模型配置到 Codex catalog；重启 Codex 需显式传 restart=true。"""
        payload = self._payload() or {}
        if not ide_write_allowed("codex"):
            return self._err(403, ide_inject_disabled_reason() or "IDE 接管已禁用")
        if not codex_catalog_exists():
            return self._err(400, "未找到 Codex 模型配置：%s" % CODEX_CATALOG)
        try:
            written, hidden = sync_codex_catalog()
        except Exception as exc:
            return self._err(500, "同步模型配置失败：%s" % exc)
        if not bool(payload.get("restart", False)):
            return self._json(200, {
                "ok": True,
                "written": written,
                "hidden": hidden,
                "restarted": False,
                "hint": "已同步模型清单；未重启 Codex（如需重启请传 restart=true，或手动重启）",
                "catalog": CODEX_CATALOG,
            })
        killed, launched = restart_codex()
        return self._json(200, {
            "ok": True,
            "written": written,
            "hidden": hidden,
            "restarted": True,
            "killed": killed or "(无)",
            "launched": launched,
            "catalog": CODEX_CATALOG,
        })
