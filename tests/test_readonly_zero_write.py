# -*- coding: utf-8 -*-
"""--readonly 真正零写入的隔离测试。

断言：
  R1  导入 wbrelay 后，数据目录**根本没被创建**（导入期不得有副作用）
  R2  只读模式下 GET 接口（含 /api/models?refresh=1 刷新接口）全部 200
  R3  只读模式下管理写入接口 403
  R4  逐个调用内部持久化函数：无一写盘；数据目录仍不存在
  R5  catalog 同步不写外部模型清单（用临时目录替代 ~/.codex，验证零写入）
  R6  IDE 备份/注入不写盘（IDE 备份目录也不创建）
  R7  HTTP 服务线程可以跑（只读下允许的线程），但没有后台写线程
  R8  /proxy-wb 写操作在只读下被拒
  R9  窗口图标（非 headless GUI 路径）走统一原子写：只读下不创建、不覆写 DATA/WBRelay.ico
  R10 零写入探针：把 open(w/a/x)/os.replace/os.makedirs/tempfile.mkstemp/winreg 写原语
      全部换成记录器，跑一遍持久化入口 + 管理写接口，任何一次触发都算失败
  R11 写入点静态清单：全包扫描写原语调用点，新增写入点必须先过清单审查

用法：python tests/test_readonly_zero_write.py
"""
import ast
import builtins
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import winreg
from http.server import ThreadingHTTPServer

import isolate

isolate.bootstrap(readonly=True)
PORT = isolate.port()

from wbrelay import (app, backup, catalog, ides, modelcache, profiles,  # noqa: E402
                     state, usage)
from wbrelay.ides import base as ides_base                                     # noqa: E402
from wbrelay.log import _log                                                    # noqa: E402
from wbrelay.paths import DATA, READONLY                                        # noqa: E402
from wbrelay.settings import load_settings, save_settings                        # noqa: E402
from wbrelay.web import api_state                                                # noqa: E402
from wbrelay.web.server import Handler                                           # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http(path, body=None, timeout=10):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")




REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 写原语调用点白名单（审查过的集合）：
#   · 每个模块都在自己的写入前判 READONLY（atomicio 除外：它是底层原子工具，
#     由调用方判只读）
WRITE_POINT_ALLOWLIST = {
    "wbrelay/atomicio.py": "原子写工具本身（不判只读，由调用方判）",
    "wbrelay/app.py": "旧数据迁移 + 窗口图标（READONLY 门禁）",
    "wbrelay/autostart.py": "网关自启注册表（READONLY 门禁 + 调用方仅在值变化时调）",
    "wbrelay/backup.py": "备份（READONLY 门禁）",
    "wbrelay/catalog.py": "Codex catalog 同步（READONLY 门禁）",
    "wbrelay/ides/base.py": "IDE 备份原语（READONLY 门禁）",
    "wbrelay/ides/codex.py": "Codex 接管（ide_write_allowed 门禁）",
    "wbrelay/ides/crush.py": "Crush CLI 接管（READONLY 原语 + 全局 IDE 门禁）",
    "wbrelay/ides/dsh.py": "DSH 接管（ide_write_allowed 门禁）",
    "wbrelay/ides/opencode.py": "OpenCode 接管（ide_write_allowed 门禁）",
    "wbrelay/log.py": "日志（READONLY 门禁）",
    "wbrelay/modelcache.py": "模型缓存（READONLY 门禁）",
    "wbrelay/paths.py": "ensure_data_dir（READONLY 直接返回 None）",
    "wbrelay/profiles.py": "模型档案（READONLY 门禁）",
    "wbrelay/settings.py": "配置保存（READONLY 门禁）",
    "wbrelay/usage.py": "用量记录与清理（READONLY 门禁）",
    "wbrelay/windowstate.py": "窗口几何（READONLY 门禁）",
}
_WRITE_FUNCS = ("write_text", "write_bytes", "write_json")
_OS_WRITE_ATTRS = ("replace", "makedirs", "mkstemp", "rename", "remove", "rmtree",
                   "SetValueEx", "DeleteValue")


def _audit_write_points():
    """扫描 wbrelay 包内所有写原语调用点（返回：命中文件集合、缺 READONLY 的文件）。"""
    found, unguarded = set(), set()
    for base, dirs, names in os.walk(os.path.join(REPO, "wbrelay")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in sorted(names):
            if not n.endswith(".py"):
                continue
            full = os.path.join(base, n)
            rel = os.path.relpath(full, REPO).replace(os.sep, "/")
            src = io.open(full, encoding="utf-8").read()
            hits = False
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if isinstance(f, ast.Name) and f.id in _WRITE_FUNCS:
                    hits = True
                elif isinstance(f, ast.Name) and f.id == "open":
                    modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                    modes += [kw.value.value for kw in node.keywords
                              if kw.arg == "mode" and isinstance(kw.value, ast.Constant)]
                    if any(m and any(c in str(m) for c in "wax+") for m in modes):
                        hits = True
                elif isinstance(f, ast.Attribute) and f.attr in _OS_WRITE_ATTRS:
                    if isinstance(f.value, ast.Name) and f.value.id in ("os", "shutil", "tempfile", "winreg"):
                        hits = True
                elif isinstance(f, ast.Attribute) and f.attr == "open" and isinstance(f.value, ast.Name) \
                        and f.value.id == "winreg":
                    hits = True
            if not hits:
                continue
            found.add(rel)
            if rel != "wbrelay/atomicio.py" and "READONLY" not in src:
                unguarded.add(rel)
    return found, unguarded


class _WriteProbe(object):
    """把「会改磁盘/注册表」的原语换成记录器：任何一次调用都会让断言失败。

    读操作（打开读、os.walk、winreg 读）照常放行，所以不会把「读了一个文件」误判成写入。
    """

    def __init__(self):
        self.hits = []
        self._orig = []

    def _wrap(self, obj, name, label):
        orig = getattr(obj, name)
        self._orig.append((obj, name, orig))

        def probe(*a, **k):
            self.hits.append((label, repr(a[:1])[:80]))
            raise AssertionError("只读探针：不应调用写原语 %s" % label)

        setattr(obj, name, probe)

    def __enter__(self):
        orig_open = builtins.open
        self._orig.append((builtins, "open", orig_open))

        def probe_open(file, mode="r", *a, **k):
            if any(c in str(mode) for c in "wax+"):
                self.hits.append(("open", "%s mode=%s" % (file, mode)))
                raise AssertionError("只读探针：不应以写模式打开 %s" % file)
            return orig_open(file, mode, *a, **k)

        builtins.open = probe_open
        for name in ("SetValueEx", "DeleteValue", "CreateKey", "CreateKeyEx", "SetKeySecurity"):
            if hasattr(winreg, name):
                self._wrap(winreg, name, "winreg." + name)
        for name in ("makedirs", "replace", "remove", "rename"):
            self._wrap(os, name, "os." + name)
        self._wrap(tempfile, "mkstemp", "tempfile.mkstemp")
        return self

    def __exit__(self, *exc):
        for obj, name, orig in reversed(self._orig):
            setattr(obj, name, orig)
        return False


def main():
    # ---- R1 导入期零副作用 ----
    check("R1a 只读模式确实生效（READONLY=True）", READONLY is True)
    check("R1b 导入 wbrelay 后数据目录未被创建", not os.path.exists(DATA), DATA)
    check("R1c 数据目录没有任何文件", isolate.list_files() == [], repr(isolate.list_files()))

    # ---- R7 HTTP 服务线程（只读下唯一允许的线程）----
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    state._SERVER_PORT = PORT
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # ---- R2 GET 接口 ----
        checks = [("GET /", "/"), ("GET /api/state", "/api/state"),
                  ("GET /api/settings", "/api/settings"), ("GET /api/usage", "/api/usage"),
                  ("GET /api/ides", "/api/ides"), ("GET /api/backups", "/api/backups"),
                  ("GET /api/profiles", "/api/profiles")]
        for label, path in checks:
            st, _ = http(path)
            check("R2 %s → 200" % label, st == 200, "status=%s" % st)

        # 刷新接口：不联网（mock 掉 OpenRouter），只验证「刷新不写盘」
        api_state.fetch_openrouter_profiles = lambda *a, **k: {}
        api_state.ensure_auto_profiles = lambda ids: 0
        st, _ = http("/api/models?refresh=1")
        check("R2 GET /api/models?refresh=1 → 200", st == 200, "status=%s" % st)

        # ---- R3 管理写入被拒绝 ----
        for label, path, body in (("POST /api/settings", "/api/settings", {"cooldown_sec": 9}),
                                  ("POST /api/suppliers", "/api/suppliers", {"suppliers": []}),
                                  ("POST /api/keys", "/api/keys", {"api_keys": []}),
                                  ("POST /api/backup", "/api/backup", {}),
                                  ("POST /api/models/enabled", "/api/models/enabled", {})):
            st, _ = http(path, body)
            check("R3 %s → 403" % label, st == 403, "status=%s" % st)

        # ---- R4 内部持久化函数逐个调用 ----
        results = [
            ("settings.save_settings", save_settings({"port": 18790})),
            ("profiles.save_profiles", profiles.save_profiles({"m": {}})),
            ("profiles._persist_profiles_meta", profiles._persist_profiles_meta()),
            ("modelcache.save_models_cache", modelcache.save_models_cache({"suppliers": {}})),
            ("backup.make_backup", backup.make_backup()),
            ("windowstate.save", __import__("wbrelay.windowstate", fromlist=["save"]).save({"width": 1})),
            ("usage.enforce_usage_retention", usage.enforce_usage_retention()),
        ]
        for name, ret in results:
            check("R4 %s 未写盘（返回假值）" % name, ret is False or ret is None or ret == 0,
                  "返回=%r" % (ret,))
        usage.record_usage({"model": "ro-test", "realm": "supplier:x", "error": True})
        _log("只读测试：这条日志不应落盘 sk-test-FAKE-LEAK-0001")
        check("R4 记录用量后仍无文件", isolate.list_files() == [], repr(isolate.list_files()))

        # ---- R5 catalog 不写外部清单（临时路径替代 ~/.codex）----
        fake_catalog = os.path.join(isolate.tmp_root(), "codex-catalog-must-not-exist.json")
        real_catalog = catalog.CODEX_CATALOG
        try:
            catalog.CODEX_CATALOG = fake_catalog
            written = catalog.sync_codex_catalog()
        finally:
            catalog.CODEX_CATALOG = real_catalog
        check("R5a 只读下 catalog 同步直接跳过（0, 0）", written == (0, 0), repr(written))
        check("R5b 外部 catalog 文件未被创建", not os.path.exists(fake_catalog), fake_catalog)

        # ---- R6 IDE 备份/注入零写入 ----
        # 直接调底层原语（绕过 IDE 守卫）：原语本身也必须拒绝写盘
        wrote = ides_base._ide_write_backup("codex", {"raw": "x"})
        check("R6a0 IDE 备份原语在只读下拒绝写盘",
              wrote is False and not os.path.exists(ides_base.ide_backup_path("codex")),
              "返回=%r" % (wrote,))
        ides.sync_all_ide_catalogs()
        ides.ides_backup_and_inject(PORT)
        check("R6a 只读下 IDE 写入守卫拒绝（ide_write_allowed=False）",
              ides.ide_write_allowed("codex") is False)
        check("R6b 只读下 IDE 接管整体不执行（IDE 备份目录未创建）",
              not os.path.isdir(ides_base.IDE_BACKUP_DIR), ides_base.IDE_BACKUP_DIR)

        # ---- 收尾：整棵树仍然零文件 ----
        files = isolate.list_files()
        check("R7 全程零文件写入（临时数据目录仍为空）", files == [], repr(files))
        check("R7b 数据目录仍未被创建", not os.path.exists(DATA), DATA)
        check("R7c 加载配置只读到空配置（不读真实 APPDATA）", load_settings() == {},
              repr(load_settings()))

        # ---- R9 窗口图标（非 headless GUI 路径）：统一原子写 + 只读门禁 ----
        check("R9a 只读下窗口图标写入器拒绝落盘（返回空串）",
              app._write_window_icon(b"fixture-icon", data_dir=DATA) == "")
        check("R9b DATA/WBRelay.ico 未被创建",
              not os.path.exists(os.path.join(DATA, "WBRelay.ico")), DATA)
        icon_dir = os.path.join(isolate.tmp_root(), "icon-probe")
        wrote = app._write_window_icon(b"fixture-icon", data_dir=icon_dir, readonly=False)
        icon_path = os.path.join(icon_dir, "WBRelay.ico")
        check("R9c 可写路径下图标确实落盘（证明 R9a 不是「函数从不写」）",
              wrote == icon_path and os.path.isfile(icon_path), repr(wrote))
        with open(icon_path, "rb") as fh:
            check("R9d 图标内容一致", fh.read() == b"fixture-icon")
        check("R9e 图标写入不留临时文件", sorted(os.listdir(icon_dir)) == ["WBRelay.ico"],
              repr(os.listdir(icon_dir)))
        app_src = io.open(os.path.join(REPO, "wbrelay", "app.py"), encoding="utf-8").read()
        app_tree = ast.parse(app_src)
        bare_wb = []
        for node in ast.walk(app_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                if any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                       and any(c in a.value for c in "wax+") for a in node.args[1:2]):
                    bare_wb.append(node.lineno)
        check("R9f app.py 不再直接 open(..., \"wb\") 覆写图标", not bare_wb, repr(bare_wb))
        check("R9g 只读下不会受窗口图标路径影响（ico_path 为空 → 跳过图标线程）",
              "if ico_path:" in app_src)

        # ---- R10 零写入探针 ----
        with _WriteProbe() as probe:
            for label, fn in (
                    ("settings.save_settings", lambda: save_settings({"port": 1})),
                    ("profiles.save_profiles", lambda: profiles.save_profiles({"m": {}})),
                    ("profiles._persist_profiles_meta", lambda: profiles._persist_profiles_meta()),
                    ("modelcache.save_models_cache", lambda: modelcache.save_models_cache({"suppliers": {}})),
                    ("backup.make_backup", lambda: backup.make_backup()),
                    ("backup.restore_backup", lambda: backup.restore_backup("settings-1.json")),
                    ("windowstate.save", lambda: __import__("wbrelay.windowstate",
                                                            fromlist=["save"]).save({"width": 1})),
                    ("usage.record_usage", lambda: usage.record_usage({"model": "ro", "realm": "supplier:x"})),
                    ("usage.enforce_usage_retention", lambda: usage.enforce_usage_retention()),
                    ("log._log", lambda: _log("R10 只读探针：这行不应落盘 sk-test-FAKE-KEY-0002")),
                    ("catalog.sync_codex_catalog", lambda: catalog.sync_codex_catalog()),
                    ("ides.sync_all_ide_catalogs", lambda: ides.sync_all_ide_catalogs()),
                    ("ides.ides_backup_and_inject", lambda: ides.ides_backup_and_inject(PORT)),
                    ("ides.ides_restore", lambda: ides.ides_restore()),
                    ("ides.base._ide_write_backup", lambda: ides_base._ide_write_backup("codex", {"raw": "x"})),
                    ("app._write_window_icon", lambda: app._write_window_icon(b"x", data_dir=DATA)),
                    ("app._migrate_legacy_data", lambda: app._migrate_legacy_data()),
                    ("autostart.set_gateway_autostart", lambda: __import__("wbrelay.autostart",
                                                                          fromlist=["set_gateway_autostart"])
                     .set_gateway_autostart(True))):
                try:
                    fn()
                except Exception:
                    pass
            for adp in ides.IDE_ADAPTERS:
                adp["owner"]()
                adp["config_paths"]()
                ides_base.ide_backup_status(adp["id"])
                adp["backup"]()
                adp["inject"](PORT)
                adp["sync_catalog"]()
                adp["restore"]()
            ides.codex_backup_and_switch(PORT)
            ides.codex_restore()
            for label, path, body in (("POST /api/settings", "/api/settings", {"autostart": True}),
                                      ("POST /api/suppliers", "/api/suppliers", {"suppliers": []}),
                                      ("POST /api/backup", "/api/backup", {}),
                                      ("POST /api/usage/clear", "/api/usage/clear", {})):
                st, _resp = http(path, body)
                if st not in (403, 404):
                    check("R10 %s 只读下被拒" % label, False, "status=%s" % st)
        check("R10 全程零写入：DATA / IDE / 注册表 / 日志 / 备份 / catalog 都没被触发写原语",
              probe.hits == [], repr(probe.hits[:5]))

        # ---- R11 写入点静态清单 ----
        found, unguarded = _audit_write_points()
        unexpected = sorted(set(found) - set(WRITE_POINT_ALLOWLIST))
        check("R11a 写原语调用点全在已审查清单内", not unexpected,
              repr(unexpected))
        check("R11b 清单内的模块除原子工具外都引用 READONLY",
              not unguarded, repr(unguarded))

        # ---- R8 /proxy-wb 的写操作在只读下被拒（且不会去连任何外部服务）----
        isolate.write_settings(isolate.base_settings(upstream_enabled=True))
        st, _body = http("/proxy-wb/", {})
        check("R8 只读下 POST /proxy-wb → 403（代理写操作被禁）", st == 403, "status=%s" % st)
    finally:
        server.shutdown()
        server.server_close()

    print("\n===== 只读零写入：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
