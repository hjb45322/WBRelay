# -*- coding: utf-8 -*-
"""上游服务面板代理 + R6 只读边界的隔离回归测试。

覆盖（所有者 2026-10-14 决定恢复「面板查看」，见 docs/方案-上游服务面板恢复.md）：
  U1  模块能力表：upstream_start / upstream_stop / _find_python_for 仍未恢复（整体缺失）
  U2  静态审计：上游模块没有 Popen、也没有任何启停类 PowerShell 动词（非注释部分）
  U3  接口审计：POST /api/upstream/start|stop → 409 + 策略说明，**零子进程、零计划任务操作**
  U4  状态接口只读：明确标注启停能力未恢复 + 面板代理真实开关
  U5  外部上游自启：调用即报错，不静默成功
  U6  计划任务查询只用 Get-ScheduledTask（只读），不含任何启停/禁用动词
  U7  上游目录只读：上述操作前后，临时上游 fixture 目录内容逐字节不变
  U8  静态审计（代理模块）：只用 urllib.request 打 127.0.0.1；无 cookie jar、无裸 read()
  U9  socket guard：除测试自己的动态 mock 端口外，任何连接尝试都让测试失败
  U10 面板代理透传：只转发 Content-Type / X-Panel-Token；Cookie / Authorization / Referer 不到上游
  U11 面板根 HTML 绝对路径改写成 /proxy-wb/（其余原样透传）
  U12 路径穿越 / 外部绝对 URL / 编码绕过 / 反斜杠 / 非法 query → 400，mock 上游零请求
  U13 超大请求体 → 413（不读 body、零上游请求）
  U14 非本机 Host / 跨站 Origin → 403
  U15 面板开关关闭 → 403 + 策略说明，mock 上游零请求
  U16 上游不在线 → 502
  U17 上游响应体超限 → 502
  U18 上游超时 → 502 且句柄关闭

隔离：全程只用自建临时目录 + 自建 mock；不联网、不占 8788/8789、不读写任何真实上游目录，
  **绝不启停真实上游**（本测试不调用任何启停能力，只断言它们仍未恢复）。
  · `api_upstream` 是 from-import 持有 `upstream_online` / `upstream_task_state` 引用，
    必须 patch **api_upstream 模块属性**（patch 源模块无效）。
  · `socket.create_connection` guard 只放行本测试自己创建的端口；真实 8788 探测会立即失败。

用法：python tests/test_upstream_readonly.py
"""
import ast
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state                                                # noqa: E402
from wbrelay.integrations import upstream                                # noqa: E402
from wbrelay.web import api_upstream                                      # noqa: E402
from wbrelay.web.server import Handler                                    # noqa: E402

from mock_upstream import MockUpstream                                     # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPSTREAM_SRC = os.path.join(REPO, "wbrelay", "integrations", "upstream.py")
API_UPSTREAM_SRC = os.path.join(REPO, "wbrelay", "web", "api_upstream.py")
CONTROL_VERBS = ("Start-ScheduledTask", "Stop-ScheduledTask", "Disable-ScheduledTask",
                 "Enable-ScheduledTask", "Start-Process", "Stop-Process", "taskkill",
                 "schtasks", "Unregister-ScheduledTask")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http(path, body=None, timeout=20, headers=None, method=None):
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)


def raw_request(method, path, headers):
    """原始请求：只发请求头（用于伪造 Host / 超大 Content-Length / Transfer-Encoding）。"""
    import http.client as http_client
    conn = http_client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, body


def dir_manifest(root):
    """临时上游 fixture 目录的只读清单（仅本测试自己创建的目录）。"""
    out = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            full = os.path.join(base, n)
            with io.open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def source_without_module_docstring(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    tree = ast.parse(text)
    first = tree.body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        lines = text.splitlines(True)
        start, end = first.lineno - 1, first.end_lineno
        text = "".join(lines[:start] + lines[end:])
    return text


def subprocess_calls(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                out.append(node.func.attr)
    return out


def main():
    up_dir = os.path.join(isolate.tmp_root(), "fake-upstream-readonly")
    os.makedirs(up_dir, exist_ok=True)
    with io.open(os.path.join(up_dir, "wb_proxy.py"), "w", encoding="utf-8") as fh:
        fh.write("# fixture：上游入口（被 R6 保护，不得被任何接口启停）\n")

    # 自建 mock 上游：动态端口 + 记录收到的每个请求（**只允许被测试自己的 socket guard 连接**）
    mock = MockUpstream("mock-upstream-readonly", html="<html>fixture</html>")
    mock.start()
    with io.open(os.path.join(up_dir, "run.bat"), "w", encoding="utf-8") as fh:
        fh.write("@echo off\nset PORT=%d\npython wb_proxy.py\n" % mock.port)
    manifest_before = dir_manifest(up_dir)

    isolate.write_settings(isolate.base_settings(
        upstream_enabled=True, upstream_dir=up_dir, upstream_task="WBRelay-Fake-Upstream"))

    server = None
    started, run_calls = [], []
    guard_violations = []
    allowed_ports = {PORT, mock.port}
    real_popen, real_run = subprocess.Popen, subprocess.run
    real_create = socket.create_connection
    real_online, real_task_state = api_upstream.upstream_online, api_upstream.upstream_task_state
    try:
        def fake_popen(*a, **k):
            started.append(("Popen", a, k))
            raise AssertionError("测试期间不得创建进程：%r" % (a,))

        def fake_run(*a, **k):
            run_calls.append(("run", a, k))
            raise AssertionError("测试期间不得创建进程：%r" % (a,))

        def guarded_create(address, *a, **k):
            port = address[1] if isinstance(address, (tuple, list)) and len(address) > 1 else None
            if port not in allowed_ports:
                guard_violations.append((address, a, k))
                raise AssertionError("测试禁止连接 %r（只允许本测试自建端口 %s）"
                                     % (address, sorted(allowed_ports)))
            return real_create(address, *a, **k)

        subprocess.Popen = fake_popen
        subprocess.run = fake_run
        socket.create_connection = guarded_create
        # ⚠️ from-import 引用：必须 patch api_upstream 模块属性（patch upstream 无效）
        api_upstream.upstream_online = lambda: False
        api_upstream.upstream_task_state = lambda: "not_configured"

        problems = isolate.isolation_problems()
        check("U0a 数据目录已隔离（临时目录）", not problems, "；".join(problems))
        check("U0b 上游 fixture 目录在临时根内（自建）",
              os.path.abspath(up_dir).startswith(os.path.abspath(isolate.tmp_root())), up_dir)
        check("U0c mock 上游端口是动态分配、不是 8788/8789",
              mock.port not in (8788, 8789), "port=%d" % mock.port)

        # ---- U1 能力表 ----
        check("U1a upstream_start 已整体移除", not hasattr(upstream, "upstream_start"))
        check("U1b upstream_stop 已整体移除", not hasattr(upstream, "upstream_stop"))
        check("U1c _find_python_for 已整体移除（不再探测外部 Python）",
              not hasattr(upstream, "_find_python_for"))
        check("U1d EXTERNAL_CONTROL_SUPPORTED 恒为 False",
              upstream.EXTERNAL_CONTROL_SUPPORTED is False)

        # ---- U2 静态审计（上游模块）----
        body = source_without_module_docstring(UPSTREAM_SRC)
        tree = ast.parse(body)
        check("U2a 代码里没有 subprocess.Popen", "Popen" not in subprocess_calls(tree),
              repr(subprocess_calls(tree)))
        check("U2b 唯一的 subprocess 调用是只读查询用的 run",
              subprocess_calls(tree) == ["run"], repr(subprocess_calls(tree)))
        hits = [v for v in CONTROL_VERBS if v in body]
        check("U2c 非注释部分不含任何启停/禁用类动词", not hits, repr(hits))

        # ---- U8 静态审计（代理模块）：同源代理已恢复，但必须仍是「只打本机回环」的形态 ----
        api_body = source_without_module_docstring(API_UPSTREAM_SRC)
        api_tree = ast.parse(api_body)
        imported = set()
        for node in ast.walk(api_tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        check("U8a 代理模块 import urllib.request，但不 import http.cookiejar / http.client",
              "urllib.request" in imported
              and not any(m.startswith("http.") or m == "http" for m in imported),
              repr(sorted(imported)))
        check("U8b 代理模块不启用 cookie jar（历史版全局登录态共享已取消）",
              "cookiejar" not in api_body and "_wb_cookiejar" not in api_body
              and "HTTPCookieProcessor" not in api_body, "")
        check("U8c 代理模块没有裸 resp.read()（一律带长度上限）",
              ".read(" in api_body and ".read(PROXY_MAX_RESPONSE_BYTES + 1)" in api_body
              and ".read()" not in api_body, "")
        check("U8d 代理模块只转发白名单头（Content-Type / X-Panel-Token）",
              api_upstream.PROXY_FORWARD_HEADERS == ("Content-Type", "X-Panel-Token"),
              repr(api_upstream.PROXY_FORWARD_HEADERS))
        check("U8e 目标地址只能是 UPSTREAM_HOST + 解析端口",
              api_upstream.UPSTREAM_HOST == "127.0.0.1" and "http://%s:%d%s" in api_body, "")
        check("U8f 能力开关 PROXY_ENABLED is True", api_upstream.PROXY_ENABLED is True)
        check("U8g 面板开关默认开启（settings 缺省值）",
              api_upstream.proxy_enabled() is True, "")

        # ---- U6 计划任务查询只读 ----
        captured = {}

        class _FakeProc(object):
            stdout = "Ready\n"

        def fake_ps(script, timeout=8):
            captured["script"] = script
            return _FakeProc()

        real_ps = upstream._run_powershell
        upstream._run_powershell = fake_ps
        try:
            st_task = upstream.upstream_task_state()
        finally:
            upstream._run_powershell = real_ps
        check("U6a 计划任务查询走 Get-ScheduledTask", "Get-ScheduledTask" in captured.get("script", ""),
              captured.get("script", "")[:60])
        check("U6b 计划任务查询脚本不含启停动词",
              not any(v in captured.get("script", "") for v in CONTROL_VERBS), captured.get("script", ""))
        check("U6c 状态返回值属于只读枚举", st_task == "ready", st_task)

        # ---- U3 / U4 接口审计 ----
        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        for label, path in (("启动", "/api/upstream/start"), ("停止", "/api/upstream/stop")):
            st, resp, _h = http(path, {})
            check("U3 %s 上游接口 → 409" % label, st == 409, "status=%s %s" % (st, resp[:80]))
            check("U3 %s 上游接口给出 R6 策略说明" % label,
                  "R6" in resp and "只读" in resp, resp[:120])
        check("U3c 接口调用后仍然零进程创建", started == [] and run_calls == [],
              "Popen=%d run=%d" % (len(started), len(run_calls)))

        st, body_status, _h = http("/api/upstream/status")
        s = json.loads(body_status)
        check("U4a 状态接口 200", st == 200, "status=%s" % st)
        check("U4b 状态里启停能力标为不支持", s.get("control_supported") is False,
              repr(s.get("control_supported")))
        check("U4c 状态里带 R6 策略说明", "R6" in str(s.get("control_policy") or ""),
              str(s.get("control_policy"))[:100])
        check("U4d 看板不再拿到启停按钮字段（manual_control_only 已移除）",
              "manual_control_only" not in s)
        check("U4e 状态接口不报 Python 探测结果（python_found 已移除）",
              "python_found" not in s)
        check("U4f 状态接口面板代理默认可用（proxy_enabled=true + proxy_url）",
              s.get("proxy_enabled") is True and s.get("proxy_url") == "/proxy-wb/"
              and not s.get("proxy_policy"),
              "proxy_enabled=%r proxy_url=%r" % (s.get("proxy_enabled"), s.get("proxy_url")))
        check("U4g 状态接口不出现任何启停/自启/守护可用标记",
              s.get("control_supported") is False and s.get("autostart_supported") is False
              and s.get("watchdog_supported") is False and s.get("autostart") is False
              and s.get("watchdog") is False, json.dumps(s, ensure_ascii=False)[:200])

        # ---- U10 面板代理透传（合法 GET）----
        mock.reset()
        st, body, hdrs_out = http("/proxy-wb/")
        check("U10a GET /proxy-wb/ → 200（同源代理到本机 mock 上游）",
              st == 200, "status=%s %s" % (st, body[:80]))
        check("U10b mock 上游只收到 1 个请求，路径是 /", mock.hits == 1,
              repr([r.get("path") for r in mock.requests]))
        check("U10c 上游收到的 User-Agent 是 WBRelay 固定值（不透传浏览器 UA）",
              mock.requests and mock.requests[0]["headers"].get("User-Agent", "").startswith("WBRelay/"),
              repr((mock.requests[0]["headers"].get("User-Agent") if mock.requests else None)))
        got = sorted(mock.requests[0]["headers"]) if mock.requests else []
        # 说明：Accept-Encoding: identity 由标准库 http.client 自动补，不是浏览器头的转发
        check("U10d 上游没收到 Cookie / Authorization / Referer / Origin（浏览器敏感头不透传）",
              mock.requests and not any(
                  k in mock.requests[0]["headers"]
                  for k in ("Cookie", "Authorization", "Referer", "Origin",
                            "X-Admin-Token", "X-Api-Key", "X-Panel-Token-Extra")),
              repr(got))
        check("U10e 代理不返回上游 cookie（没有 Set-Cookie）",
              not any(k.lower() == "set-cookie" for k in hdrs_out), repr(sorted(hdrs_out)))
        rewritten = api_upstream.ApiUpstreamMixin._rewrite_panel_html(
            ('<html><script src="/static/app.js"></script>'
             '<a href="/logout">x</a><script>fetch("/api/x")</script>'
             '<script>getJSON("/api/y")</script></html>').encode("utf-8")).decode("utf-8")
        check("U10f 面板根 HTML 绝对路径改写（src/href/fetch/getJSON → /proxy-wb/）",
              'src="/proxy-wb/static/app.js"' in rewritten
              and 'href="/proxy-wb/logout"' in rewritten
              and 'fetch("/proxy-wb/api/x")' in rewritten
              and 'getJSON("/proxy-wb/api/y")' in rewritten, rewritten[:160])

        # ---- U10g X-Panel-Token 与 Content-Type 会被转发（白名单内的两个头）----
        mock.reset()
        st, body, _h = http("/proxy-wb/v1/responses", {"model": "demo-model"},
                            headers={"X-Panel-Token": "panel-token-FAKE"})
        check("U10g POST 面板接口 → 200（透传）", st == 200, "status=%s %s" % (st, body[:80]))
        check("U10h 上游收到白名单内的 X-Panel-Token",
              mock.requests and mock.requests[0]["headers"].get("X-Panel-Token") == "panel-token-FAKE",
              repr(mock.requests[0]["headers"].get("X-Panel-Token") if mock.requests else None))
        check("U10i 上游收到 Content-Type（POST JSON）",
              mock.requests and "application/json" in (mock.requests[0]["headers"].get("Content-Type") or ""),
              repr(mock.requests[0]["headers"].get("Content-Type") if mock.requests else None))

        # ---- U11 路径穿越 / 外部目标 / 编码绕过 → 400，上游零请求 ----
        mock.reset()
        for label, path in (
                ("路径穿越", "/proxy-wb/../../etc/passwd"),
                ("编码穿越", "/proxy-wb/%2e%2e/%2e%2e/etc/passwd"),
                ("外部绝对 URL", "/proxy-wb/http://evil.example.com/"),
                ("协议相对", "/proxy-wb//evil.example.com/"),
                ("反斜杠", "/proxy-wb/\\evil\\path"),
                ("非法 query", "/proxy-wb/?x=<script>"),
                ("非 / 开头", "/proxy-wbx"),
        ):
            st, resp, _h = http(path)
            check("U11 %s → 400" % label, st == 400, "status=%s %s" % (st, resp[:100]))
        check("U11h 全部被拒路径都没打到 mock 上游", mock.hits == 0, "hits=%d" % mock.hits)

        # ---- U13 超大请求体 → 413，不读 body、零上游请求 ----
        st, resp = raw_request("POST", "/proxy-wb/api/panel", {
            "Host": "127.0.0.1:%d" % PORT,
            "Content-Length": str(api_upstream.PROXY_MAX_REQUEST_BYTES + 1),
            "Content-Type": "application/json",
        })
        check("U13a 超大请求体 → 413（先拒后读）", st == 413, "status=%s %s" % (st, resp[:100]))
        check("U13b 超大请求体没有打到 mock 上游", mock.hits == 0, "hits=%d" % mock.hits)
        st, resp = raw_request("POST", "/proxy-wb/api/panel", {
            "Host": "127.0.0.1:%d" % PORT,
            "Transfer-Encoding": "chunked",
            "Content-Type": "application/json",
        })
        check("U13c 分块请求体 → 413", st == 413, "status=%s %s" % (st, resp[:100]))

        # ---- U14 非本机 Host / 跨站 Origin → 403 ----
        st, resp = raw_request("GET", "/proxy-wb/", {"Host": "evil.example.com"})
        check("U14a 非回环 Host → 403（DNS rebinding）", st == 403, "status=%s" % st)
        st, resp = raw_request("GET", "/proxy-wb/", {"Host": "127.0.0.1:%d" % PORT,
                                                    "Origin": "http://evil.example.com"})
        check("U14b 跨站 Origin → 403（CSRF）", st == 403, "status=%s" % st)
        st, resp = raw_request("GET", "/proxy-wb/", {"Host": "127.0.0.1:%d" % PORT,
                                                    "Origin": "http://127.0.0.1:%d" % PORT})
        check("U14c 同源 Origin → 放行", st == 200, "status=%s" % st)

        # ---- U15 面板开关关闭 → 403 + 策略说明，mock 上游零请求 ----
        mock.reset()
        isolate.write_settings(isolate.base_settings(
            upstream_enabled=True, upstream_dir=up_dir,
            upstream_task="WBRelay-Fake-Upstream",
            upstream_proxy_enabled=False))
        st, resp, _h = http("/proxy-wb/")
        check("U15a upstream_proxy_enabled=false → 403 + 策略说明",
              st == 403 and "已停用" in resp, "status=%s %s" % (st, resp[:100]))
        check("U15b 关闭时 mock 上游零请求", mock.hits == 0, "hits=%d" % mock.hits)
        st, resp, _h = http("/proxy-wb/api/panel", {"probe": "no"})
        check("U15c 关闭时 POST 同样 403", st == 403, "status=%s" % st)
        check("U15d 关闭时仍零上游请求", mock.hits == 0, "hits=%d" % mock.hits)
        isolate.write_settings(isolate.base_settings(
            upstream_enabled=True, upstream_dir=up_dir,
            upstream_task="WBRelay-Fake-Upstream",
            upstream_proxy_enabled=True))

        # ---- U16 上游不在线 → 502 ----
        mock.reset()
        real_online_now = api_upstream.upstream_online
        api_upstream.upstream_online = lambda: False
        # 关掉 mock：让连接真的失败（与"探测说离线"一致）
        mock.stop()
        try:
            st, resp, _h = http("/proxy-wb/")
            check("U16a 上游不在线 → 502", st == 502, "status=%s %s" % (st, resp[:100]))
            check("U16b 502 文案已脱敏（不含堆栈 / 内部路径）",
                  "Traceback" not in resp and "D:\\" not in resp, resp[:120])
        finally:
            api_upstream.upstream_online = real_online_now
            mock.start()

        # ---- U17 上游响应体超限 → 502 ----
        mock.reset()
        mock.big_bytes = api_upstream.PROXY_MAX_RESPONSE_BYTES + 1024
        try:
            st, resp, _h = http("/proxy-wb/big")
            check("U17a 响应体超限 → 502（不整体读进内存）", st == 502,
                  "status=%s %s" % (st, resp[:100]))
        finally:
            mock.big_bytes = 1024 * 1024

        # ---- U18 上游超时 → 502 ----
        mock.reset()
        mock.mode = "hang"
        mock.timeout_hold = 2.0
        old_timeout = api_upstream.PROXY_TIMEOUT_SEC
        api_upstream.PROXY_TIMEOUT_SEC = 1
        try:
            st, resp, _h = http("/proxy-wb/")
            check("U18a 上游超时 → 502", st == 502, "status=%s %s" % (st, resp[:100]))
        finally:
            api_upstream.PROXY_TIMEOUT_SEC = old_timeout
            mock.mode = "ok"
            mock.timeout_hold = 3.0

        check("U19 socket guard 未记录任何对真实 8788 等端口的连接尝试",
              guard_violations == [], repr(guard_violations[:3]))

        # ---- U5 外部自启 ----
        raised = False
        try:
            upstream.set_autostart_reg(True)
        except RuntimeError as exc:
            raised = "R5" in str(exc)
        check("U5 设置外部上游开机自启 → 明确报错（不静默）", raised)

        # ---- U7 上游目录只读 ----
        check("U7 上游 fixture 目录内容逐字节未变",
              dir_manifest(up_dir) == manifest_before, repr(dir_manifest(up_dir)))
        check("U7b 测试数据都落在临时数据目录（日志也在内，不外溢）",
              all(f in ("settings.json", "wb.log") for f in isolate.list_files())
              and isolate.list_files() != [], repr(isolate.list_files()))
    finally:
        subprocess.Popen, subprocess.run = real_popen, real_run
        socket.create_connection = real_create
        api_upstream.upstream_online, api_upstream.upstream_task_state = real_online, real_task_state
        if server is not None:
            server.shutdown()
            server.server_close()
        mock.stop()

    print("\n===== R6 上游只读：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())