# -*- coding: utf-8 -*-
"""管理类 API：供应商、API Key、设置、备份恢复。"""
import os
import time
import urllib.error
import urllib.request

from .. import state
from ..autostart import (gateway_autostart_registered, set_gateway_autostart)
from ..backup import (list_backups, make_backup, restore_backup)
from ..clipboard import read_text as read_clipboard_text
from ..ides import sync_all_ide_catalogs
from ..ides.base import IDE_BACKUP_DIR
from ..log import _log
from ..paths import (DATA, LOG_PATH, MODELS_CACHE_PATH, PROFILES_PATH,
                     SETTINGS_PATH, USAGE_PATH)
from ..net import _urlopen, close_quietly, read_limited
from ..redact import redact
from ..settings import (_mask_key, admin_token, admin_token_enabled,
                        cooldown_429_secs, cooldown_secs, configured_port,
                        default_timeout_sec, get_keys, get_suppliers,
                        group_models_enabled_map, load_settings,
                        model_prices, models_enabled_map, require_api_key,
                        set_suppliers, update_settings, usage_refresh_secs,
                        usage_retention_days)
from ..state import _cooldowns, _lock
from ..validate import (MAX_API_KEY, MAX_SUPPLIER_NAME, check_base_url,
                        check_models, check_port, check_supplier_count,
                        check_supplier_id, check_text)

# 单供应商测速（GET {base}/models）的边界：请求超时 + 响应体上限
SUPPLIER_PING_TIMEOUT_SEC = 10
MAX_SUPPLIER_PING_BODY = 2 * 1024 * 1024


def exposed_paths():
    """设置页「配置文件位置」：网关自身数据/配置文件的实际路径（只读展示）。"""
    return {
        "data_dir": DATA,
        "settings": SETTINGS_PATH,
        "usage": USAGE_PATH,
        "profiles": PROFILES_PATH,
        "models_cache": MODELS_CACHE_PATH,
        "log": LOG_PATH,
        "ide_backups": IDE_BACKUP_DIR,
    }


class ApiAdminMixin:
    def _api_clipboard(self):
        """本机剪贴板文本（供看板「粘贴」按钮用，免浏览器权限弹窗）。

        只读；非 Windows 或读取失败回 supported=False，前端自行回退浏览器剪贴板 API。
        内容不进日志。
        """
        text = read_clipboard_text()
        if text is None:
            return self._json(200, {"supported": False, "text": ""})
        return self._json(200, {"supported": True, "text": text})

    def _save_suppliers(self):
        payload = self._payload()
        if payload is None or not isinstance(payload.get("suppliers"), list):
            return self._err(400, "suppliers must be a list")
        err = check_supplier_count(payload["suppliers"])
        if err:
            return self._err(400, err)
        existing = {s["id"]: s for s in get_suppliers()}
        cleaned = []
        for item in payload["suppliers"]:
            if not isinstance(item, dict):
                return self._err(400, "each supplier must be an object")
            sid, err = check_supplier_id(item.get("id"))
            if err:
                return self._err(400, err)
            # base_url 必须 http(s)、长度受限、且不允许内嵌用户名/密码
            base, err = check_base_url(item.get("base_url"))
            if err:
                return self._err(400, err)
            key = str(item.get("api_key") or "").strip()
            if sid and sid in existing:
                old_key = existing[sid]["api_key"]
                # 空 Key 或提交的是掩码（前端编辑回显 sk-S***1XNS）→ 沿用旧 Key
                if not key or key == _mask_key(old_key):
                    key = old_key
            if not key:
                return self._err(400, "supplier api_key is required")
            key, err = check_text(key, MAX_API_KEY, "api_key")
            if err:
                return self._err(400, err)
            name, err = check_text(str(item.get("name") or sid or "中转").strip(),
                                   MAX_SUPPLIER_NAME, "供应商名称")
            if err:
                return self._err(400, err)
            models, err = check_models(item.get("models") or [])
            if err:
                return self._err(400, err)
            # models 允许为空：空列表 = 接管该供应商拉取到的全部模型（supplier_matches 已实现）
            try:
                timeout_sec = int(item.get("timeout_sec") or 120)
            except (TypeError, ValueError):
                timeout_sec = 120
            if timeout_sec < 1:
                timeout_sec = 120
            cleaned.append({
                "id": sid, "name": name,
                "base_url": base, "api_key": key,
                "wire_api": "chat" if str(item.get("wire_api") or "responses") == "chat" else "responses",
                "models": models,
                "enabled": item.get("enabled", True) is not False,
                "timeout_sec": timeout_sec,
            })
        if not set_suppliers(cleaned):
            # 只读模式或写盘失败：不得伪称保存成功（原配置保持不变）
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        try:
            sync_all_ide_catalogs()
        except Exception as exc:
            # 供应商已保存，但 catalog 没同步上 —— 不能静默（接口仍 200，日志要留痕）
            _log("[WBRelay] IDE catalog 同步失败（供应商已保存）: %s" % exc)
        return self._json(200, {"ok": True, "suppliers_saved": len(cleaned)})

    def _clone_supplier(self):
        """复制供应商：后端克隆（保留真实 Key），新 id + 名称加（副本），追加到末尾。"""
        payload = self._payload()
        sid = str((payload or {}).get("id") or "").strip()
        if not sid:
            return self._err(400, "supplier id is required")
        sups = get_suppliers()
        src = next((s for s in sups if s["id"] == sid), None)
        if src is None:
            return self._err(404, "supplier not found: %s" % sid)
        new_id = "sup_" + os.urandom(5).hex()
        new = dict(src)
        new["id"] = new_id
        new["name"] = str(src.get("name") or "中转") + "（副本）"
        sups.append(new)
        if not set_suppliers(sups):
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        return self._json(200, {"ok": True, "suppliers_saved": len(sups), "id": new_id,
                                "name": new["name"]})

    def _api_supplier_ping(self):
        """单供应商测速：GET {base}/models 计时（轻量、不消费对话额度）。"""
        payload = self._payload()
        sid = str((payload or {}).get("id") or "").strip()
        if not sid:
            return self._err(400, "supplier id is required")
        sup = next((s for s in get_suppliers() if s["id"] == sid), None)
        if sup is None:
            return self._err(404, "supplier not found: %s" % sid)
        base = sup["base_url"].rstrip("/")
        url = base + "/models"
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + (sup.get("api_key") or ""),
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        t0 = time.time()
        try:
            with _urlopen(req, SUPPLIER_PING_TIMEOUT_SEC) as resp:
                read_limited(resp, MAX_SUPPLIER_PING_BODY)   # 超限即停，不整体读进内存
                status = resp.status
            ms = int((time.time() - t0) * 1000)
            return self._json(200, {"ok": True, "ms": ms, "status": status})
        except urllib.error.HTTPError as exc:
            ms = int((time.time() - t0) * 1000)
            close_quietly(exc)
            return self._json(200, {"ok": False, "ms": ms, "status": exc.code,
                                    "error": "HTTP %d" % exc.code})
        except Exception as exc:
            ms = int((time.time() - t0) * 1000)
            # 异常文本可能夹带 URL 凭据：先脱敏再回显
            return self._json(200, {"ok": False, "ms": ms, "error": redact(exc, 100)})

    def _save_keys(self):
        payload = self._payload()
        if payload is None or not isinstance(payload.get("api_keys"), list):
            return self._err(400, "api_keys must be a list")
        existing = {k.get("id"): k for k in get_keys()}
        cleaned = []
        for item in payload["api_keys"]:
            if not isinstance(item, dict):
                return self._err(400, "each api key must be an object")
            kid = str(item.get("id") or "").strip()
            key = str(item.get("key") or "").strip()
            if not key and kid and kid in existing:
                key = existing[kid]["key"]
            if not key:
                return self._err(400, "api key is required")
            if not kid:
                kid = "k_" + os.urandom(5).hex()
            cleaned.append({"id": kid, "name": str(item.get("name") or "Codex").strip(),
                            "key": key, "enabled": item.get("enabled", True) is not False})
        # 持锁 read-modify-write：与并发 POST /api/settings、/api/suppliers 等互不覆盖
        def _apply(data):
            data["api_keys"] = cleaned
            return True

        ok, _result = update_settings(_apply)
        if not ok:
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        return self._json(200, {"ok": True, "api_keys_saved": len(cleaned)})

    # ---- 设置 ----
    def _api_settings(self):
        d = load_settings()
        paths = exposed_paths()
        return self._json(200, {
            "port": configured_port() or state._SERVER_PORT,
            "require_api_key": require_api_key(),
            "cooldown_sec": cooldown_secs(),
            "cooldown_429_sec": cooldown_429_secs(),
            "default_timeout_sec": default_timeout_sec(),
            "usage_retention_days": usage_retention_days(),
            "usage_refresh_sec": usage_refresh_secs(),
            "require_admin_token": admin_token_enabled(),
            "admin_token_set": bool(admin_token()),
            "autostart": bool(d.get("autostart")),
            "codex_provider": str(d.get("codex_provider") or "WBRelay"),
            "autostart_registered": gateway_autostart_registered(),
            "model_prices": model_prices(),
            "models_enabled": models_enabled_map(),
            "group_models_enabled": group_models_enabled_map(),
            "api_keys": [{"id": k.get("id") or "", "name": k.get("name") or "",
                          "masked": _mask_key(k.get("key") or ""),
                          "key_set": bool(k.get("key"))} for k in get_keys()],
            "paths": paths,
            "path_exists": {k: os.path.exists(v) for k, v in paths.items()},
        })

    def _save_settings(self):
        payload = self._payload()
        if payload is None:
            return self._err(400, "invalid json")
        # 保存前的开机自启状态：只有请求显式带 autostart 且值真的变了，
        # 才同步 HKCU Run（否则改其它设置会把已有启动项误删）。
        # 注意这里读的是**锁前**快照，只用于「值是否变了」的对比；
        # 真正落盘的配置以 update_settings 锁内重读的那份为准（不会覆盖别人的并发修改）。
        autostart_before = bool(load_settings().get("autostart"))
        # 新生成的管理令牌只在本次 POST 响应里回一次，之后只回布尔值（不落明文回显）
        issued_admin_token = []
        # ---- 先做纯校验（不碰配置、不落盘）：与修复前完全一致的 400 语义 ----
        cleaned = {}
        for key in ("require_api_key", "cooldown_sec", "cooldown_429_sec",
                    "default_timeout_sec", "usage_retention_days", "port"):
            if key in payload:
                try:
                    if key == "require_api_key":
                        cleaned[key] = bool(payload[key])
                    elif key == "port":
                        port, err = check_port(payload[key])
                        if err:
                            return self._err(400, err)
                        cleaned[key] = port
                    else:
                        cleaned[key] = int(payload[key])
                except (TypeError, ValueError):
                    return self._err(400, "invalid value for %s" % key)
        # 看板自动刷新间隔：允许 0（关闭自动刷新），clamp 到 0~600
        if "usage_refresh_sec" in payload:
            try:
                cleaned["usage_refresh_sec"] = max(0, min(600,
                                                          int(payload["usage_refresh_sec"])))
            except (TypeError, ValueError):
                return self._err(400, "invalid value for usage_refresh_sec")
        # Codex provider 名：空串则删除该字段（回到默认值 WBRelay）；
        # 改名后历史 Codex 会话会因 provider 查不到而报错，仅全新环境建议改默认名
        codex_provider = None
        if "codex_provider" in payload:
            name = str(payload.get("codex_provider") or "").strip()
            name, err = check_text(name, 64, "codex_provider")
            if err:
                return self._err(400, err)
            codex_provider = name
        if isinstance(payload.get("model_prices"), dict):
            prices = {}
            for k, v in payload["model_prices"].items():
                try:
                    prices[str(k).strip()] = float(v)
                except (TypeError, ValueError):
                    pass
            cleaned["model_prices"] = prices
        if isinstance(payload.get("models_enabled"), dict):
            cleaned["models_enabled"] = {str(k).strip(): bool(v)
                                         for k, v in payload["models_enabled"].items()
                                         if str(k).strip()}
        if "autostart" in payload:
            cleaned["autostart"] = bool(payload["autostart"])

        # ---- 再持锁做 read-modify-write：整段「读最新配置 → 应用 → 原子写」在同一把
        # RLock 内完成。两个并发 POST /api/settings（或 /api/settings 与 /api/keys、
        # /api/suppliers）因此串行化，后保存者看到的是前保存者的结果，不再互相覆盖。
        def _apply(current):
            for key, value in cleaned.items():
                current[key] = value
            if "require_admin_token" in payload:
                current["require_admin_token"] = bool(payload["require_admin_token"])
            if current.get("require_admin_token"):
                # 管理令牌：开启后若未提供则自动生成；新生成的令牌只在本次 POST 响应回一次，
                # 之后 GET /api/settings 只回 admin_token_set 布尔值，不再明文回传。
                new_tok = (str(payload.get("admin_token") or "").strip()
                           if "admin_token" in payload else "")
                if new_tok:
                    current["admin_token"] = new_tok
                elif not current.get("admin_token"):
                    current["admin_token"] = os.urandom(16).hex()
                    issued_admin_token.append(current["admin_token"])
            elif "require_admin_token" in payload:
                current["admin_token"] = ""
            if codex_provider is not None:
                if codex_provider:
                    current["codex_provider"] = codex_provider
                else:
                    current.pop("codex_provider", None)
            return True

        ok, _result = update_settings(_apply)
        if not ok:
            # 只读模式或写盘失败：不得伪称保存成功（原配置保持不变）
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        # 冷却清理由 save_settings 统一负责（保存成功才清）
        # provider 名改了：重新注入 config.toml（让 Codex 立即看到新 provider，不必重启网关）
        if "codex_provider" in payload:
            try:
                from ..ides import ides_backup_and_inject
                ides_backup_and_inject(configured_port() or state._SERVER_PORT)
            except Exception as exc:
                _log("[WBRelay] provider 改名后重新注入失败: %s" % exc)
        # 网关本体开机自启动：**仅**当请求显式带 autostart 且值真的变了时同步 HKCU Run。
        # 这里的「真的变了」以**落盘后的最新配置**为准（锁内已重读，不会读到旧快照）。
        autostart_after = bool(load_settings().get("autostart"))
        autostart_changed = "autostart" in payload and autostart_after != autostart_before
        if autostart_changed:
            try:
                set_gateway_autostart(autostart_after)
            except Exception as exc:
                _log("[WBRelay] 写入开机自启动注册表失败: %s" % redact(exc, 200))
        return self._json(200, {"ok": True, "port_changed":
                                ("port" in payload and configured_port() != state._SERVER_PORT),
                                "admin_token": (issued_admin_token[0]
                                                if issued_admin_token else ""),
                                "autostart_registered": gateway_autostart_registered()})

    # ---- 备份 ----
    def _api_backups(self):
        return self._json(200, {"backups": list_backups()})

    def _api_backup_make(self):
        fn = make_backup()
        if not fn:
            return self._err(500, "备份失败")
        return self._json(200, {"ok": True, "file": fn, "backups": list_backups()})

    def _api_restore(self):
        payload = self._payload()
        if payload is None or not payload.get("file"):
            return self._err(400, "file is required")
        data = restore_backup(str(payload["file"]))
        if data is None:
            return self._err(400, "备份文件无效或不存在")
        return self._json(200, {"ok": True, "restored": True})
