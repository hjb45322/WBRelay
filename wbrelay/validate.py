# -*- coding: utf-8 -*-
"""入参边界校验（标准库；HTTP 层与存储层共用）。

为什么单独收口：审查结论里「设置端口、超长模型名、供应商数量/字符串长度、
用量分页及时间范围」都缺少边界校验。把上限集中在这里，写路径统一调用，
避免每个接口各写一套、互相漂移。

约定：所有 check_* 返回 (值, 错误信息)；错误信息为空串表示通过。
"""
import re
from urllib.parse import urlsplit

# ---- 供应商 ----
MAX_SUPPLIERS = 50
MAX_SUPPLIER_ID = 64
MAX_SUPPLIER_NAME = 80
MAX_BASE_URL = 2048
MAX_API_KEY = 512
MAX_MODELS_PER_SUPPLIER = 500
MAX_MODEL_PATTERN = 120
# ---- 路由 / 请求 ----
MAX_MODEL_NAME = 200
# ---- 其它 ----
MAX_TASK_NAME = 120
MIN_PORT = 1
MAX_PORT = 65535

_SUPPLIER_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,%d}$" % MAX_SUPPLIER_ID)
_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9 ._\-]{1,%d}$" % MAX_TASK_NAME)
_AUMID_RE = re.compile(r"^[A-Za-z0-9._!\-]{1,120}$")


def check_port(value):
    """端口：1~65535 的整数。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 0, "端口必须是整数"
    if not (MIN_PORT <= port <= MAX_PORT):
        return 0, "端口必须在 %d~%d 之间" % (MIN_PORT, MAX_PORT)
    return port, ""


def check_model_name(model):
    """请求里的模型名：长度受限（超长 / 畸形值不该进入路由与用量记录）。"""
    name = str(model or "")
    if len(name) > MAX_MODEL_NAME:
        return "", "模型名过长（上限 %d 字符）" % MAX_MODEL_NAME
    return name, ""


def check_base_url(url):
    """供应商 base_url：http(s)、长度受限、**不允许内嵌用户名/密码**。"""
    raw = str(url or "").strip()
    if not raw:
        return "", "supplier base_url is required"
    if len(raw) > MAX_BASE_URL:
        return "", "base_url 过长（上限 %d 字符）" % MAX_BASE_URL
    if not raw.startswith(("http://", "https://")):
        return "", "supplier base_url must start with http:// or https://"
    parts = urlsplit(raw)
    if parts.username or parts.password:
        return "", "base_url 不允许内嵌用户名/密码（凭据请填 API Key）"
    if not parts.hostname:
        return "", "base_url 缺少主机名"
    return raw, ""


def has_url_credentials(url):
    """URL 里是否内嵌了用户名/密码（存储层防御性判定）。"""
    try:
        parts = urlsplit(str(url or ""))
        return bool(parts.username or parts.password)
    except Exception:
        return False


def check_supplier_id(sid):
    sid = str(sid or "").strip()
    if sid and not _SUPPLIER_ID_RE.match(sid):
        return "", "供应商 id 只允许字母/数字/._-，且不超过 %d 字符" % MAX_SUPPLIER_ID
    return sid, ""


def check_text(value, limit, field):
    """通用长度校验：返回截断前后的原文（超长直接报错，不静默截断）。"""
    text = str(value if value is not None else "")
    if len(text) > limit:
        return "", "%s 过长（上限 %d 字符）" % (field, limit)
    return text, ""


def check_models(models):
    """供应商 models 清单：条数与单条长度受限。"""
    items = list(models or [])
    if len(items) > MAX_MODELS_PER_SUPPLIER:
        return [], "模型清单条目过多（上限 %d）" % MAX_MODELS_PER_SUPPLIER
    out = []
    for m in items:
        text = str(m or "").strip()
        if not text:
            continue
        if len(text) > MAX_MODEL_PATTERN:
            return [], "模型名/通配符过长（上限 %d 字符）" % MAX_MODEL_PATTERN
        out.append(text)
    return out, ""


def check_supplier_count(items):
    if len(list(items or [])) > MAX_SUPPLIERS:
        return "供应商数量过多（上限 %d）" % MAX_SUPPLIERS
    return ""


def check_task_name(name):
    """计划任务名：只允许安全字符（禁止拼进 PowerShell 造成注入）。"""
    text = str(name or "").strip()
    if not text:
        return "", ""
    if not _TASK_NAME_RE.match(text):
        return "", "计划任务名只允许字母/数字/空格/._-，且不超过 %d 字符" % MAX_TASK_NAME
    return text, ""


def check_aumid(value):
    """Codex AUMID：只允许安全字符；不合法则返回空串（调用方回落默认值）。"""
    text = str(value or "").strip()
    return text if _AUMID_RE.match(text) else ""


def check_time_range(since, until):
    """自定义时间范围：必须为正数、且 since <= until（超范围一律视为无效）。"""
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    s, u = _num(since), _num(until)
    if s is not None and (s < 0 or s != s):
        return None
    if u is not None and (u < 0 or u != u):
        return None
    if s is not None and u is not None and s > u:
        return None
    return (s, u)