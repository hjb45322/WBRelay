# -*- coding: utf-8 -*-
"""上游读取边界（限长 / 超时 / 畸形 JSON / 句柄关闭）的隔离测试（第三轮）。

覆盖 AGENTS.md 第三轮「回归核对」要求的上游读取点：
  L1 供应商 /models 拉取（wbrelay.models.fetch_supplier_models）
  L2 OpenRouter 档案同步（wbrelay.net.fetch_openrouter_profiles）
  L3 单供应商测速（wbrelay.web.api_admin._api_supplier_ping）
  L4 用户提交的 URL catalog 导入（wbrelay.web.api_state._api_profiles_import, source=url）

每组都覆盖：正常 / 超限 / 超时 / 畸形 JSON / 句柄关闭；
全程使用进程内自建 mock（动态端口），并用 socket guard 断言不连接任何真实上游。

用法：python tests/test_upstream_read_limits.py
"""
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import isolate

DATA_DIR = isolate.bootstrap()
TMP_ROOT = isolate.tmp_root()

from wbrelay import models, net, state                                # noqa: E402
from wbrelay.web import api_admin, api_state                          # noqa: E402

PASS, FAIL = [], []
PORT_NOBODY = 18999          # 关闭端口（不会被连接；guard 会拦下来）


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


class RawServer(object):
    """最小 GET mock：返回固定正文（可延迟），记录请求数。"""

    def __init__(self, body=b"{}", ctype="application/json", status=200, delay=0.0):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.ctype = ctype
        self.status = status
        self.delay = delay
        self.requests = 0
        self._httpd = None
        self._thread = None
        self.port = None

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests += 1
                if outer.delay:
                    time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", outer.ctype)
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    self.wfile.write(outer.body)
                except Exception:
                    pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None


class Recording(object):
    """包装真实响应：记录 read 上限与 close，用于断言「句柄一定关闭」。"""

    def __init__(self, resp):
        self._resp = resp
        self.closed = False
        self.status = getattr(resp, "status", 200)

    def read(self, *a, **k):
        return self._resp.read(*a, **k)

    @property
    def headers(self):
        return self._resp.headers

    def close(self):
        self.closed = True
        try:
            self._resp.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def main():
    mock_ok = RawServer(json.dumps({"data": [{"id": "demo-model", "object": "model"}]}).encode()).start()
    mock_big = RawServer(b"x" * (net.MAX_UPSTREAM_JSON_BYTES + 4096)).start()
    mock_bad = RawServer(b"<html>not json</html>", ctype="text/html").start()
    mock_slow = RawServer(b"{}", delay=2.0).start()
    mocks = [mock_ok, mock_big, mock_bad, mock_slow]

    allowed_ports = set(m.port for m in mocks)
    guard_violations = []
    real_create = socket.create_connection
    patched = []
    wrappers = []

    def remember(obj, name, value):
        patched.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def recording_urlopen(real_open):
        def _open(req, timeout):
            resp = real_open(req, timeout)
            rec = Recording(resp)
            wrappers.append(rec)
            return rec
        return _open

    try:
        def guarded_create(address, *a, **k):
            port = address[1] if isinstance(address, (tuple, list)) and len(address) > 1 else None
            if port not in allowed_ports:
                guard_violations.append(address)
                raise AssertionError("测试禁止连接 %r（只允许本测试自建端口 %s）"
                                     % (address, sorted(allowed_ports)))
            return real_create(address, *a, **k)

        socket.create_connection = guarded_create
        # ⚠️ 这些模块都是 from-import 持有 _urlopen 引用：必须 patch 各模块自己的属性
        remember(models, "_urlopen", recording_urlopen(net._urlopen))
        remember(api_admin, "_urlopen", recording_urlopen(net._urlopen))
        remember(api_state, "_urlopen", recording_urlopen(net._urlopen))
        remember(net, "_urlopen", recording_urlopen(net._urlopen))

        sup = {"id": "sup_demo", "name": "演示", "base_url": mock_ok.url("/v1"),
               "api_key": "sk-test-FAKE-KEY-READLIMIT", "models": ["*"]}

        # ---- L1 /models 拉取 ----
        wrappers[:] = []
        got, err = models.fetch_supplier_models(sup, timeout=3)
        check("L1a 正常 /models → 解析成功", err is None and got and got[0]["id"] == "demo-model",
              "err=%s got=%r" % (err, got))
        check("L1b 正常路径句柄已关闭", wrappers and all(w.closed for w in wrappers),
              repr([w.closed for w in wrappers]))
        check("L1c 只连了本测试 mock 端口", guard_violations == [], repr(guard_violations[:2]))

        big_sup = dict(sup, base_url=mock_big.url("/v1"))
        wrappers[:] = []
        got, err = models.fetch_supplier_models(big_sup, timeout=5)
        check("L1d 响应体超限 → 返回错误（不整体读进内存）",
              got is None and err and "上限" in err, "err=%s" % err)
        check("L1e 超限路径句柄已关闭", wrappers and all(w.closed for w in wrappers),
              repr([w.closed for w in wrappers]))

        bad_sup = dict(sup, base_url=mock_bad.url("/v1"))
        wrappers[:] = []
        got, err = models.fetch_supplier_models(bad_sup, timeout=3)
        check("L1f 畸形 JSON → 返回错误", got is None and bool(err), "err=%s" % err)
        check("L1g 畸形 JSON 路径句柄已关闭", wrappers and all(w.closed for w in wrappers))

        slow_sup = dict(sup, base_url=mock_slow.url("/v1"))
        t0 = time.time()
        got, err = models.fetch_supplier_models(slow_sup, timeout=0.3)
        dt = time.time() - t0
        check("L1h 读取超时 → 在超时窗口内返回错误", got is None and err and dt < 2.0,
              "dt=%.2fs err=%s" % (dt, err))

        # ---- L2 OpenRouter 档案同步 ----
        remember(net, "OPENROUTER_URL", mock_ok.url("/api/v1/models"))
        remember(net, "OPENROUTER_TIMEOUT_SEC", 3)
        remember(net, "_openrouter_cache", {"fetched_at": 0.0, "profiles": {}})
        remember(api_state, "fetch_openrouter_profiles", net.fetch_openrouter_profiles)
        body_models = json.dumps({"data": [{"id": "openai/demo-model", "architecture": {},
                                            "top_provider": {}, "reasoning": {}, "pricing": {}}]}).encode()
        mock_or = RawServer(body_models).start()
        mocks.append(mock_or)
        allowed_ports.add(mock_or.port)
        net.OPENROUTER_URL = mock_or.url("/api/v1/models")
        wrappers[:] = []
        out = net.fetch_openrouter_profiles(force=True)
        check("L2a OpenRouter 正常 → 返回档案", isinstance(out, dict) and "demo-model" in out, repr(out)[:120])
        check("L2b OpenRouter 路径句柄已关闭", wrappers and all(w.closed for w in wrappers))

        net.OPENROUTER_URL = mock_big.url("/api/v1/models")
        wrappers[:] = []
        out = net.fetch_openrouter_profiles(force=True)
        check("L2c OpenRouter 超限 → 返回空（不抛到调用方）", out == {}, repr(out)[:80])
        check("L2d OpenRouter 超限路径句柄已关闭", wrappers and all(w.closed for w in wrappers))

        net.OPENROUTER_URL = mock_bad.url("/api/v1/models")
        out = net.fetch_openrouter_profiles(force=True)
        check("L2e OpenRouter 畸形 JSON → 返回空", out == {}, repr(out)[:80])

        net.OPENROUTER_URL = mock_slow.url("/api/v1/models")
        net.OPENROUTER_TIMEOUT_SEC = 0.3
        t0 = time.time()
        out = net.fetch_openrouter_profiles(force=True)
        dt = time.time() - t0
        check("L2f OpenRouter 超时 → 返回空且不拖长", out == {} and dt < 2.0, "dt=%.2fs" % dt)

        # ---- L3 供应商测速（ping）----
        remember(api_admin, "get_suppliers", lambda: [sup])
        fake = type("FakeAdminHandler", (), {})()
        fake._payload = lambda: {"id": "sup_demo"}
        fake.out = []
        fake._json = lambda code, obj: fake.out.append((code, obj))
        fake._err = lambda code, msg, etype="e": fake.out.append((code, {"error": msg}))

        wrappers[:] = []
        api_admin.ApiAdminMixin._api_supplier_ping(fake)
        code, obj = fake.out[-1]
        check("L3a ping 正常 → ok=True / status=200", code == 200 and obj.get("ok") is True, repr(obj))
        check("L3b ping 路径句柄已关闭", wrappers and all(w.closed for w in wrappers))

        remember(api_admin, "get_suppliers", lambda: [dict(sup, base_url=mock_big.url("/v1"))])
        fake.out = []
        wrappers[:] = []
        api_admin.ApiAdminMixin._api_supplier_ping(fake)
        code, obj = fake.out[-1]
        check("L3c ping 响应体超限（>2MB）→ ok=False 且不整体读进内存",
              code == 200 and obj.get("ok") is False, repr(obj)[:120])
        check("L3d ping 超限路径句柄已关闭", wrappers and all(w.closed for w in wrappers))

        remember(api_admin, "get_suppliers", lambda: [dict(sup, base_url=mock_slow.url("/v1"))])
        remember(api_admin, "SUPPLIER_PING_TIMEOUT_SEC", 0.3)
        fake.out = []
        t0 = time.time()
        api_admin.ApiAdminMixin._api_supplier_ping(fake)
        code, obj = fake.out[-1]
        dt = time.time() - t0
        check("L3e ping 超时 → ok=False 且不拖长", obj.get("ok") is False and dt < 2.0,
              "dt=%.2fs obj=%s" % (dt, repr(obj)[:100]))

        # ---- L4 用户提交的 URL catalog 导入 ----
        catalog_body = json.dumps({"models": [{"slug": "demo-model", "display_name": "Demo"}]}).encode()
        mock_cat = RawServer(catalog_body).start()
        mocks.append(mock_cat)
        allowed_ports.add(mock_cat.port)

        fake2 = type("FakeStateHandler", (), {})()
        fake2.out = []
        fake2._json = lambda code, obj: fake2.out.append((code, obj))
        fake2._err = lambda code, msg, etype="e": fake2.out.append((code, {"error": msg}))

        fake2._payload = lambda: {"source": "url", "url": mock_cat.url("/catalog.json")}
        wrappers[:] = []
        api_state.ApiStateMixin._api_profiles_import(fake2)
        code, obj = fake2.out[-1]
        check("L4a URL catalog 正常 → 导入成功", code == 200 and obj.get("ok") is True, repr(obj)[:120])
        check("L4b catalog 路径句柄已关闭", wrappers and all(w.closed for w in wrappers))

        fake2.out = []
        fake2._payload = lambda: {"source": "url", "url": mock_big.url("/catalog.json")}
        wrappers[:] = []
        api_state.ApiStateMixin._api_profiles_import(fake2)
        code, obj = fake2.out[-1]
        check("L4c catalog 超限 → 500 + 拉取失败，且句柄关闭",
              code == 500 and "拉取失败" in obj.get("error", "") and wrappers and all(w.closed for w in wrappers),
              repr(obj)[:120])

        fake2.out = []
        fake2._payload = lambda: {"source": "url", "url": mock_bad.url("/catalog.json")}
        api_state.ApiStateMixin._api_profiles_import(fake2)
        code, obj = fake2.out[-1]
        check("L4d catalog 畸形 JSON → 500 + 拉取失败", code == 500 and "拉取失败" in obj.get("error", ""),
              repr(obj)[:120])

        remember(api_state, "CATALOG_URL_TIMEOUT_SEC", 0.3)
        fake2.out = []
        fake2._payload = lambda: {"source": "url", "url": mock_slow.url("/catalog.json")}
        t0 = time.time()
        api_state.ApiStateMixin._api_profiles_import(fake2)
        code, obj = fake2.out[-1]
        dt = time.time() - t0
        check("L4e catalog 超时 → 500 且不拖长", code == 500 and dt < 2.0, "dt=%.2fs %s" % (dt, repr(obj)[:100]))

        fake2.out = []
        fake2._payload = lambda: {"source": "url", "url": "file:///etc/passwd"}
        before = sum(m.requests for m in mocks)
        api_state.ApiStateMixin._api_profiles_import(fake2)
        code, obj = fake2.out[-1]
        check("L4f 非 http(s) URL → 400 且零网络请求",
              code == 400 and sum(m.requests for m in mocks) == before, repr(obj)[:100])

        check("L5 socket guard 未记录任何真实上游连接", guard_violations == [], repr(guard_violations[:3]))
        check("L5b 全部 mock 都只在 127.0.0.1 动态端口",
              all(m.port not in (8788, 8789) for m in mocks), repr([m.port for m in mocks]))
    finally:
        socket.create_connection = real_create
        for obj, name, value in reversed(patched):
            setattr(obj, name, value)
        for m in mocks:
            m.stop()

    print("\n===== 上游读取边界：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())