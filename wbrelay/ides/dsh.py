# -*- coding: utf-8 -*-
"""DSH（DeepSeek Harness）适配器：profile 用户 patch + 凭据 refs。

DSH 0.2（Cordis 化）之后不再使用 `~/.dsh/settings.yaml`：

  · 组合面 = profile 的用户 patch 层 `~/.dsh/profiles/<profile>/cordis.patch.yml`
    （DSH 自己的「设置 → 模型」页写的也是这一个文件）；
  · 路由面 = `llm-pi-ai` 条目里的 `config.providers.<route>`；
  · 模型面 = 该路由下的 `models` 列表（自定义路由必须显式列出）；
  · 默认模型 = `agent-default-model` 条目的 `provider` / `model`；
  · 凭据面 = `~/.dsh/.credentials.yaml` 的 `refs:`（路由用 `apiKeyEnv` 引用）。

逐行取证见 docs/方案-DSH接管修复.md §2。

硬约束：所有写入走 atomicio.write_text（tmp + os.replace）；--readonly 一律不写；
只动 WBRelay 自己的托管块、`agent-default-model` 的 provider/model 两行（连带移除属于
旧模型的 `reasoningEffort` 行）、以及 `refs:` 里的一个键 —— 不重排、不改写用户其余内容。
关掉开关按备份整文件还原。
"""
import os
import re
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
                   ide_backup_exists, ide_external_owner, read_ide_text, validate_ide_text)


# ===========================================================================
# 路径：harness home / profile / 凭据
# ===========================================================================
def _dsh_home():
    """harness home：显式 `$DSH_HOME`（非空白）优先，否则 `~/.dsh`。"""
    raw = (os.environ.get("DSH_HOME") or "").strip()
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    return os.path.join(os.path.expanduser("~"), ".dsh")


DSH_HOME = _dsh_home()
DSH_PROFILES = os.path.join(DSH_HOME, "profiles")
DSH_CREDENTIALS_PATH = os.path.join(DSH_HOME, ".credentials.yaml")
# 旧版（Cordis 化之前）遗留文件：只读探测用于给日志提示，**绝不写**
DSH_SETTINGS_PATH = os.path.join(DSH_HOME, "settings.yaml")

PATCH_NAME = "cordis.patch.yml"
PROFILE_HINTS = ("cordis.yml", PATCH_NAME, "package.json")
PROFILE_PREFER = ("desktop", "web")

PLUGIN_PI_AI = "@deepseek-ai/dsh-llm-pi-ai"
PLUGIN_DEFAULT_MODEL = "@deepseek-ai/dsh-agent-default-model"
ROUTE_NAME = "wbrelay"
REF_NAME = "WBRELAY_API_KEY"
API_KIND = "openai-completions"

WB_BEGIN = "# >>> WBRelay managed block（由 WBRelay 自动维护，请勿手工编辑）>>>"
WB_END = "# <<< WBRelay managed block <<<"


def _dsh_write(path, text):
    """统一写入点：只读模式下一律不写（第二道防线）。返回 True=已写。"""
    if READONLY:
        _log("[WBRelay] 只读模式：不写 DSH 配置（%s）" % path)
        return False
    validate_ide_text(text)
    write_text(path, text)
    return True


def _read_text(path):
    """读文本：不存在返回 ""；读取失败抛异常（调用方须放弃写入）。"""
    return read_ide_text(path, missing_ok=True)


def _dsh_profile_dir():
    """可接管的 profile 目录；找不到返回 ""。

    判定依据是「有 profile 骨架」：`cordis.yml` / `cordis.patch.yml` / `package.json`
    任一存在。桌面端 profile 名是 `desktop`，其余模板（web/headless/...）同理可按名发现。
    """
    if not os.path.isdir(DSH_PROFILES):
        return ""
    try:
        names = sorted(n for n in os.listdir(DSH_PROFILES)
                       if os.path.isdir(os.path.join(DSH_PROFILES, n)))
    except OSError:
        return ""
    cands = [n for n in names
             if any(os.path.isfile(os.path.join(DSH_PROFILES, n, f)) for f in PROFILE_HINTS)]
    for want in PROFILE_PREFER:
        if want in cands:
            return os.path.join(DSH_PROFILES, want)
    return os.path.join(DSH_PROFILES, cands[0]) if cands else ""


def _dsh_patch_path(profile=""):
    profile = profile or _dsh_profile_dir()
    return os.path.join(profile, PATCH_NAME) if profile else ""


def _dsh_detect():
    """「已安装且可接管」= 找得到 profile 的用户 patch 面。

    旧版遗留的 `settings.yaml` 不再参与判定：DSH 0.2 只在启动时把它当一次性迁移
    输入读取，写它既不生效、又会留下垃圾文件（docs/方案-DSH接管修复.md §2.2）。
    """
    return bool(_dsh_profile_dir() or os.path.isdir(DSH_HOME) or shutil.which("dsh"))


def _dsh_config_paths():
    patch = _dsh_patch_path()
    return ([os.path.abspath(patch)] if patch else []) + [os.path.abspath(DSH_CREDENTIALS_PATH)]


def _dsh_owner():
    try:
        patch = _dsh_patch_path()
        if not patch:
            return ide_owner(source=PATCH_NAME)
        raw = _read_text(patch)
        lines = raw.splitlines()
        rng = _entry_range(lines, "agent-default-model")
        if rng:
            selected = [_unquote(m.group(2)) for line in lines[rng[0]:rng[1]]
                        for m in [_PROVIDER_LINE_RE.match(line)] if m]
            if selected and selected[0] != ROUTE_NAME:
                return ide_external_owner(PATCH_NAME)
        for block in _MANAGED_RE.finditer(raw):
            text = block.group()
            if not re.search(r'(?m)^\s*apiKeyEnv:\s*WBRELAY_API_KEY\s*$', text):
                continue
            match = re.search(r'(?m)^\s*baseURL:\s*(.*?)\s*$', text)
            if match:
                return ide_owner(_unquote(match.group(1)), PATCH_NAME)
    except (OSError, ValueError):
        pass
    return ide_owner(source=PATCH_NAME)


# ===========================================================================
# YAML 文本工具（核心只用标准库：不引 YAML 依赖，只做最小、可验证的文本改写）
# ===========================================================================
def _yaml_str(value):
    """把值写成 YAML 双引号标量（转义反斜杠/引号/控制字符）。"""
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + text + '"'


def _unquote(value):
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _as_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


# ===========================================================================
# 模型清单（注入与同步共用的**唯一**构造点）
# ===========================================================================
def _dsh_model_entries():
    """（[(mid, info)], hidden）——只列「已启用且可路由」的模型。

    与 OpenCode 适配器同一口径：`models_view()` 负责启用过滤，路由表负责可路由过滤。
    档位来自 `reasoning_efforts`，写进 pi-ai 的 `reasoningEfforts`（等级名 → 协议拼写）。
    """
    models = _collect_models_for_ide()
    route_set = _route_models_set()
    out, hidden = [], 0
    for mid in sorted(models, key=_natural_key):
        if mid not in route_set:
            hidden += 1
            continue
        out.append((mid, models[mid]))
    return out, hidden


def _route_lines(indent, port):
    """`providers.<ROUTE_NAME>` 这一段（不含托管标记）。"""
    pad = " " * indent
    sub = " " * (indent + 2)
    sub2 = " " * (indent + 4)
    entries, _hidden = _dsh_model_entries()
    out = [pad + ROUTE_NAME + ":",
           sub + "displayName: WBRelay",
           sub + "api: " + API_KIND,
           sub + 'baseURL: "http://127.0.0.1:%d/v1"' % port,
           sub + "apiKeyEnv: " + REF_NAME]
    if entries:
        out.append(sub + "models:")
        for mid, info in entries:
            out.append(sub2 + "- id: " + _yaml_str(mid))
            name = str(info.get("display_name") or "")
            if name and name != mid:
                out.append(sub2 + "  name: " + _yaml_str(name))
            ctx = _as_int(info.get("context_window"))
            if ctx:
                out.append(sub2 + "  contextWindow: %d" % ctx)
            out_tok = _as_int(info.get("max_output_tokens"))
            if out_tok:
                out.append(sub2 + "  maxTokens: %d" % out_tok)
            efforts = []
            for e in (info.get("reasoning_efforts") or []):
                level = str(e or "").strip()
                if level and level not in efforts:
                    efforts.append(level)
            if efforts:
                out.append(sub2 + "  reasoningEfforts:")
                for level in efforts:
                    out.append(sub2 + "    " + _yaml_str(level) + ": " + _yaml_str(level))
    return out


def _route_block(indent, port):
    """带托管标记的路由块（并入已有的 `llm-pi-ai` 条目时用）。"""
    return ([" " * indent + WB_BEGIN] + _route_lines(indent, port)
            + [" " * indent + WB_END])


# ===========================================================================
# patch 文本引擎
# ===========================================================================
_MANAGED_RE = re.compile(r"^[ \t]*" + re.escape(WB_BEGIN) + r".*?^[ \t]*"
                         + re.escape(WB_END) + r"[ \t]*\r?\n?",
                         re.MULTILINE | re.DOTALL)
_TOP_ITEM_RE = re.compile(r"^-[ \t]")
_ENTRY_ID_RE = re.compile(r"^-[ \t]+id[ \t]*:[ \t]*([A-Za-z0-9_.\-]+)[ \t]*$")
_PROVIDERS_RE = re.compile(r"^([ \t]*)providers[ \t]*:[ \t]*$")
_PROVIDERS_EMPTY_RE = re.compile(r"^([ \t]*)providers[ \t]*:[ \t]*\{[ \t]*\}[ \t]*$")
_PROVIDERS_ANY_RE = re.compile(r"^[ \t]*providers[ \t]*:")
_CONFIG_RE = re.compile(r"^([ \t]+)config[ \t]*:[ \t]*$")
_CONFIG_ANY_RE = re.compile(r"^[ \t]*config[ \t]*:")
_PROVIDER_LINE_RE = re.compile(r"^([ \t]+)provider[ \t]*:[ \t]*(.*)$")
_MODEL_LINE_RE = re.compile(r"^([ \t]+)model[ \t]*:[ \t]*(.*)$")
_EFFORT_LINE_RE = re.compile(r"^[ \t]+reasoningEffort[ \t]*:")
_PORT_RE = re.compile(r"baseURL[ \t]*:[ \t]*[\"']?http://127\.0\.0\.1:(\d+)/v1")


def _strip_managed(text):
    """摘掉所有 WBRelay 托管块。标记不嵌套，非贪婪匹配即可。"""
    return _MANAGED_RE.sub("", text or "")


def _entry_range(lines, entry_id):
    """顶层条目 `- id: <entry_id>` 的行区间 [start, end)；找不到返回 None。"""
    start = None
    for i, line in enumerate(lines):
        m = _ENTRY_ID_RE.match(line)
        if m and m.group(1) == entry_id:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if _TOP_ITEM_RE.match(lines[j]):
            return (start, j)
    return (start, len(lines))


def _switch_default_model(lines, default_model):
    """把 `agent-default-model` 指到 ROUTE_NAME/<default_model>。

    已经指向 ROUTE_NAME 的条目**原样保留**：接管期间的默认模型属于用户
    （他们可能在 DSH 里换过），重注入不该把它扳回来。
    返回 (lines, changed, found)。
    """
    rng = _entry_range(lines, "agent-default-model")
    if not rng:
        return lines, False, False
    start, end = rng
    prov_i = model_i = None
    for i in range(start, end):
        if prov_i is None and _PROVIDER_LINE_RE.match(lines[i]):
            prov_i = i
            continue
        if model_i is None and _MODEL_LINE_RE.match(lines[i]):
            model_i = i
    if prov_i is not None:
        cur = _unquote(_PROVIDER_LINE_RE.match(lines[prov_i]).group(2))
        if cur == ROUTE_NAME:
            return lines, False, True
    lines = list(lines)
    if prov_i is not None:
        lines[prov_i] = (_PROVIDER_LINE_RE.match(lines[prov_i]).group(1)
                         + "provider: " + ROUTE_NAME)
    else:
        cfg_i = None
        for i in range(start, end):
            if _CONFIG_RE.match(lines[i]):
                cfg_i = i
                break
        if cfg_i is not None:
            indent = _CONFIG_RE.match(lines[cfg_i]).group(1) + "  "
            at = cfg_i + 1
        else:
            indent = "  "
            at = start + 1
        lines.insert(at, indent + "provider: " + ROUTE_NAME)
        prov_i = at
        end += 1
        if model_i is not None and model_i >= at:
            model_i += 1
    if default_model:
        if model_i is not None:
            lines[model_i] = (_MODEL_LINE_RE.match(lines[model_i]).group(1)
                              + "model: " + _yaml_str(default_model))
        else:
            indent = _PROVIDER_LINE_RE.match(lines[prov_i]).group(1)
            lines.insert(prov_i + 1, indent + "model: " + _yaml_str(default_model))
    # 档位属于「切换前那个模型」：留着可能和新模型的能力对不上，一并移除
    # （条目本身仍由整文件快照还原）。
    rng2 = _entry_range(lines, "agent-default-model")
    if rng2:
        for i in range(rng2[1] - 1, rng2[0] - 1, -1):
            if _EFFORT_LINE_RE.match(lines[i]):
                del lines[i]
    return lines, True, True


def _merge_route(lines, port, default_model):
    """把托管路由块放进 patch，返回 (lines, ok, note)。

    优先并入**已有的**顶层 `- id: llm-pi-ai` 条目（DSH 设置页写的就是它）；
    没有该条目时追加一条独立托管条目。结构无法安全识别时 ok=False（调用方放弃写入）。
    """
    rng = _entry_range(lines, "llm-pi-ai")
    if rng:
        start, end = rng
        for i in range(start, end):
            m = _PROVIDERS_RE.match(lines[i])
            if m:
                indent = len(m.group(1)) + 2
                return (lines[:i + 1] + _route_block(indent, port) + lines[i + 1:], True, "")
            m = _PROVIDERS_EMPTY_RE.match(lines[i])
            if m:
                indent = len(m.group(1)) + 2
                new = (lines[:i] + [m.group(1) + "providers:"]
                       + _route_block(indent, port) + lines[i + 1:])
                return (new, True, "")
        if any(_PROVIDERS_ANY_RE.match(lines[i]) for i in range(start, end)):
            return (lines, False, "已有 llm-pi-ai 条目的 providers 写法无法安全并入")
        for i in range(start, end):
            m = _CONFIG_RE.match(lines[i])
            if m:
                indent = len(m.group(1)) + 2
                new = (lines[:i + 1] + [m.group(1) + "  providers:"]
                       + _route_block(indent, port) + lines[i + 1:])
                return (new, True, "")
        if any(_CONFIG_ANY_RE.match(lines[i]) for i in range(start, end)):
            return (lines, False, "已有 llm-pi-ai 条目的 config 写法无法安全并入")
        block = ["  config:", "    providers:"] + _route_block(6, port)
        return (lines[:end] + block + lines[end:], True, "")
    block = [WB_BEGIN,
             "- id: llm-pi-ai",
             "  name: '%s'" % PLUGIN_PI_AI,
             "  config:",
             "    providers:"]
    block += _route_lines(6, port)
    if default_model and _entry_range(lines, "agent-default-model") is None:
        block += ["- id: agent-default-model",
                  "  name: '%s'" % PLUGIN_DEFAULT_MODEL,
                  "  config:",
                  "    provider: " + ROUTE_NAME,
                  "    model: " + _yaml_str(default_model)]
    block.append(WB_END)
    lines = list(lines)
    if lines and lines[-1].strip():
        lines.append("")
    return (lines + block, True, "")


def _build_patch(text, port, switch_default=True):
    """返回 (new_text, 模型数, 隐藏数, note)；new_text=None 表示放弃（note 给原因）。"""
    entries, hidden = _dsh_model_entries()
    default_model = entries[0][0] if entries else ""
    nl = "\r\n" if "\r\n" in (text or "") else "\n"
    lines = _strip_managed(text or "").split(nl)
    if lines and lines[-1] == "":
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    if switch_default:
        lines, _changed, _found = _switch_default_model(lines, default_model)
    lines, ok, note = _merge_route(lines, port, default_model)
    if not ok:
        return None, len(entries), hidden, note
    out = nl.join(lines)
    if out and not out.endswith(nl):
        out += nl
    return out, len(entries), hidden, ""


def _port_in(text):
    m = _PORT_RE.search(text or "")
    return int(m.group(1)) if m else 0


# ===========================================================================
# 凭据（~/.dsh/.credentials.yaml 的 refs: 段）
# ===========================================================================
_CRED_TOP_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*:")
_CRED_ITEM_RE = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*:[ \t]*(.*)$")


def _cred_section(lines, key):
    """定位顶层 section：返回 (行号, 该行冒号后的内联值)；找不到返回 (None, None)。"""
    for i, line in enumerate(lines):
        m = _CRED_TOP_RE.match(line)
        if m and m.group(1) == key:
            return i, line[m.end():].strip()
    return None, None


def _cred_section_end(lines, start):
    for j in range(start + 1, len(lines)):
        if _CRED_TOP_RE.match(lines[j]):
            return j
    return len(lines)


def _cred_ref_get(text, name):
    """(是否存在, 旧值)；识别不了结构时返回 (False, None)。"""
    lines = (text or "").splitlines()
    refs_i, inline = _cred_section(lines, "refs")
    if refs_i is None or inline:
        return False, None
    end = _cred_section_end(lines, refs_i)
    for j in range(refs_i + 1, end):
        m = _CRED_ITEM_RE.match(lines[j])
        if m and m.group(1) == name:
            return True, _unquote(m.group(2))
    return False, None


def _cred_ref_upsert(text, name, value):
    """写入/更新 refs 里的一个键，返回 (new_text, had_key, old_value)。

    只认 DSH 文档记载的形态（`version` / `refs` / `records` 三个顶层 mapping，
    子项缩进两格）；识别不了返回 (None, False, None) —— 调用方必须放弃写入，
    绝不猜结构、绝不写出重复顶层键（重复键会被凭据存储直接拒绝加载）。
    """
    text = text or ""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        lines = ["version: 1"]
    elif _cred_section(lines, "version")[0] is None:
        return None, False, None
    refs_i, inline = _cred_section(lines, "refs")
    if refs_i is not None and inline:
        return None, False, None
    if refs_i is None:
        # 不自造空行：保持文件原有的分段排版 —— 这样删除时能逐字节回到原样
        at = _cred_section(lines, "version")[0] + 1
        lines[at:at] = ["refs:", "  %s: %s" % (name, _yaml_str(value))]
        return nl.join(lines) + nl, False, None
    end = _cred_section_end(lines, refs_i)
    for j in range(refs_i + 1, end):
        m = _CRED_ITEM_RE.match(lines[j])
        if m and m.group(1) == name:
            old = _unquote(m.group(2))
            lines[j] = "  %s: %s" % (name, _yaml_str(value))
            return nl.join(lines) + nl, True, old
    lines.insert(refs_i + 1, "  %s: %s" % (name, _yaml_str(value)))
    return nl.join(lines) + nl, False, None


def _cred_ref_remove(text, name):
    """删除 refs 里的一个键；该段因此变空则连段头一起删。返回新文本。"""
    text = text or ""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    if lines and lines[-1] == "":
        lines.pop()
    refs_i, inline = _cred_section(lines, "refs")
    if refs_i is None or inline:
        return text
    hit = None
    for j in range(refs_i + 1, _cred_section_end(lines, refs_i)):
        m = _CRED_ITEM_RE.match(lines[j])
        if m and m.group(1) == name:
            hit = j
            break
    if hit is None:
        return text
    del lines[hit]
    rest = [l for l in lines[refs_i + 1:_cred_section_end(lines, refs_i)] if l.strip()]
    if not rest:
        del lines[refs_i]
        if refs_i > 0 and not lines[refs_i - 1].strip():
            del lines[refs_i - 1]
    out = nl.join(lines)
    if out and not out.endswith(nl):
        out += nl
    return out


def _cred_is_empty(text):
    """只剩 `version:` 与空行/注释 —— 用于判断「这个凭据文件其实是我们建出来的空壳」。"""
    for line in (text or "").splitlines():
        m = _CRED_TOP_RE.match(line)
        if m:
            if m.group(1) != "version":
                return False
            continue
        s = line.strip()
        if s and not s.startswith("#"):
            return False
    return True


# ===========================================================================
# 备份 / 注入 / 同步 / 恢复
# ===========================================================================
def _dsh_backup():
    """备份「即将改动的最小面」：patch 原文 + refs 里那一个键的旧值。

    绝不整份复制 `.credentials.yaml`：它含 DSH 自己的真实凭据（登录 grant /
    platform token），复制出去既扩大暴露面，也会在恢复时用旧快照覆盖 DSH
    轮换后的新值。
    """
    if READONLY:
        return False
    profile = _dsh_profile_dir()
    if not profile:
        return False
    patch_path = _dsh_patch_path(profile)
    try:
        raw = _read_text(patch_path)
        cred_raw = _read_text(DSH_CREDENTIALS_PATH)
    except Exception as exc:
        _log("[WBRelay] DSH 配置读取失败，跳过接管: %s" % exc)
        return False
    old = _ide_read_backup("dsh")
    if ide_backup_exists("dsh"):
        return isinstance(old, dict) and bool(old.get("patch"))
    had, cred_old = (False, None)
    cred_ref_line = None
    if cred_raw:
        had, cred_old = _cred_ref_get(cred_raw, REF_NAME)
        if had:
            lines = cred_raw.splitlines()
            refs_i, _inline = _cred_section(lines, "refs")
            for line in lines[refs_i + 1:_cred_section_end(lines, refs_i)]:
                match = _CRED_ITEM_RE.match(line)
                if match and match.group(1) == REF_NAME:
                    cred_ref_line = line
                    break
    ok = _ide_write_backup("dsh", {
        "patch": patch_path,
        "patch_existed": os.path.isfile(patch_path),
        "raw": raw,
        "credentials": DSH_CREDENTIALS_PATH,
        "cred_existed": os.path.isfile(DSH_CREDENTIALS_PATH),
        "cred_ref_had": had,
        "cred_ref_old": cred_old,
        "cred_ref_line": cred_ref_line,
    })
    if not ok:
        _log("[WBRelay] DSH 备份未落盘（只读模式或写盘失败），跳过接管")
    return ok


def _dsh_inject(port):
    """把 WBRelay 写进 DSH：llm-pi-ai 路由 + agent-default-model + 凭据 ref。

    顺序：先写凭据（让引用先有值），再写 patch；任一步不能安全进行就整体放弃，
    且不写任何文件（半接管比不接管更难收拾）。
    """
    if READONLY:
        return False
    profile = _dsh_profile_dir()
    if not profile:
        _log("[WBRelay] DSH 未找到可接管的 profile（%s/*/cordis.patch.yml），跳过接管"
             % DSH_PROFILES)
        return False
    patch_path = _dsh_patch_path(profile)
    try:
        raw = _read_text(patch_path)
        cred_raw = _read_text(DSH_CREDENTIALS_PATH)
    except Exception as exc:
        _log("[WBRelay] DSH 读取失败，放弃接管（未写入任何文件）: %s" % exc)
        return False
    new, count, hidden, note = _build_patch(raw, port)
    if new is None:
        _log("[WBRelay] DSH 接管放弃（未写入任何文件）：%s" % note)
        return False
    cred_new, _had, _old = _cred_ref_upsert(cred_raw, REF_NAME, _wb_api_key())
    if cred_new is None:
        _log("[WBRelay] DSH 凭据文件（%s）结构无法安全识别，放弃接管（未写入任何文件）"
             % DSH_CREDENTIALS_PATH)
        return False
    validate_ide_text(new)
    validate_ide_text(cred_new)
    if cred_new != cred_raw and not _dsh_write(DSH_CREDENTIALS_PATH, cred_new):
        return False
    if new != raw and not _dsh_write(patch_path, new):
        return False
    if not _ide_record_applied("dsh", [patch_path, DSH_CREDENTIALS_PATH]):
        return False
    _log("[WBRelay] DSH 已指向 WBRelay: http://127.0.0.1:%d/v1"
         "（%d 个模型，隐藏 %d；api=%s）" % (port, count, hidden, API_KIND))
    return True


def _dsh_sync_catalog():
    """只刷新托管路由的 models（端口沿用现有托管块）。返回 (模型数, 隐藏数)。"""
    if READONLY:
        return (0, 0)
    profile = _dsh_profile_dir()
    if not profile:
        return (0, 0)
    patch_path = _dsh_patch_path(profile)
    try:
        raw = _read_text(patch_path)
    except Exception:
        return (0, 0)
    if not raw or WB_BEGIN not in raw:
        return (0, 0)                    # 未接管：不凭空写入
    port = _port_in(raw)
    if not port:
        return (0, 0)
    new, count, hidden, note = _build_patch(raw, port, switch_default=False)
    if new is None:
        _log("[WBRelay] DSH 模型清单同步放弃：%s" % note)
        return (0, hidden)
    if new != raw and not _dsh_write(patch_path, new):
        return (0, hidden)
    if new != raw and not _ide_record_applied("dsh", [patch_path, DSH_CREDENTIALS_PATH]):
        return (0, hidden)
    return (count, hidden)


def _restore_patch(bak):
    patch_path = bak.get("patch") or _dsh_patch_path()
    if not patch_path:
        return True
    if bak.get("patch_existed"):
        _read_text(patch_path)
        raw = bak.get("raw") or ""
        return bool(_dsh_write(patch_path, raw))
    if not os.path.isfile(patch_path):
        return True
    rest = _strip_managed(_read_text(patch_path))
    if rest.strip():
        if not rest.endswith("\n"):
            rest += "\n"
        return bool(_dsh_write(patch_path, rest))
    if READONLY:
        return False
    try:
        os.remove(patch_path)            # 原本来就没有这个文件：撤掉我们建出来的空壳
    except OSError as exc:
        _log("[WBRelay] DSH patch 清理失败: %s" % exc)
        return False
    return True


def _restore_credentials(bak):
    try:
        cred_raw = _read_text(DSH_CREDENTIALS_PATH)
    except Exception as exc:
        _log("[WBRelay] DSH 凭据读取失败: %s" % exc)
        return False
    if not cred_raw:
        return True
    old_ref = bak.get("cred_ref_old") or ""
    old_line = bak.get("cred_ref_line")
    if bak.get("cred_ref_had") and isinstance(old_line, str):
        # Save only the owned ref line, preserving its original quoting/comments.
        lines = cred_raw.splitlines(True)
        plain = cred_raw.splitlines()
        refs_i, inline = _cred_section(plain, "refs")
        if refs_i is None or inline:
            return False
        at = None
        for j in range(refs_i + 1, _cred_section_end(plain, refs_i)):
            match = _CRED_ITEM_RE.match(plain[j])
            if match and match.group(1) == REF_NAME:
                at = j
                break
        if at is None:
            return False
        ending = lines[at][len(plain[at]):]
        lines[at] = old_line + ending
        new = "".join(lines)
    elif bak.get("cred_ref_had") and old_ref:
        # 原本就有这个键：还原旧值（空值不算「有」，DSH 拒绝空字符串）
        new, _had, _old = _cred_ref_upsert(cred_raw, REF_NAME, old_ref)
        if new is None:
            return False
    else:
        new = _cred_ref_remove(cred_raw, REF_NAME)
        if not bak.get("cred_existed") and _cred_is_empty(new):
            if READONLY:
                return False
            try:
                os.remove(DSH_CREDENTIALS_PATH)
            except OSError as exc:
                _log("[WBRelay] DSH 凭据清理失败: %s" % exc)
                return False
            return True
    if new != cred_raw:
        return bool(_dsh_write(DSH_CREDENTIALS_PATH, new))
    return True


def _dsh_restore():
    """按备份还原：patch 逐字节回写 + refs 键删除/还原。"""
    bak = _ide_read_backup("dsh")
    if not isinstance(bak, dict):
        return False
    if READONLY:
        _log("[WBRelay] 只读模式：不恢复 DSH 配置")
        return False
    ok = True
    for label, fn in (("patch", _restore_patch), ("凭据引用", _restore_credentials)):
        try:
            if not fn(bak):
                ok = False
                _log("[WBRelay] DSH 恢复%s未完成" % label)
        except Exception as exc:
            ok = False
            _log("[WBRelay] DSH 恢复%s失败: %s" % (label, exc))
    if not ok:
        _log("[WBRelay] DSH 恢复未全部成功：保留备份 %s 以便重试" % ide_backup_path("dsh"))
        return False
    try:
        os.remove(ide_backup_path("dsh"))
    except Exception:
        pass
    _log("[WBRelay] DSH 已恢复原始配置")
    return True


# ===========================================================================
# 重启（只由看板显式点击触发；R5）
# ===========================================================================
def _dsh_exe():
    """找 DSH 桌面端 exe；找不到返回 ""。"""
    local_apps = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")
    if not os.path.isdir(local_apps):
        return ""
    try:
        names = sorted(os.listdir(local_apps))
    except OSError:
        return ""
    for d in names:
        dl = d.lower()
        if not ("dsh" in dl or "deepseek" in dl or "harness" in dl):
            continue
        for cand in ("DSH.exe", d + ".exe"):
            exe = os.path.join(local_apps, d, cand)
            if os.path.isfile(exe):
                return exe
    return ""


def restart_dsh():
    """重启 DSH 桌面端（Electron）：杀进程再拉起。返回 (killed, launched)。

    只由看板「↻ 重启」显式点击触发（R5 禁止自动启停外部进程）；
    找不到安装目录就什么都不碰。
    """
    exe = _dsh_exe()
    if not exe:
        _log("[WBRelay] 未找到 DSH 桌面端 exe，未重启任何进程")
        return "", False
    exe_dir = os.path.dirname(exe)
    ps = (
        "$dir = '" + exe_dir.replace("'", "''") + "'; "
        "$t = Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($dir) }; "
        "$k = @($t | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; "
        "$_.ProcessName }) -join ','; "
        "Start-Sleep -Milliseconds 800; "
        "Start-Process '" + exe.replace("'", "''") + "'; "
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
