# -*- coding: utf-8 -*-
"""汇率可配置（usd_cny_rate）的隔离回归测试。

覆盖：
  R1 默认费率下成本与历史口径一致（回归）：_cost_per_request == round(cost_usd * 7.2, 4)
  R2 改 usd_cny_rate 后成本按比例变化（如 1.0 → 成本 == cost_usd）
  R3 非法值回落默认 7.2：0 / -1 / "abc" / None / 1e9
  R4 无价格档案时仍返回 None（不因汇率改造价行为）
  R5 usage_summary 的 total_cost / usage_recent 的 cost 也随费率变化
  R6 /api/usage 响应新增 usd_cny_rate 字段（既有字段不受影响）
  R7 汇率不写进 usage.jsonl 历史行（历史行只有 token 数）
  R8 数据隔离自检：DATA 指向自己创建的临时目录；不碰真实 %APPDATA%

测试数据全部由本脚本写入临时目录（假模型名 / 假供应商名 / 假 Key），
**不读取、不复制** %APPDATA%\WBRelay 或任何在用数据；不占用 8789。

用法：python tests/test_cost_rate.py
"""
import json
import os
import sys

import isolate

isolate.bootstrap()

from wbrelay import usage as _usage            # noqa: E402
from wbrelay.paths import DATA, USAGE_PATH      # noqa: E402
from wbrelay.web.server import Handler          # noqa: E402

PASS, FAIL = [], []

MODEL = "demo-rate-model-1"
SUP = isolate.demo_supplier("sup_demo_rate", "演示供应商 RATE", "http://127.0.0.1:18901/v1")
FAKE_KEY = "sk-test-FAKE-KEY-0000"     # 明显的假凭据（isolate.demo_supplier 默认值同）

# 单价按「每 token 美元」给（与 OpenRouter 一致）：便于独立复算
PROMPT_PRICE = 3e-6
COMPLETION_PRICE = 15e-6
CACHE_READ_PRICE = 1.5e-6


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


def set_rate(value):
    """写一次 usd_cny_rate（None 表示删掉该 key，回到默认口径）。"""
    st = isolate.base_settings(suppliers=[SUP])
    if value is not None:
        st["usd_cny_rate"] = value
    isolate.write_settings(st)


def build_profiles():
    """写 model_profiles.json：一个带价格的模型 + 一个无价格的模型。"""
    prof = {
        MODEL: {
            "prompt_price": PROMPT_PRICE,
            "completion_price": COMPLETION_PRICE,
            "cache_read_price": CACHE_READ_PRICE,
            "context_window": 128000,
            "reasoning_efforts": ["low", "high"],
        },
        "demo-rate-model-noprice": {"context_window": 128000},
    }
    path = os.path.join(DATA, "model_profiles.json")
    tmp = path + ".fixture.tmp"
    os.makedirs(DATA, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(prof, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    # 档案文件按 mtime 缓存，换文件后必须失效，否则读到的是上一个 fixture
    import wbrelay.profiles as _profiles
    _profiles._PROFILES_CACHE["mtime"] = None
    _profiles._PROFILES_CACHE["data"] = None
    return prof


def usage_row(**over):
    row = {"at": 1700000000.0, "iso": "2026-10-13T00:00:00", "model": MODEL,
           "realm": "supplier:" + SUP["id"], "stream": False,
           "tokens": 100, "input_tokens": 60, "output_tokens": 40,
           "cached_tokens": 20, "elapsed_ms": 1000, "ttft_ms": 200}
    row.update(over)
    return row


def cost_usd_of(row):
    """独立复算美元成本：非缓存输入 * prompt + 缓存 * cache_read + 输出 * completion。"""
    it = row.get("input_tokens") or 0
    ot = row.get("output_tokens") or 0
    ct = row.get("cached_tokens") or 0
    non_cached = max(it - ct, 0)
    return non_cached * PROMPT_PRICE + ct * CACHE_READ_PRICE + ot * COMPLETION_PRICE


def expected_cost_usd(row, rate):
    """产品口径成本：美元成本 × 汇率后按 4 位小数四舍五入（与 _cost_per_request 一致）。"""
    return round(cost_usd_of(row) * rate, 4)


def main():
    build_profiles()
    row = usage_row()
    isolate.write_usage([row])
    set_rate(None)      # 不写 usd_cny_rate → 默认口径

    print("临时数据目录 : %s" % DATA)
    print("（未读取真实 %%APPDATA%%\\WBRelay）\n")

    # ---- R8 隔离自检 ----
    try:
        isolate.assert_isolated()
        check("R8  DATA 指向本测试创建的临时目录（隔离断言通过）", True, DATA)
    except AssertionError as exc:
        check("R8  DATA 指向本测试创建的临时目录（隔离断言通过）", False, str(exc))

    # ---- R1 默认费率回归 ----
    check("R1a usd_cny_rate() 默认 7.2", _usage.usd_cny_rate() == 7.2, repr(_usage.usd_cny_rate()))
    cost_default = _usage._cost_per_request(row)
    usd = cost_usd_of(row)
    check("R1b 默认费率下成本 == round(cost_usd * 7.2, 4)（与历史口径一致）",
          cost_default == round(usd * 7.2, 4),
          "got=%r want=%r" % (cost_default, round(usd * 7.2, 4)))
    check("R1c 默认口径下成本为正数", cost_default > 0, repr(cost_default))

    # ---- R2 改费率 ----
    set_rate(1.0)
    check("R2a usd_cny_rate() 读到配置值 1.0", _usage.usd_cny_rate() == 1.0, repr(_usage.usd_cny_rate()))
    cost_1 = _usage._cost_per_request(row)
    check("R2b 费率 1.0 时成本 == cost_usd（比例关系成立）",
          cost_1 == round(usd * 1.0, 4), "got=%r want=%r" % (cost_1, round(usd, 4)))
    check("R2c 成本按费率比例变化（7.2 口径 > 1.0 口径，且都等于各自的 usd×rate）",
          cost_default == round(usd * 7.2, 4) and cost_1 == round(usd * 1.0, 4)
          and cost_default > cost_1,
          "7.2→%r  1.0→%r" % (cost_default, cost_1))

    # ---- R3 非法值回落默认 ----
    for label, bad in (("0", 0), ("-1", -1), ("\"abc\"", "abc"), ("None", None),
                       ("1e9（超上限）", 1e9), ("0.05（低于下限）", 0.05),
                       ("101（高于上限）", 101)):
        set_rate(bad)
        got = _usage.usd_cny_rate()
        check("R3 非法值 %-14s 回落默认 7.2" % label, got == 7.2, repr(got))

    # 合法边界值不回落
    set_rate(0.1)
    check("R3b 下限边界 0.1 被接受", _usage.usd_cny_rate() == 0.1, repr(_usage.usd_cny_rate()))
    set_rate(100)
    check("R3c 上限边界 100 被接受", _usage.usd_cny_rate() == 100, repr(_usage.usd_cny_rate()))

    # ---- R4 无价格档案仍返回 None ----
    set_rate(1.0)
    row_noprice = usage_row(model="demo-rate-model-noprice")
    check("R4a 无价格档案时 _cost_per_request 返回 None",
          _usage._cost_per_request(row_noprice) is None,
          repr(_usage._cost_per_request(row_noprice)))
    check("R4b 模型名为空时返回 None", _usage._cost_per_request({"model": ""}) is None)

    # ---- R5 聚合口径一致 ----
    set_rate(1.0)
    _usage.load_usage_rows()          # 先把缓存刷成当前文件（费率不影响缓存）
    s1 = _usage.usage_summary()
    r1 = _usage.usage_recent(page_size=20)
    # 产品口径：每行先 round(...,4)，再累加成 total_cost / by_model.cost
    want_1 = round(expected_cost_usd(row, 1.0), 4)
    check("R5a usage_summary total_cost 随费率变化（1.0 口径）",
          abs(s1["kpi"]["total_cost"] - want_1) < 1e-9,
          "got=%r want=%r" % (s1["kpi"]["total_cost"], want_1))
    check("R5b usage_summary by_model cost 同口径",
          abs(s1["by_model"][0]["cost"] - want_1) < 1e-9,
          "got=%r want=%r" % (s1["by_model"][0]["cost"], want_1))
    check("R5c usage_recent items[0].cost 同口径",
          abs(r1["items"][0]["cost"] - want_1) < 1e-9,
          "got=%r want=%r" % (r1["items"][0]["cost"], want_1))

    set_rate(7.2)
    s2 = _usage.usage_summary()
    r2 = _usage.usage_recent(page_size=20)
    want_72 = round(expected_cost_usd(row, 7.2), 4)
    check("R5d 费率改回 7.2 后 total_cost 按 7.2 倍恢复",
          abs(s2["kpi"]["total_cost"] - want_72) < 1e-9,
          "got=%r want=%r" % (s2["kpi"]["total_cost"], want_72))
    check("R5e 费率改回 7.2 后 recent cost 按 7.2 倍恢复",
          abs(r2["items"][0]["cost"] - want_72) < 1e-9,
          "got=%r want=%r" % (r2["items"][0]["cost"], want_72))
    check("R5f 汇率确实参与聚合（7.2 口径 total_cost 显著大于 1.0 口径）",
          abs(s2["kpi"]["total_cost"] - s1["kpi"]["total_cost"]) > 1e-6,
          "1.0→%r  7.2→%r" % (s1["kpi"]["total_cost"], s2["kpi"]["total_cost"]))

    # ---- R6 /api/usage 新增 usd_cny_rate ----
    set_rate(6.5)
    body = call("range=all&limit=20")
    check("R6a /api/usage 返回 usd_cny_rate", body.get("usd_cny_rate") == 6.5,
          repr(body.get("usd_cny_rate")))
    check("R6b /api/usage 既有字段未变（kpi / by_model / by_supplier / recent / filters）",
          all(k in body for k in ("kpi", "by_model", "by_supplier", "recent", "filters")),
          repr(sorted(body.keys())))
    check("R6c /api/usage recent.items[0].cost 与 usd_cny_rate 同口径",
          abs(body["recent"]["items"][0]["cost"] - round(expected_cost_usd(row, 6.5), 4)) < 1e-9,
          repr(body["recent"]["items"][0]["cost"]))

    set_rate(None)
    body_d = call("range=all&limit=20")
    check("R6d 不配置时 /api/usage 下发默认 7.2", body_d.get("usd_cny_rate") == 7.2,
          repr(body_d.get("usd_cny_rate")))

    # ---- R7 历史行不写费率 ----
    with open(USAGE_PATH, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    check("R7a usage.jsonl 只有本 fixture 的一行", len(lines) == 1, "%d 行" % len(lines))
    stored = json.loads(lines[0])
    check("R7b 历史行不含 usd_cny_rate（费率只展示时按当前值算）",
          "usd_cny_rate" not in stored, repr(sorted(stored.keys())))
    check("R7c 历史行不含成本字段", "cost" not in stored, repr(sorted(stored.keys())))

    # ---- R8 再次隔离自检 ----
    try:
        isolate.assert_isolated()
        check("R8b 收尾隔离自检通过", True)
    except AssertionError as exc:
        check("R8b 收尾隔离自检通过", False, str(exc))

    print("\n===== 汇率可配置：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
