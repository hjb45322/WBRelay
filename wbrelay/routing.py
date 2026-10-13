# -*- coding: utf-8 -*-
"""路由表与供应商选择：匹配、优先级、冷却、健康度。"""
import json
import os
import time

from .paths import SETTINGS_PATH, MODELS_CACHE_PATH, USAGE_PATH
from .modelcache import load_models_cache
from .state import _lock, _cooldown_reasons, _cooldowns
from .log import _log
from .redact import redact
from .settings import (load_settings, models_enabled_map, group_models_enabled_map,
                       get_suppliers, supplier_config_names, model_allowed)

_ROUTE_TABLE = {"key": None, "table": None}


def _file_sig(path):
    """单文件签名：纳秒级 mtime + size。任一变化（保存启用/拉取/改供应商）即视为失效。

    必须用 `st_mtime_ns` 而不是 `st_mtime`：`"%d" % st_mtime` 会把小数秒截断，
    于是同一秒内两次**字节数相同**的写入（`True`→`False` 少 1 字节、被别的字段
    补回同样长度）会得到一模一样的签名 → 路由表不重建 → 已停用的模型仍可路由。
    Windows 的 NTFS mtime 粒度约 100ns~1ms，纳秒值足以区分同一秒内的连续写入
    （配置写入是低频的人工操作，两次写入间隔远大于该粒度）。
    """
    try:
        st = os.stat(path)
    except OSError:
        return "-"
    return "%d-%d" % (st.st_mtime_ns, st.st_size)


def _route_table_sig():
    """settings + models_cache 的签名：任一变化即失效重建（同秒不同内容也能区分）。"""
    return "|".join(_file_sig(p) for p in (SETTINGS_PATH, MODELS_CACHE_PATH))


def route_table():
    """返回路由表 {sup_id: [可路由模型...]}（惰性重建）。"""
    key = _route_table_sig()
    if _ROUTE_TABLE["key"] == key and _ROUTE_TABLE["table"] is not None:
        return _ROUTE_TABLE["table"]
    gem = group_models_enabled_map()
    cache = load_models_cache()
    table = {}
    for sup in get_suppliers():
        if not sup.get("enabled", True):
            continue
        base = sup["base_url"].rstrip("/")
        gbase = gem.get(base) or {}
        info = (cache.get("suppliers") or {}).get(sup["id"]) or {}
        rows = []
        for m in (info.get("models") or []):
            mid = m.get("id")
            if mid and gbase.get(mid) is not False:
                rows.append(mid)
        table[sup["id"]] = rows
    _ROUTE_TABLE["key"] = key
    _ROUTE_TABLE["table"] = table
    return table


def _route_models_set():
    """路由表并集：所有能路由的模型集合（/v1/models 与 Codex catalog 用）。"""
    s = set()
    for ml in route_table().values():
        s.update(ml)
    return s


def route_has_model(model):
    """是否有任一供应商可路由该模型（503 拦截用）。"""
    return any(model in (ml or []) for ml in route_table().values())


def supplier_matches(sup, model):
    """拉取显示过滤：models 清单为空 = 显示该上游全部模型；配了 glm* 只显示 glm 开头。
    仅影响拉取结果（models_cache）的显示范围，不决定路由归属（路由由路由表决定）。"""
    pats = sup.get("models") or []
    if not pats:
        # 未配置模型清单 = 拉取显示全部（路由层面不因此接管任何模型）
        return True
    model_l = str(model or "").lower()
    for pat in pats:
        if not pat:
            continue
        pat_l = str(pat).lower()
        if pat_l.endswith("*"):
            if model_l.startswith(pat_l[:-1]):
                return True
        elif pat_l == model_l:
            return True
    return False


def supplier_cooled(sid):
    try:
        return time.time() < _cooldowns.get(sid, 0.0)
    except Exception:
        return False


def supplier_mark_cool(sid, seconds=60, reason=""):
    """把供应商放进冷却。`reason` 只用于展示与排错（进程内，不落盘，已脱敏截断）。

    为什么记原因：冷却期的 503 若只说「请检查供应商是否启用」，会把
    「供应商已启用、只是刚失败过」误报成配置问题（见 docs/方案-OpenCode接管修复.md §2.2）。
    """
    if not sid:
        return
    with _lock:
        _cooldowns[sid] = time.time() + max(1.0, float(seconds))
        if reason:
            _cooldown_reasons[sid] = redact(reason, 160)


def supplier_cooldown_remaining(sid):
    """该供应商剩余冷却秒数；不在冷却返回 0.0。"""
    try:
        return max(0.0, float(_cooldowns.get(sid, 0.0)) - time.time())
    except Exception:
        return 0.0


def supplier_cooldown_reason(sid):
    """该供应商最近一次进入冷却的原因（没有则空串）。"""
    with _lock:
        return _cooldown_reasons.get(sid) or ""


def cooling_suppliers_for(model):
    """能路由该模型、但正在冷却的供应商列表（供 503 文案与排错使用）。

    只读：不改冷却表，不触发路由表重建以外的写操作。
    """
    table = route_table()
    out = []
    for s in get_suppliers():
        if not s.get("enabled", True):
            continue
        sid = s.get("id")
        if model not in (table.get(sid) or []):
            continue
        remaining = supplier_cooldown_remaining(sid)
        if remaining > 0:
            out.append({"id": sid, "name": s.get("name") or sid,
                        "remaining": int(remaining) + 1,
                        "reason": supplier_cooldown_reason(sid)})
    return out


def _tail_lines(path, n=200):
    """读文件最后 n 行（避免大文件全量读）。文件不存在/异常返回空列表。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 65536)
            fh.seek(size - block)
            data = fh.read().decode("utf-8", "replace")
        lines = [l for l in data.splitlines() if l.strip()]
        return lines[-n:]
    except Exception:
        return []


def supplier_health(sid):
    """基于用量记录统计供应商健康度（被动统计，不额外发请求）。"""
    cooling = supplier_cooled(sid)
    recs = []
    for ln in _tail_lines(USAGE_PATH, 200):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("realm") == "supplier:" + sid:
            recs.append(r)
    if not recs:
        return {"requests": 0, "fail": 0, "rate": None, "cooling": cooling, "last_status": None}
    fail = sum(1 for r in recs if r.get("error"))
    statuses = [r.get("status") for r in recs if r.get("status") is not None]
    return {"requests": len(recs), "fail": fail,
            "rate": round((len(recs) - fail) * 100.0 / len(recs)),
            "cooling": cooling, "last_status": statuses[-1] if statuses else None}


def pick_supplier(model):
    """返回第一个命中路由表且健康的供应商；无 → None。"""
    table = route_table()
    for s in get_suppliers():
        if not s.get("enabled", True):
            continue
        if supplier_cooled(s.get("id")):
            continue
        if model in (table.get(s["id"]) or []):
            return s
    return None


def supplier_candidates(model):
    return [s for s in get_suppliers()
            if s.get("enabled", True) and not supplier_cooled(s.get("id"))
            and model in (route_table().get(s["id"]) or [])]


# ---------------------------------------------------------------------------
# 协议翻译（responses <-> chat，简版够用）
# ---------------------------------------------------------------------------
