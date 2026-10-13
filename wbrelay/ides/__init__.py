# -*- coding: utf-8 -*-
"""各 IDE / 编辑器客户端的配置接管：Codex、OpenCode、DSH、Crush。

包收口层：组装 IDE_ADAPTERS 注册表，提供启停/同步/恢复的聚合入口，
并转口 base 的公共 API（兼容旧 ``wbrelay.ides.*`` 引用）。
"""
from ..catalog import restart_codex
from ..log import _log
from ..paths import NO_IDE_INJECT, READONLY

from .base import (ide_backup_exists, ide_enabled, ide_inject_disabled_reason,
                   ide_backup_status,
                   ide_write_allowed, ide_write_globally_blocked,
                   ides_settings, set_ide_enabled)
from .codex import (_codex_backup, _codex_detect, _codex_inject, _codex_restore,
                    _codex_sync_catalog, _codex_owner, _codex_config_paths)
from .crush import (_crush_backup, _crush_detect, _crush_inject, _crush_restore,
                    _crush_sync_catalog, _crush_owner, _crush_config_paths)
from .dsh import (_dsh_backup, _dsh_detect, _dsh_inject, _dsh_restore,
                  _dsh_sync_catalog, restart_dsh, _dsh_owner, _dsh_config_paths)
from .opencode import (_opencode_backup, _opencode_detect, _opencode_inject,
                       _opencode_restore, _opencode_sync_catalog,
                       restart_opencode, _opencode_owner, _opencode_config_paths)

# ===========================================================================
# IDE 适配器注册表
# ===========================================================================
IDE_ADAPTERS = [
    {
        "id": "codex", "name": "Codex",
        "detect": _codex_detect, "backup": _codex_backup,
        "inject": _codex_inject, "restore": _codex_restore,
        "restart": restart_codex, "sync_catalog": _codex_sync_catalog,
        "restartable": True,
        "owner": _codex_owner, "config_paths": _codex_config_paths,
    },
    {
        "id": "opencode", "name": "OpenCode",
        "detect": _opencode_detect, "backup": _opencode_backup,
        "inject": _opencode_inject, "restore": _opencode_restore,
        "restart": restart_opencode, "sync_catalog": _opencode_sync_catalog,
        "restartable": True,
        "owner": _opencode_owner, "config_paths": _opencode_config_paths,
    },
    {
        "id": "dsh", "name": "DSH",
        "detect": _dsh_detect, "backup": _dsh_backup,
        "inject": _dsh_inject, "restore": _dsh_restore,
        "restart": restart_dsh, "sync_catalog": _dsh_sync_catalog,
        "restartable": True,
        "owner": _dsh_owner, "config_paths": _dsh_config_paths,
    },
    {
        "id": "crush", "name": "Crush",
        "detect": _crush_detect, "backup": _crush_backup,
        "inject": _crush_inject, "restore": _crush_restore,
        "sync_catalog": _crush_sync_catalog, "restartable": False, "restart": None,
        "owner": _crush_owner, "config_paths": _crush_config_paths,
    },
]


def ide_adapter(ide_id):
    for adp in IDE_ADAPTERS:
        if adp["id"] == ide_id:
            return adp
    return None


def ides_backup_and_inject(port):
    """启动时：先同步模型清单（catalog），再按已启用且已检测到的 IDE 备份+注入。
    顺序关键：catalog 必须先就绪，否则 config.toml 里配置的清单文件还没生成。"""
    if NO_IDE_INJECT or READONLY:
        _log("[WBRelay] IDE 接管未执行：%s" % ide_inject_disabled_reason())
        return
    for adp in IDE_ADAPTERS:
        if not ide_write_allowed(adp["id"]):
            continue
        try:
            if not adp["detect"]():
                continue
            adp["sync_catalog"]()
        except Exception as exc:
            _log("[WBRelay] IDE %s catalog 同步异常: %s" % (adp["id"], exc))
    for adp in IDE_ADAPTERS:
        if not ide_write_allowed(adp["id"]):
            continue
        try:
            if not adp["detect"]():
                continue
            if adp["backup"]():
                adp["inject"](port)
        except Exception as exc:
            _log("[WBRelay] IDE %s 接管异常: %s" % (adp["id"], exc))


def ides_restore():
    """关闭时按备份恢复 IDE 配置。--no-ide-inject / --readonly 下同样禁止。"""
    if NO_IDE_INJECT or READONLY:
        return
    for adp in IDE_ADAPTERS:
        if not ide_write_allowed(adp["id"]):
            continue
        try:
            if ide_backup_exists(adp["id"]):
                adp["restore"]()
        except Exception as exc:
            _log("[WBRelay] IDE %s 恢复异常: %s" % (adp["id"], exc))


def sync_all_ide_catalogs():
    """模型配置变化后同步 catalog（仅对已启用且已检测到的 IDE）。"""
    if NO_IDE_INJECT or READONLY:
        return
    for adp in IDE_ADAPTERS:
        try:
            if not ide_write_allowed(adp["id"]):
                continue
            if not adp["detect"]():
                continue
            adp["sync_catalog"]()
        except Exception as exc:
            _log("[WBRelay] IDE %s catalog 同步异常: %s" % (adp["id"], exc))


# 兼容旧调用
def codex_backup_and_switch(port):
    if ide_write_allowed("codex") and _codex_detect():
        if _codex_backup():
            _codex_inject(port)


def codex_restore():
    if not ide_write_globally_blocked() and ide_backup_exists("codex"):
        _codex_restore()
