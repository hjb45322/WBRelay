# -*- coding: utf-8 -*-
"""设置保存与网关自启的边界回归测试（第二轮发布修复）。

背景：`_save_settings()` 曾经每次保存都调 `set_gateway_autostart(bool(data.get("autostart")))`，
所以「只改 require_api_key」也会去删 HKCU Run 里的 `WBRelayGateway`。现在只有请求
**显式带 autostart 字段且值真的变了** 才同步注册表。

测试把 `set_gateway_autostart` 与 `gateway_autostart_registered` 都换成记录器：
**全程不读、不写真实注册表**（注册表读写在本测试中一次都不发生）。

用法：python tests/test_settings_autostart.py
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import state                                                    # noqa: E402
from wbrelay.paths import SETTINGS_PATH                                      # noqa: E402
from wbrelay.web import api_admin                                            # noqa: E402
from wbrelay.web.server import Handler                                       # noqa: E402

PASS, FAIL = [], []
AUTOSTART_CALLS = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http(path, body=None, timeout=20):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def on_disk():
    with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    isolate.write_settings(isolate.base_settings(autostart=False, require_api_key=False))
    server = None
    real_set = api_admin.set_gateway_autostart
    real_reg = api_admin.gateway_autostart_registered
    try:
        # 记录器：代替真实注册表读写
        def fake_set(on):
            AUTOSTART_CALLS.append(bool(on))
            return True

        api_admin.set_gateway_autostart = fake_set
        api_admin.gateway_autostart_registered = lambda: False

        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        problems = isolate.isolation_problems()
        check("A0a 数据目录已隔离（临时目录）", not problems, "；".join(problems))
        check("A0b 初始配置 autostart=False（假值）", on_disk().get("autostart") is False)

        # ---- A1：只改 require_api_key：不能动注册表 ----
        st, body = http("/api/settings", {"require_api_key": True})
        check("A1a POST /api/settings {require_api_key} → 200", st == 200, "status=%s %s" % (st, body[:80]))
        check("A1b 保存真的落盘了（require_api_key=True）", on_disk().get("require_api_key") is True,
              repr(on_disk().get("require_api_key")))
        check("A1c 改 require_api_key **完全不调** set_gateway_autostart", AUTOSTART_CALLS == [],
              repr(AUTOSTART_CALLS))
        check("A1d 磁盘上的 autostart 保持 False（没被隐式改写）",
              on_disk().get("autostart") is False, repr(on_disk().get("autostart")))

        # ---- A2：其它保存路径（供应商 / keys / 上游 / 模型启停 / 备份）也不能动注册表 ----
        isolate.write_models_cache({"fetched_at": 0, "iso": "", "suppliers": {}})
        for label, path, body in (
                ("POST /api/suppliers", "/api/suppliers", {"suppliers": []}),
                ("POST /api/keys", "/api/keys", {"api_keys": []}),
                ("POST /api/upstream/settings", "/api/upstream/settings", {"enabled": True}),
                ("POST /api/models/enabled", "/api/models/enabled", {"models_enabled": {"demo-model": True}}),
                ("POST /api/backup", "/api/backup", {}),
                ("POST /api/settings（无 autostart 字段）", "/api/settings", {"cooldown_sec": 77})):
            st, resp = http(path, body)
            check("A2 %s → 200" % label, st == 200, "status=%s %s" % (st, resp[:80]))
        check("A2b 所有其它保存路径加起来仍然零次自启调用",
              AUTOSTART_CALLS == [], repr(AUTOSTART_CALLS))

        # ---- A3：显式传 autostart 但值没变：也不调 ----
        st, body = http("/api/settings", {"autostart": False})
        check("A3a 显式传 autostart=False（值未变）→ 200", st == 200, "status=%s" % st)
        check("A3b 值未变时不调 set_gateway_autostart", AUTOSTART_CALLS == [], repr(AUTOSTART_CALLS))

        # ---- A4：值真的变了：才同步 ----
        st, body = http("/api/settings", {"autostart": True})
        check("A4a autostart false→true → 200", st == 200, "status=%s" % st)
        check("A4b 值变化时才调一次 set_gateway_autostart(True)", AUTOSTART_CALLS == [True],
              repr(AUTOSTART_CALLS))
        check("A4c 磁盘 autostart=True", on_disk().get("autostart") is True, repr(on_disk().get("autostart")))

        st, body = http("/api/settings", {"default_timeout_sec": 130})
        check("A4d 再次只改其它字段（此时 autostart=true）也不会重写注册表",
              st == 200 and AUTOSTART_CALLS == [True], repr(AUTOSTART_CALLS))

        st, body = http("/api/settings", {"autostart": False})
        check("A4e true→false 再调一次 set_gateway_autostart(False)", AUTOSTART_CALLS == [True, False],
              repr(AUTOSTART_CALLS))

        files = isolate.list_files()
        allowed = ("settings.json", "models_cache.json", "usage.jsonl", "backups")
        check("A5 测试数据全部落在临时目录（不碰真实注册表/APPDATA）",
              bool(files) and all(f.startswith(allowed) for f in files), repr(files))
    finally:
        api_admin.set_gateway_autostart = real_set
        api_admin.gateway_autostart_registered = real_reg
        if server is not None:
            server.shutdown()
            server.server_close()

    print("\n===== 设置保存与自启边界：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
