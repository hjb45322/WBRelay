# -*- coding: utf-8 -*-
"""请求体解析回归：Content-Length / chunked / 错误语义 / 大请求。

覆盖 2026-10-19 长会话 `invalid json` 误报：
  · 默认 32 MiB，9 MiB 请求必须进入路由；
  · chunked 请求必须可解析；
  · 413 / 411 / 400 必须区分；
  · 失败连接关闭后，服务器仍可继续服务；
  · 日志只记录帧元数据，不记录正文或凭据。

用法：python tests/test_payload_parser.py
"""
import http.client
import io
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from email.message import Message

import isolate

isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state                                      # noqa: E402
from wbrelay.app import _GatewayServer                         # noqa: E402
from wbrelay.paths import (LOG_PATH, MAX_PAYLOAD,              # noqa: E402
                           MAX_PAYLOAD_CAP, MAX_PAYLOAD_DEFAULT,
                           max_payload_from_env)
from wbrelay.web.payload import (MAX_CHUNK_LINE, PayloadError,  # noqa: E402
                                  read_json_payload)
from wbrelay.web.server import Handler                         # noqa: E402

PASS, FAIL = [], []
FAKE_KEY = "sk-test-FAKE-PAYLOAD-LOG-1234567890"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def headers(pairs):
    msg = Message()
    for key, value in pairs:
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            msg.add_header(key, str(item))
    return msg


def parse(pairs, raw, limit=1024):
    return read_json_payload(headers(pairs), io.BytesIO(raw), limit)


def expect_error(name, pairs, raw, limit, status, message):
    try:
        parse(pairs, raw, limit)
    except PayloadError as exc:
        check(name, exc.status == status and exc.message == message,
              "status=%s message=%s bytes_read=%s" % (exc.status, exc.message, exc.bytes_read))
        return exc
    check(name, False, "没有抛出 PayloadError")
    return None


def chunked_body(parts, trailer=b""):
    out = []
    for part in parts:
        out.append(("%x\r\n" % len(part)).encode("ascii"))
        out.append(part)
        out.append(b"\r\n")
    out.append(b"0\r\n")
    out.append(trailer)
    out.append(b"\r\n")
    return b"".join(out)


def log_size():
    try:
        return os.path.getsize(LOG_PATH)
    except OSError:
        return 0


def log_since(offset):
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def start_server():
    state._SERVER_PORT = PORT
    server = _GatewayServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def raw_exchange(path, request_headers, body=b"", method="POST", timeout=30,
                 shutdown_write=False):
    """直接拼 HTTP 请求，保留重复 Content-Length 等测试所需的原始帧。"""
    lines = ["%s %s HTTP/1.1" % (method, path),
             "Host: 127.0.0.1:%d" % PORT]
    for key, value in request_headers:
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            lines.append("%s: %s" % (key, item))
    raw_headers = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    sock = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
    try:
        sock.sendall(raw_headers + body)
        if shutdown_write:
            sock.shutdown(socket.SHUT_WR)
        resp = http.client.HTTPResponse(sock, method=method)
        resp.begin()
        data = resp.read()
        out = {}
        for key, value in resp.getheaders():
            out.setdefault(key.lower(), []).append(value)
        return resp.status, out, data.decode("utf-8", "replace")
    finally:
        sock.close()


def http_get(path, timeout=20):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, "<%s>" % type(exc).__name__


def parser_checks():
    raw = b'{"ok":true}'
    got = parse([("Content-Length", len(raw))], raw)
    check("P1 Content-Length 正常 JSON", got == {"ok": True}, repr(got))

    exact_raw = b'{"x":"' + b"a" * 40 + b'"}'
    exact = parse([("Content-Length", len(exact_raw))], exact_raw, len(exact_raw))
    check("P2 Content-Length 恰好等于上限", exact.get("x") == "a" * 40, repr(exact)[:80])

    expect_error("P3 Content-Length 上限 + 1 → 413",
                 [("Content-Length", 11)], b"", 10, 413, "payload too large")

    chunked_parts = [raw[:3], raw[3:]]
    got = parse([("Transfer-Encoding", "chunked")], chunked_body(chunked_parts), 1024)
    check("P4 chunked JSON 成功", got == {"ok": True}, repr(got))

    over_parts = [b"12345678", b"9012345"]
    exc = expect_error("P5 chunked 累计超限 → 413",
                       [("Transfer-Encoding", "chunked")],
                       chunked_body(over_parts), 10, 413, "payload too large")
    check("P5a 超限时未继续读取后续 chunk", exc is not None and exc.bytes_read == 8,
          "bytes_read=%s" % (exc.bytes_read if exc else None))

    expect_error("P6 无 Content-Length 且非 chunked → 411",
                 [], b"", 1024, 411, "length required")
    expect_error("P7 Content-Length + Transfer-Encoding → 400",
                 [("Content-Length", 2), ("Transfer-Encoding", "chunked")],
                 b"{}", 1024, 400, "invalid request framing")
    expect_error("P8 重复 Content-Length → 400",
                 [("Content-Length", "2"), ("Content-Length", "2")],
                 b"{}", 1024, 400, "invalid request framing")
    expect_error("P9 正文截断 → 400",
                 [("Content-Length", 10)], b"{}", 1024, 400, "incomplete request body")
    expect_error("P10 真正 malformed JSON → invalid json",
                 [("Content-Length", 5)], b'{"a":', 1024, 400, "invalid json")
    expect_error("P11 非法 UTF-8 → invalid utf-8",
                 [("Content-Length", 1)], b"\xff", 1024, 400, "invalid utf-8")
    expect_error("P12 chunk size 非法 → invalid chunked body",
                 [("Transfer-Encoding", "chunked")], b"z\r\n", 1024,
                 400, "invalid chunked body")
    expect_error("P12b chunk size 行超过长度上限 → 400",
                 [("Transfer-Encoding", "chunked")],
                 b"0" * (MAX_CHUNK_LINE + 1) + b"\r\n", 1024,
                 400, "invalid chunked body")

    trailer_raw = chunked_body([raw], b"X-Test: 1\r\n")
    got = parse([("Transfer-Encoding", "chunked")], trailer_raw, 1024)
    check("P13 chunked trailer 可读取并忽略", got == {"ok": True}, repr(got))

    got = parse([("Content-Length", 0)], b"")
    check("P14 Content-Length: 0 → 空对象（兼容可选正文）", got == {}, repr(got))

    check("P15 默认上限为 32 MiB",
          MAX_PAYLOAD_DEFAULT == 32 * 1024 * 1024 and MAX_PAYLOAD == MAX_PAYLOAD_DEFAULT,
          "MAX_PAYLOAD=%d DEFAULT=%d" % (MAX_PAYLOAD, MAX_PAYLOAD_DEFAULT))
    check("P16 环境变量十进制值生效", max_payload_from_env("1024") == 1024)
    check("P17 环境变量超过硬上限时截断",
          max_payload_from_env(str(MAX_PAYLOAD_CAP + 1)) == MAX_PAYLOAD_CAP)
    check("P18 非法环境变量回退默认值",
          max_payload_from_env("not-a-number") == MAX_PAYLOAD_DEFAULT)


def integration_checks():
    # 9 MiB：旧实现会在这里返回 invalid json；新实现应完成解析并进入路由。
    large = b'{"input":"' + b"x" * (9 * 1024 * 1024) + b'"}'
    st, _hdrs, body = raw_exchange(
        "/v1/responses",
        [("Content-Length", len(large)), ("Content-Type", "application/json")],
        large,
    )
    check("I1 9 MiB Content-Length 请求不再误报 invalid json",
          st == 400 and "model" in body and "invalid json" not in body,
          "status=%s %s" % (st, body[:100]))

    chunked = b'{"model":""}'
    st, _hdrs, body = raw_exchange(
        "/v1/responses",
        [("Transfer-Encoding", "chunked"), ("Content-Type", "application/json")],
        chunked_body([chunked[:5], chunked[5:]]),
    )
    check("I2 chunked 请求进入实际 API 路由",
          st == 400 and "model" in body and "invalid json" not in body,
          "status=%s %s" % (st, body[:100]))

    oversized_size = format(MAX_PAYLOAD + 1, "x").encode("ascii") + b"\r\n"
    st, response_headers, body = raw_exchange(
        "/v1/responses",
        [("Transfer-Encoding", "chunked"), ("Content-Type", "application/json")],
        oversized_size,
    )
    check("I3 chunked 超限 → 413 payload too large",
          st == 413 and "payload too large" in body,
          "status=%s %s" % (st, body[:100]))
    check("I4 413 明确要求关闭连接",
          "close" in " ".join(response_headers.get("connection", [])).lower(),
          repr(response_headers.get("connection")))
    st, _body = http_get("/api/state")
    check("I5 413 后新连接仍正常服务", st == 200, "status=%s" % st)

    st, _hdrs, body = raw_exchange(
        "/v1/responses",
        [("Content-Length", "5"), ("Content-Type", "application/json")],
        b'{"a":',
    )
    check("I6 malformed JSON 仍明确返回 invalid json",
          st == 400 and "invalid json" in body, "status=%s %s" % (st, body[:100]))

    st, _hdrs, body = raw_exchange("/v1/responses", [], b"")
    check("I7 无长度 → 411 length required",
          st == 411 and "length required" in body, "status=%s %s" % (st, body[:100]))

    st, _hdrs, body = raw_exchange(
        "/v1/responses",
        [("Content-Length", "2"), ("Content-Length", "2")],
        b"{}",
    )
    check("I8 重复 Content-Length → 400 invalid request framing",
          st == 400 and "invalid request framing" in body,
          "status=%s %s" % (st, body[:100]))

    st, _hdrs, body = raw_exchange(
        "/v1/responses",
        [("Content-Length", "2"), ("Transfer-Encoding", "chunked")],
        b"{}",
    )
    check("I9 CL + TE → 400 invalid request framing",
          st == 400 and "invalid request framing" in body,
          "status=%s %s" % (st, body[:100]))

    st, _hdrs, body = raw_exchange(
        "/v1/responses",
        [("Content-Length", "100")],
        b'{"a":',
        shutdown_write=True,
    )
    check("I10 截断正文 → 400 incomplete request body",
          st == 400 and "incomplete request body" in body,
          "status=%s %s" % (st, body[:100]))

    offset = log_size()
    st, _hdrs, body = raw_exchange(
        "/v1/responses",
        [("Content-Length", MAX_PAYLOAD + 1),
         ("Authorization", "Bearer " + FAKE_KEY)],
        b"",
    )
    delta = log_since(offset)
    check("I11 超限请求日志记录原因与帧元数据",
          st == 413 and "payload rejected" in delta and "payload too large" in delta
          and "content_length=" in delta,
          repr(delta[:240]))
    check("I12 日志不包含请求体或 Authorization 凭据",
          FAKE_KEY not in delta and "Authorization" not in delta and '{"' not in delta,
          repr(delta[:240]))


def main():
    isolate.assert_isolated()
    parser_checks()

    isolate.write_settings(isolate.base_settings())
    server = start_server()
    try:
        integration_checks()
        isolate.assert_isolated()
    finally:
        server.shutdown()
        server.server_close()

    print("\n===== 请求体解析：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("  失败项：" + name)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())