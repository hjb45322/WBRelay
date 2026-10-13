# -*- coding: utf-8 -*-
"""e2e 用 mock 上游：127.0.0.1:18788(MOCK1) / 18789(MOCK2)。

用法：python mock_server.py MOCK1 18788
      python mock_server.py MOCK2 18789

GET  /v1/models            -> gpt 系列模型列表
POST /v1/responses         -> 文本 "MOCK1: ok" / "MOCK2: ok"（stream=False）
POST /v1/responses stream  -> SSE 流（含 [DONE]）
零第三方依赖（http.server 标准库），符合项目约束。
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

NAME = sys.argv[1] if len(sys.argv) > 1 else "MOCK1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18788
MODELS = ["gpt-5.6-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "codex-auto-review"]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list",
                             "data": [{"id": m, "object": "model"} for m in MODELS]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
            except Exception:
                payload = {}
        if self.path.rstrip("/").endswith("/responses"):
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [
                    {"type": "response.output_text.delta", "delta": "hi from " + NAME},
                    {"type": "response.completed",
                     "response": {"id": "resp_%s" % NAME.lower(), "object": "response"}},
                ]
                for c in chunks:
                    self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                self._send(200, {
                    "id": "resp_%s" % NAME.lower(),
                    "object": "response",
                    "output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "%s: ok" % NAME}]}],
                })
        else:
            self._send(404, {"error": {"message": "not found"}})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("mock %s listening on 127.0.0.1:%d" % (NAME, PORT), flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
