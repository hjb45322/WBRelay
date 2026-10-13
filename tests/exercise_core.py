# -*- coding: utf-8 -*-
"""接口巡检：导入各模块并对主要入口做一次轻量调用，捕捉重构后的缺失导入与名字错误。

**只读模式**运行（`--readonly`）且数据目录是自建临时目录，因此：
  - 不写任何数据、不联网、不触碰外部进程
  - 不导入真实 %APPDATA%\\WBRelay 的数据
  - 不调用真实数据迁移（`app._migrate_legacy_data` 会搬运文件，只验证可调用性 + 只读门禁）
  - **catalog 模板 / 外部 catalog 加载器全部 mock 成测试 fixture**：绝不读取
    真实 `~/.codex/model-catalogs`（即使本测试已把家目录重定向到临时根，也不依赖这一点）

用法：python tests/exercise_core.py
退出码：0 = 全部通过
"""
import os
import sys

import isolate

# 只读 + 临时数据目录：任何写入原语都会早退，收尾断言数据目录根本没被创建
isolate.bootstrap(readonly=True)

from wbrelay import (app, autostart, backup, catalog, ides, integrations,  # noqa: E402
                     modelcache, models, net, paths, profiles, protocol,
                     routing, settings, state, usage)

# ---- catalog 模板 / 外部 catalog：全部换成测试 fixture ----
_TEMPLATE = {"base_instructions": "fixture", "shell_type": "shell_command",
             "context_window": 128000}
_EXISTING_TEMPLATE = {"shell_type": "shell_command", "priority": 5}
_CALLS = {"relay_template": 0, "existing_template": 0, "upstream_map": 0}


def _fixture_relay_template():
    _CALLS["relay_template"] += 1
    return dict(_TEMPLATE)


def _fixture_existing_template():
    _CALLS["existing_template"] += 1
    return dict(_EXISTING_TEMPLATE)


def _fixture_upstream_map():
    _CALLS["upstream_map"] += 1
    return {}


catalog._load_relay_template = _fixture_relay_template
catalog._load_template_from_existing_catalog = _fixture_existing_template
catalog._load_upstream_catalog_map = _fixture_upstream_map

CHECKS = [
    ("paths.DATA 落在测试临时目录（隔离）", lambda: isolate.isolation_problems() == []),
    ("USERPROFILE / HOME 已重定向到临时根",
     lambda: all(os.path.abspath(os.environ.get(k) or "") == os.path.abspath(isolate.tmp_root())
                 for k in ("USERPROFILE", "HOME"))),
    ("运行模式环境变量已清理（模式只由参数决定）",
     lambda: all(os.environ.get(k) is None for k in
                 ("WBRELAY_READONLY", "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS"))),
    ("paths.READONLY 生效（只读模式）", lambda: paths.READONLY is True),
    ("paths.DEFAULT_PORT", lambda: isinstance(paths.DEFAULT_PORT, int)),
    ("paths.DASHBOARD_PATH 指向存在的看板资源", lambda: os.path.isfile(paths.DASHBOARD_PATH)),
    ("state._SERVER_PORT", lambda: isinstance(state._SERVER_PORT, int)),
    ("settings.load_settings", lambda: isinstance(settings.load_settings(), dict)),
    ("settings.cooldown_secs", lambda: settings.cooldown_secs() > 0),
    ("settings.clean_suppliers", lambda: settings.clean_suppliers([]) == []),
    ("settings._mask_key", lambda: settings._mask_key("sk-abcdefghijkl").endswith("ijkl")),
    ("settings.save_settings 只读下不写盘（返回 False）",
     lambda: settings.save_settings({"port": 1}) is False),
    ("autostart.gateway_autostart_cmd", lambda: isinstance(autostart.gateway_autostart_cmd(), str)),
    ("autostart.gateway_autostart_registered 只读查询不抛异常",
     lambda: isinstance(autostart.gateway_autostart_registered(), bool)),
    ("integrations.upstream.upstream_dir 未配置返回空串",
     lambda: integrations.upstream.upstream_dir() == ""),
    ("integrations.upstream.upstream_task_name", lambda: isinstance(integrations.upstream.upstream_task_name(), str)),
    ("integrations.upstream 不再自动拉起外部进程（无 watchdog 线程函数）",
     lambda: not hasattr(integrations.upstream, "_watchdog_loop")),
    ("R6：外部上游启停能力已整体移除（无 start/stop/找 python）",
     lambda: not any(hasattr(integrations.upstream, n) for n in
                     ("upstream_start", "upstream_stop", "_find_python_for"))),
    ("R6：上游探测只剩只读接口",
     lambda: all(callable(getattr(integrations.upstream, n)) for n in
                 ("upstream_dir", "upstream_port", "upstream_online", "upstream_task_state"))),
    ("routing.route_table", lambda: isinstance(routing.route_table(), dict)),
    ("routing._route_models_set", lambda: isinstance(routing._route_models_set(), set)),
    ("routing.supplier_matches", lambda: routing.supplier_matches({"models": ["gpt-*"]}, "gpt-6-luna") is True),
    ("protocol.responses_to_chat", lambda: "messages" in protocol.responses_to_chat({"input": "hi"})),
    ("usage.extract_usage_tokens", lambda: usage.extract_usage_tokens({"input_tokens": 7})["input_tokens"] == 7),
    ("usage.stats_view", lambda: "requests" in usage.stats_view()),
    ("usage.usage_summary", lambda: isinstance(usage.usage_summary(), dict)),
    ("usage.usage_recent", lambda: isinstance(usage.usage_recent(), dict)),
    ("usage.enforce_usage_retention 只读下不清理（返回 0）",
     lambda: usage.enforce_usage_retention() == 0),
    ("profiles.load_profiles", lambda: isinstance(profiles.load_profiles(), dict)),
    ("profiles.parse_catalog_profiles", lambda: isinstance(profiles.parse_catalog_profiles({}), dict)),
    ("profiles._persist_profiles_meta 只读下不写盘",
     lambda: profiles._persist_profiles_meta() is False),
    ("modelcache.load_models_cache", lambda: isinstance(modelcache.load_models_cache(), dict)),
    ("modelcache.save_models_cache 只读下不写盘",
     lambda: modelcache.save_models_cache({"suppliers": {}}) is False),
    # 契约是「返回 dict 或 None」，不是「这台机器一定配了代理」：
    # GitHub Actions 的运行器没有系统代理，返回 None 是**正确行为**，不能当失败。
    ("net._system_proxy", lambda: net._system_proxy() is None
                        or isinstance(net._system_proxy(), dict)),
    ("net.read_limited 超限报错（不整体读进内存）",
     lambda: _read_limited_over_limit()),
    ("catalog.codex_provider_name", lambda: isinstance(catalog.codex_provider_name(), str)),
    ("catalog.codex_aumid", lambda: "." in catalog.codex_aumid()),
    ("catalog 构造走测试 fixture（不读 ~/.codex/model-catalogs）",
     lambda: "models" in catalog.build_codex_catalog({}, {}) and _CALLS["relay_template"] > 0),
    ("catalog.sync_codex_catalog 只读下不写 catalog",
     lambda: catalog.sync_codex_catalog() == (0, 0)),
    ("models.models_view", lambda: isinstance(models.models_view(), dict)),
    ("models.normalize_model_info", lambda: models.normalize_model_info("m", {})["id"] == "m"),
    ("models._natural_key", lambda: isinstance(models._natural_key("gpt-6.1"), list)),
    ("ides.ides_settings", lambda: set(ides.ides_settings()) >= {"codex", "opencode", "dsh"}),
    ("ides.ide_write_allowed 只读下恒为 False", lambda: ides.ide_write_allowed("codex") is False),
    ("ides.IDE_ADAPTERS 注册表", lambda: len(ides.IDE_ADAPTERS) == 4),
    ("backup.list_backups", lambda: isinstance(backup.list_backups(), list)),
    ("backup.make_backup 只读下不写盘（返回 None）", lambda: backup.make_backup() is None),
    ("backup.IDE_BACKUP_DIR 落在临时数据目录内",
     lambda: os.path.dirname(os.path.abspath(ides.base.IDE_BACKUP_DIR)) == os.path.abspath(paths.DATA)),
    ("app.窗口图标写入器只读下拒绝写盘（R3 图标）",
     lambda: app._write_window_icon(b"fixture-icon", data_dir=paths.DATA) == ""),
    ("app._migrate_legacy_data 可调用", lambda: callable(app._migrate_legacy_data)),
    ("app 只读门禁生效（跳过迁移与后台线程）", lambda: app.READONLY is True),
    ("app.main 可调用", lambda: callable(app.main)),
    ("web.Handler 存在", lambda: hasattr(__import__("wbrelay.web", fromlist=["Handler"]), "Handler")),
    ("子包 wbrelay.web / .ides / .integrations 可导入",
     lambda: all(__import__("wbrelay.%s" % m, fromlist=["x"]) for m in ("web", "ides", "integrations"))),
]


class _FakeResp(object):
    """内存假响应：超限时只能被读到 limit+1 字节。"""

    def __init__(self, data):
        self.data = data
        self.read_bytes = 0
        self.closed = False

    def read(self, n=None):
        chunk = self.data[self.read_bytes:self.read_bytes + (n or len(self.data))]
        self.read_bytes += len(chunk)
        return chunk

    def close(self):
        self.closed = True


def _read_limited_over_limit():
    resp = _FakeResp(b"x" * 64)
    raised = False
    try:
        net.read_limited(resp, 16)
    except net.ResponseTooLarge:
        raised = True
    return raised and resp.read_bytes == 17


def main():
    bad = []
    for name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception as exc:
            bad.append((name, "%s: %s" % (type(exc).__name__, exc)))
            continue
        if not ok:
            bad.append((name, "返回 False"))

    # 只读零写入：临时数据目录不应被创建
    files = isolate.list_files()
    zero_write = (not os.path.exists(paths.DATA)) and not files
    if not zero_write:
        bad.append(("只读零写入（临时数据目录未被创建）",
                    "DATA=%s files=%r" % (paths.DATA, files)))
    else:
        CHECKS.append(("只读零写入（临时数据目录未被创建）", lambda: True))

    # catalog 外部加载器必须走 fixture：读到真实目录就会在这里暴露
    if not _CALLS["relay_template"]:
        bad.append(("catalog 模板加载器已被 fixture 接管", "模板加载器未被调用"))

    print("接口巡检（只读、临时数据目录）：%d 项" % len(CHECKS))
    print("  数据目录 : %s" % paths.DATA)
    for name, err in bad:
        print("  FAIL  %-46s %s" % (name, err))
    print("结果：%s（%d/%d）" % ("PASS" if not bad else "FAIL",
                                len(CHECKS) - len(bad), len(CHECKS)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
