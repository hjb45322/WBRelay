# -*- coding: utf-8 -*-
"""上游模型列表的拉取、缓存刷新与看板视图组装。"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from .paths import MODELS_CACHE_PATH, MODEL_FETCH_TIMEOUT
from .state import _lock
from .log import _log
from .settings import (load_settings, models_enabled_map, group_models_enabled_map,
                       get_suppliers, model_allowed, group_model_enabled)
from .profiles import load_profiles
from .modelcache import load_models_cache, save_models_cache
from .net import _urlopen, close_quietly, read_limited
from .redact import redact
from .routing import route_table, supplier_matches

def normalize_model_info(mid, it):
    """把上游 /v1/models 的一行归一化成前端表格字段（兼容多种字段命名）。"""
    credits = it.get("credits")
    if credits is None:
        credits = it.get("multiplier")
    modalities = it.get("input_modalities") or []
    if not isinstance(modalities, list):
        modalities = []
    efforts = []
    for e in (it.get("reasoning_efforts") or it.get("supported_reasoning_levels") or []):
        if isinstance(e, str):
            efforts.append(e)
        elif isinstance(e, dict) and e.get("effort"):
            efforts.append(str(e["effort"]))
    # 内置档案兜底：上游没给能力/档位/上下文时，用已知模型档案补全（倍率不补）
    prof = (load_profiles().get(mid) or {})
    if not efforts:
        efforts = list(prof.get("reasoning_efforts") or [])
    context = it.get("context_window") or it.get("context_length")
    if not context:
        context = prof.get("context_window")
    max_out = it.get("max_output_tokens")
    if not max_out:
        max_out = prof.get("max_output_tokens")
    vis = ("image" in modalities) or bool(it.get("supports_vision")
          or it.get("vision") or it.get("multimodal"))
    if not vis:
        vis = bool(prof.get("supports_vision"))
    tool = bool(it.get("supports_tool_calls")
                or (it.get("capabilities") or {}).get("tool_calls"))
    if not tool:
        tool = bool(prof.get("supports_tool_calls", True))
    return {
        "id": mid,
        "display_name": it.get("display_name") or mid,
        "credits": credits,
        "context_window": context,
        "max_context_window": it.get("max_context_window"),
        "max_output_tokens": max_out,
        "reasoning_efforts": efforts,
        "supports_vision": vis,
        "supports_tool_calls": tool,
        "supports_reasoning": bool(it.get("supports_reasoning") or it.get("reasoning_fixed_effort") or efforts),
    }


def fetch_supplier_models(sup, timeout=MODEL_FETCH_TIMEOUT):
    """拉取单个供应商 /models，返回 (models, error)。

    边界：请求带 timeout、响应体有大小上限（超限不整体读进内存）、
    句柄一定关闭；错误一律脱敏后再返回。
    """
    base = sup["base_url"].rstrip("/")
    url = base + "/models"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + (sup.get("api_key") or ""),
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    try:
        with _urlopen(req, timeout) as resp:
            data = read_limited(resp).decode("utf-8", "replace")
        obj = json.loads(data)
        items = obj.get("data") or []
        if not isinstance(items, list):
            return None, "upstream /models 返回格式异常"
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("id") or "").strip()
            if not mid:
                continue
            # 按供应商配置的通配符过滤：留空=全部；配了 glm* 就只保留 glm 开头的
            if not supplier_matches(sup, mid):
                continue
            out.append(normalize_model_info(mid, it))
        return out, None
    except urllib.error.HTTPError as exc:
        close_quietly(exc)
        return None, "HTTP %d" % exc.code
    except Exception as exc:
        return None, redact(exc, 200)


def refresh_models_cache():
    """拉取所有启用供应商的模型，合并写缓存。返回本次结果（含降级状态）。"""
    sups = [s for s in get_suppliers() if s.get("enabled", True)]
    suppliers = {}
    errors = {}
    ok_count = 0
    for s in sups:
        models, err = fetch_supplier_models(s)
        suppliers[s["id"]] = {"name": s["name"], "base_url": s["base_url"],
                              "models": models or [], "error": err, "ok": err is None}
        if err:
            errors[s["id"]] = err
        else:
            ok_count += 1
    cache = {"fetched_at": time.time(),
             "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "suppliers": suppliers, "errors": errors, "ok_count": ok_count,
             "total_suppliers": len(sups)}
    save_models_cache(cache)
    return cache


def _natural_key(s):
    """自然排序 key：gpt-5.6 < gpt-6 < gpt-10，数字按数值比较。"""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(s).lower()) if p != ""]


def models_view(refresh=False):
    """给前端模型页的视图：按 Base URL 分组（同上游多 Key 合并），组名取该上游第一个供应商名称。"""
    if refresh:
        fresh = refresh_models_cache()
        cache = fresh
    else:
        cache = load_models_cache()
    suppliers = cache.get("suppliers") or {}
    sups = get_suppliers()
    by_id = {s["id"]: s for s in sups}
    view = {}
    all_models = {}
    group_meta = {}   # base_url -> 组元信息
    # 按 settings 数组顺序（优先级）建组，组名取第一个供应商
    for sup in sups:
        if not sup.get("enabled", True):
            continue
        base = (sup.get("base_url") or "").rstrip("/")
        if not base:
            continue
        if base not in group_meta:
            group_meta[base] = {"name": sup.get("name") or base, "sids": [],
                                "ok": True, "errors": [], "models": {}}
        group_meta[base]["sids"].append(sup["id"])
    rtable = route_table()
    for sid, info in suppliers.items():
        sup = by_id.get(sid, {})
        if not sup.get("enabled", True):
            continue
        base = (sup.get("base_url") or "").rstrip("/")
        rows = []
        for m in info.get("models") or []:
            mid = m.get("id")
            rows.append(dict(m,
                             enabled=group_model_enabled(base, mid),
                             routed=(mid in (rtable.get(sid) or []))))
            all_models.setdefault(mid, {"id": mid, "suppliers": []})
            all_models[mid]["suppliers"].append(sid)
        rows.sort(key=lambda m: _natural_key(m.get("id") or ""))   # 组内按模型名称排序
        view[sid] = {"id": sid, "name": sup.get("name") or info.get("name"),
                     "base_url": sup.get("base_url") or info.get("base_url"),
                     "ok": info.get("ok"), "error": info.get("error"),
                     "enabled": sup.get("enabled", True), "models": rows}
        gm = group_meta.get(base)
        if not gm:
            continue
        if not info.get("ok"):
            gm["ok"] = False
            gm["errors"].append("%s: %s" % (sup.get("name") or sid,
                                            (info.get("error") or "未知错误").strip()))
        for m in rows:
            if m["id"] not in gm["models"]:
                gm["models"][m["id"]] = m
    groups = []
    for base, gm in group_meta.items():
        ms = sorted(gm["models"].values(),
                    key=lambda m: _natural_key(m.get("id") or ""))
        groups.append({"key": base, "name": gm["name"], "base_url": base,
                       "sids": gm["sids"], "ok": gm["ok"],
                       "error": "; ".join(gm["errors"]) if gm["errors"] else "",
                       "models": ms})
    # 配置变更检测：缓存里的供应商集合 ≠ 当前启用供应商集合 → 提示重新拉取
    # （新增/删除供应商后缓存未刷新，模型页继续显示旧缓存会让用户误判为拉取错误）
    stale = False
    stale_desc = ""
    if not refresh:
        cached_sids = set((cache.get("suppliers") or {}).keys())
        cur_sids = set()
        for s in sups:
            if s.get("enabled", True) and (s.get("base_url") or "").strip():
                cur_sids.add(s["id"])
        if cached_sids != cur_sids:
            stale = True
            parts = []
            added = cur_sids - cached_sids
            removed = cached_sids - cur_sids
            if added:
                names = [next((s.get("name") or s["id"] for s in sups if s["id"] == sid),
                              sid) for sid in sorted(added)]
                parts.append("新增供应商：" + "、".join(names))
            if removed:
                names = []
                for sid in sorted(removed):
                    info = (cache.get("suppliers") or {}).get(sid) or {}
                    names.append(info.get("name") or sid)
                parts.append("已移除供应商：" + "、".join(names))
            stale_desc = "；".join(parts)
    return {
        "fetched_at": cache.get("iso"),
        "age_sec": int(time.time() - (cache.get("fetched_at") or 0)) if cache.get("fetched_at") else None,
        "suppliers": view,
        "groups": groups,
        "model_count": len(all_models),
        "stale": stale,
        "stale_desc": stale_desc,
        "group_models_enabled": group_models_enabled_map(),
        "route_table": rtable,
    }
