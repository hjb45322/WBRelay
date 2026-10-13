# -*- coding: utf-8 -*-
"""Crush global data JSON adapter (CLI only, no process management).

Verified against charmbracelet/crush: internal/config/load.go GlobalConfigData,
internal/config/config.go ProviderConfig/SelectedModel, and schema.json Model.
Project/workspace overrides and shell configuration are deliberately not executed.
"""
import json
import os
import shutil

from ..atomicio import write_text
from ..log import _log
from ..models import _natural_key
from ..paths import READONLY
from ..routing import _route_models_set
from .base import (_collect_models_for_ide, _ide_read_backup, _ide_write_backup,
                   _ide_record_applied, _wb_api_key, ide_backup_path, ide_owner,
                   ide_backup_exists, ide_external_owner, read_ide_json,
                   read_ide_text, validate_ide_text)


def _crush_home():
    explicit = (os.environ.get("CRUSH_GLOBAL_DATA") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        return os.path.join(os.path.abspath(os.path.expanduser(xdg)), "crush")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(local, "crush")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "crush")


CRUSH_HOME = _crush_home()
CRUSH_CONFIG_PATH = os.path.join(CRUSH_HOME, "crush.json")


def _crush_config_paths():
    return [os.path.abspath(CRUSH_CONFIG_PATH)]


def _crush_detect():
    return (os.path.isfile(CRUSH_CONFIG_PATH) or os.path.isdir(CRUSH_HOME)
            or bool(shutil.which("crush")))


def _crush_owner():
    try:
        _raw, cfg = read_ide_json(CRUSH_CONFIG_PATH)
        selected = ((cfg.get("models") or {}).get("large") or {}).get("provider") or "wbrelay"
        provider = (cfg.get("providers") or {}).get(selected) or {}
        owner = ide_owner(provider.get("base_url") or "", "crush.json", managed=selected == "wbrelay")
        if selected != "wbrelay" and owner["state"] == "none":
            return ide_external_owner("crush.json")
        return owner
    except (OSError, ValueError, TypeError, AttributeError):
        return ide_owner(source="crush.json")


def _crush_model_entries():
    models, routed = _collect_models_for_ide(), _route_models_set()
    entries, hidden = [], 0
    for mid in sorted(models, key=_natural_key):
        if mid not in routed:
            hidden += 1
            continue
        info = models[mid]
        entry = {"id": mid, "name": str(info.get("display_name") or mid),
                 "context_window": int(info.get("context_window") or 131072),
                 "default_max_tokens": int(info.get("max_output_tokens") or 8192),
                 "cost_per_1m_in": 0, "cost_per_1m_out": 0,
                 "cost_per_1m_in_cached": 0, "cost_per_1m_out_cached": 0,
                 "can_reason": bool(info.get("reasoning_efforts")),
                 "supports_attachments": False}
        efforts = list(dict.fromkeys(str(e).strip() for e in info.get("reasoning_efforts", []) if e))
        if efforts:
            entry["reasoning_levels"] = efforts
        entries.append(entry)
    return entries, hidden


def _crush_write(text):
    if READONLY:
        return False
    validate_ide_text(text)
    write_text(CRUSH_CONFIG_PATH, text)
    return True


def _crush_backup():
    if READONLY:
        return False
    try:
        raw, _cfg = read_ide_json(CRUSH_CONFIG_PATH, missing_ok=True)
        if ide_backup_exists("crush"):
            return _ide_read_backup("crush") is not None
        return _ide_write_backup("crush", {"raw": raw,
                                        "existed": os.path.isfile(CRUSH_CONFIG_PATH)})
    except (OSError, ValueError):
        _log("[WBRelay] Crush config cannot be safely read; backup skipped")
        return False


def _crush_inject(port):
    if READONLY:
        return False
    try:
        raw, cfg = read_ide_json(CRUSH_CONFIG_PATH, missing_ok=True)
        entries, _hidden = _crush_model_entries()
        if not entries:
            return False
        providers = cfg.setdefault("providers", {})
        selections = cfg.setdefault("models", {})
        if not isinstance(providers, dict) or not isinstance(selections, dict):
            return False
        providers["wbrelay"] = {"name": "WBRelay", "type": "openai-compat",
                                "base_url": "http://127.0.0.1:%d/v1" % port,
                                "api_key": _wb_api_key(), "models": entries,
                                "discover_models": False}
        for kind in ("large", "small"):
            selected = selections.get(kind) or {}
            if not isinstance(selected, dict):
                return False
            if selected.get("provider") != "wbrelay":
                selections[kind] = {"provider": "wbrelay", "model": entries[0]["id"]}
        new = json.dumps(cfg, ensure_ascii=False, indent=2)
        if new != raw and not _crush_write(new):
            return False
        return _ide_record_applied("crush", _crush_config_paths())
    except (OSError, ValueError, TypeError):
        _log("[WBRelay] Crush config cannot be safely edited; injection failed")
        return False


def _crush_sync_catalog():
    if READONLY:
        return (0, 0)
    try:
        raw, cfg = read_ide_json(CRUSH_CONFIG_PATH)
        provider = (cfg.get("providers") or {}).get("wbrelay")
        if not isinstance(provider, dict):
            return (0, 0)
        entries, hidden = _crush_model_entries()
        provider["models"] = entries
        new = json.dumps(cfg, ensure_ascii=False, indent=2)
        if new != raw:
            if not _crush_write(new) or not _ide_record_applied("crush", _crush_config_paths()):
                return (0, hidden)
        return (len(entries), hidden)
    except (OSError, ValueError, TypeError, AttributeError):
        return (0, 0)


def _crush_restore():
    if READONLY:
        return False
    bak = _ide_read_backup("crush")
    if bak is None or "raw" not in bak:
        return False
    try:
        read_ide_text(CRUSH_CONFIG_PATH, missing_ok=True)
        if bak.get("existed", True):
            if not _crush_write(bak["raw"]):
                return False
        elif os.path.isfile(CRUSH_CONFIG_PATH):
            os.remove(CRUSH_CONFIG_PATH)
        os.remove(ide_backup_path("crush"))
        return True
    except (OSError, ValueError):
        return False
