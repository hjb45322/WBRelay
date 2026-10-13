# -*- coding: utf-8 -*-
"""高优先级安全加固的隔离测试。

覆盖：
  H1  面板代理（2026-10-14 恢复）：超大请求体 → 413；浏览器敏感头不转发
  H2  面板代理：路径穿越 / 外部目标 → 400；超大响应 → 502（不整体读进内存）
  H3  面板代理：GET 同源透传到本机 mock 上游（只打 127.0.0.1）
  H4  Host 门禁：非回环 Host → 403（DNS rebinding）
  H5  Origin 门禁：跨站 Origin → 403（CSRF）；同源 Origin → 放行
  H6  /api/clipboard 同样受门禁保护（本机敏感数据）
  H7  端口边界校验（0 / 70000 / 非数字 → 400）
  H8  供应商数量 / 字符串长度 / 模型条数 / id 字符集边界 → 400
  H9  用量分页与时间范围边界 → 400
  H10 用户可配置值不拼进 PowerShell（计划任务名 / AUMID 字符集校验）
  H11 外部进程自动拉起能力已移除（R5）
  H12 /v1/* 回环 Host / 同源 Origin 门禁（默认无鉴权不变；恶意 Host/Origin → 403）

用法：python tests/test_hardening.py
"""
import ast
import http.client as http_client
import io
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import isolate

isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state                        # noqa: E402
from wbrelay.integrations.upstream import upstream_task_name  # noqa: E402
from wbrelay.web import api_upstream, server as web_server        # noqa: E402
from wbrelay.web.server import Handler           # noqa: E402

from mock_upstream import MockUpstream            # noqa: E402

PASS, FAIL = [], []
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 无害的注入探针：只是让 PowerShell「多输出一行」，用于证明拼接路径已被切断
INJECT_TASK = "demo'; Write-Output injected-probe; '"


def strip_module_docstring(src):
    """去掉模块 docstring：只审查可执行代码，不把「移除记录」当成还在的能力。"""
    tree = ast.parse(src)
    first = tree.body[0] if tree.body else None
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        lines = src.splitlines(True)
        return "".join(lines[:first.lineno - 1] + lines[first.end_lineno:])
    return src


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http(path, body=None, headers=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def raw_request(method, path, headers):
    """原始请求：只发请求头（用于伪造 Host / 超大 Content-Length）。"""
    conn = http_client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, body


def main():
    upstream = MockUpstream("mockwb", html='<html><head>'
                                           '<script src="/static/app.js"></script></head>'
                                           '<body><script>fetch("/api/x")</script></body></html>')
    upstream.big_bytes = 1024 * 1024      # 只在「代理被误用」时才会被读到；停用后应保持零请求
    upstream.start()
    server = None
    real_create = socket.create_connection
    real_auth_key_ok = web_server.auth_key_ok
    guard_violations = []
    allowed_ports = {PORT, upstream.port}      # 只放行本测试自己的端口
    try:
        def guarded_create(address, *a, **k):
            port = address[1] if isinstance(address, (tuple, list)) and len(address) > 1 else None
            if port not in allowed_ports:
                guard_violations.append(address)
                raise AssertionError("测试禁止连接 %r（只允许本测试自建端口 %s）"
                                     % (address, sorted(allowed_ports)))
            return real_create(address, *a, **k)

        socket.create_connection = guarded_create
        # 让 /proxy-wb 指向 mock：上游目录里放一个声明 PORT= 的启动脚本
        up_dir = os.path.join(isolate.tmp_root(), "fake-upstream")
        os.makedirs(up_dir, exist_ok=True)
        with io.open(os.path.join(up_dir, "run.bat"), "w", encoding="utf-8") as fh:
            fh.write("@echo off\nset PORT=%d\npython wb_proxy.py\n" % upstream.port)
        sup = isolate.demo_supplier("sup_demo_a", "演示供应商 A", upstream.base_url)
        isolate.write_settings(isolate.base_settings(
            suppliers=[sup], upstream_enabled=True, upstream_dir=up_dir))
        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # 状态接口会 TCP 探测上游端口 / 查询计划任务：测试里一律 mock，
        # 确保**不接触**本机可能存在的真实外部服务（端口 8788）
        api_upstream.upstream_online = lambda: False
        api_upstream.upstream_task_state = lambda: "not_configured"

        # ---- H1/H2/H3 面板代理：所有者 2026-10-14 恢复「面板查看」（方案见
        #      docs/方案-上游服务面板恢复.md）。同源代理到本机回环上游，只转发两个白名单头；
        #      路径穿越 / 外部目标 / 超大请求体一律在连接前拒绝。启停/自启/守护仍未恢复（见 H11）。
        upstream.reset()
        st, body = http("/proxy-wb/")
        check("H3a GET /proxy-wb/ → 200（同源代理到 mock 上游）",
              st == 200, "status=%s %s" % (st, body[:80]))
        check("H3b 上游只收到 1 个请求且路径为 /",
              upstream.hits == 1 and upstream.requests[0]["path"] == "/",
              repr([r.get("path") for r in upstream.requests]))
        got = upstream.requests[0]["headers"] if upstream.requests else {}
        check("H1b 浏览器敏感头（Cookie/Authorization/Referer/Origin）不转发",
              not any(k in got for k in ("Cookie", "Authorization", "Referer", "Origin",
                                         "X-Admin-Token")),
              repr(sorted(got)))
        st, body = raw_request("POST", "/proxy-wb/", {
            "Host": "127.0.0.1:%d" % PORT,
            "Content-Length": str(4 * 1024 * 1024),
            "Content-Type": "application/x-www-form-urlencoded",
        })
        check("H1a 超大 Content-Length → 413（先拒后读，不读 body）",
              st == 413, "status=%s %s" % (st, body[:80]))
        for label, path in (("路径穿越", "/proxy-wb/../../etc/passwd"),
                            ("外部绝对 URL", "/proxy-wb/http://evil.example.com/"),
                            ("反斜杠", "/proxy-wb/\\evil")):
            st, body = http(path)
            check("H2b %s → 400（连接前拒绝）" % label, st == 400,
                  "status=%s %s" % (st, body[:80]))
        upstream.reset()
        upstream.big_bytes = 16 * 1024 * 1024
        st, body = http("/proxy-wb/big")
        check("H2a /proxy-wb/big 超大响应 → 502（不整体读进内存）",
              st == 502, "status=%s %s" % (st, body[:80]))
        upstream.big_bytes = 1024 * 1024
        check("H1c mock 上游收到的请求都来自合法路径（穿越/超限请求没打到它）",
              all(r["path"] in ("/", "/big") for r in upstream.requests),
              repr([r.get("path") for r in upstream.requests]))

        # ---- H4 Host 门禁 ----
        st, body = raw_request("GET", "/api/state", {"Host": "evil.example.com"})
        check("H4a 非回环 Host 访问 /api/* → 403", st == 403, "status=%s" % st)
        st, body = raw_request("GET", "/api/state", {"Host": "127.0.0.1:%d" % PORT})
        check("H4b 回环 Host → 放行 200", st == 200, "status=%s" % st)
        st, body = raw_request("GET", "/api/state", {"Host": "localhost:%d" % PORT})
        check("H4c localhost Host → 放行 200", st == 200, "status=%s" % st)

        # ---- H5 Origin 门禁 ----
        st, body = http("/api/state", headers={"Origin": "http://evil.example"})
        check("H5a 跨站 Origin → 403（CSRF 防护）", st == 403, "status=%s" % st)
        st, body = http("/api/state", headers={"Origin": "null"})
        check("H5b 不透明来源（Origin: null）→ 403", st == 403, "status=%s" % st)
        st, body = http("/api/state", headers={"Origin": "http://127.0.0.1:%d" % PORT})
        check("H5c 同源 Origin → 放行 200", st == 200, "status=%s" % st)
        st, body = http("/api/state", headers={"Origin": "http://localhost:%d" % PORT})
        check("H5d localhost 同源 → 放行 200", st == 200, "status=%s" % st)

        # ---- H6 剪贴板接口同样受门禁 ----
        st, _ = http("/api/clipboard", headers={"Origin": "http://evil.example"})
        check("H6a 跨站 Origin 读剪贴板 → 403", st == 403, "status=%s" % st)
        st, _ = raw_request("GET", "/api/clipboard", {"Host": "evil.example"})
        check("H6b 非回环 Host 读剪贴板 → 403", st == 403, "status=%s" % st)

        # ---- H7 端口边界 ----
        for bad_port in (0, 70000, -1, "abc", None):
            st, body = http("/api/settings", {"port": bad_port})
            check("H7 端口 %r → 400" % (bad_port,), st == 400, "status=%s %s" % (st, body[:60]))
        st, body = http("/api/settings", {"port": 18790})
        check("H7b 合法端口 → 200", st == 200, "status=%s" % st)

        # ---- H8 供应商边界 ----
        many = [isolate.demo_supplier("sup_%d" % i, "S%d" % i,
                                      "http://127.0.0.1:1/v1") for i in range(60)]
        st, body = http("/api/suppliers", {"suppliers": many})
        check("H8a 供应商数量超限 → 400", st == 400, "status=%s %s" % (st, body[:80]))
        st, body = http("/api/suppliers", {"suppliers": [
            isolate.demo_supplier("sup_long", "x" * 200, "http://127.0.0.1:1/v1")]})
        check("H8b 供应商名称超长 → 400", st == 400, "status=%s %s" % (st, body[:80]))
        st, body = http("/api/suppliers", {"suppliers": [
            isolate.demo_supplier("sup_models", "S", "http://127.0.0.1:1/v1",
                                  models=["m%d" % i for i in range(600)])]})
        check("H8c 模型清单条数超限 → 400", st == 400, "status=%s %s" % (st, body[:80]))
        st, body = http("/api/suppliers", {"suppliers": [
            isolate.demo_supplier("bad id!", "S", "http://127.0.0.1:1/v1")]})
        check("H8d 非法供应商 id → 400", st == 400, "status=%s %s" % (st, body[:80]))

        # ---- H9 用量分页 / 时间范围 ----
        for qs in ("range=all&page=abc", "range=all&page=0", "range=all&page=999999",
                   "range=custom&since=-1&until=10", "range=custom&since=20&until=10",
                   "range=custom&since=abc&until=10"):
            st, body = http("/api/usage?" + qs)
            check("H9 /api/usage?%s → 400" % qs, st == 400, "status=%s %s" % (st, body[:60]))
        st, body = http("/api/usage?range=custom&since=10&until=20")
        check("H9b 合法自定义范围 → 200", st == 200, "status=%s" % st)

        # ---- H10 可配置值不拼进 PowerShell ----
        isolate.write_settings(isolate.base_settings(upstream_task=INJECT_TASK, suppliers=[sup]))
        check("H10a 非法计划任务名被丢弃（不交给 PowerShell）",
              upstream_task_name() == "", repr(upstream_task_name()))
        ok_task = "WBRelay UpstreamTask-1"
        isolate.write_settings(isolate.base_settings(upstream_task=ok_task, suppliers=[sup]))
        check("H10b 合法计划任务名保留", upstream_task_name() == ok_task, upstream_task_name())
        from wbrelay.validate import check_aumid
        check("H10c AUMID 注入串被拒绝",
              check_aumid("$(probe)") == "" and check_aumid("OpenAI.Codex_2p2nqsd0c76g0!App") != "")
        task_src = io.open(os.path.join(REPO, "wbrelay", "integrations", "upstream.py"),
                           encoding="utf-8").read()
        check("H10d 计划任务名/上游目录通过环境变量传入（不拼接进脚本）",
              "$env:WBRELAY_UPSTREAM_TASK" in task_src and
              "WBRELAY_UPSTREAM_DIR" in task_src)
        # R6（第二轮）：外部上游启停已整体移除 —— 旧的 stop 脚本（Stop/Disable
        # + Stop-Process）与 start 的 Popen 都不应再存在（详细回归见 tests/test_upstream_readonly.py）
        task_body = strip_module_docstring(task_src)
        check("H10e 外部上游停止脚本已删除（可执行代码里无 Stop-ScheduledTask / Stop-Process）",
              "Stop-ScheduledTask" not in task_body and "Stop-Process" not in task_body)

        # ---- H11 R5：外部进程只能显式操作 ----
        # 注意：本函数里 `upstream` 是 MockUpstream 实例，模块改用别名绑定，避免名字撞车
        from wbrelay.integrations import upstream as upstream_service
        check("H11a 后台守护线程（自动拉起外部进程）已移除",
              not hasattr(upstream_service, "_watchdog_loop") and
              upstream_service.watchdog_thread() is None)
        check("H11b 外部进程开机自启已移除（调用即报错，不静默）",
              upstream_service.UPSTREAM_AUTOSTART_SUPPORTED is False and
              upstream_service.WATCHDOG_SUPPORTED is False)
        raised = False
        try:
            upstream_service.set_autostart_reg(True)
        except RuntimeError:
            raised = True
        check("H11c 设置外部进程开机自启 → 明确报错 rather than 静默成功", raised)
        st, body = http("/api/upstream/settings", {"watchdog": True})
        check("H11d 请求已移除的进程守护能力 → 409 + 明确说明",
              st == 409 and "R5" in body, "status=%s %s" % (st, body[:120]))
        st, body = http("/api/upstream/status")
        status = json.loads(body)
        check("H11e 状态接口明确标注能力不支持",
              status["watchdog_supported"] is False and
              status["autostart_supported"] is False and
              status["control_supported"] is False, body[:200])
        check("H11f 状态里带 R6 策略说明（外部上游启停已移除）",
              "R6" in status["control_policy"], status["control_policy"][:120])
        st, body = http("/api/upstream/start", {})
        check("H11g /api/upstream/start → 409（不启动任何进程）", st == 409, "status=%s" % st)
        st, body = http("/api/upstream/stop", {})
        check("H11h /api/upstream/stop → 409（不停止任何进程/任务）", st == 409, "status=%s" % st)

        # ---- H12 /v1/* 回环 Host / 同源 Origin 门禁（默认无鉴权不变）----
        # 用 auth_key_ok=False 让「门禁放行」表现为 401（而不是真的去打上游）；
        # 门禁拦截仍然是 403 —— 两种状态可区分，且不需要真实上游请求。
        web_server.auth_key_ok = lambda _k: False
        st, body = raw_request("GET", "/v1/models", {"Host": "evil.example.com"})
        check("H12a /v1/models 非回环 Host → 403（DNS rebinding）",
              st == 403, "status=%s" % st)
        st, body = raw_request("GET", "/v1/models", {"Host": "127.0.0.1:%d" % PORT})
        check("H12b /v1/models 回环 Host + 无 Origin（Codex/CLI 形态）→ 过门禁（401 需鉴权）",
              st == 401, "status=%s" % st)
        st, body = raw_request("GET", "/v1/models", {"Host": "localhost:%d" % PORT})
        check("H12c /v1/models localhost Host → 过门禁（401）", st == 401, "status=%s" % st)
        st, body = http("/v1/models", headers={"Origin": "http://evil.example"})
        check("H12d /v1/models 跨站 Origin → 403（CSRF）", st == 403, "status=%s" % st)
        st, body = http("/v1/models", headers={"Origin": "null"})
        check("H12e /v1/models 不透明来源 Origin: null → 403", st == 403, "status=%s" % st)
        st, body = http("/v1/models", headers={"Origin": "http://127.0.0.1:%d" % PORT})
        check("H12f /v1/models 同源 Origin → 过门禁（401）", st == 401, "status=%s" % st)
        st, body = http("/v1/models", headers={"Origin": "http://localhost:%d" % PORT})
        check("H12g /v1/models localhost 同源 Origin → 过门禁（401）", st == 401, "status=%s" % st)
        st, body = http("/v1/responses", {"model": "demo-model", "input": "hi"},
                        headers={"Origin": "http://evil.example"})
        check("H12h POST /v1/responses 跨站 Origin → 403（默认无鉴权也不放开来源）",
              st == 403, "status=%s" % st)

        check("H13 socket guard 未记录任何对真实端口的连接尝试",
              guard_violations == [], repr(guard_violations[:3]))
    finally:
        socket.create_connection = real_create
        web_server.auth_key_ok = real_auth_key_ok
        if server is not None:
            server.shutdown()
            server.server_close()
        upstream.stop()

    print("\n===== 安全加固：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())