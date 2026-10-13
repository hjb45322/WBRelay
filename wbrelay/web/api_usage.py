# -*- coding: utf-8 -*-
"""用量看板 API：/api/usage（KPI / 汇总 / 明细 / 筛选候选）。"""
import time
from urllib.parse import parse_qs, urlparse

from ..settings import (resolve_supplier_label, supplier_aliases,
                        supplier_config_names)
from ..usage import (load_usage_rows, usage_recent, usage_summary, usd_cny_rate)


class ApiUsageMixin:
    def _api_usage(self):
        qs = parse_qs(urlparse(self.path).query)
        rng = (qs.get("range") or ["all"])[0]
        now = time.time()
        tm = time.localtime(now)
        since, until = None, None
        if rng == "today":
            since = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))
        elif rng == "week":
            monday = tm.tm_mday - tm.tm_wday
            since = time.mktime((tm.tm_year, tm.tm_mon, monday, 0, 0, 0, 0, 0, -1))
        elif rng == "month":
            since = time.mktime((tm.tm_year, tm.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        elif rng == "custom":
            # 边界校验：时间戳必须是数值、非负、且 since <= until
            try:
                since = float((qs.get("since") or ["0"])[0]) or None
                until = float((qs.get("until") or ["0"])[0]) or None
            except (TypeError, ValueError):
                return self._err(400, "since/until 必须是 Unix 时间戳（秒）")
            if (since is not None and since < 0) or (until is not None and until < 0):
                return self._err(400, "since/until 不能为负数")
            if since is not None and until is not None and since > until:
                return self._err(400, "since 不能大于 until")
        supplier = (qs.get("supplier") or [""])[0].strip() or None
        model = (qs.get("model") or [""])[0].strip() or None
        rows = load_usage_rows()   # 全量只读一次，summary/recent/筛选候选共用（原为各读一次）
        summary = usage_summary(since=since, until=until, supplier=supplier, model=model,
                                rows=rows)
        try:
            page = int((qs.get("page") or ["1"])[0] or 1)
        except (TypeError, ValueError):
            return self._err(400, "page 必须是整数")
        if page < 1 or page > 100000:
            return self._err(400, "page 超出范围（1~100000）")
        try:
            page_size = int((qs.get("limit") or ["20"])[0] or 20)
        except (TypeError, ValueError):
            page_size = 20
        if page_size not in (10, 20, 50, 100):
            page_size = 20
        recent = usage_recent(since=since, until=until, page=page, page_size=page_size,
                              supplier=supplier, model=model, rows=rows)
        # 筛选器候选：只收当前时间范围内出现过的供应商/模型，口径与 KPI/汇总/明细一致。
        # 方案 A：两个下拉互不联动，仅随时间范围收敛（此前基于全量历史，选了「今日」
        # 仍会列出几十个当天没有任何数据的选项）。
        sup_cfg = supplier_config_names()
        sup_alias = supplier_aliases()
        sup_snap = {}      # 供应商 id -> 用量记录里的历史快照名（首个非空值）
        mods = set()
        for r in rows:
            at = r.get("at") or 0
            if since is not None and at < since:
                continue
            if until is not None and at > until:
                continue
            realm = str(r.get("realm") or "")
            sid = realm[9:] if realm.startswith("supplier:") else realm
            if sid and not sup_snap.get(sid):
                sup_snap[sid] = str(r.get("supplier_name") or "")
            m = str(r.get("model") or "")
            if m:
                mods.add(m)
        sup_list = [{"id": sid,
                     "name": resolve_supplier_label(sid, sup_snap.get(sid), sup_cfg, sup_alias)}
                    for sid in sup_snap]
        filters = {"suppliers": sorted(sup_list, key=lambda x: x["name"].lower()),
                   "models": sorted(mods)}
        # 估算成本用的汇率（配置项 usd_cny_rate）：看板据此把单价折成人民币，
        # 与后端 _cost_per_request 用同一个来源，避免前后端各写一个常数。
        return self._json(200, {"range": rng, "since": since, "until": until,
                                "supplier": supplier, "model": model,
                                "filters": filters,
                                "usd_cny_rate": usd_cny_rate(),
                                "kpi": summary["kpi"],
                                "by_model": summary["by_model"],
                                "by_supplier": summary["by_supplier"],
                                "recent": recent})
