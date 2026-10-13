# -*- coding: utf-8 -*-
"""Codex: scoped TOML edits and byte-exact recovery of config.toml."""
import json
import os
import re
import shutil

from .. import state
from ..atomicio import write_text
from ..catalog import (CODEX_CATALOG, CODEX_CONFIG_PATH, CODEX_HOME,
                       CODEX_ORIGINAL_BAK, codex_provider_name, sync_codex_catalog)
from ..log import _log
from ..paths import READONLY
from .base import (_ide_read_backup, _ide_write_backup, _ide_record_applied,
                   _wb_api_key, ide_backup_path, ide_backup_exists, ide_owner,
                   read_ide_text, validate_ide_text)


def _read_codex_config():
    try:
        return read_ide_text(CODEX_CONFIG_PATH)
    except (OSError, ValueError):
        return None


def _write_codex_config(text):
    if READONLY:
        return False
    validate_ide_text(text)
    write_text(CODEX_CONFIG_PATH, text)
    return True


_KEY_PART = r'(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27)'
_TABLE_RE = re.compile(r'^\s*\[(?!\[)(.+?)\]\s*(?:#.*)?$')
_KEY_RE = re.compile(_KEY_PART)
_STRING_RE = re.compile(r'^\s*("(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27)\s*(?:#.*)?$')


def _string_value(value):
    m = _STRING_RE.fullmatch(value)
    if not m:
        raise ValueError("Unsupported TOML string")
    val = m.group(1)
    return json.loads(val) if val[0] == '"' else val[1:-1]


def _tables(text):
    """Locate boundaries without reserializing unrelated TOML values."""
    out = []
    pos = 0
    for line in text.splitlines(True):
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith("["):
            m = _TABLE_RE.fullmatch(stripped)
            if m:
                body = m.group(1)
                parts, cursor = [], 0
                for key in _KEY_RE.finditer(body):
                    sep = body[cursor:key.start()].strip()
                    if sep != ("." if parts else ""):
                        raise ValueError("Invalid TOML table")
                    val = key.group()
                    parts.append(_string_value(val) if val[0] in "\"'" else val)
                    cursor = key.end()
                if not parts or body[cursor:].strip():
                    raise ValueError("Invalid TOML table")
                out.append((tuple(parts), pos, pos + len(line)))
            elif stripped.startswith("[[") and "]]" in stripped:
                out.append(((), pos, pos + len(line)))
            else:
                raise ValueError("Invalid TOML table")
        pos += len(line)
    return out


def _segment(text, table=()):
    tables = _tables(text)
    if not table:
        return 0, tables[0][1] if tables else len(text)
    matches = [i for i, entry in enumerate(tables) if entry[0] == table]
    if len(matches) > 1:
        raise ValueError("Duplicate TOML provider table")
    if not matches:
        return None
    i = matches[0]
    return tables[i][2], tables[i + 1][1] if i + 1 < len(tables) else len(text)


def _extract_toml_string(text, key):
    matches = list(re.finditer(r'(?m)^[ \t]*%s[ \t]*=[ \t]*(.*)$' % re.escape(key), text))
    if len(matches) > 1:
        raise ValueError("Duplicate TOML key")
    return _string_value(matches[0].group(1).rstrip("\r")) if matches else None


def _root_string(text, key):
    a, b = _segment(text)
    return _extract_toml_string(text[a:b].lstrip("\ufeff"), key)


def _replace_toml_string(text, key, value):
    _extract_toml_string(text, key)
    pat = r'(?m)^([ \t]*%s[ \t]*=[ \t]*)("(?:[^"\\]|\\.)*"|\x27[^\x27]*\x27)' % re.escape(key)
    return re.sub(pat, lambda m: m.group(1) + json.dumps(value, ensure_ascii=False),
                  text, count=1)


def _ensure_toml_string(text, key, value):
    nl = "\r\n" if "\r\n" in text else "\n"
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    if _extract_toml_string(body, key) is not None:
        return bom + _replace_toml_string(body, key, value)
    return bom + '%s = %s%s' % (key, json.dumps(value, ensure_ascii=False), nl) + body


def _ensure_codex_provider_segment(text, name=None):
    name = name or codex_provider_name()
    if _segment(text, ("model_providers", name)):
        return text
    active = _root_string(text, "model_provider")
    nl = "\r\n" if "\r\n" in text else "\n"
    if active:
        for table, start, content in _tables(text):
            if table == ("model_providers", active):
                return text[:start] + "[model_providers.%s]%s" % (json.dumps(name), nl) + text[content:]
    return text + (nl if not text.endswith("\n") else "") + nl + (
        '[model_providers.%s]%sname = %s%swire_api = "responses"%s'
        'requires_openai_auth = false%s' % (json.dumps(name), nl, json.dumps(name), nl, nl, nl))


def _codex_endpoint(text):
    active = _root_string(text, "model_provider")
    if not active:
        return ""
    bounds = _segment(text, ("model_providers", active))
    if bounds:
        return _extract_toml_string(text[bounds[0]:bounds[1]], "base_url") or ""
    return ""


def _codex_config_paths():
    return [os.path.abspath(CODEX_CONFIG_PATH)]


def _codex_owner():
    try:
        return ide_owner(_codex_endpoint(read_ide_text(CODEX_CONFIG_PATH)), "config.toml")
    except (OSError, ValueError):
        return ide_owner(source="config.toml")


def _codex_detect():
    return (os.path.isfile(CODEX_CONFIG_PATH) or os.path.isdir(CODEX_HOME)
            or bool(shutil.which("codex")))


def _codex_backup():
    if READONLY:
        return False
    try:
        text = read_ide_text(CODEX_CONFIG_PATH, missing_ok=True)
        _codex_endpoint(text)
        if ide_backup_exists("codex"):
            return _ide_read_backup("codex") is not None
        if os.path.isfile(CODEX_ORIGINAL_BAK):
            legacy = json.loads(read_ide_text(CODEX_ORIGINAL_BAK))
            if isinstance(legacy, dict) and legacy.get("base_url"):
                return _ide_write_backup("codex", {
                    "base_url": legacy["base_url"],
                    "experimental_bearer_token": legacy.get("experimental_bearer_token")})
        return _ide_write_backup("codex", {"raw": text,
                                          "existed": os.path.isfile(CODEX_CONFIG_PATH)})
    except (OSError, ValueError):
        _log("[WBRelay] Codex 配置无法安全读取，跳过备份")
        return False


def _codex_inject(port):
    if READONLY:
        return False
    try:
        text = read_ide_text(CODEX_CONFIG_PATH, missing_ok=True)
        name = codex_provider_name()
        new = _ensure_codex_provider_segment(text, name)
        a, b = _segment(new, ("model_providers", name))
        section = new[a:b]
        for key, value in (("name", name), ("base_url", "http://127.0.0.1:%d/v1" % port),
                           ("experimental_bearer_token", _wb_api_key())):
            section = _ensure_toml_string(section, key, value)
        new = new[:a] + section + new[b:]
        a, b = _segment(new)
        root = _ensure_toml_string(new[a:b], "model_provider", name)
        root = _ensure_toml_string(root, "model_catalog_json", CODEX_CATALOG)
        new = root + new[b:]
        if new != text and not _write_codex_config(new):
            return False
        bak = _ide_read_backup("codex")
        if bak is not None:
            bak["applied_base_url"] = "http://127.0.0.1:%d/v1" % port
            if not _ide_write_backup("codex", bak):
                return False
        if not _ide_record_applied("codex", _codex_config_paths()):
            return False
    except (OSError, ValueError):
        _log("[WBRelay] Codex 配置无法安全修改，未完成接管")
        return False
    _log("[WBRelay] Codex 已切到 WBRelay（provider=%s）" % name)
    return True


def _codex_restore():
    if READONLY:
        return False
    original = _ide_read_backup("codex")
    if original is None:
        return False
    try:
        text = read_ide_text(CODEX_CONFIG_PATH)
        expected = original.get("applied_base_url") or "http://127.0.0.1:%d/v1" % state._SERVER_PORT
        if _codex_endpoint(text) != expected:
            _log("[WBRelay] Codex 入口已被外部改写，跳过恢复并保留备份")
            return False
        if "raw" in original:
            if original.get("existed", True):
                if not _write_codex_config(original["raw"]):
                    return False
            else:
                os.remove(CODEX_CONFIG_PATH)
        else:
            # Legacy field snapshots cannot recover fields they never recorded.
            active = _root_string(text, "model_provider")
            a, b = _segment(text, ("model_providers", active))
            section = text[a:b]
            for key in ("base_url", "experimental_bearer_token"):
                if original.get(key):
                    section = _replace_toml_string(section, key, original[key])
            if not _write_codex_config(text[:a] + section + text[b:]):
                return False
        os.remove(ide_backup_path("codex"))
    except (OSError, ValueError, TypeError):
        return False
    _log("[WBRelay] Codex 已恢复原始配置")
    return True


def _codex_sync_catalog():
    if READONLY:
        return (0, 0)
    try:
        _codex_endpoint(read_ide_text(CODEX_CONFIG_PATH, missing_ok=True))
    except (OSError, ValueError):
        return (0, 0)
    return sync_codex_catalog()
