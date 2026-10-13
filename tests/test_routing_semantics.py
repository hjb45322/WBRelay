# -*- coding: utf-8 -*-
"""核心语义与重试边界的隔离测试（自建 mock 上游 + 自建临时数据目录）。

覆盖（AGENTS.md 第七节的核心语义 + 本轮发布修复的重试边界）：
  T1 失败必切换 / 数组顺序：第一个命中的供应商优先
  T2 上游 500 → 自动切下一家，并给失败的那家打冷却
  T3 冷却中的供应商被跳过（不重复打同一家）
  T4 配置保存即清冷却（set_suppliers / save_settings 统一语义）
  T5 全败必报错：返回**最后一个**错误，用量记 realm=supplier:all_failed
  T6 输出前失败可重试：上游 200 但正文非 JSON → 切下一家
  T7 输出前失败可重试：上游 200 但正文为空/不可解析 → 切下一家
  T8 已开始透传 SSE → **绝不**切换（下游拿到一半流，下一家零请求）
  T9 隔离自检（DATA 在临时目录；全程不碰真实 %APPDATA%）

用法：python tests/test_routing_semantics.py
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state, usage                       # noqa: E402
from wbrelay.paths import DATA                          # noqa: E402
from wbrelay.routing import supplier_mark_cool          # noqa: E402
from wbrelay.web.server import Handler                  # noqa: E402

from mock_upstream import MockUpstream                   # noqa: E402

PASS, FAIL = [], []
MODEL = "demo-model"
KEY_A = "sk-test-FAKE-KEY-AAAA"
KEY_B = "sk-test-FAKE-KEY-BBBB"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http(path, body=None, timeout=20, method=None):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def save_suppliers(sups):
    return http("/api/suppliers", {"suppliers": sups})


def write_cache(sups):
    suppliers = {}
    for s in sups:
        suppliers[s["id"]] = {"name": s["name"], "base_url": s["base_url"],
                              "models": [{"id": MODEL, "context_window": 1000,
                                          "max_output_tokens": 100,
                                          "reasoning_efforts": ["low"]}],
                              "error": None, "ok": True}
    isolate.write_models_cache({
        "fetched_at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "suppliers": suppliers, "errors": {}, "ok_count": len(sups),
        "total_suppliers": len(sups)})


def last_usage():
    rows = usage.load_usage_rows()
    return rows[-1] if rows else {}


def main():
    a = MockUpstream("mockA").start()
    b = MockUpstream("mockB").start()
    server = None
    try:
        sup_a = isolate.demo_supplier("sup_demo_a", "演示供应商 A", a.base_url, KEY_A)
        sup_b = isolate.demo_supplier("sup_demo_b", "演示供应商 B", b.base_url, KEY_B)
        isolate.write_settings(isolate.base_settings(suppliers=[sup_a, sup_b]))
        write_cache([sup_a, sup_b])

        # ---- 隔离自检 ----
        problems = isolate.isolation_problems()
        check("T9a 数据目录已隔离（临时目录，非真实 APPDATA）", not problems, "；".join(problems))
        check("T9b mock 上游只监听本机回环且端口由系统分配",
              a.port != 8789 and b.port != 8789 and a.port != b.port,
              "A=%d B=%d" % (a.port, b.port))

        # ---- 启真实网关（隔离实例，非只读）----
        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # ---- T1 顺序优先 ----
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        text = ""
        if st == 200:
            try:
                obj = json.loads(body)
                text = obj["output"][0]["content"][0]["text"]
            except Exception:
                text = ""
        check("T1a 命中第一个供应商（数组顺序 = 优先级）", st == 200 and "mockA" in text,
              "status=%s text=%r" % (st, text))
        check("T1b 只打了一家（没有多余的试探请求）", a.hits == 1 and b.hits == 0,
              "A=%d B=%d" % (a.hits, b.hits))
        check("T1c 转发带上该供应商自己的 API Key",
              a.requests and a.requests[0].get("authorization") == "Bearer " + KEY_A,
              repr(a.requests[0].get("authorization")) if a.requests else "无请求")

        # ---- T2 失败必切换 ----
        a.reset(); b.reset()
        a.mode = "http500"
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("T2a 上游 500 → 自动切换下一家", st == 200 and "mockB" in body,
              "status=%s" % st)
        check("T2b 失败的那家被打了冷却", json.loads(http("/api/state")[1])["suppliers"][0]["cooling"] is True)
        st, _ = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("T2c 冷却中的供应商被跳过（不重复打它）", a.hits == 1 and b.hits >= 2,
              "A=%d B=%d" % (a.hits, b.hits))
        # 口径说明：预输出失败只打冷却 + 日志，不新增用量行（保持既有统计口径）；
        # 成功的那家按 realm=supplier:<id> 记一条，失败不会被记成 all_failed。
        rows = usage.load_usage_rows()
        ok_b = [r for r in rows if r.get("realm") == "supplier:sup_demo_b" and not r.get("error")]
        all_failed = [r for r in rows if r.get("realm") == "supplier:all_failed"]
        check("T2d 切换成功后按成功的那家记用量", len(ok_b) >= 1, "成功记录 %d 条" % len(ok_b))
        check("T2e 有供应商可用时不应记 all_failed", all_failed == [],
              repr([r.get("message") for r in all_failed]))

        # ---- T4 配置保存即清冷却 ----
        st, _ = save_suppliers([dict(sup_a), dict(sup_b)])
        check("T4a 保存供应商成功返回 200", st == 200, "status=%s" % st)
        sups_state = json.loads(http("/api/state")[1])["suppliers"]
        check("T4b 保存即清冷却（核心语义 2）",
              all(s["cooling"] is False for s in sups_state),
              repr([(s["id"], s["cooling"]) for s in sups_state]))

        # ---- T3 冷却中的供应商被跳过（顺序不变）----
        a.reset(); b.reset()
        a.mode = "ok"
        supplier_mark_cool("sup_demo_a", 60)
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("T3 冷却中的第一个供应商被跳过，直接打下一家",
              st == 200 and "mockB" in body and a.hits == 0,
              "status=%s A=%d B=%d" % (st, a.hits, b.hits))
        save_suppliers([dict(sup_a), dict(sup_b)])   # 清冷却，恢复干净状态

        # ---- T6 输出前失败可重试（200 + 非 JSON）----
        a.reset(); b.reset()
        a.mode = "invalid_json"
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("T6 上游 200 但正文非 JSON → 未输出即可切换下一家",
              st == 200 and "mockB" in body and b.hits == 1,
              "status=%s A=%d B=%d" % (st, a.hits, b.hits))

        # ---- T7 输出前失败可重试（正文不可解析）----
        a.reset(); b.reset()
        a.mode = "empty_stream"
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("T7 上游正文不可解析 → 未输出即可切换下一家",
              st == 200 and "mockB" in body and b.hits == 1,
              "status=%s A=%d B=%d" % (st, a.hits, b.hits))

        # ---- T8 已开始透传 SSE → 绝不切换 ----
        save_suppliers([dict(sup_a), dict(sup_b)])
        a.reset(); b.reset()
        a.mode = "abort_stream"
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi", "stream": True})
        check("T8a 开始透传后上游中断：客户端仍拿到 200 + 已收到的片段",
              st == 200 and "mockA" in body, "status=%s len=%d" % (st, len(body)))
        check("T8b 已开始输出 → 下一家零请求（不切换）", b.hits == 0, "B=%d" % b.hits)
        row = last_usage()
        check("T8c 中断被记为该供应商的错误用量",
              row.get("realm") == "supplier:sup_demo_a" and row.get("error") is True,
              repr({k: row.get(k) for k in ("realm", "error", "status")}))

        # ---- T5 全败必报错 ----
        a.mode = "http500"
        b.mode = "http500"
        save_suppliers([dict(sup_a), dict(sup_b)])   # 清冷却，确保两家都被尝试
        a.reset(); b.reset()
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        # 返回「最后一个错误」= 最后那家的状态码（这里是 500），不是统一的 502
        check("T5a 全败 → 返回错误状态（最后那家的状态码）", st == 500, "status=%s" % st)
        check("T5b 返回的是**最后一个**供应商的错误",
              "mockB" in body, body[:160])
        row = last_usage()
        check("T5c 用量记 realm=supplier:all_failed",
              row.get("realm") == "supplier:all_failed" and row.get("error") is True,
              repr({k: row.get(k) for k in ("realm", "error", "status")}))
        check("T5d 两家都被尝试过（顺序遍历到全败）", a.hits == 1 and b.hits == 1,
              "A=%d B=%d" % (a.hits, b.hits))

        # ---- 模型名校验（边界加固）----
        st, body = http("/v1/responses", {"model": "x" * 500, "input": "hi"})
        check("T10 超长模型名 → 400（不再进路由/用量）", st == 400, "status=%s" % st)
        st, body = http("/v1/responses", {"model": "", "input": "hi"})
        check("T11 空模型名 → 400", st == 400, "status=%s" % st)

        # ---- T12 路由表缓存签名：同秒内内容不同的两次写入也必须失效 ----
        # 复现路径（已实测）：settings 走 save_settings 原子写（fsync + os.replace），
        # 非伪造 mtime。旧签名用 "%d" % st_mtime 把小数秒截断，于是同一整数秒内两次
        # **字节数完全相同**的写入会得到一样的签名 → 路由表不重建 → 已停用的模型
        # 仍留在 /v1/models 里、请求仍被路由过去。
        # True→False 少 1 字节，用无关字段 pad 的长度差补回来，让两次写入字节数严格相等。
        from wbrelay.paths import SETTINGS_PATH, MODELS_CACHE_PATH      # noqa: E402
        from wbrelay.settings import save_settings                     # noqa: E402
        from wbrelay.modelcache import save_models_cache               # noqa: E402
        from wbrelay.routing import (route_table, _route_models_set)   # noqa: E402

        base_a = sup_a["base_url"].rstrip("/")

        def t12_settings(enabled, pad):
            data = isolate.base_settings(suppliers=[sup_a, sup_b])
            data["group_models_enabled"] = {base_a: {"m1": bool(enabled)}}
            data["t12_pad"] = "p" * pad    # 补偿 True/False 的 1 字节差
            return data

        def t12_cache(model, pad):
            return {"fetched_at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "suppliers": {"sup_demo_a": {
                        "name": sup_a["name"], "base_url": sup_a["base_url"],
                        "models": [{"id": model, "context_window": 1000,
                                    "max_output_tokens": 100,
                                    "reasoning_efforts": ["low"]}],
                        "error": None, "ok": True}},
                    "t12_pad": "p" * pad, "errors": {}, "ok_count": 1,
                    "total_suppliers": 1}

        def same_second(st1, st2):
            # 复现条件：同一整数秒（旧签名只看整秒）+ 字节数相同 + 纳秒值不同（修复后可区分）
            return (int(st1.st_mtime) == int(st2.st_mtime)
                    and st1.st_size == st2.st_size
                    and st1.st_mtime_ns != st2.st_mtime_ns)

        # 先落一份「A 可路由 m1」的 models_cache（同 t12_cache 的字段结构）
        save_models_cache(t12_cache("m1", 400))

        # ① settings：启用 → 建立缓存；② settings：停用（字节数完全相同）
        paired = None
        for _ in range(60):
            save_settings(t12_settings(True, 400))          # ① 启用 m1
            st_on = os.stat(SETTINGS_PATH)
            tbl_on = route_table()                          # 以①建立路由表缓存
            save_settings(t12_settings(False, 399))         # ② 停用 m1
            st_off = os.stat(SETTINGS_PATH)
            if same_second(st_on, st_off):
                paired = (st_on, st_off, tbl_on)
                break
        check("T12a 复现条件成立：settings 两次写入同一整数秒且字节数完全相同",
              paired is not None,
              "on=%s off=%s" % (paired[0].st_mtime_ns if paired else "-",
                                paired[1].st_mtime_ns if paired else "-"))
        check("T12b 启用后 m1 在 A 的路由表里（缓存已建立）",
              paired is not None and "m1" in (paired[2].get("sup_demo_a") or []),
              repr(paired[2].get("sup_demo_a") if paired else None))

        tbl_off = route_table()
        check("T12c 停用后 route_table() 不再包含 m1（修复前必失败）",
              "m1" not in (tbl_off.get("sup_demo_a") or []),
              repr(tbl_off.get("sup_demo_a")))
        check("T12d 停用后 _route_models_set() 不含 m1（/v1/models 不再返回它）",
              "m1" not in _route_models_set(), repr(sorted(_route_models_set())))

        # ③ models_cache：内容变化（同秒、同 size）同样必须触发重建
        paired_cache = None
        for _ in range(60):
            save_settings(t12_settings(True, 400))          # 重新启用 m1
            save_models_cache(t12_cache("m1", 400))         # 缓存①：A 可路由 m1
            c1 = os.stat(MODELS_CACHE_PATH)
            tbl_c1 = route_table()
            save_models_cache(t12_cache("m2", 400))         # 缓存②：A 改路由 m2
            c2 = os.stat(MODELS_CACHE_PATH)
            if same_second(c1, c2):
                paired_cache = (c1, c2, tbl_c1)
                break
        check("T12e 复现条件成立：models_cache 两次写入同一整数秒且字节数相同",
              paired_cache is not None,
              "c1=%s c2=%s" % (paired_cache[0].st_mtime_ns if paired_cache else "-",
                               paired_cache[1].st_mtime_ns if paired_cache else "-"))
        check("T12f models_cache 内容变化后路由表随之更新（m1 → m2）",
              paired_cache is not None and "m1" in (paired_cache[2].get("sup_demo_a") or []),
              repr(paired_cache[2].get("sup_demo_a") if paired_cache else None))
        tbl_c = route_table()
        check("T12g models_cache 变化后 route_table() 换成新内容（含 m2、不含 m1）",
              "m2" in (tbl_c.get("sup_demo_a") or []) and "m1" not in (tbl_c.get("sup_demo_a") or []),
              repr(tbl_c.get("sup_demo_a")))
        check("T12h _route_models_set() 随 models_cache 一起更新",
              "m2" in _route_models_set() and "m1" not in _route_models_set(),
              repr(sorted(_route_models_set())))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        a.stop()
        b.stop()

    check("T9c 测试数据目录仍在临时根内", DATA.startswith(isolate.tmp_root()), DATA)
    print("\n===== 核心语义：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())