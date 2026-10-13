# -*- coding: utf-8 -*-
"""用量解析、记录与聚合（只统计 token 与耗时，不记录请求/回复正文）。"""
import json
import os
import time

from .atomicio import write_text
from .paths import USAGE_PATH, READONLY, ensure_data_dir
from .profiles import load_profiles
from .state import _lock, _stats
from .log import _log
from .settings import (usage_retention_days, model_prices, supplier_config_names,
                       resolve_supplier_label, supplier_aliases, get_suppliers, load_settings)

# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------
def extract_usage_tokens(usage):
    """从上游 usage 对象提取 token 拆分（responses / chat 两种结构都兼容）。"""
    if not isinstance(usage, dict):
        return {}
    out = {}
    total = usage.get("total_tokens")
    if total is None:
        total = (usage.get("completion_tokens") or 0) + (usage.get("prompt_tokens") or 0)
    out["tokens"] = total
    if usage.get("input_tokens") is not None:
        out["input_tokens"] = usage["input_tokens"]
    if usage.get("prompt_tokens") is not None:
        out["input_tokens"] = usage["prompt_tokens"]
    if usage.get("output_tokens") is not None:
        out["output_tokens"] = usage["output_tokens"]
    if usage.get("completion_tokens") is not None:
        out["output_tokens"] = usage["completion_tokens"]
    if usage.get("reasoning_tokens") is not None:
        out["reasoning_tokens"] = usage["reasoning_tokens"]
    details = usage.get("output_tokens_details") or {}
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        out["reasoning_tokens"] = details["reasoning_tokens"]
    # 缓存命中 Token（OpenAI chat: prompt_tokens_details.cached_tokens；responses: input_tokens_details.cached_tokens）
    for dkey in ("prompt_tokens_details", "input_tokens_details"):
        dd = usage.get(dkey)
        if isinstance(dd, dict) and dd.get("cached_tokens") is not None:
            out["cached_tokens"] = dd["cached_tokens"]
            break
    if usage.get("cached_tokens") is not None:
        out["cached_tokens"] = usage["cached_tokens"]
    return out


def extract_usage_from_sse_line(line):
    """从一行 SSE data 提取 usage dict（流式透传时不影响转发，只顺便抓 usage）。

    兼容两种协议：
      - responses: {"type":"response.completed","response":{"usage":{...}}}
      - chat:      {"choices":[...],"usage":{...}}
    没有 usage 返回 None。
    """
    if not line or not line.startswith(b"data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "response.completed":
        resp = obj.get("response")
        if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
            return resp["usage"]
    u = obj.get("usage")
    if isinstance(u, dict):
        return u
    return None


def extract_effort(payload):
    """从入站请求体提取推理档位（responses: reasoning.effort / chat: reasoning_effort）。"""
    if not isinstance(payload, dict):
        return None
    e = payload.get("reasoning_effort")
    if e is None:
        rsn = payload.get("reasoning")
        if isinstance(rsn, dict):
            e = rsn.get("effort")
        elif rsn is not None:
            e = rsn
    return str(e) if e else None


def record_usage(row):
    row = dict(row)
    row["at"] = time.time()
    row["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 供应商显示名快照：记录只带 id，供应商日后被删/改名时靠这个字段还原当时的名字。
    if not row.get("supplier_name"):
        realm = str(row.get("realm") or "")
        if realm.startswith("supplier:"):
            nm = supplier_config_names().get(realm[len("supplier:"):])
            if nm:
                row["supplier_name"] = nm
    with _lock:
        _stats["requests"] += 1
        m = _stats["by_model"].setdefault(row.get("model", "?"), {"requests": 0, "errors": 0})
        m["requests"] += 1
        if row.get("error"):
            _stats["errors"] += 1
            m["errors"] += 1
    if READONLY:
        return
    if ensure_data_dir() is None:
        return
    try:
        with open(USAGE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        _log("[WBRelay] 用量记录写入失败: %s" % exc)


def stats_view():
    with _lock:
        return {"requests": _stats["requests"], "errors": _stats["errors"],
                "by_model": dict(_stats["by_model"])}


# ---------------------------------------------------------------------------
# 上游模型拉取与缓存
# ---------------------------------------------------------------------------
# 内置模型档案（默认值，首次运行写入 model_profiles.json 后可被导入更新）。
# 来源：公开模型规格整理 + 常见 gpt 系列补充。
# 用途：上游 /v1/models 只给模型名（中转站常见）时，用它补全能力/档位/上下文展示。
# 注意：credits 倍率【不补】——按用户口径“显示上游服务里倍率就行”，其他家拿不到显示 -。


# ---------------------------------------------------------------------------
# 汇率（美元 -> 人民币估算）
# ---------------------------------------------------------------------------
DEFAULT_USD_CNY_RATE = 7.2


def usd_cny_rate():
    """美元->人民币估算汇率（配置项 usd_cny_rate，默认 7.2）。

    暂时在 usage.py 内解析，未走 settings.py 的统一入口 —— 避免与 settings 层
    的改动互相干扰。后续若 settings.py 提供带范围校验的统一读取入口，应迁移过去。
    """
    try:
        v = float(load_settings().get("usd_cny_rate") or 0)
    except (TypeError, ValueError):
        return DEFAULT_USD_CNY_RATE
    return v if 0.1 <= v <= 100 else DEFAULT_USD_CNY_RATE


# ---------------------------------------------------------------------------
# 用量聚合（看板）
# ---------------------------------------------------------------------------
_USAGE_CACHE = {"mtime": None, "size": 0, "rows": []}


def _parse_usage_lines(fh):
    """逐行解析已打开的 usage.jsonl（坏行跳过，口径与既有实现一致）。"""
    out = []
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load_usage_rows():
    """读 usage.jsonl 全部行，返回 list[dict]。

    mtime+size 缓存：文件未变时直接返回缓存；
    仅追加时增量解析新增行（record_usage/外部迁移均为追加写）；
    retention 重写（变小）或替换时全量重读。

    并发安全：`ThreadingHTTPServer` 下多个看板页面会同时自动刷新，`record_usage`
    也在并发追加 usage.jsonl，因此「stat + 读文件 + 更新 _USAGE_CACHE」整个临界区
    必须持 `_lock`。否则两个线程会读到同一个旧的 `size`，都 seek 到同一位置、都解析
    同一段新增行、各自拼到缓存行上，同一行被重复计入，KPI（requests/tokens/by_model）
    与 recent.total 全部虚高。

    锁内**重新读取**缓存键（`prev_mtime` / `prev_size` / `prev_rows`），绝不用进锁前
    读到的旧值——否则仍会按过期 size 去 seek，重复计入。因此并发读者里只有一个线程
    真正做增量解析并更新缓存，其余线程（锁释放后进来）双检发现 mtime+size 已一致，
    直接复用刚更新好的缓存：任何并发读者看到的 rows 都与串行执行结果完全一致。
    """
    with _lock:
        c = _USAGE_CACHE
        try:
            st = os.stat(USAGE_PATH)
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            return []
        if c["mtime"] == mtime and c["size"] == size:
            return c["rows"]
        # 临界区内重新取缓存键：绝不用进锁前读到的旧 size/rows
        prev_mtime, prev_size, prev_rows = c["mtime"], c["size"], c["rows"]
        rows = []
        try:
            if prev_mtime is not None and size > prev_size and prev_rows:
                # 追加场景：只解析新增部分，复用已有缓存行
                with open(USAGE_PATH, "r", encoding="utf-8") as fh:
                    fh.seek(prev_size)
                    rows = _parse_usage_lines(fh)
                rows = prev_rows + rows
            else:
                # retention 重写（变小）/ 文件被替换：全量重读
                with open(USAGE_PATH, "r", encoding="utf-8") as fh:
                    rows = _parse_usage_lines(fh)
        except Exception:
            return prev_rows if prev_rows else rows
        c["mtime"], c["size"], c["rows"] = mtime, size, rows
        return rows


def _cost_per_request(r, profs=None):
    """按 OpenRouter 公开价估算单请求成本（人民币）。无价格档案返回 None。

    汇率取配置项 `usd_cny_rate`（默认 7.2，取值 0.1~100，非法值回落默认），
    见 `usd_cny_rate()`。**汇率不写进 usage.jsonl 历史行**：历史行只存 token 数，
    展示时一律按当前费率重算，改费率对全部历史记录立即生效。

    profs 为已加载的模型档案 dict（批量调用时传入避免每行重复 load_profiles）；
    rate 为本次聚合统一使用的汇率（同上，避免每行重复读配置）。
    """
    model = str(r.get("model") or "")
    if not model:
        return None
    prof = ((profs if profs is not None else load_profiles()).get(model) or {})
    pp = prof.get("prompt_price") or 0
    cp = prof.get("completion_price") or 0
    crp = prof.get("cache_read_price") or 0
    if not pp and not cp:
        return None
    it = r.get("input_tokens") or 0
    ot = r.get("output_tokens") or 0
    ct = r.get("cached_tokens") or 0
    non_cached = max(it - ct, 0)
    cost_usd = non_cached * pp + ct * crp + ot * cp
    return round(cost_usd * usd_cny_rate(), 4)


def usage_summary(since=None, until=None, prices=None, supplier=None, model=None, rows=None):
    """按时间范围聚合用量：KPI + 按模型 + 按供应商。supplier/model 可选过滤。"""
    if rows is None:
        rows = load_usage_rows()
    prices = prices or model_prices()
    sup_cfg = supplier_config_names()
    sup_alias = supplier_aliases()
    sup_snap = {}                 # 供应商 id -> 用量记录里的历史快照名
    filt = []
    for r in rows:
        at = r.get("at") or 0
        if since is not None and at < since:
            continue
        if until is not None and at > until:
            continue
        if supplier:
            realm = str(r.get("realm") or "")
            sup_name = realm[9:] if realm.startswith("supplier:") else realm
            if sup_name != supplier:
                continue
        if model:
            if str(r.get("model") or "") != model:
                continue
        filt.append(r)
    kpi = {"requests": 0, "errors": 0, "tokens": 0, "input_tokens": 0,
           "output_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
           "elapsed_sum_ms": 0, "elapsed_count": 0,
           "ttft_sum_ms": 0, "ttft_count": 0, "total_cost": 0.0}
    by_model = {}
    by_supplier = {}
    profs = load_profiles()  # 预取一次，避免聚合循环内每行重复加载
    for r in filt:
        model = str(r.get("model") or "?")
        realm = str(r.get("realm") or "none")
        is_err = bool(r.get("error"))
        kpi["requests"] += 1
        kpi["errors"] += 1 if is_err else 0
        tk = r.get("tokens") or 0
        kpi["tokens"] += tk or 0
        kpi["input_tokens"] += r.get("input_tokens") or 0
        kpi["output_tokens"] += r.get("output_tokens") or 0
        kpi["reasoning_tokens"] += r.get("reasoning_tokens") or 0
        kpi["cached_tokens"] += r.get("cached_tokens") or 0
        el = r.get("elapsed_ms") or 0
        if el:
            kpi["elapsed_sum_ms"] += el
            kpi["elapsed_count"] += 1
        tt = r.get("ttft_ms")
        if tt:
            kpi["ttft_sum_ms"] += tt
            kpi["ttft_count"] += 1
        cost = _cost_per_request(r, profs)
        if cost:
            kpi["total_cost"] += cost
        mm = by_model.setdefault(model, {"model": model, "requests": 0, "errors": 0,
                                         "tokens": 0, "input_tokens": 0, "output_tokens": 0,
                                         "reasoning_tokens": 0, "cached_tokens": 0,
                                         "elapsed_sum_ms": 0, "elapsed_count": 0,
                                         "ttft_sum_ms": 0, "ttft_count": 0, "cost": 0.0})
        mm["requests"] += 1
        mm["errors"] += 1 if is_err else 0
        mm["tokens"] += tk or 0
        mm["input_tokens"] += r.get("input_tokens") or 0
        mm["output_tokens"] += r.get("output_tokens") or 0
        mm["reasoning_tokens"] += r.get("reasoning_tokens") or 0
        mm["cached_tokens"] += r.get("cached_tokens") or 0
        if el:
            mm["elapsed_sum_ms"] += el
            mm["elapsed_count"] += 1
        if tt:
            mm["ttft_sum_ms"] += tt
            mm["ttft_count"] += 1
        if cost:
            mm["cost"] += cost
        sup_name = realm[9:] if realm.startswith("supplier:") else realm
        if sup_name and not sup_snap.get(sup_name):
            sup_snap[sup_name] = str(r.get("supplier_name") or "")
        ss = by_supplier.setdefault(sup_name, {"realm": sup_name, "requests": 0,
                                               "errors": 0, "tokens": 0, "input_tokens": 0,
                                               "output_tokens": 0, "reasoning_tokens": 0,
                                               "cached_tokens": 0,
                                               "elapsed_sum_ms": 0, "elapsed_count": 0,
                                               "ttft_sum_ms": 0, "ttft_count": 0, "cost": 0.0})
        ss["requests"] += 1
        ss["errors"] += 1 if is_err else 0
        ss["tokens"] += tk or 0
        ss["input_tokens"] += r.get("input_tokens") or 0
        ss["output_tokens"] += r.get("output_tokens") or 0
        ss["reasoning_tokens"] += r.get("reasoning_tokens") or 0
        ss["cached_tokens"] += r.get("cached_tokens") or 0
        if el:
            ss["elapsed_sum_ms"] += el
            ss["elapsed_count"] += 1
        if tt:
            ss["ttft_sum_ms"] += tt
            ss["ttft_count"] += 1
        if cost:
            ss["cost"] += cost
    if kpi["elapsed_count"]:
        kpi["avg_elapsed_ms"] = int(kpi["elapsed_sum_ms"] / kpi["elapsed_count"])
        gen = max(kpi["elapsed_sum_ms"] - kpi["ttft_sum_ms"], 1)
        kpi["avg_speed_tok_s"] = round(kpi["output_tokens"] / (gen / 1000.0), 1)
    else:
        kpi["avg_elapsed_ms"] = 0
        kpi["avg_speed_tok_s"] = 0
    kpi["avg_ttft_ms"] = int(kpi["ttft_sum_ms"] / kpi["ttft_count"]) if kpi["ttft_count"] else 0
    for mm in by_model.values():
        mm["avg_elapsed_ms"] = int(mm["elapsed_sum_ms"] / mm["elapsed_count"]) if mm["elapsed_count"] else 0
        mm_gen = max(mm["elapsed_sum_ms"] - mm["ttft_sum_ms"], 1)
        mm["avg_speed_tok_s"] = round(mm["output_tokens"] / (mm_gen / 1000.0), 1) if mm["elapsed_sum_ms"] else 0
        mm["avg_ttft_ms"] = int(mm["ttft_sum_ms"] / mm["ttft_count"]) if mm["ttft_count"] else 0
    for sup_key, ss in by_supplier.items():
        ss["supplier_label"] = resolve_supplier_label(sup_key, sup_snap.get(sup_key),
                                                      sup_cfg, sup_alias)
        ss["avg_elapsed_ms"] = int(ss["elapsed_sum_ms"] / ss["elapsed_count"]) if ss["elapsed_count"] else 0
        ss_gen = max(ss["elapsed_sum_ms"] - ss["ttft_sum_ms"], 1)
        ss["avg_speed_tok_s"] = round(ss["output_tokens"] / (ss_gen / 1000.0), 1) if ss["elapsed_sum_ms"] else 0
        ss["avg_ttft_ms"] = int(ss["ttft_sum_ms"] / ss["ttft_count"]) if ss["ttft_count"] else 0
    by_model_list = sorted(by_model.values(), key=lambda x: -x["tokens"])
    by_supplier_list = sorted(by_supplier.values(), key=lambda x: -x["tokens"])
    return {"kpi": kpi, "by_model": by_model_list, "by_supplier": by_supplier_list}


def usage_recent(since=None, until=None, page=1, page_size=20, supplier=None, model=None,
                 rows=None):
    """请求明细分页（新的在前）。supplier/model 可选过滤。"""
    if rows is None:
        rows = load_usage_rows()
    sup_cfg = supplier_config_names()
    sup_alias = supplier_aliases()
    filt = []
    for r in rows:
        at = r.get("at") or 0
        if since is not None and at < since:
            continue
        if until is not None and at > until:
            continue
        if supplier:
            realm = str(r.get("realm") or "")
            sup_name = realm[9:] if realm.startswith("supplier:") else realm
            if sup_name != supplier:
                continue
        if model:
            if str(r.get("model") or "") != model:
                continue
        filt.append(r)
    filt.sort(key=lambda r: r.get("at") or 0, reverse=True)
    total = len(filt)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))   # 越界回夹：切时间范围/筛选后旧页码可能已超过总页数
    start = (page - 1) * page_size

    def _sup_label(row):
        _realm = str(row.get("realm") or "")
        _sid = _realm[9:] if _realm.startswith("supplier:") else _realm
        return resolve_supplier_label(_sid, row.get("supplier_name"), sup_cfg, sup_alias)

    items = [{
        "iso": r.get("iso") or "",
        "model": r.get("model") or "",
        "effort": r.get("effort"),
        "realm": r.get("realm") or "",
        "supplier_label": _sup_label(r),
        "stream": bool(r.get("stream")),
        "tokens": r.get("tokens"),
        "input_tokens": r.get("input_tokens"),
        "output_tokens": r.get("output_tokens"),
        "reasoning_tokens": r.get("reasoning_tokens"),
        "cached_tokens": r.get("cached_tokens"),
        "ttft_ms": r.get("ttft_ms"),
        "elapsed_ms": r.get("elapsed_ms"),
        "cost": _cost_per_request(r),
        "error": bool(r.get("error")),
        "status": r.get("status"),
        "message": r.get("message"),
    } for r in filt[start:start + page_size]]
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": pages}


def enforce_usage_retention():
    """按保留天数清理 usage.jsonl（0 = 永久保留）。"""
    if READONLY:
        return 0
    days = usage_retention_days()
    if not days:
        return 0
    cutoff = time.time() - days * 86400
    rows = load_usage_rows()
    kept = [r for r in rows if (r.get("at") or 0) >= cutoff]
    if len(kept) == len(rows):
        return 0
    if ensure_data_dir() is None:
        return 0
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
    try:
        with _lock:
            write_text(USAGE_PATH, text)
    except Exception as exc:
        _log("[WBRelay] 用量保留清理失败（原文件保持不变）：%s" % exc)
        return 0
    return len(rows) - len(kept)


def _retention_loop():
    """后台线程：每小时检查一次用量保留策略，超期就清理。"""
    while True:
        try:
            time.sleep(3600)
            if usage_retention_days():
                removed = enforce_usage_retention()
                if removed:
                    _log("[WBRelay] 用量保留清理：移除 %d 条过期记录" % removed)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
