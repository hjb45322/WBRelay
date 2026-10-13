# -*- coding: utf-8 -*-
"""统一脱敏 / 截断：日志、错误正文、接口回显的唯一出口。

为什么需要：上游错误正文、异常 `str(exc)`、URL、请求头都可能夹带
Bearer 令牌、供应商 API Key、admin token 或 URL 内嵌凭据。
「每条日志各自记得脱敏」不可靠，因此：

  · `wbrelay.log._log()` 在写盘前统一过一遍 `redact()`
  · HTTP 层把上游错误正文回显给客户端/写日志前也过一遍 `redact()`

只做**掩码 + 截断**，不做任何解码/还原；不会因为脱敏失败而抛异常。
"""
import re

DEFAULT_LIMIT = 400

# 顺序有意义：先掩码带前缀的凭据，再处理 URL userinfo 与 key=value 形式
_PATTERNS = (
    # Authorization: Bearer <token> / 裸 Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{6,}"), r"\1 ******"),
    # OpenAI 风格的 sk-/rk-/pk-/ak- 密钥
    (re.compile(r"\b(sk|rk|pk|ak)[-_][A-Za-z0-9._\-]{8,}"), r"\1-******"),
    # scheme://user:pass@host → scheme://******@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s:@]+@"), r"\1******@"),
    # scheme://user@host → scheme://host（去掉纯用户名）
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+@"), r"\1"),
    # key / token / password / secret 赋值（JSON、表单、命令行）
    (re.compile(r"(?i)([\"']?(?:api[_-]?key|apikey|admin[_-]?token|access[_-]?token|"
                r"refresh[_-]?token|token|password|passwd|pwd|secret|authorization)[\"']?"
                r"\s*[:=]\s*[\"']?)([^\s\"'&,;}\]]{4,})"), r"\1******"),
    # 中文口令字段
    (re.compile(r"(?i)(wb_password\s*[:=]\s*)(\S+)"), r"\1******"),
)


def redact(text, limit=DEFAULT_LIMIT):
    """脱敏 + 截断。limit=None 表示只脱敏不截断。"""
    if text is None:
        return ""
    try:
        out = str(text)
    except Exception:
        return "<unprintable>"
    for pattern, repl in _PATTERNS:
        try:
            out = pattern.sub(repl, out)
        except Exception:
            continue
    out = out.replace("\r", " ").replace("\n", " ")
    if limit is not None and len(out) > limit:
        out = out[:limit] + "…(已截断)"
    return out