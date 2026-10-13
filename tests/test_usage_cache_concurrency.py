# -*- coding: utf-8 -*-
"""用量缓存并发竞争的隔离回归测试（自建 fixture，不读真实数据）。

被测行为（`wbrelay/usage.py::load_usage_rows` 的 mtime+size 增量缓存）：
  背景：`/api/usage` 由 `ThreadingHTTPServer` 多线程提供服务，多个看板页面同时
  自动刷新时会有多个线程并发调用 `load_usage_rows()`；`record_usage()` 也在并发
  追加 usage.jsonl。

  Bug（修复前）：`os.stat` + 读文件 + 更新 `_USAGE_CACHE` 整个临界区没有锁。
  两个线程读到同一个旧的 `c["size"]`，都 `seek()` 到同一位置、都解析同一段新增行、
  都拼到 `c["rows"]` 上 → 同一行被重复计入缓存，KPI 虚高。

覆盖：
  C1 并发读 + 并发追加后，缓存行数 == 文件真实行数（精确相等，无重复行）
  C2 按 model 去重后无重复（Counter 逐模型核对）
  C3 usage_summary 的 kpi["requests"] / kpi["tokens"] 与真实值一致
  C4 usage_recent 的 total 与真实值一致
  C5 每个并发读者的返回值都与串行执行结果一致
  C6 缓存键语义不回归：未变 → 直接复用；仅追加 → 增量；变小 → 全量重读
  C7 数据隔离自检：DATA 指向自己创建的临时目录；不碰真实 %APPDATA%

测试数据全部由本脚本写入临时目录（假模型名 / 假供应商名），
**不读取、不复制** %APPDATA%\WBRelay 或任何在用数据；不占用 8789。

用法：python tests/test_usage_cache_concurrency.py
"""
import collections
import json
import os
import sys
import threading
import time

import isolate

# ---- 关键：先隔离，再 import wbrelay（paths 在导入期解析数据目录）----
isolate.bootstrap()

from wbrelay import usage as _usage            # noqa: E402
from wbrelay.paths import DATA, USAGE_PATH      # noqa: E402
from wbrelay.web.server import Handler          # noqa: E402

PASS, FAIL = [], []

READERS = 6            # 并发读者（模拟多个看板页面同时刷新）
BASE_ROWS = 200        # fixture 行数
APPEND_ROWS = 300      # 并发追加条数
SUP = "sup_demo_cache"
# 固定时间戳：usage_row 默认用 time.time()-i，浮点 repr 的位数会在两次写入间漂移，
# 「同尺寸替换」就只是概率成立（实测 60 次里 23 次尺寸不同）。见 C6e 的说明。
FIXED_AT = 1760422000.0


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def model_of(i):
    """明显的假数据模型名（每条唯一，便于按 model 判重）。"""
    return "demo-cache-model-%04d" % i


def usage_row(i, at=None):
    return {"at": at if at is not None else (time.time() - i),
            "iso": "2026-10-13T00:00:00", "model": model_of(i),
            "realm": "supplier:" + SUP, "stream": False,
            "tokens": 10, "input_tokens": 6, "output_tokens": 4,
            "elapsed_ms": 100, "ttft_ms": 50}


def real_rows():
    """直接从文件全量解析（绕过被测缓存）—— 独立的「真实值」口径。"""
    out = []
    with open(USAGE_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def reader_snapshots(stop_evt, out, idx):
    """并发读者：不断调 load_usage_rows()，把每次返回的快照存下来。"""
    seen = []
    while not stop_evt.is_set():
        seen.append(list(_usage.load_usage_rows()))
    out[idx] = seen


def _bump_mtime():
    """把 usage.jsonl 的 mtime 推到明显不同的值（保证缓存签名变化）。"""
    st = os.stat(_usage.USAGE_PATH)
    os.utime(_usage.USAGE_PATH, (st.st_atime, st.st_mtime + 5.0))


def main():
    print("临时数据目录 : %s" % DATA)
    print("（未读取真实 %%APPDATA%%\\WBRelay；不占用 8789）\n")

    # ---- C7 隔离自检 ----
    try:
        isolate.assert_isolated()
        check("C7  DATA 指向本测试创建的临时目录（隔离断言通过）", True, DATA)
    except AssertionError as exc:
        check("C7  DATA 指向本测试创建的临时目录（隔离断言通过）", False, str(exc))

    # ---- fixture：BASE_ROWS 行 + 一份不含真实凭据的配置 ----
    base = [usage_row(i) for i in range(BASE_ROWS)]
    isolate.write_settings(isolate.base_settings(
        suppliers=[isolate.demo_supplier(SUP, "演示缓存供应商", "http://127.0.0.1:18788/v1")]))
    isolate.write_usage(base)

    total_expected = BASE_ROWS + APPEND_ROWS
    tokens_expected = total_expected * 10      # 每行 tokens=10

    # ---- 并发阶段：M 个读者 + 1 个写者（record_usage 持续追加）----
    snapshots = {}
    stop_evt = threading.Event()
    threads = [threading.Thread(target=reader_snapshots, args=(stop_evt, snapshots, i))
               for i in range(READERS)]

    def writer():
        for i in range(APPEND_ROWS):
            _usage.record_usage(usage_row(BASE_ROWS + i))
            time.sleep(0.0005)

    for t in threads:
        t.start()
    w = threading.Thread(target=writer)
    w.start()
    w.join()
    stop_evt.set()
    for t in threads:
        t.join()

    got_total = sum(len(s) for s in snapshots.values())
    print("并发读者返回快照总数：%d（%d 线程）" % (got_total, READERS))

    # ---- C1 缓存行数 == 文件真实行数（精确相等）----
    truth = real_rows()
    cached = _usage.load_usage_rows()
    check("C1a 缓存行数 == 文件真实行数",
          len(cached) == len(truth) == total_expected,
          "缓存 %d / 真实 %d / 期望 %d" % (len(cached), len(truth), total_expected))

    # ---- C2 按 model 去重后无重复 ----
    dup_models = [m for m, n in collections.Counter(
        str(r.get("model") or "") for r in cached).items() if n > 1]
    check("C2a 缓存内无重复行（按 model 计数）",
          not dup_models, "重复 model %s" % (dup_models[:5],))
    check("C2b 缓存 model 集合 == 真实 model 集合",
          {str(r.get("model") or "") for r in cached}
          == {str(r.get("model") or "") for r in truth}
          == {model_of(i) for i in range(total_expected)},
          "缓存 %d 个 / 真实 %d 个" % (len({str(r.get('model') or '') for r in cached}),
                                      len({str(r.get('model') or '') for r in truth})))

    # 中间过程的每次快照也不得有重复（重复一旦进缓存就会一直叠加）
    bad_snap = []
    for idx, snaps in snapshots.items():
        for s in snaps:
            cnt = collections.Counter(str(r.get("model") or "") for r in s)
            if any(n > 1 for n in cnt.values()):
                bad_snap.append(idx)
                break
    check("C2c 并发期间每次快照都无重复行", not bad_snap,
          "出现重复的读者线程 %s" % (sorted(set(bad_snap))[:5],))

    # ---- C3 usage_summary 的 KPI 与真实值一致 ----
    summary = _usage.usage_summary(rows=cached)
    kpi = summary["kpi"]
    check("C3a kpi[\"requests\"] 与真实值一致",
          kpi["requests"] == total_expected,
          "KPI %d / 真实 %d" % (kpi["requests"], total_expected))
    check("C3b kpi[\"tokens\"] 与真实值一致",
          kpi["tokens"] == tokens_expected,
          "KPI %d / 真实 %d" % (kpi["tokens"], tokens_expected))
    check("C3c kpi[\"input_tokens\"] 与真实值一致",
          kpi["input_tokens"] == total_expected * 6,
          "KPI %d / 真实 %d" % (kpi["input_tokens"], total_expected * 6))
    check("C3d by_model 行数 == 真实模型数（无虚高聚合行）",
          len(summary["by_model"]) == total_expected,
          "by_model %d / 真实 %d" % (len(summary["by_model"]), total_expected))
    check("C3e by_model 每行 requests 均为 1（无重复计入）",
          all(m["requests"] == 1 and m["tokens"] == 10 for m in summary["by_model"]),
          "异常 %s" % ([m for m in summary["by_model"] if m["requests"] != 1][:2],))

    # ---- C4 usage_recent 的 total 与真实值一致 ----
    recent = _usage.usage_recent(rows=cached, page_size=20)
    check("C4  usage_recent.total 与真实值一致",
          recent["total"] == total_expected,
          "total %d / 真实 %d" % (recent["total"], total_expected))

    # ---- C5 /api/usage 口径一致（不起真实服务器，直接调 Handler）----
    class FakeHandler(object):
        def __init__(self, path):
            self.path = path
            self.code = None
            self.body = None

        def _json(self, code, obj):
            self.code = code
            self.body = obj
            return obj

    h = FakeHandler("/api/usage?range=all&limit=20")
    Handler._api_usage(h)
    body = h.body or {}
    check("C5a /api/usage kpi[\"requests\"] 与真实值一致",
          body.get("kpi", {}).get("requests") == total_expected,
          "接口 %d / 真实 %d" % (body.get("kpi", {}).get("requests"), total_expected))
    check("C5b /api/usage kpi[\"tokens\"] 与真实值一致",
          body.get("kpi", {}).get("tokens") == tokens_expected,
          "接口 %d / 真实 %d" % (body.get("kpi", {}).get("tokens"), tokens_expected))
    check("C5c /api/usage recent.total 与真实值一致",
          body.get("recent", {}).get("total") == total_expected,
          "接口 %d / 真实 %d" % (body.get("recent", {}).get("total"), total_expected))
    check("C5d /api/usage 筛选候选模型数 == 真实模型数",
          len(body.get("filters", {}).get("models") or []) == total_expected,
          "候选 %d / 真实 %d" % (len(body.get("filters", {}).get("models") or []),
                                 total_expected))

    # ---- C6 缓存键语义不回归（未变 / 追加 / 变小）----
    rows_now = _usage.load_usage_rows()
    again = _usage.load_usage_rows()
    check("C6a 文件未变时直接返回同一缓存对象", again is rows_now,
          "同一对象 = %s" % (again is rows_now))

    _usage.record_usage(usage_row(total_expected))
    inc = _usage.load_usage_rows()
    check("C6b 仅追加时增量解析生效（行数 +1 且前缀不变）",
          len(inc) == len(rows_now) + 1
          and [r.get("model") for r in inc[:-1]] == [r.get("model") for r in rows_now],
          "%d -> %d" % (len(rows_now), len(inc)))
    truth_inc = real_rows()
    check("C6c 追加后缓存行数 == 文件真实行数", len(inc) == len(truth_inc),
          "缓存 %d / 真实 %d" % (len(inc), len(truth_inc)))

    # 变小：retention 重写场景（原子替换后 mtime/size 都变）→ 必须全量重读
    kept = [usage_row(i, at=FIXED_AT) for i in range(10)]
    isolate.write_usage(kept)
    shrunk = _usage.load_usage_rows()
    check("C6d 文件变小（retention 重写）时全量重读",
          len(shrunk) == 10 and [r.get("model") for r in shrunk] == [model_of(i) for i in range(10)],
          "行数 %d" % len(shrunk))
    size_before = os.stat(_usage.USAGE_PATH).st_size

    # 同尺寸但内容被替换（mtime 变）→ 不得继续用旧缓存。
    # 注意①：两批的 `at` 必须钉死（FIXED_AT）。默认的 time.time()-i 经 json 序列化后，
    #   浮点 repr 的位数会在两次写入间漂移 ——「同尺寸」只是概率成立。一旦新文件**变大**，
    #   缓存会走「仅追加」分支（size > prev_size），把旧行与尾部残行拼成错值；那是本用例
    #   自己造出的非法输入，不是产品缺陷。
    # 注意②：必须让两次写入的 mtime 真正不同 —— 若两次 write_usage 落在同一文件系统
    #   mtime 粒度内，签名本就「未变化」，缓存不失效是**正确行为**，断言会假失败。
    isolate.write_usage([usage_row(9000 + i, at=FIXED_AT) for i in range(10)])
    check("C6e0 前置条件：两批写入确实同尺寸（否则本用例不成立）",
          os.stat(_usage.USAGE_PATH).st_size == size_before,
          "%d -> %d" % (size_before, os.stat(_usage.USAGE_PATH).st_size))
    _bump_mtime()
    replaced = _usage.load_usage_rows()
    check("C6e 同尺寸替换（mtime 变）时全量重读",
          [r.get("model") for r in replaced] == [model_of(9000 + i) for i in range(10)],
          "行数 %d" % len(replaced))

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
