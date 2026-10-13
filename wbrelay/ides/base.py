# -*- coding: utf-8 -*-
"""IDE 适配器公共层：备份文件管理、settings 开关、API Key 与模型汇总工具。"""
import hashlib
import ipaddress
import json
import os
import time
from urllib.parse import urlsplit

from ..models import models_view
from ..atomicio import write_text
from ..paths import DATA, NO_IDE_INJECT, READONLY
from ..settings import get_keys, load_settings, update_settings

IDE_BACKUP_DIR = os.path.join(DATA, "ide_backups")
MAX_CONFIG_BYTES = 8 * 1024 * 1024


def read_ide_text(path, missing_ok=False, limit=MAX_CONFIG_BYTES):
    """Bounded, strict UTF-8 read; preserve BOM and line endings for restoration."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
    except FileNotFoundError:
        if missing_ok:
            return ""
        raise
    if len(raw) > limit:
        raise ValueError("IDE config exceeds size limit")
    return raw.decode("utf-8")


def read_ide_json(path, missing_ok=False):
    raw = read_ide_text(path, missing_ok=missing_ok)
    cfg = json.loads(raw.lstrip("\ufeff")) if raw else {}
    if not isinstance(cfg, dict):
        raise ValueError("IDE config must be a JSON object")
    return raw, cfg


def validate_ide_text(text):
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("IDE config exceeds size limit")


def ide_owner(url="", source="", managed=True):
    """Classify an endpoint without exposing userinfo, queries, or arbitrary paths."""
    from .. import state
    owner = {"state": "none", "label": "未接管", "detail": "",
             "port": None, "source": source}
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme not in ("http", "https") or not host:
            return owner
        port = parts.port or (443 if parts.scheme == "https" else 80)
        local = host.lower() == "localhost"
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        kind = "external"
        if managed and local and parts.path.rstrip("/") == "/v1":
            kind = "wbrelay" if port == state._SERVER_PORT else "wbrelay_stale"
        display_host = "[%s]" % host if ":" in host else host
        owner.update(state=kind, port=port,
                     label={"wbrelay": "WBRelay", "wbrelay_stale": "WBRelay 残留",
                            "external": "其他工具或手工接管"}[kind],
                     detail="%s://%s:%d%s" % (parts.scheme, display_host, port,
                                              "/v1" if parts.path.rstrip("/") == "/v1" else ""))
    except (TypeError, ValueError):
        pass
    return owner


def ide_external_owner(source):
    owner = ide_owner(source=source)
    owner.update(state="external", label="其他工具或手工接管")
    return owner


def _file_sha256(path):
    raw = read_ide_text(path)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ide_record_applied(ide_id, paths):
    """Only augment an existing recovery snapshot after a successful write."""
    if READONLY:
        return False
    bak = _ide_read_backup(ide_id)
    if bak is None:
        return True
    bak["files"] = [{"path": os.path.abspath(p), "sha256": _file_sha256(p)}
                    for p in paths]
    bak["applied_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return _ide_write_backup(ide_id, bak)


def ide_backup_status(ide_id):
    bak = _ide_read_backup(ide_id) or {}
    changed = False
    for item in bak.get("files", []) if isinstance(bak.get("files", []), list) else []:
        try:
            if _file_sha256(item["path"]) != item["sha256"]:
                changed = True
        except (OSError, ValueError, KeyError, TypeError):
            changed = True
    return {"backup_exists": ide_backup_exists(ide_id),
            "applied_at": bak.get("applied_at") or "", "external_change": changed}


def _wb_api_key():
    """取第一个 enabled 的 api key 作为下游 IDE 的 bearer token。"""
    for k in get_keys():
        if k.get("enabled", True) is not False and k.get("key"):
            return k["key"]
    return "wb-relay-key-1"


# ---- IDE 备份文件管理 ----
def ide_backup_path(ide_id):
    return os.path.join(IDE_BACKUP_DIR, ide_id + ".json")


def ide_backup_exists(ide_id):
    return os.path.isfile(ide_backup_path(ide_id))


def _ide_write_backup(ide_id, obj):
    """写某 IDE 的配置备份。返回 True=已落盘；只读模式直接拒绝（原语级兜底）。

    只读模式（--readonly）连备份目录都不创建 —— 方案-隔离机制要求的
    「写操作原语级兜底」：即使将来新增调用路径，也写不进任何文件。
    """
    if READONLY:
        return False
    os.makedirs(IDE_BACKUP_DIR, exist_ok=True)
    write_text(ide_backup_path(ide_id), json.dumps(obj, ensure_ascii=False, indent=2))
    return True


def _ide_read_backup(ide_id):
    p = ide_backup_path(ide_id)
    if not os.path.isfile(p):
        return None
    try:
        obj = json.loads(read_ide_text(p, limit=MAX_CONFIG_BYTES * 8))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ---- settings 里的 ides 开关 ----
def ides_settings():
    s = load_settings()
    ides = s.get("ides")
    if not isinstance(ides, dict):
        ides = {}
    if "codex" not in ides or not isinstance(ides["codex"], dict):
        ides["codex"] = {"enabled": bool(s.get("codex_auto_switch", False))}
    for k in ("opencode", "dsh", "crush"):
        if k not in ides or not isinstance(ides[k], dict):
            ides[k] = {"enabled": False}
    return ides


def ide_enabled(ide_id):
    return bool(ides_settings().get(ide_id, {}).get("enabled", False))


def ide_write_globally_blocked():
    """全局开关是否禁止一切 IDE 操作（与具体 IDE 是否 enabled 无关）。
    用于「开启接管」这类需要先放行动作、再改变状态的操作。返回原因字符串，空串表示放行。"""
    if READONLY:
        return "只读模式（--readonly）"
    if NO_IDE_INJECT:
        return "IDE 接管已禁用（--no-ide-inject）"
    return ""


def ide_write_allowed(ide_id):
    """是否允许对该 IDE 执行写入/进程操作（备份、注入、同步清单、重启、恢复）。"""
    if ide_write_globally_blocked():
        return False
    return ide_enabled(ide_id)


def ide_inject_disabled_reason():
    return ide_write_globally_blocked()


def set_ide_enabled(ide_id, on):
    """切换某 IDE 的接管开关。返回 True=已落盘；False=未写入（只读 / 写盘失败）。

    持锁 read-modify-write：`ides` 是嵌套 dict，裸的「读 → 改 → 写」在并发
    切换多个 IDE 时会互相覆盖（后写的把前一个 IDE 的开关还原）。
    """
    def _apply(data):
        ides = data.get("ides")
        if not isinstance(ides, dict):
            ides = {}
        if ide_id not in ides or not isinstance(ides[ide_id], dict):
            ides[ide_id] = {}
        ides[ide_id]["enabled"] = bool(on)
        data["ides"] = ides
        return True

    ok, _result = update_settings(_apply)
    return ok


def _collect_models_for_ide():
    """汇总当前所有已启用供应商的模型列表（dict[model_id -> info]）。"""
    view = models_view(refresh=False)
    models = {}
    for sup in (view.get("suppliers") or {}).values():
        for m in (sup.get("models") or []):
            models[m["id"]] = m
    return models
