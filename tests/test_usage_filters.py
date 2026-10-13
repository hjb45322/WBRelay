# -*- coding: utf-8 -*-
"""用量看板筛选的隔离测试（自建 fixture，不读真实数据；只读断言，不改配置）。

覆盖：
  T1 筛选候选集随时间范围收敛（子集关系）
  T2 与 fixture 独立复算的对照（模型数 / 供应商数）
  T3 自定义时间范围同样收敛
  T4 页码越界回夹
  T5 rows 复用与默认加载结果一致（性能优化的正确性前提）
  T6 设置项 usage_refresh_sec 的默认值与边界
  T7 数据隔离自检：DATA 指向自己创建的临时目录；不碰真实 %APPDATA%

测试数据由本脚本写入临时目录（假 Key、通用供应商名）。

用法：python tests/test_usage_filters.py
"""
import os
import sys
import time

import isolate

isolate.bootstrap()

from wbrelay import usage as _usage            # noqa: E402
from wbrelay.paths import DATA                  # noqa: E402
from wbrelay.settings import usage_refresh_secs  # noqa: E402
from wbrelay.web.server import Handler          # noqa: E402

PASS, FAIL = [], []

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


class FakeHandler(object):
    """只提供 _api_usage 需要的最小接口（self.path + self._json）"""

    def __init__(self, path):
        self.path = path
        self.code = None
        self.body = None

    def _json(self, code, obj):
        self.code = code
        self.body = obj
        return obj


def call(qs):
    h = FakeHandler("/api/usage?" + qs)
    Handler._api_usage(h)
    return h.body


SUP_A = isolate.demo_supplier("sup_demo_a", "演示供应商 A", "http://127.0.0.1:18788/v1")
SUP_B = isolate.demo_supplier("sup_demo_b", "演示供应商 B", "http://127.0.0.1:18789/v1")


def build_fixture():
    """造三段用量：今天 / 本月内但非今天（10 天前）/ 40 天前。"""
    now = time.time()
    rows = [
        {"at": now - 60, "iso": "2026-01-01T00:01:00", "model": "demo-model-1",
         "realm": "supplier:" + SUP_A["id"], "stream": False, "tokens": 10,
         "input_tokens": 6, "output_tokens": 4, "elapsed_ms": 100, "ttft_ms": 50},
        {"at": now - 120, "iso": "2026-01-01T00:02:00", "model": "demo-model-2",
         "realm": "supplier:" + SUP_A["id"], "stream": True, "tokens": 20,
         "input_tokens": 12, "output_tokens": 8, "elapsed_ms": 200, "ttft_ms": 80},
        {"at": now - 10 * 86400, "iso": "2025-12-20T00:00:00", "model": "demo-model-3",
         "realm": "supplier:" + SUP_B["id"], "stream": False, "tokens": 30},
        {"at": now - 40 * 86400, "iso": "2025-11-20T00:00:00", "model": "demo-model-old",
         "realm": "supplier:" + SUP_B["id"], "stream": False, "tokens": 40, "error": True,
         "status": 502, "message": "all suppliers failed: demo"},
    ]
    isolate.write_settings(isolate.base_settings(suppliers=[SUP_A, SUP_B]))
    isolate.write_usage(rows)
    return rows


def main():
    rows = build_fixture()
    print("临时数据目录 : %s" % DATA)
    print("（未读取真实 %%APPDATA%%\\WBRelay）")
    print("fixture 用量行数：%d\n" % len(rows))

    # ---- T7 隔离自检 ----
    try:
        isolate.assert_isolated()
        check("T7  DATA 指向本测试创建的临时目录（隔离断言通过）", True, DATA)
    except AssertionError as exc:
        check("T7  DATA 指向本测试创建的临时目录（隔离断言通过）", False, str(exc))

    allv = call("range=all&limit=20")
    todayv = call("range=today&limit=20")
    weekv = call("range=week&limit=20")
    monthv = call("range=month&limit=20")

    # ---- T1 子集关系 ----
    set_all = set(allv["filters"]["models"])
    set_today = set(todayv["filters"]["models"])
    set_week = set(weekv["filters"]["models"])
    set_month = set(monthv["filters"]["models"])
    check("T1a today 模型候选 <= 全部", set_today <= set_all)
    check("T1b week  模型候选 <= 全部", set_week <= set_all)
    check("T1c month 模型候选 <= 全部", set_month <= set_all)
    sup_all = {s["id"] for s in allv["filters"]["suppliers"]}
    sup_today = {s["id"] for s in todayv["filters"]["suppliers"]}
    check("T1d today 供应商候选 <= 全部", sup_today <= sup_all)

    # ---- T2 独立复算对照（不用产品的筛选逻辑，手工按 at 过滤）----
    tm = time.localtime(time.time())
    day0 = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))
    monday = tm.tm_mday - tm.tm_wday
    week0 = time.mktime((tm.tm_year, tm.tm_mon, monday, 0, 0, 0, 0, 0, -1))
    month0 = time.mktime((tm.tm_year, tm.tm_mon, 1, 0, 0, 0, 0, 0, -1))

    def manual(since):
        ms, ss = set(), set()
        for r in rows:
            if (r.get("at") or 0) < since:
                continue
            if r.get("model"):
                ms.add(r["model"])
            realm = str(r.get("realm") or "")
            sid = realm[9:] if realm.startswith("supplier:") else realm
            if sid:
                ss.add(sid)
        return ms, ss

    for label, since, got in (("今日", day0, todayv), ("本周", week0, weekv), ("本月", month0, monthv)):
        ms, ss = manual(since)
        gm = set(got["filters"]["models"])
        gs = {s["id"] for s in got["filters"]["suppliers"]}
        check("T2 %s 模型候选与独立复算一致" % label, gm == ms,
              "接口 %d / 复算 %d" % (len(gm), len(ms)))
        check("T2 %s 供应商候选与独立复算一致" % label, gs == ss,
              "接口 %d / 复算 %d" % (len(gs), len(ss)))

    print("\n候选规模对照表：")
    print("  %-6s %-10s %-10s" % ("范围", "模型候选", "供应商候选"))
    for label, v in (("全部", allv), ("本月", monthv), ("本周", weekv), ("今日", todayv)):
        print("  %-6s %-10d %-10d" % (label, len(v["filters"]["models"]), len(v["filters"]["suppliers"])))

    check("T3a 收敛确有生效（今日模型候选 < 全部）",
          len(set_today) < len(set_all),
          "%d < %d" % (len(set_today), len(set_all)))

    # ---- T3 自定义范围 ----
    now = time.time()
    cust = call("range=custom&since=%d&until=%d&limit=20" % (int(now - 3600), int(now)))
    ms1h, _ = manual(now - 3600)
    check("T3b 自定义（近 1 小时）模型候选与复算一致",
          set(cust["filters"]["models"]) == ms1h,
          "接口 %d / 复算 %d" % (len(cust["filters"]["models"]), len(ms1h)))
    check("T3c 自定义模型候选 <= 全部", set(cust["filters"]["models"]) <= set_all)

    # ---- T4 页码越界回夹 ----
    big = call("range=all&limit=20&page=99999")
    check("T4a 超范围页码被回夹", big["recent"]["page"] <= big["recent"]["pages"],
          "page=%s pages=%s" % (big["recent"]["page"], big["recent"]["pages"]))
    check("T4b 回夹后仍有明细返回", len(big["recent"]["items"]) > 0,
          "items=%d" % len(big["recent"]["items"]))

    # ---- T5 rows 复用与默认加载一致 ----
    loaded = _usage.load_usage_rows()
    a = _usage.usage_summary(rows=loaded)
    b = _usage.usage_summary()
    c = _usage.usage_recent(rows=loaded, page_size=20)
    d = _usage.usage_recent(page_size=20)
    check("T5a usage_summary(rows=) 与默认加载结果一致", a == b)
    check("T5b usage_recent(rows=) 与默认加载结果一致", c == d)

    # ---- T6 设置项 ----
    sec = usage_refresh_secs()
    check("T6a usage_refresh_secs 取值为 0~600 或默认 5", (0 <= sec <= 600) or sec == 5,
          "当前值 = %s" % sec)

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())