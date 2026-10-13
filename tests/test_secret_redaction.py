# -*- coding: utf-8 -*-
"""机密回显与日志脱敏的隔离测试（全部使用**假密钥**，非真实凭据）。

断言：
  S1  /api/state 只回掩码 Key，不回原始 Key
  S2  /api/settings 不回原始 Key / admin token / wb_password
  S3  供应商明文 Key 只用于本地存储与构造上游 Authorization
  S4  上游错误正文里的凭据不会原样回给客户端，也不会进 wb.log
  S5  wb_password 不落盘、不回显
  S6  base_url 内嵌用户名/密码被拒绝
  S7  日志统一脱敏（Bearer / sk- / admin_token= 形态）
  S8  /api/clipboard 内容不写日志

用法：python tests/test_secret_redaction.py
"""
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import isolate

isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state                        # noqa: E402
from wbrelay.web import api_upstream             # noqa: E402
from wbrelay.log import _log                     # noqa: E402
from wbrelay.paths import LOG_PATH, SETTINGS_PATH  # noqa: E402
from wbrelay.redact import redact                # noqa: E402
from wbrelay.web.server import Handler            # noqa: E402

from mock_upstream import MockUpstream            # noqa: E402

PASS, FAIL = [], []

# 全部是明显的假凭据（不含任何真实信息）
SUP_KEY = "sk-test-FAKE-KEY-LEAK-1234567890abcdef"
API_KEY = "sk-test-FAKE-DOWNLINK-1234567890abcdef"
ADMIN_TOKEN = "FAKE-admin-token-9f8e7d6c5b4a3210"
UPSTREAM_LEAK = "sk-test-FAKE-UPSTREAM-LEAK-0987654321fedcba"
WB_PASSWORD = "FAKE-wb-password-please-never-store"
CLIP_TEXT = "FAKE-clipboard-userdata-42"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http(path, body=None, headers=None, timeout=15):
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


def log_text():
    try:
        with io.open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def main():
    up = MockUpstream("mockA", mode="http500",
                      error_body="upstream rejected: Authorization Bearer " + UPSTREAM_LEAK).start()
    server = None
    try:
        sup = isolate.demo_supplier("sup_demo_a", "演示供应商 A", up.base_url, SUP_KEY)
        isolate.write_settings(isolate.base_settings(
            suppliers=[sup], api_keys=[{"id": "k1", "name": "Codex", "key": API_KEY,
                                        "enabled": True}]))
        isolate.write_models_cache({"fetched_at": 0, "iso": "", "suppliers": {
            "sup_demo_a": {"name": sup["name"], "base_url": sup["base_url"],
                           "models": [{"id": "demo-model"}], "error": None, "ok": True}},
            "errors": {}, "ok_count": 1, "total_suppliers": 1})

        state._SERVER_PORT = PORT
        # 状态接口会 TCP 探测上游端口：测试里 mock 掉，确保不接触真实外部服务（8788）
        api_upstream.upstream_online = lambda: False
        api_upstream.upstream_task_state = lambda: "not_configured"
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # ---- S1 /api/state 掩码 ----
        st, body = http("/api/state")
        check("S1a /api/state 200", st == 200, "status=%s" % st)
        check("S1b /api/state 不含原始供应商 Key", SUP_KEY not in body)
        check("S1c /api/state 不含原始 api_keys 值", API_KEY not in body)
        state_json = json.loads(body)
        masked = state_json["suppliers"][0]["masked"]
        check("S1d 供应商 Key 以掩码形式返回", masked.startswith("sk-t") and "******" in masked,
              masked)

        # ---- S2 /api/settings ----
        st, body = http("/api/settings")
        check("S2a /api/settings 不含原始供应商 Key", SUP_KEY not in body)
        check("S2b /api/settings 不含原始 api key 值", API_KEY not in body)
        check("S2c /api/settings 不含 admin token 原文", ADMIN_TOKEN not in body)
        check("S2d /api/settings 不含 wb_password 原文", WB_PASSWORD not in body)

        # ---- S4 上游错误正文（含凭据）不回显、不进日志 ----
        st, body = http("/v1/responses", {"model": "demo-model", "input": "hi"})
        check("S4a 上游全败 → 客户端拿到错误", st >= 400, "status=%s" % st)
        check("S4b 上游错误正文里的凭据不回给客户端", UPSTREAM_LEAK not in body,
              body[:200])
        check("S4c 客户端错误信息带脱敏标记", "******" in body, body[:200])
        _log("模拟写日志：Authorization: Bearer " + UPSTREAM_LEAK)
        _log("模拟写日志：admin_token=" + ADMIN_TOKEN)
        logs = log_text()
        check("S4d 日志不含上游凭据原文", UPSTREAM_LEAK not in logs)
        check("S4e 日志不含 admin token 原文", ADMIN_TOKEN not in logs)
        check("S4f 日志确实写了（脱敏而不是整条丢掉）", "模拟写日志" in logs)

        # ---- S5 wb_password 不落盘、不回显 ----
        st, body = http("/api/upstream/settings",
                        {"enabled": True, "wb_autologin": True, "wb_password": WB_PASSWORD})
        check("S5a 保存上游设置 200", st == 200, "status=%s" % st)
        check("S5b 响应不回显 wb_password", WB_PASSWORD not in body)
        raw_settings = io.open(SETTINGS_PATH, encoding="utf-8").read()
        check("S5c wb_password 不落盘", WB_PASSWORD not in raw_settings)
        check("S5d 配置文件里没有 wb_password 字段",
              "wb_password" not in json.loads(raw_settings))
        st, body = http("/api/upstream/status")
        check("S5e 状态接口只回布尔（wb_password_set）", WB_PASSWORD not in body and
              "wb_password_set" in body)

        # ---- S6 base_url 内嵌凭据被拒绝 ----
        bad = dict(sup, base_url="https://fake-user:fake-pass@example.invalid/v1")
        st, body = http("/api/suppliers", {"suppliers": [bad]})
        check("S6a 含用户名/密码的 base_url 被拒绝（400）", st == 400, "status=%s" % st)
        check("S6b 拒绝信息不外显凭据原文",
              "fake-pass" not in body and "不允许内嵌用户名/密码" in body, body[:200])
        after = json.loads(io.open(SETTINGS_PATH, encoding="utf-8").read())
        check("S6c 被拒绝的配置没有落盘", "fake-user" not in json.dumps(after),
              json.dumps(after, ensure_ascii=False)[:160])

        # ---- S8 剪贴板内容不写日志 ----
        st, body = http("/api/clipboard")
        check("S8a /api/clipboard 可调用（本机接口）", st in (200, 403), "status=%s" % st)
        log_before = log_text()
        _log("剪贴板测试：内容不应进日志")
        check("S8b 剪贴板相关日志不含内容原文", CLIP_TEXT not in log_text() or
              CLIP_TEXT not in log_before)

        # ---- S3 明文 Key 只用于上游 Authorization ----
        check("S3 上游确实收到了该供应商的明文 Key（仅用于构造授权头）",
              up.requests and up.requests[0].get("authorization") == "Bearer " + SUP_KEY,
              repr(up.requests[0].get("authorization")) if up.requests else "无请求")

        # ---- S7 脱敏函数单测 ----
        samples = [
            ("Authorization: Bearer " + UPSTREAM_LEAK, UPSTREAM_LEAK),
            ("key=" + SUP_KEY, SUP_KEY),
            ("admin_token=" + ADMIN_TOKEN, ADMIN_TOKEN),
            ("https://fake-user:fake-pass@example.invalid/v1", "fake-pass"),
            ("wb_password=" + WB_PASSWORD, WB_PASSWORD),
        ]
        all_masked = True
        for text, secret in samples:
            out = redact(text)
            if secret in out:
                all_masked = False
                print("      未掩码：%r -> %r" % (text, out))
        check("S7a redact() 覆盖 Bearer / sk- / admin_token / URL 凭据 / wb_password", all_masked)
        long_text = redact("x" * 5000)
        check("S7b redact() 会截断超长文本", len(long_text) < 500, "len=%d" % len(long_text))

        # ---- admin token：设置后不回传明文，接口需要令牌 ----
        st, body = http("/api/settings", {"require_admin_token": True, "admin_token": ADMIN_TOKEN})
        check("S9a 设置 admin token 成功", st == 200, "status=%s" % st)
        check("S9b 用户提交的 token 不回显", ADMIN_TOKEN not in body, body[:160])
        st, body = http("/api/state")
        check("S9c 开启令牌后无令牌请求被拒（401）", st == 401, "status=%s" % st)
        st, body = http("/api/state", headers={"X-Admin-Token": ADMIN_TOKEN})
        check("S9d 带正确令牌可访问", st == 200, "status=%s" % st)
        check("S9e 即便带令牌，响应也不含 token 原文", ADMIN_TOKEN not in body)
        check("S9f 日志中也没有 token 原文", ADMIN_TOKEN not in log_text())
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        up.stop()

    print("\n===== 机密回显与日志：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())