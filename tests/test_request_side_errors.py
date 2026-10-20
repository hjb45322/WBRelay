# -*- coding: utf-8 -*-
"""请求侧错误语义测试（docs/方案-请求侧错误语义.md）。

覆盖：
  R1 上游 413 → 客户端拿到 413（不是 503）、文案指明「请求体过大 / 精简会话」
  R2 413 不冷却供应商，且**不再切换**下一家（下一家零请求）
  R3 上游 400（请求侧）→ 不冷却，但仍按核心语义切下一家并成功
  R4 冷却原因文案不重复状态码（不再出现 `HTTP 413 HTTP 413`）
  R5 「大 body 被掐断」判定：只有 8 MiB 量级 + 连接被断才算请求侧
  R6 供应商侧失败（500）仍照旧冷却 —— 语义没有被回退
  R7 用量口径：413 记 realm=none / status=413，不污染供应商健康度

用法：python tests/test_request_side_errors.py
"""
import json
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
import threading

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state, usage                             # noqa: E402
from wbrelay.web.api_v1 import (_http_reason, _looks_like_oversize_abort,
                                LARGE_REQUEST_BODY)           # noqa: E402
from wbrelay.web.server import Handler                        # noqa: E402

from mock_upstream import MockUpstream                        # noqa: E402

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


def cooling_of(sid):
    for s in json.loads(http("/api/state")[1])["suppliers"]:
        if s["id"] == sid:
            return s["cooling"]
    return None


def main():
    a = MockUpstream("mockA").start()
    b = MockUpstream("mockB").start()
    server = None
    try:
        sup_a = isolate.demo_supplier("sup_demo_a", "演示供应商 A", a.base_url, KEY_A)
        sup_b = isolate.demo_supplier("sup_demo_b", "演示供应商 B", b.base_url, KEY_B)
        isolate.write_settings(isolate.base_settings(suppliers=[sup_a, sup_b]))
        write_cache([sup_a, sup_b])

        problems = isolate.isolation_problems()
        check("R0a 数据目录已隔离（临时目录，非真实 APPDATA）", not problems, "；".join(problems))

        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # ---- R1 / R2：413 → 客户端 413，不冷却、不切换 ----
        a.mode = "http413"
        b.mode = "ok"
        save_suppliers([dict(sup_a), dict(sup_b)])
        a.reset(); b.reset()
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("R1a 上游 413 → 客户端拿到 413（不再伪装成 503）", st == 413, "status=%s" % st)
        check("R1b 文案指明请求体过大并要求精简/新建会话",
              "拒绝了本次请求" in body and "请求体" in body and "新建会话" in body, body[:200])
        check("R1c 413 之后供应商未被冷却", cooling_of("sup_demo_a") is False,
              "cooling=%r" % cooling_of("sup_demo_a"))
        check("R2 413 不再切换：下一家零请求", b.hits == 0 and a.hits == 1,
              "A=%d B=%d" % (a.hits, b.hits))
        row = last_usage()
        check("R7 413 记 realm=none / status=413（不污染供应商健康度）",
              row.get("realm") == "none" and row.get("status") == 413 and row.get("error") is True,
              repr({k: row.get(k) for k in ("realm", "status", "error")}))

        # ---- R3：400（请求侧）→ 不冷却，但仍切换 ----
        a.mode = "http400"
        b.mode = "ok"
        save_suppliers([dict(sup_a), dict(sup_b)])
        a.reset(); b.reset()
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("R3a 上游 400 → 仍然切到下一家并成功（核心语义「失败必切换」）",
              st == 200 and "mockB" in body, "status=%s" % st)
        check("R3b 400 之后第一家未被冷却", cooling_of("sup_demo_a") is False,
              "cooling=%r" % cooling_of("sup_demo_a"))
        check("R3c 两家各打一次", a.hits == 1 and b.hits == 1, "A=%d B=%d" % (a.hits, b.hits))

        # ---- R4：文案不重复状态码 ----
        check("R4a 没有正文时只出现一次状态码", _http_reason(413, "") == "HTTP 413",
              _http_reason(413, ""))
        check("R4b 有正文时状态码 + 正文", _http_reason(413, "payload too large")
              == "HTTP 413 payload too large", _http_reason(413, "payload too large"))

        # ---- R5：大 body 被掐断的判定 ----
        check("R5a 9 MiB + ConnectionReset → 请求侧",
              _looks_like_oversize_abort(ConnectionResetError(), 9 * 1024 * 1024) is True)
        check("R5b 1 MiB + ConnectionReset → 仍按供应商侧（不误判）",
              _looks_like_oversize_abort(ConnectionResetError(), 1 * 1024 * 1024) is False)
        check("R5c 9 MiB + 非连接异常 → 不误判",
              _looks_like_oversize_abort(ValueError(), 9 * 1024 * 1024) is False)
        check("R5d 阈值常量是 8 MiB", LARGE_REQUEST_BODY == 8 * 1024 * 1024,
              str(LARGE_REQUEST_BODY))
        check("R5e urllib 包一层的 URLError(reason=ConnectionReset) 也认",
              _looks_like_oversize_abort(
                  urllib.error.URLError(ConnectionResetError()), 9 * 1024 * 1024) is True)

        # ---- R6：供应商侧失败仍照旧冷却 ----
        a.mode = "http500"
        b.mode = "ok"
        save_suppliers([dict(sup_a), dict(sup_b)])
        a.reset(); b.reset()
        st, body = http("/v1/responses", {"model": MODEL, "input": "hi"})
        check("R6a 上游 500 → 切换下一家成功", st == 200 and "mockB" in body, "status=%s" % st)
        check("R6b 500 仍然冷却（供应商侧语义未回退）", cooling_of("sup_demo_a") is True,
              "cooling=%r" % cooling_of("sup_demo_a"))

    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        a.stop()
        b.stop()

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
