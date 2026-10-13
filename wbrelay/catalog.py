# -*- coding: utf-8 -*-
"""Codex 模型清单（catalog）的生成、同步与 Codex 重启。"""
import json
import os
import subprocess

from .atomicio import write_json, write_text
from .paths import DATA, READONLY, ensure_data_dir
from .state import _NO_WINDOW
from .log import _log
from .settings import load_settings
from .profiles import load_profiles
from .routing import _route_models_set
from .models import models_view, _natural_key
from .validate import check_aumid

# ---------------------------------------------------------------------------
# Codex 联动：同步模型配置（catalog）并重启 Codex
# ---------------------------------------------------------------------------
CODEX_HOME = os.path.join(os.path.expanduser("~"), ".codex")
CODEX_CATALOG = os.path.join(CODEX_HOME, "model-catalogs", "wbrelay-models.json")
CODEX_CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
CODEX_ORIGINAL_BAK = os.path.join(DATA, "codex_original_config.json")   # 旧版备份路径（已迁移到 ide_backups/）
DEFAULT_CODEX_AUMID = "OpenAI.Codex_2p2nqsd0c76g0!App"


def codex_aumid():
    """Codex 桌面应用 AUMID，可用 settings.codex_aumid 覆盖（不同渠道可能不同）。"""
    return str(load_settings().get("codex_aumid") or DEFAULT_CODEX_AUMID)
# OpenCode / DSH 配置路径


CODEX_TEMPLATE_KEYS = (
    "base_instructions", "shell_type", "effective_context_window_percent",
    "truncation_policy", "supports_parallel_tool_calls", "supports_search_tool",
    "supports_reasoning_summaries", "default_reasoning_summary", "support_verbosity",
    "supports_image_detail_original", "experimental_supported_tools", "availability_nux",
    "upgrade", "priority", "additional_speed_tiers", "supported_in_api",
    "default_reasoning_level", "description",
)
# Codex 对 catalog 条目的硬性要求：缺 shell_type 会**直接拒绝启动**
# （见 docs/事故记录.md 2026-10-11 21:45）。落盘前必须校验，宁可退化也不产出无效条目。
CODEX_REQUIRED_KEYS = ("slug", "shell_type")
DEFAULT_SHELL_TYPE = "shell_command"      # 兜底值：与 Codex 现有 catalog 一致
# Codex provider 名：默认 WBRelay（公开发布用名）；个人实例可在 settings.codex_provider
# 覆盖为历史托管名，保持旧 Codex 会话不报错。
# 注意：改名后历史会话会因查不到该 provider 而报 "xxx not found"，仅全新环境建议修改。
DEFAULT_CODEX_PROVIDER = "WBRelay"


def codex_provider_name():
    return str(load_settings().get("codex_provider") or DEFAULT_CODEX_PROVIDER)


CODEX_EFFORT_DESC = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balanced speed and reasoning depth",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extended reasoning depth for the hardest problems",
    "max": "Maximum reasoning depth for the hardest problems",
    "ultra": "Maximum reasoning depth with extended planning",
}


def codex_catalog_exists():
    return os.path.isfile(CODEX_CATALOG)


def _load_relay_template():
    """从 Codex 自带的模型定义库取一个 GPT 条目作模板（含 model_messages 等字段）。
    优先 relay-*.json，其次扫描目录内任意 catalog。
    都找不到时返回 {}，调用方退化为最小条目（功能不受影响，仅档位展示略简）。"""
    base = os.path.join(CODEX_HOME, "model-catalogs")
    try:
        names = os.listdir(base)
    except Exception:
        return {}
    ordered = ([f for f in names if f.startswith("relay-") and f.endswith(".json")]
               + [f for f in names if not f.startswith("relay-") and f.endswith(".json")])
    for fn in ordered:
        try:
            with open(os.path.join(base, fn), "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except Exception:
            continue
        for m in (obj.get("models") or []):
            if str(m.get("slug") or "").startswith("gpt-") and "model_messages" in m:
                return m
    return {}


def _load_upstream_catalog_map():
    """可选的外部 catalog -> {slug: entry}，用于对齐 display_name / credits。
    路径由 settings.upstream_catalog_path 指定；未配置或读取失败时返回空字典。"""
    path = str(load_settings().get("upstream_catalog_path") or "").strip()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return {str(m.get("slug") or ""): m for m in (obj.get("models") or []) if m.get("slug")}
    except Exception:
        return {}


def _load_template_from_existing_catalog():
    """从已有 catalog 取模板条目（CODEX_TEMPLATE_KEYS 字段）。

    优先读 CODEX_CATALOG；读不到时按目录扫描 model-catalogs 下任意 *-models.json
    （兼容旧版本写入的历史文件名，不硬编码任何品牌名），避免模板为空导致输出的
    条目缺 shell_type 等 Codex 必填字段（缺了 Codex 直接拒绝启动）。都读不到返回 {}。"""
    catalog_dir = os.path.join(CODEX_HOME, "model-catalogs")
    candidates = [CODEX_CATALOG]
    try:
        for fn in sorted(os.listdir(catalog_dir)):
            if not fn.lower().endswith("-models.json"):
                continue
            full = os.path.join(catalog_dir, fn)
            if os.path.normcase(os.path.abspath(full)) != os.path.normcase(os.path.abspath(CODEX_CATALOG)):
                candidates.append(full)
    except Exception:
        pass
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = json.load(fh)
            first = (old.get("models") or [{}])[0]
            tpl = {k: first[k] for k in CODEX_TEMPLATE_KEYS if k in first}
            if "shell_type" in tpl:
                return tpl
        except Exception:
            continue
    return {}


def validate_codex_catalog(catalog):
    """落盘前自检：返回不合法条目说明（空列表 = 通过）。

    Codex 对 catalog 是「严格模式」：条目缺必填字段直接拒绝启动，不是警告。
    因此写生效路径前必须先过这一关，避免把上一版好文件覆盖成起不来的版本。
    """
    bad = []
    models = (catalog or {}).get("models") or []
    if not models:
        bad.append("模型清单为空")
    for i, m in enumerate(models):
        lack = [k for k in CODEX_REQUIRED_KEYS if not str((m or {}).get(k) or "").strip()]
        if lack:
            bad.append("第 %d 条（%s）缺 %s" % (i + 1, (m or {}).get("slug") or "?", "/".join(lack)))
    return bad


def build_codex_catalog(models, enabled_map):
    """models: dict[模型名 -> normalize 后字段]；enabled_map: 全局启停。
    生成 Codex model-catalog 结构。GPT 模型套官方级模板（完整显示档位），其余用普通模板+档位映射。"""
    tpl = _load_template_from_existing_catalog()
    relay_tpl = _load_relay_template()
    up_map = _load_upstream_catalog_map()
    # 官方模板：保留数据字段之外的所有字段（model_messages、tool_mode 等）
    DATA_KEYS = {"slug", "display_name", "description", "context_window",
                 "max_context_window", "max_output_tokens", "supported_reasoning_levels",
                 "input_modalities", "credits", "visibility", "default_reasoning_level",
                 "supported_in_api", "effective_context_window_percent"}
    official_base = {k: v for k, v in relay_tpl.items() if k not in DATA_KEYS} if relay_tpl else {}
    out = []
    shell_fallback = 0
    for mid in sorted(models, key=_natural_key):
        m = models[mid]
        enabled = enabled_map.get(mid) is not False
        efforts = m.get("reasoning_efforts") or []
        is_gpt = str(mid).lower().startswith("gpt-")
        if is_gpt and official_base:
            # GPT 模型：官方级模板 + 完整档位（保留 max/ultra）
            levels_raw = [str(e) for e in efforts]
            base = dict(official_base)
        else:
            # 其他模型：普通模板 + 档位原样保留（不再把 max/ultra 映射成 xhigh，
            # Codex 已支持 max，deepseek 等模型真实档位如 low/high/max 应原样显示）
            levels_raw = [str(e) for e in efforts]
            base = dict(tpl)
        # 档位去重保序
        seen = set()
        levels_raw = [e for e in levels_raw if not (e in seen or seen.add(e))]
        levels = [{"effort": e, "description": CODEX_EFFORT_DESC.get(e,
                   "Reasoning depth: %s" % e)} for e in levels_raw]
        # 默认档位 = 第二档（如 low/high/xhigh → high）；不足两档则取第一档，再退回 medium
        default_lvl = (levels_raw[1] if len(levels_raw) >= 2
                       else (levels_raw[0] if levels_raw else "medium"))
        cred = m.get("credits")
        cred_txt = str(cred).strip() if cred is not None else ""
        up_entry = up_map.get(mid) or {}
        if not cred_txt and up_entry.get("credits"):
            # 倍率回退到上游服务官方 catalog（缓存行缺 credits 时仍显示真实倍率）
            cred_txt = str(up_entry["credits"]).strip()
        if cred_txt and not cred_txt.lower().startswith("x"):
            cred_txt = "x" + cred_txt.lstrip("x")
        # display_name 优先用上游服务原始名（"GPT-6-Luna (x0.07)" 大写），Codex UI 不裁前缀；
        # 上游服务没有的模型才用 "gpt-6-luna (x0.07)" 生成
        up_disp = str(up_entry.get("display_name") or "").strip()
        disp = up_disp or (("%s (%s)" % (mid, cred_txt)) if cred_txt else mid)
        # Codex UI 对 display_name 开头的 "gpt-"（大小写不敏感）会裁掉前缀：
        # "GPT-6-Luna" → 显示成 "6-Luna"。把首个连字符换成空格（"GPT 6-Luna"）避开前缀匹配，
        # UI 仍完整显示 GPT。
        if disp.lower().startswith("gpt-"):
            disp = disp[:3] + " " + disp[4:]
        entry = dict(base)
        entry.update({
            "slug": mid,
            "display_name": disp,
            "description": ("%s · 积分倍率 %s" % (mid, cred_txt)) if cred_txt else mid,
            "default_reasoning_level": default_lvl,
            "context_window": m.get("context_window"),
            "max_context_window": m.get("context_window"),
            "max_output_tokens": m.get("max_output_tokens"),
            "supported_reasoning_levels": levels,
            "input_modalities": ["text", "image"] if m.get("supports_vision") else ["text"],
            "visibility": "list" if enabled else "hide",   # Codex 枚举：list/hide/none（停用=hide）
            "supported_in_api": True,
        })
        if not str(entry.get("shell_type") or "").strip():
            # 模板链（wbrelay-models.json → 旧 upstream-models.json）全部失效时兜底
            entry["shell_type"] = DEFAULT_SHELL_TYPE
            shell_fallback += 1
        if cred_txt:
            entry["credits"] = cred_txt
        else:
            # 没有倍率数据 → 不写 credits 字段，Codex 不再显示 X1.00 等错误倍率
            entry.pop("credits", None)
        out.append(entry)
    if shell_fallback:
        _log("[WBRelay] Codex catalog：%d 条模型没取到 shell_type 模板，已用 %s 兜底"
             % (shell_fallback, DEFAULT_SHELL_TYPE))
    return {"models": out}


def sync_codex_catalog():
    """把当前 WBRelay 模型状态写入 Codex catalog（备份旧文件 + 原子写）。返回 (written, hidden)。

    只读模式（--readonly）一律不写：catalog 属于「只读模式不得写入」的清单之一。
    """
    if READONLY:
        _log("[WBRelay] Codex catalog 同步跳过：只读模式（--readonly）")
        return 0, 0
    view = models_view(refresh=False)
    models = {}
    for sup in (view.get("suppliers") or {}).values():
        for m in (sup.get("models") or []):
            mid = m["id"]
            old = models.get(mid)
            if old is None:
                models[mid] = m
                continue
            # 同名模型跨供应商合并：credits / reasoning_efforts 等保留首个非空值
            # （例：同一模型在不同中转行的倍率可能只有部分行有值，不能被空值覆盖）
            merged = dict(old)
            for k, v in m.items():
                if v is None or v == "":
                    continue
                if k in ("credits", "reasoning_efforts") and old.get(k):
                    continue
                merged[k] = v
            models[mid] = merged
    if not models:
        # 启动早期 models_view 可能尚未预热：此时写会把清单清空，直接跳过
        _log("[WBRelay] Codex catalog 同步跳过：当前模型列表为空")
        return 0, 0
    # 可见性以路由表为准：能路由的 list，其余 hide
    route_set = _route_models_set()
    enabled_map = {mid: (mid in route_set) for mid in models}
    catalog = build_codex_catalog(models, enabled_map)
    hidden = sum(1 for m in catalog["models"] if m.get("visibility") == "hide")
    bad = validate_codex_catalog(catalog)
    if bad:
        # 保留上一版好文件：Codex 拿到缺字段的 catalog 会直接起不来（见 docs/事故记录.md）
        _log("[WBRelay] Codex catalog 自检不通过，保留上一版不覆盖：%s" % "；".join(bad[:5]))
        return 0, 0
    os.makedirs(os.path.dirname(CODEX_CATALOG), exist_ok=True)
    if os.path.isfile(CODEX_CATALOG):
        # .bak 也必须原子写：直接覆写会在异常时留下半截备份文件
        try:
            with open(CODEX_CATALOG, "r", encoding="utf-8") as fh:
                previous = fh.read()
            write_text(CODEX_CATALOG + ".bak", previous)
        except Exception as exc:
            _log("[WBRelay] Codex catalog 备份失败（继续写主文件）：%s" % exc)
    write_json(CODEX_CATALOG, catalog)
    return len(catalog["models"]), hidden


def restart_codex():
    """重启 Codex：杀应用/CLI 进程（保留 sandbox 服务），再启动桌面应用。返回 (killed, launched)。

    只应由用户显式点击触发（R5：不自动重启外部进程）。
    AUMID 由用户配置，因此**先严格校验字符集**，再通过环境变量传给 PowerShell，
    不拼进脚本字符串（避免命令注入）。
    """
    aumid = check_aumid(codex_aumid()) or DEFAULT_CODEX_AUMID
    ps = (
        "$t = Get-Process | Where-Object { "
        "($_.Path -match 'WindowsApps\\\\OpenAI\\.Codex' -and $_.ProcessName -ne 'codex-windows-sandbox-service') "
        "-or $_.Path -match 'LocalAppData\\\\OpenAI\\\\Codex' }; "
        "$k = @($t | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; "
        "$_.ProcessName }) -join ','; "
        "Start-Sleep -Milliseconds 800; "
        "Start-Process explorer.exe -ArgumentList ('shell:AppsFolder\\' + $env:WBRELAY_CODEX_AUMID); "
        "Write-Output ('killed=' + $k + '|launched=1')"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=30, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW,
            env=dict(os.environ, WBRELAY_CODEX_AUMID=aumid))
        text = out.decode("utf-8", "replace").strip()
        killed = ""
        launched = False
        for part in text.split("|"):
            if part.startswith("killed="):
                killed = part[len("killed="):]
            elif part.startswith("launched="):
                launched = part[len("launched="):] == "1"
        return killed, launched
    except Exception as exc:
        return "", False


# ---------------------------------------------------------------------------
# Codex config.toml 自动接管：WBRelay 启动时切到 8789，关闭时恢复原始值
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# IDE 适配器：WBRelay 启动时备份并注入各 IDE 配置，关闭时恢复
# ---------------------------------------------------------------------------
