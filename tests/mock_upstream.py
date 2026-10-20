# -*- coding: utf-8 -*-
"""进程内 mock 上游（标准库 http.server），供隔离测试使用。

特点：
  · 端口由系统分配（`127.0.0.1:0`），测试之间不会撞车，也绝不占用 8788/8789
  · 记录收到的每个请求（路径 / 模型 / 授权头）——「有没有真的重试到下一家」靠它断言
  · 多种故障模式，覆盖重试边界：
      ok            正常 JSON / 正常 SSE
      http500       直接返回 500（可重试）
      http400       返回 400（请求侧拒绝：不得冷却供应商，但应切换下一家）
      http413       返回 413 并关闭连接（请求体超限：不得冷却、不再切换）
      invalid_json  返回 200 + 非 JSON 正文（输出前失败 → 可重试）
      empty_stream  返回 200 + 空的 SSE 正文（输出前失败 → 可重试）
      abort_stream  先发 200 + 一段 SSE，再粗暴断开（已开始输出 → 不得重试）
      hang          不返回正文（读取超时）

    SSE 终止判定专用（响应完整、只是没有终止事件）：
      chat_sse                Chat Completions 多 chunk + data: [DONE]（正常）
      chat_sse_truncated      Chat 多 chunk 后直接结束，**没有** [DONE]（截断）
      responses_sse_no_done   Responses 多事件 + response.completed，**不发** [DONE]（正常）
      responses_sse_truncated Responses 多事件后结束，没有 response.completed（截断）
      responses_sse_done_only Responses 只发 [DONE]、不发 response.completed（不得算正常）

**不会访问任何真实上游**；只在 127.0.0.1 上由测试自己创建。
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_MODELS = ["demo-model", "demo-model-2"]


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MockUpstream(object):
    def __init__(self, name, mode="ok", models=None, timeout_hold=3.0, error_body=None,
                 big_bytes=0, html=None):
        self.name = name
        self.mode = mode
        self.error_body = error_body            # http500 模式的响应正文（可用假凭据验证脱敏）
        self.big_bytes = big_bytes              # GET /big 返回的字节数（代理响应上限测试）
        self.html = html                        # GET / 返回的 HTML（代理重写测试）
        self.models = list(models or DEFAULT_MODELS)
        self.timeout_hold = timeout_hold
        self.port = free_port()
        self.requests = []          # [{path, model, authorization}]
        self._httpd = None
        self._thread = None

    # ---- 生命周期 ----
    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _raw(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append({"path": self.path, "model": "",
                                       "authorization": self.headers.get("Authorization") or "",
                                       "headers": dict(self.headers)})
                path = self.path.rstrip("/") or "/"
                if outer.mode == "hang":
                    import time as _t
                    _t.sleep(outer.timeout_hold)
                    return self._json(200, {"id": "late", "object": "response", "output": []})
                if path.endswith("/models"):
                    return self._json(200, {"object": "list",
                                            "data": [{"id": m, "object": "model"}
                                                     for m in outer.models]})
                if path.endswith("/big"):
                    if not outer.big_bytes:
                        return self._json(404, {"error": {"message": "not found"}})
                    return self._raw(200, b"x" * outer.big_bytes, "application/octet-stream")
                if outer.html is not None:
                    return self._raw(200, outer.html.encode("utf-8"), "text/html; charset=utf-8")
                return self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    payload = {}
                outer.requests.append({"path": self.path, "model": payload.get("model") or "",
                                       "authorization": self.headers.get("Authorization") or "",
                                       "headers": dict(self.headers),
                                       "body": payload})
                if not self.path.rstrip("/").endswith(("/responses", "/chat/completions")):
                    return self._json(404, {"error": {"message": "not found"}})
                mode = outer.mode
                if mode == "http500":
                    return self._json(500, {"error": {
                        "message": outer.error_body or ("mock %s 故意失败" % outer.name)}})
                if mode == "http413":
                    # 模拟上游按 body 上限「先拒后关」：回 413 并关闭连接
                    # （真实上游 wb-proxy 就是这样，见 docs/方案-请求侧错误语义.md §1.2）
                    self.close_connection = True
                    return self._json(413, {"error": {
                        "message": "payload too large (mock) > max limit"}})
                if mode == "http400":
                    return self._json(400, {"error": {
                        "message": "prompt is too long (mock)"}})
                if mode == "hang":
                    import time as _t
                    _t.sleep(outer.timeout_hold)
                    return self._json(200, {"id": "late", "object": "response", "output": []})
                if mode == "invalid_json":
                    body = b"<html>not json at all</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if mode in ("chat_sse", "chat_sse_truncated"):
                    return self._chat_stream(mode)
                if mode in ("responses_sse_no_done", "responses_sse_truncated",
                            "responses_sse_done_only"):
                    return self._responses_stream(mode)
                if payload.get("stream"):
                    return self._stream(mode)
                if mode == "empty_stream":
                    return self._stream(mode)
                return self._json(200, {
                    "id": "resp_%s" % outer.name,
                    "object": "response",
                    "output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "%s: ok" % outer.name}]}],
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                })

            def _sse_body(self, body):
                """把整段 SSE 作为**完整的 HTTP 响应**发出（带 Content-Length）。

                这样「缺少终止事件」就是上游真正的行为，而不是连接异常：
                两者在流程上必须分开判定。"""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def _responses_stream(self, mode):
                events = [
                    {"type": "response.created", "response": {"id": "resp_%s" % outer.name,
                                                              "object": "response"}},
                    {"type": "response.output_text.delta", "delta": "第一段"},
                    {"type": "response.output_text.delta", "delta": "第二段"},
                ]
                if mode == "responses_sse_no_done":
                    events.append({"type": "response.completed",
                                   "response": {"id": "resp_%s" % outer.name,
                                                "object": "response"}})
                body = b"".join(("data: " + json.dumps(e, ensure_ascii=False) + "\n\n").encode("utf-8")
                                for e in events)
                if mode == "responses_sse_done_only":
                    body += b"data: [DONE]\n\n"
                return self._sse_body(body)

            def _chat_stream(self, mode):
                chunks = ["第一段", "第二段", "chat.completion"]
                body = b""
                for text in chunks:
                    obj = {"id": "chatcmpl-%s" % outer.name,
                           "object": "chat.completion.chunk", "model": "demo-model",
                           "choices": [{"index": 0, "delta": {"content": text},
                                        "finish_reason": None}]}
                    body += ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
                if mode == "chat_sse":
                    body += b"data: [DONE]\n\n"
                return self._sse_body(body)

            def _stream(self, mode):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if mode == "abort_stream":
                    # 声明一个大于实际发送量的长度，再粗暴断开：
                    # 这样客户端会拿到 IncompleteRead（真的「中断」，而不是正常结束）
                    self.send_header("Content-Length", "100000")
                self.end_headers()
                if mode == "empty_stream":
                    try:
                        self.wfile.flush()
                        self.close_connection = True
                    except Exception:
                        pass
                    return
                chunks = [
                    {"type": "response.output_text.delta", "delta": "hi from " + outer.name},
                ]
                for c in chunks:
                    try:
                        self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        return
                if mode == "abort_stream":
                    # 粗暴断开：不发 [DONE]、不发终止块（客户端会拿到 IncompleteRead）
                    try:
                        self.wfile.flush()
                        self.connection.close()
                    except Exception:
                        pass
                    return
                try:
                    self.wfile.write(("data: " + json.dumps(
                        {"type": "response.completed",
                         "response": {"id": "resp_%s" % outer.name, "object": "response"}}) + "\n\n").encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
        self._httpd = None

    # ---- 断言辅助 ----
    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.port

    @property
    def hits(self):
        return len(self.requests)

    def reset(self):
        self.requests = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False