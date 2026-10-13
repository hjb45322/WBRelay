# -*- coding: utf-8 -*-
"""OpenCode 适配器（~/.config/opencode/opencode.json）。"""
import json
import os
import shutil
import subprocess

from ..atomicio import write_text
from ..log import _log
from ..models import _natural_key
from ..paths import READONLY
from ..routing import _route_models_set
from ..state import _NO_WINDOW

from .base import (_collect_models_for_ide, _ide_read_backup, _ide_write_backup,
                   _ide_record_applied, _wb_api_key, ide_backup_path, ide_owner,
                   ide_backup_exists, ide_external_owner, read_ide_json,
                   read_ide_text, validate_ide_text)

OPENCODE_HOME = os.path.join(os.path.expanduser("~"), ".config", "opencode")
OPENCODE_CONFIG_PATH = os.path.join(OPENCODE_HOME, "opencode.json")
OPENCODE_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Programs", "@opencode-aidesktop", "OpenCode.exe")


def _opencode_write(path, text):
    """统一写入点：只读模式下一律不写（第二道防线）。返回 True=已写。"""
    if READONLY:
        _log("[WBRelay] 只读模式：不写 OpenCode 配置（%s）" % path)
        return False
    validate_ide_text(text)
    write_text(path, text)
    return True


# ===========================================================================
# OpenCode 适配器（~/.config/opencode/opencode.json）
# ===========================================================================
def _opencode_detect():
    return (os.path.isfile(OPENCODE_CONFIG_PATH) or os.path.isdir(OPENCODE_HOME)
            or bool(shutil.which("opencode")))


def _opencode_config_paths():
    return [os.path.abspath(OPENCODE_CONFIG_PATH)]


def _opencode_owner():
    try:
        _raw, cfg = read_ide_json(OPENCODE_CONFIG_PATH)
        providers = cfg.get("provider") or {}
        selected = str(cfg.get("model") or "").split("/", 1)[0]
        provider = providers.get(selected or "wbrelay") or {}
        url = (provider.get("options") or {}).get("baseURL") or ""
        owner = ide_owner(url, "opencode.json", managed=selected in ("", "wbrelay"))
        if selected and selected != "wbrelay" and owner["state"] == "none":
            return ide_external_owner("opencode.json")
        return owner
    except (OSError, ValueError, TypeError, AttributeError):
        return ide_owner(source="opencode.json")


def _opencode_backup():
    if READONLY:
        return False
    try:
        raw, _cfg = read_ide_json(OPENCODE_CONFIG_PATH, missing_ok=True)
    except (OSError, ValueError):
        _log("[WBRelay] OpenCode 配置无法安全读取，跳过备份")
        return False
    if ide_backup_exists("opencode"):
        return _ide_read_backup("opencode") is not None
    return _ide_write_backup("opencode", {"raw": raw,
                                          "existed": os.path.isfile(OPENCODE_CONFIG_PATH)})


def _opencode_model_entries():
    """OpenCode 配置里的 models 条目（注入与同步共用的**唯一**构造点）。

    档位必须写成 `variants`：OpenCode 桌面端拿 `Object.keys(model.variants)` 当档位列表，
    Codex catalog 风格的 `supported_reasoning_levels` / `supportsResponsesApi` 它完全不读
    （逐行取证见 docs/方案-OpenCode接管修复.md §2.3）。

    返回 (entries, hidden)：hidden = 因未启用 / 不可路由而被略过的模型数。
    """
    models = _collect_models_for_ide()
    route_set = _route_models_set()
    entries = {}
    hidden = 0
    for mid in sorted(models, key=_natural_key):
        m = models[mid]
        if mid not in route_set:
            hidden += 1
            continue
        entry = {"name": mid}
        ctx = m.get("context_window")
        out_tok = m.get("max_output_tokens")
        if ctx or out_tok:
            entry["limit"] = {}
            if ctx:
                entry["limit"]["context"] = ctx
            if out_tok:
                entry["limit"]["output"] = out_tok
        variants = {}
        for e in (m.get("reasoning_efforts") or []):
            name = str(e or "").strip()
            if name and name not in variants:
                variants[name] = {"reasoningEffort": name}
        if variants:
            entry["variants"] = variants
        entries[mid] = entry
    return entries, hidden


def _opencode_inject(port):
    if READONLY:
        return False
    try:
        raw, cfg = read_ide_json(OPENCODE_CONFIG_PATH, missing_ok=True)
    except (OSError, ValueError):
        _log("[WBRelay] OpenCode 配置无法安全解析，跳过接管")
        return False
    providers = cfg.get("provider")
    if providers is None:
        providers = {}
        cfg["provider"] = providers
    elif not isinstance(providers, dict):
        return False
    wb_base = "http://127.0.0.1:%d/v1" % port
    provider_models, _hidden = _opencode_model_entries()
    providers["wbrelay"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "WBRelay",
        "options": {"apiKey": _wb_api_key(), "baseURL": wb_base},
        "models": provider_models,
    }
    new = json.dumps(cfg, ensure_ascii=False, indent=2)
    if new != raw and not _opencode_write(OPENCODE_CONFIG_PATH, new):
        return False
    if not _ide_record_applied("opencode", _opencode_config_paths()):
        return False
    _log("[WBRelay] OpenCode 已指向 WBRelay: %s（%d 个模型）" % (wb_base, len(provider_models)))
    return True


def _opencode_restore():
    if READONLY:
        return False
    bak = _ide_read_backup("opencode")
    if bak is None or "raw" not in bak:
        return False
    try:
        read_ide_text(OPENCODE_CONFIG_PATH, missing_ok=True)
        if bak.get("existed", True):
            if not _opencode_write(OPENCODE_CONFIG_PATH, bak["raw"]):
                return False
        elif os.path.isfile(OPENCODE_CONFIG_PATH):
            os.remove(OPENCODE_CONFIG_PATH)
        os.remove(ide_backup_path("opencode"))
    except (OSError, ValueError):
        return False
    _log("[WBRelay] OpenCode 已恢复原始配置")
    return True


def _opencode_sync_catalog():
    """更新 opencode.json 中 wbrelay provider 的 models 列表。"""
    if READONLY or not os.path.isfile(OPENCODE_CONFIG_PATH):
        return (0, 0)
    try:
        raw, cfg = read_ide_json(OPENCODE_CONFIG_PATH)
    except Exception:
        return (0, 0)
    providers = cfg.get("provider")
    if not isinstance(providers, dict) or not isinstance(providers.get("wbrelay"), dict):
        return (0, 0)
    provider_models, hidden = _opencode_model_entries()
    providers["wbrelay"]["models"] = provider_models
    new = json.dumps(cfg, ensure_ascii=False, indent=2)
    if new != raw:
        if not _opencode_write(OPENCODE_CONFIG_PATH, new):
            return (0, hidden)
        if not _ide_record_applied("opencode", _opencode_config_paths()):
            return (0, hidden)
    return (len(provider_models), hidden)


def restart_opencode():
    """重启 OpenCode 桌面端（Electron 应用）：杀进程再拉起 exe。返回 (killed, launched)。"""
    if not os.path.isfile(OPENCODE_EXE):
        return "", False
    ps = (
        "$dir = '" + os.path.dirname(OPENCODE_EXE).replace("'", "''") + "'; "
        "$t = Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($dir) }; "
        "$k = @($t | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; "
        "$_.ProcessName }) -join ','; "
        "Start-Sleep -Milliseconds 800; "
        "Start-Process '" + OPENCODE_EXE.replace("'", "''") + "'; "
        "Write-Output ('killed=' + $k + '|launched=1')"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=30, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
        text = out.decode("utf-8", "replace").strip()
        killed, launched = "", False
        for part in text.split("|"):
            if part.startswith("killed="):
                killed = part[len("killed="):]
            elif part.startswith("launched="):
                launched = part[len("launched="):] == "1"
        return killed, launched
    except Exception:
        return "", False
