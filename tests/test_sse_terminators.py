# -*- coding: utf-8 -*-
"""SSE 终止判定的隔离测试（第二轮发布修复）。

被测行为（`wbrelay/web/api_v1.py::_pipe_sse`）：
  S1  Chat 正常流：多个 chat.completion.chunk + `data: [DONE]` → 不记错误用量
  S2  Chat 截断：多个 chunk 后正常结束但**没有 [DONE]** → 记错误（截断）
  S3  Chat 普通 chunk 不得被当成终止事件（chunk 的 object 就是 chat.completion.chunk）
  S4  Responses 正常流：多事件 + response.completed、**不发 [DONE]** → 不记错误
  S5  Responses 截断：没有 response.completed → 记错误
  S6  Responses 只发 [DONE]、不发 response.completed → 不得算正常（[DONE] 不是 Responses 的终止事件）
  S7  一旦开始输出，绝不切换供应商：它后面那家 mock 连一个请求都不能收到

隔离：全程只用自建临时目录 + 进程内 mock 上游（端口由系统分配），不联网、不占 8789。
配置只写一次（避开 mtime 粒度导致的缓存未失效），每个用例用独立模型名。

用法：python tests/test_sse_terminators.py
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state, usage                                            # noqa: E402
from wbrelay.web.server import Handler                                      # noqa: E402

from mock_upstream import MockUpstream                                      # noqa: E402

KEY = "sk-test-FAKE-KEY-0000"
M_CHAT_OK = "mock-chat-ok"
M_CHAT_CUT = "mock-chat-cut"
M_RESP_OK = "mock-resp-ok"
M_RESP_CUT = "mock-resp-cut"
M_RESP_DONE = "mock-resp-done"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def post(path, body, timeout=30):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def stream_chat(model):
    return post("/v1/chat/completions", {"model": model, "stream": True,
                                         "messages": [{"role": "user", "content": "hi"}]})


def stream_responses(model):
    return post("/v1/responses", {"model": model, "stream": True, "input": "hi"})


def write_cache(by_model):
    suppliers = {}
    for sid, models in by_model.items():
        suppliers[sid] = {"name": "演示供应商 " + sid, "base_url": "",
                          "models": [{"id": m, "context_window": 1000,
                                      "max_output_tokens": 100,
                                      "reasoning_efforts": ["low"]} for m in models],
                          "error": None, "ok": True}
    isolate.write_models_cache({
        "fetched_at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "suppliers": suppliers, "errors": {}, "ok_count": len(by_model),
        "total_suppliers": len(by_model)})


def usage_rows():
    try:
        return usage.load_usage_rows()
    except Exception:
        return []


def last_row():
    rows = usage_rows()
    return rows[-1] if rows else {}


def main():
    chat_ok = MockUpstream("chatok", mode="chat_sse").start()
    chat_cut = MockUpstream("chatcut", mode="chat_sse_truncated").start()
    chat_next = MockUpstream("chatnext", mode="chat_sse").start()
    resp_ok = MockUpstream("respok", mode="responses_sse_no_done").start()
    resp_cut = MockUpstream("respcul", mode="responses_sse_truncated").start()
    resp_done = MockUpstream("resdone", mode="responses_sse_done_only").start()
    server = None
    try:
        problems = isolate.isolation_problems()
        check("S0a 数据目录已隔离（临时目录）", not problems, "；".join(problems))
        mocks = (chat_ok, chat_cut, chat_next, resp_ok, resp_cut, resp_done)
        ports = [m.port for m in mocks]
        check("S0b mock 上游端口均为系统分配且非 8789",
              all(p != 8789 for p in ports) and len(set(ports)) == len(ports), repr(ports))

        sups = [
            isolate.demo_supplier("sup_chat_ok", "演示供应商 chatok", chat_ok.base_url,
                                  KEY, models=[M_CHAT_OK], wire_api="chat"),
            isolate.demo_supplier("sup_chat_cut", "演示供应商 chatcut", chat_cut.base_url,
                                  KEY, models=[M_CHAT_CUT], wire_api="chat"),
            isolate.demo_supplier("sup_chat_next", "演示供应商 chatnext", chat_next.base_url,
                                  KEY, models=[M_CHAT_CUT], wire_api="chat"),
            isolate.demo_supplier("sup_resp_ok", "演示供应商 respok", resp_ok.base_url,
                                  KEY, models=[M_RESP_OK], wire_api="responses"),
            isolate.demo_supplier("sup_resp_cut", "演示供应商 respcut", resp_cut.base_url,
                                  KEY, models=[M_RESP_CUT], wire_api="responses"),
            isolate.demo_supplier("sup_resp_done", "演示供应商 respdone", resp_done.base_url,
                                  KEY, models=[M_RESP_DONE], wire_api="responses"),
        ]
        isolate.write_settings(isolate.base_settings(suppliers=sups))
        write_cache({"sup_chat_ok": [M_CHAT_OK], "sup_chat_cut": [M_CHAT_CUT],
                     "sup_chat_next": [M_CHAT_CUT], "sup_resp_ok": [M_RESP_OK],
                     "sup_resp_cut": [M_RESP_CUT], "sup_resp_done": [M_RESP_DONE]})

        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # ---- S1 Chat 正常流（多 chunk + [DONE]）----
        st, body = stream_chat(M_CHAT_OK)
        check("S1a Chat SSE 流→ 200", st == 200, "status=%s" % st)
        check("S1b 客户端收到 3 个 chunk 与 [DONE]",
              body.count("chat.completion.chunk") == 3 and "[DONE]" in body, body[-40:])
        row = last_row()
        check("S1c 正常 Chat 流不记错误用量",
              bool(row) and not row.get("error"), repr(row.get("error")))
        check("S1d realm 记到实际供应商", row.get("realm") == "supplier:sup_chat_ok",
              repr(row.get("realm")))

        # ---- S2/S3 Chat 截断：有 chunk、没 [DONE]（chunk 的 object 就是 chat.completion.chunk）----
        st, body = stream_chat(M_CHAT_CUT)
        check("S2a 截断 Chat 流仍然向客户端输出（200）", st == 200, "status=%s" % st)
        check("S2b 客户端没有收到 [DONE]（上游真的截断）", "[DONE]" not in body, body[-40:])
        check("S2c 客户端已收到多个 chunk（不是空流）",
              body.count("chat.completion.chunk") == 3, repr(body[:40]))
        row = last_row()
        check("S2d 截断 Chat 流记一条错误用量", bool(row.get("error")), repr(row))
        check("S2e 错误原因说明是截断（不是普通 chunk）",
              "截断" in str(row.get("message") or ""), repr(row.get("message")))
        check("S2f 已开始输出后绝不切换供应商（第二家零请求）",
              chat_next.hits == 0, "hits=%d" % chat_next.hits)
        check("S2g 用量记在第一家（不是第二家）",
              row.get("realm") == "supplier:sup_chat_cut", repr(row.get("realm")))

        # ---- S4 Responses 正常流（response.completed，不发 [DONE]）----
        st, body = stream_responses(M_RESP_OK)
        check("S4a Responses SSE 流→ 200", st == 200, "status=%s" % st)
        check("S4b 客户端收到 response.completed", "response.completed" in body, body[-40:])
        row = last_row()
        check("S4c Responses 正常流不记错误（不依赖 [DONE]）",
              bool(row) and not row.get("error"), repr(row.get("error")))

        # ---- S5 Responses 截断（没有 response.completed）----
        st, body = stream_responses(M_RESP_CUT)
        check("S5a 截断 Responses 流向客户端输出（200）", st == 200, "status=%s" % st)
        row = last_row()
        check("S5b 截断 Responses 流记错误用量", bool(row.get("error")), repr(row))

        # ---- S6 Responses 只发 [DONE]：不得算正常结束 ----
        st, body = stream_responses(M_RESP_DONE)
        row = last_row()
        check("S6 Responses 只有 [DONE] 时仍记错误（[DONE] 不是 Responses 终止事件）",
              bool(row.get("error")), repr(row))

        files = sorted(isolate.list_files())
        check("S7 测试数据全部落在临时目录（不碰真实 APPDATA/上游）",
              all(f in ("settings.json", "models_cache.json", "usage.jsonl") for f in files), repr(files))
        check("S7b 全程未占用 8789", PORT != 8789, "port=%d" % PORT)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        for m in (chat_ok, chat_cut, chat_next, resp_ok, resp_cut, resp_done):
            m.stop()

    print("\n===== SSE 终止判定：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
