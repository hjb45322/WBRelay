# -*- coding: utf-8 -*-
"""协议矩阵四象限回归（见 docs/方案-OpenCode接管修复.md §三）。

矩阵：

    | 客户端                | 上游 wire_api=responses | 上游 wire_api=chat |
    |-----------------------|-------------------------|--------------------|
    | /v1/responses         | 透传（Q3）              | 翻译（Q4）         |
    | /v1/chat/completions  | 翻译（Q2，本方案新增）  | 透传（Q1）         |

另外覆盖：
  U1/U2 两个纯函数（chat_to_responses / responses_to_chat_completion）的字段映射
  Q5    chat 客户端 × responses 上游，**非流式**
  Q6    新方向的上游截断 → 用量记 error（截断必须看得见）
  Q7    已开始向客户端输出 → 绝不切换供应商（新方向）
  Q8    冷却期的 503 文案区分「无供应商」与「都在冷却（含原因与剩余秒数）」

隔离：全程只用自建临时目录 + 进程内 mock 上游（端口由系统分配），不联网、不占 8789。
用法：python tests/test_protocol_matrix.py
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
from wbrelay.protocol import chat_to_responses, responses_to_chat_completion  # noqa: E402
from wbrelay.web.server import Handler                                      # noqa: E402

from mock_upstream import MockUpstream                                      # noqa: E402

KEY = "sk-test-FAKE-KEY-0000"
M_Q1 = "m-q1-chat-to-chat"
M_Q2 = "m-q2-chat-to-resp"
M_Q3 = "m-q3-resp-to-resp"
M_Q4 = "m-q4-resp-to-chat"
M_Q2CUT = "m-q2-chat-to-resp-cut"
M_Q2NS = "m-q2-chat-to-resp-nostream"
M_FAIL = "m-q8-cool-message"
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


def chat_stream(model, **extra):
    payload = {"model": model, "stream": True,
               "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return post("/v1/chat/completions", payload)


def chat_json(model, **extra):
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    payload.update(extra)
    return post("/v1/chat/completions", payload)


def responses_stream(model):
    return post("/v1/responses", {"model": model, "stream": True, "input": "hi"})


def write_cache(cache_map):
    suppliers = {}
    for sid, models in cache_map.items():
        suppliers[sid] = {"name": "演示供应商 " + sid, "base_url": "",
                          "models": [{"id": m, "context_window": 1000,
                                      "max_output_tokens": 100,
                                      "reasoning_efforts": ["low", "high"]} for m in models],
                          "error": None, "ok": True}
    isolate.write_models_cache({
        "fetched_at": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "suppliers": suppliers, "errors": {}, "ok_count": len(cache_map),
        "total_suppliers": len(cache_map)})


def usage_rows():
    try:
        return usage.load_usage_rows()
    except Exception:
        return []


def last_row():
    rows = usage_rows()
    return rows[-1] if rows else {}


def err_message(body):
    try:
        return ((json.loads(body) or {}).get("error") or {}).get("message") or ""
    except Exception:
        return ""


def main():
    # ---------- U1/U2：纯函数字段映射 ----------
    req = chat_to_responses({
        "model": "m", "stream": True,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "你好"}]},
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
        ],
        "tools": [{"type": "function",
                   "function": {"name": "f", "description": "d",
                                "parameters": {"type": "object", "properties": {}}}}],
        "tool_choice": {"type": "function", "function": {"name": "f"}},
        "max_tokens": 128, "temperature": 0.2, "reasoning_effort": "high",
        "frequency_penalty": 1, "presence_penalty": 1, "stop": ["x"],
    })
    check("U1a system → instructions", req.get("instructions") == "sys", repr(req.get("instructions")))
    check("U1b messages → input 条目（含 function_call / function_call_output）",
          [it.get("type") for it in req.get("input") or []]
          == ["message", "function_call", "function_call_output"], repr(req.get("input"))[:200])
    check("U1c tools 形态转换（function 展平）",
          (req.get("tools") or [{}])[0].get("type") == "function"
          and "function" not in (req.get("tools") or [{}])[0]
          and (req.get("tools") or [{}])[0].get("name") == "f", repr(req.get("tools")))
    check("U1d tool_choice / 采样参数 / 输出上限 / 推理档位映射",
          req.get("tool_choice") == {"type": "function", "name": "f"}
          and req.get("max_output_tokens") == 128 and req.get("temperature") == 0.2
          and req.get("reasoning") == {"effort": "high"}, repr({k: req.get(k) for k in
                                                              ("tool_choice", "max_output_tokens",
                                                               "temperature", "reasoning")}))
    check("U1e responses 没有位置的字段被显式丢弃",
          "frequency_penalty" not in req and "presence_penalty" not in req and "stop" not in req,
          repr(sorted(req)))

    resp = responses_to_chat_completion({
        "id": "resp_1", "model": "m",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "答案"}]},
                   {"type": "function_call", "call_id": "call_9", "name": "g",
                    "arguments": "{}"}],
        "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12,
                  "input_tokens_details": {"cached_tokens": 3},
                  "output_tokens_details": {"reasoning_tokens": 2}},
    }, "m")
    choice = (resp.get("choices") or [{}])[0]
    check("U2a 文本 + 工具调用 → chat completion",
          choice.get("message", {}).get("content") == "答案"
          and (choice.get("message", {}).get("tool_calls") or [{}])[0].get("id") == "call_9"
          and choice.get("finish_reason") == "tool_calls", repr(choice))
    check("U2b usage 字段名转换（含 cached / reasoning）",
          resp.get("usage") == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12,
                                "prompt_tokens_details": {"cached_tokens": 3},
                                "completion_tokens_details": {"reasoning_tokens": 2}},
          repr(resp.get("usage")))

    # ---------- Q1..Q8：四象限 + 边界 ----------
    q1 = MockUpstream("q1", mode="chat_sse").start()
    q2 = MockUpstream("q2", mode="responses_sse_no_done").start()
    q3 = MockUpstream("q3", mode="responses_sse_no_done").start()
    q4 = MockUpstream("q4", mode="chat_sse").start()
    cut_a = MockUpstream("cutA", mode="responses_sse_truncated").start()
    cut_b = MockUpstream("cutB", mode="responses_sse_no_done").start()
    ns = MockUpstream("respns", mode="ok").start()
    bad = MockUpstream("bad", mode="http500").start()
    server = None
    try:
        problems = isolate.isolation_problems()
        check("Q0a 数据目录已隔离（临时目录）", not problems, "；".join(problems))

        sups = [
            isolate.demo_supplier("sup_q1", "演示供应商 q1", q1.base_url, KEY,
                                  models=[M_Q1], wire_api="chat"),
            isolate.demo_supplier("sup_q2", "演示供应商 q2", q2.base_url, KEY,
                                  models=[M_Q2], wire_api="responses"),
            isolate.demo_supplier("sup_q3", "演示供应商 q3", q3.base_url, KEY,
                                  models=[M_Q3], wire_api="responses"),
            isolate.demo_supplier("sup_q4", "演示供应商 q4", q4.base_url, KEY,
                                  models=[M_Q4], wire_api="chat"),
            isolate.demo_supplier("sup_cut_a", "演示供应商 cutA", cut_a.base_url, KEY,
                                  models=[M_Q2CUT], wire_api="responses"),
            isolate.demo_supplier("sup_cut_b", "演示供应商 cutB", cut_b.base_url, KEY,
                                  models=[M_Q2CUT], wire_api="responses"),
            isolate.demo_supplier("sup_ns", "演示供应商 respns", ns.base_url, KEY,
                                  models=[M_Q2NS], wire_api="responses"),
            isolate.demo_supplier("sup_fail", "演示供应商 bad", bad.base_url, KEY,
                                  models=[M_FAIL], wire_api="responses"),
        ]
        isolate.write_settings(isolate.base_settings(suppliers=sups))
        write_cache({"sup_q1": [M_Q1], "sup_q2": [M_Q2], "sup_q3": [M_Q3], "sup_q4": [M_Q4],
                     "sup_cut_a": [M_Q2CUT], "sup_cut_b": [M_Q2CUT], "sup_ns": [M_Q2NS],
                     "sup_fail": [M_FAIL]})

        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # ---- Q1 chat 客户端 × chat 上游（透传）----
        st, body = chat_stream(M_Q1)
        row = last_row()
        check("Q1a chat×chat 透传 200 + [DONE]",
              st == 200 and "[DONE]" in body and "chat.completion.chunk" in body,
              "status=%s" % st)
        check("Q1b 用量记在该供应商 realm 且无 error",
              row.get("realm") == "supplier:sup_q1" and not row.get("error"),
              repr({k: row.get(k) for k in ("realm", "error")}))

        # ---- Q2 chat 客户端 × responses 上游（本方案新增的翻译）----
        st, body = chat_stream(M_Q2, reasoning_effort="high")
        row = last_row()
        check("Q2a 翻译后的流式回答 200 + 文本 + 终块 + [DONE]",
              st == 200 and "第一段" in body and "第二段" in body
              and '"finish_reason": "stop"' in body and "[DONE]" in body,
              "status=%s body=%s" % (st, body[:160]))
        check("Q2b 首个 chunk 带 role=assistant",
              '"role": "assistant"' in body, body[:160])
        check("Q2c 上游收到的是 /responses 形态（input，不是 messages）",
              bool(q2.requests) and q2.requests[-1]["path"].endswith("/responses")
              and "input" in (q2.requests[-1].get("body") or {})
              and "messages" not in (q2.requests[-1].get("body") or {}),
              repr(q2.requests[-1].get("path")))
        check("Q2d chat 的 reasoning_effort 映射成 reasoning.effort",
              (q2.requests[-1].get("body") or {}).get("reasoning") == {"effort": "high"},
              repr((q2.requests[-1].get("body") or {}).get("reasoning")))
        check("Q2e 用量记在供应商 realm：stream + effort + ttft",
              row.get("realm") == "supplier:sup_q2" and not row.get("error")
              and row.get("effort") == "high" and row.get("ttft_ms") is not None,
              repr({k: row.get(k) for k in ("realm", "error", "effort", "ttft_ms")}))

        # ---- Q3 responses 客户端 × responses 上游（透传）----
        st, body = responses_stream(M_Q3)
        row = last_row()
        check("Q3a responses×responses 透传 200 + response.completed",
              st == 200 and "response.completed" in body, "status=%s" % st)
        check("Q3b 用量记在该供应商 realm",
              row.get("realm") == "supplier:sup_q3", repr(row.get("realm")))

        # ---- Q4 responses 客户端 × chat 上游（既有翻译）----
        st, body = responses_stream(M_Q4)
        row = last_row()
        check("Q4a responses×chat 翻译 200 + response.completed",
              st == 200 and "response.completed" in body, "status=%s" % st)
        check("Q4b 上游收到的是 /chat/completions 形态（messages，不是 input）",
              bool(q4.requests) and q4.requests[-1]["path"].endswith("/chat/completions")
              and "messages" in (q4.requests[-1].get("body") or {}),
              repr((q4.requests[-1].get("body") or {})).split("'")[1] if q4.requests else "no req")
        check("Q4c 用量记在该供应商 realm",
              row.get("realm") == "supplier:sup_q4", repr(row.get("realm")))

        # ---- Q5 chat 客户端 × responses 上游：非流式 ----
        st, body = chat_json(M_Q2NS)
        try:
            obj = json.loads(body)
        except Exception:
            obj = {}
        check("Q5a 非流式翻译成 chat.completion",
              st == 200 and obj.get("object") == "chat.completion"
              and (obj.get("choices") or [{}])[0].get("message", {}).get("content") == "respns: ok",
              "status=%s body=%s" % (st, body[:160]))
        check("Q5b usage 字段名转换为 chat 形态",
              (obj.get("usage") or {}).get("prompt_tokens") == 3
              and (obj.get("usage") or {}).get("completion_tokens") == 4,
              repr(obj.get("usage")))

        # ---- Q6 新方向的上游截断：补终块但用量记 error ----
        st, body = chat_stream(M_Q2CUT)
        row = last_row()
        check("Q6a 截断仍收尾（客户端拿到已有片段 + [DONE]）",
              st == 200 and "第一段" in body and "[DONE]" in body, "status=%s" % st)
        check("Q6b 截断被记为该供应商的错误用量",
              row.get("realm") == "supplier:sup_cut_a" and row.get("error") is True
              and "截断" in (row.get("message") or ""),
              repr({k: row.get(k) for k in ("realm", "error", "message")}))
        check("Q6c 已开始输出 → 下一家零请求（不切换）", cut_b.hits == 0, "cutB=%d" % cut_b.hits)

        # ---- Q7 冷却期文案：区分「没启用」与「都在冷却」----
        st1, _ = chat_json(M_FAIL)
        st2, body2 = chat_json(M_FAIL)
        msg2 = err_message(body2)
        row = last_row()
        check("Q7a 首次失败按最后一家状态码返回", st1 == 500, "status=%s" % st1)
        check("Q7b 冷却期内回 503 且文案说明「冷却」+ 剩余秒数 + 原因",
              st2 == 503 and "冷却" in msg2 and "剩余" in msg2 and "HTTP 500" in msg2,
              "status=%s msg=%s" % (st2, msg2))
        check("Q7c 用量记录区分「都在冷却」", row.get("realm") == "none"
              and "cooling" in (row.get("message") or ""), repr(row.get("message")))

        print("\n断言：PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
        if FAIL:
            return 1
        return 0
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        for m in (q1, q2, q3, q4, cut_a, cut_b, ns, bad):
            m.stop()


if __name__ == "__main__":
    sys.exit(main())
