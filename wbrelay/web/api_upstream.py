# -*- coding: utf-8 -*-
"""上游相关 API：面板代理（/proxy-wb）与只读状态 / 配置。

边界（所有者 2026-10-14 决定恢复「面板查看」，方案见 `docs/方案-上游服务面板恢复.md`）：
  · **只恢复查看**：`/proxy-wb` 把同源浏览器请求代理到本机回环上的上游面板，
    看板用 iframe 原样呈现它。这是「看」，不是「管」。
  · R6 **上游项目只读**：`/api/upstream/start` 与 `/api/upstream/stop` **仍未恢复**，
    请求它们只会拿到 409 + 策略说明，**不会启动/停止任何进程，也不会操作计划任务**。
  · R5 **外部进程只能由用户显式操作**：没有自动拉起、没有开机自启、没有守护线程。
    用户在上游自己的界面里点什么，是上游与应用户之间的事，**不经过也不依赖 WBRelay 的启停路径**。

面板代理的护栏（比历史实现更严，历史版取证见 `docs/方案-上游服务面板恢复.md` §2）：
  · 目标只允许 `http://127.0.0.1:<upstream_port()>`：端口由上游目录解析，
    **不接受任何外部/用户提供的目标**，重定向到别的 host 一律 502。
  · 子路径先过「路径穿越 / 绝对 URL / 编码绕过 / 反斜杠 / 畸形 query」判定，任何一项命中即 400。
  · 只转发 `Content-Type` 与 `X-Panel-Token` 两个头；Cookie / Authorization /
    Referer / Origin / Accept-Encoding 一律不转发，也**不启用 cookie jar**
    （历史版的全局登录态共享随之取消）。
  · 请求体与响应体都有上限：请求体超限**先拒后读**（413，不把超大 body 读进内存），
    响应体多读 1 字节判定（超限即 502）。
  · 超时 + `try/finally` 关闭句柄；失败一律脱敏后的 502，不回显上游原文。
  · `--readonly` 下只允许 GET / HEAD（只读浏览），写方法 403：WBRelay 侧零写入。
  · 只有 `text/html` 且路径为面板根时才做绝对路径改写，其余内容原样透传。
  · 设置开关 `upstream_proxy_enabled`（默认 true）：关掉时回落为 403 + 策略说明。
  · 浏览器跨站防护沿用 `_browser_gate`（`/proxy-wb` 在它的覆盖清单内）。
"""
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .. import __version__
from ..integrations.upstream import (EXTERNAL_CONTROL_SUPPORTED,
                                     UPSTREAM_AUTOSTART_SUPPORTED,
                                     UPSTREAM_HOST, WATCHDOG_SUPPORTED,
                                     autostart_registered,
                                     external_control_removed, upstream_dir,
                                     upstream_online, upstream_port,
                                     upstream_task_name, upstream_task_state)
from ..log import _log
from ..paths import READONLY
from ..redact import redact
from ..settings import load_settings, update_settings

# 上游目录路径长度上限（配置项，防止畸形超长值落盘）
MAX_UPSTREAM_DIR = 260
# 上游显示名上限（只看不改，超长截断）
MAX_LABEL = 60


def upstream_label():
    """上游服务的显示名：**只从运行时数据取，绝不硬编码任何上游标识**。

    为什么要这么绕：私有上游标识是发布扫描的绊线（`tools/release_identifiers.txt`
    里只存 sha256 摘要），把它写进看板源码会让发布门禁失败。所以名字的来源是：
      ① 上游目录里 README.md 的第一个标题（去掉 markdown 记号后截断）；
      ② 退回目录名（`os.path.basename`）。
    只读 + best-effort：目录没配、文件缺失、编码异常都返回 ""，不影响状态接口。
    """
    d = upstream_dir()
    if not d:
        return ""
    try:
        readme = os.path.join(d, "README.md")
        if os.path.isfile(readme):
            with open(readme, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(8192)
            for line in head.splitlines():
                s = line.strip()
                if not s.startswith("#"):
                    continue
                s = re.sub(r"[*_`\[\]]", "", s.lstrip("#").strip())
                s = re.split(r"\s+[—–-]\s+", s)[0].strip()
                if s:
                    return s[:MAX_LABEL]
    except Exception:
        pass
    try:
        return os.path.basename(os.path.normpath(d))[:MAX_LABEL]
    except Exception:
        return ""

POLICY_REMOVED = ("已移除：%s（规则 R5 —— 外部进程只能由用户显式操作）。"
                  "如需该外部服务的开机启动，请使用它自己的自启方式。")
# R6：上游项目只读 —— 启停能力仍未恢复（不是“手动才能用”，而是根本不提供）
POLICY_CONTROL_REMOVED = external_control_removed("启停（启动 / 停止）")

# 面板代理能力开关：常量保留给测试与状态接口引用；运行时以 settings.upstream_proxy_enabled 为准
PROXY_ENABLED = True
PROXY_PREFIX = "/proxy-wb"
POLICY_PROXY_DISABLED = (
    "已停用：/proxy-wb 面板代理（规则 R6 —— 上游项目只读、不写、不改、不启停）。"
    "面板代理只同源呈现上游自己的界面；如需彻底关闭它，请在设置页关掉「面板内嵌查看」。"
    "上游服务的启停 / 开机自启 / 进程守护均未恢复，请在该服务自身完成。")

# 面板代理的体积 / 超时上限（超限即拒，不整体读进内存）
PROXY_MAX_REQUEST_BYTES = 1024 * 1024        # 请求体上限：1MB（先拒后读）
PROXY_MAX_RESPONSE_BYTES = 8 * 1024 * 1024   # 响应体上限：8MB（多读 1 字节判定）
PROXY_TIMEOUT_SEC = 15
PROXY_USER_AGENT = "WBRelay/%s" % __version__
# 只转发这两个头；其余（Cookie / Authorization / Referer / Origin / Accept-Encoding …）一律不转发
PROXY_FORWARD_HEADERS = ("Content-Type", "X-Panel-Token")
# 只读模式下允许的方法：只读 = 只浏览，不产生任何写动作
PROXY_READONLY_METHODS = ("GET", "HEAD")
# query 允许的字符集（其余一律拒绝：防 URL / 请求行注入与畸形值）
_QUERY_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                  "0123456789-._~%&=+?,;:/@!*'()[]$")


def proxy_enabled():
    """面板代理开关：settings.upstream_proxy_enabled，默认 true。"""
    return bool(load_settings().get("upstream_proxy_enabled", True))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝跟随重定向：面板代理的目标必须是本机回环，不允许被带去别的 host。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, "面板代理不允许重定向（只呈现本机上游）", headers, fp)


_opener = urllib.request.build_opener(_NoRedirect())


def _proxy_target_path(raw_path):
    """把 `/proxy-wb<sub>?<query>` 解析成上游子路径；不合法返回 (None, 错误文案)。

    判定都在这里做完，调用方只负责拼 `127.0.0.1:<port>`：
      ① 必须以 `/` 开头（`/proxy-wb` 本身按 `/` 处理）；
      ② 不允许 `..` 段（逐段判定，不靠字符串包含，避免 `....//` 之类绕过）；
      ③ 不允许绝对 URL / scheme 前缀（不接受外部目标）；
      ④ 不允许反斜杠与百分号编码的斜杠、点（防大小写 / 编码绕过）；
      ⑤ query 只允许安全字符（防 URL / 请求行注入）。
    """
    try:
        parsed = urlsplit(raw_path)
    except ValueError:
        return None, "面板代理路径畸形"
    path_only = parsed.path or "/"
    sub = path_only[len(PROXY_PREFIX):] or "/"
    if not sub.startswith("/"):
        return None, "面板代理路径必须从 / 开始"
    if "\\" in sub:
        return None, "面板代理路径不允许反斜杠"
    if "://" in sub or "//" in sub:
        return None, "面板代理路径不允许外部绝对地址"
    low = sub.lower()
    if "%2f" in low or "%2e" in low or "%5c" in low:
        return None, "面板代理路径不允许编码的斜杠 / 点"
    for seg in sub.split("/"):
        if seg == "..":
            return None, "面板代理路径不允许 .."
    query = parsed.query or ""
    if query:
        bad = sorted({c for c in query if c not in _QUERY_SAFE})
        if bad:
            return None, "面板代理 query 含非法字符"
        sub = sub + "?" + query
    return sub, ""


class ApiUpstreamMixin:
    # ---- /proxy-wb（面板代理：同源呈现本机上游面板）----
    def _proxy_text_response(self, code, message):
        """纯文本响应（带 Content-Length；错误信息先脱敏 + 截断）。"""
        body = redact(message, 400).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_wb(self, path):
        """面板代理：把同源浏览器请求转发到本机上游面板（只读呈现，不启停任何进程）。

        任何被拒的路径都在**建立连接之前**返回，因此拒绝时上游一个请求都收不到。
        `path` 是 `urlparse(self.path).path`；query 从 `self.path` 原样保留。
        """
        if not load_settings().get("upstream_enabled", True):
            return self._err(404, "上游服务管理已关闭")
        if not proxy_enabled():
            return self._proxy_text_response(403, POLICY_PROXY_DISABLED)
        if READONLY and self.command not in PROXY_READONLY_METHODS:
            return self._proxy_text_response(
                403, "只读模式：面板代理仅支持 GET / HEAD（--readonly 下零写入）")

        sub, err = _proxy_target_path(self.path)
        if err:
            return self._proxy_text_response(400, "拒绝：%s" % err)

        port = upstream_port()
        # 目标只可能是本机回环 + 解析出来的端口；不接受任何外部/用户提供的目标
        url = "http://%s:%d%s" % (UPSTREAM_HOST, port, sub)

        # 请求体：先看长度再决定读不读（超限直接 413，不把超大 body 读进内存）
        body_data = None
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return self._proxy_text_response(400, "拒绝：Content-Length 非法")
            if length < 0:
                return self._proxy_text_response(400, "拒绝：Content-Length 非法")
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                return self._proxy_text_response(413, "拒绝：面板代理不支持分块请求体")
            if length > PROXY_MAX_REQUEST_BYTES:
                return self._proxy_text_response(
                    413, "拒绝：面板代理请求体超过上限 %d 字节" % PROXY_MAX_REQUEST_BYTES)
            if length > 0:
                body_data = self.rfile.read(length)

        # 请求头白名单：只转发 Content-Type 与 X-Panel-Token
        headers = {"User-Agent": PROXY_USER_AGENT}
        for name in PROXY_FORWARD_HEADERS:
            value = self.headers.get(name)
            if value:
                headers[name] = value

        resp = None
        try:
            req = urllib.request.Request(url, data=body_data, headers=headers,
                                         method=self.command)
            resp = _opener.open(req, timeout=PROXY_TIMEOUT_SEC)
            data = resp.read(PROXY_MAX_RESPONSE_BYTES + 1)
            if len(data) > PROXY_MAX_RESPONSE_BYTES:
                return self._proxy_text_response(
                    502, "面板代理失败：上游响应体超过上限 %d 字节" % PROXY_MAX_RESPONSE_BYTES)
            ctype = resp.headers.get("Content-Type", "text/html")
            status = resp.status
            # 只有面板根 HTML 才改写绝对路径，让面板里的请求继续走同源代理
            if "text/html" in (ctype or "").lower() and sub.split("?")[0] == "/":
                data = self._rewrite_panel_html(data)
        except Exception as exc:
            # 不回显上游原文（可能含凭据 / 内部路径），统一脱敏 + 截断
            _log_proxy_failure(url, exc)
            return self._proxy_text_response(502, "面板代理失败：%s" % redact(exc, 200))
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

        self.send_response(status)
        self.send_header("Content-Type", ctype or "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _rewrite_panel_html(data):
        """面板根 HTML：把绝对路径改写成代理路径（其余内容原样透传）。"""
        try:
            html = data.decode("utf-8", "replace")
            for a, b in (("getURL('/", "getURL('/proxy-wb/"),
                          ('getURL("/', 'getURL("/proxy-wb/'),
                          ("getJSON('/", "getJSON('/proxy-wb/"),
                          ('getJSON("/', 'getJSON("/proxy-wb/'),
                          ("postJSON('/", "postJSON('/proxy-wb/"),
                          ('postJSON("/', 'postJSON("/proxy-wb/'),
                          ("fetch('/", "fetch('/proxy-wb/"),
                          ('fetch("/', 'fetch("/proxy-wb/'),
                          ('src="/', 'src="/proxy-wb/'),
                          ('href="/', 'href="/proxy-wb/')):
                html = html.replace(a, b)
            return html.encode("utf-8")
        except Exception:
            return data

    # ---- 状态 / 配置 ----
    def _api_upstream_status(self):
        cfg = load_settings()
        port = upstream_port()
        d = upstream_dir()
        entry = os.path.join(d, "wb_proxy.py") if d else ""
        proxy_on = proxy_enabled()
        return self._json(200, {
            "enabled": bool(cfg.get("upstream_enabled", True)),
            "online": upstream_online(),
            "task": upstream_task_state(),
            "url": "http://%s:%d" % (UPSTREAM_HOST, port),
            "port": port,
            "task_name": upstream_task_name(),
            "upstream_dir": d,
            "entry_found": bool(d and os.path.isfile(entry)),
            # R6：上游项目只读 —— 启停能力仍未恢复（看板不渲染启停按钮）
            "control_supported": EXTERNAL_CONTROL_SUPPORTED,
            "control_policy": POLICY_CONTROL_REMOVED,
            # 面板代理：默认可用；关掉时给策略说明（看板改为提示，不渲染 iframe）
            "label": upstream_label(),
            "proxy_enabled": proxy_on,
            "proxy_url": (PROXY_PREFIX + "/") if proxy_on else "",
            "proxy_policy": "" if proxy_on else POLICY_PROXY_DISABLED,
            # R5：开机自启 / 进程守护仍未恢复（看板开关置灰并说明原因）
            "autostart": False,
            "watchdog": False,
            "autostart_supported": UPSTREAM_AUTOSTART_SUPPORTED,
            "watchdog_supported": WATCHDOG_SUPPORTED,
            "autostart_policy": POLICY_REMOVED % "外部进程开机自启",
            "watchdog_policy": POLICY_REMOVED % "进程守护自动拉起",
            "wb_autologin": bool(cfg.get("wb_autologin")),
            "wb_password_set": bool(cfg.get("wb_password")),
            "autostart_registered": autostart_registered(),
        })

    def _api_upstream_settings(self):
        data = self._payload() or {}
        if not isinstance(data, dict):
            return self._err(400, "bad json")
        # R5 门禁：请求「仍未恢复的能力」时给明确错误，不静默忽略
        if data.get("autostart"):
            return self._err(409, POLICY_REMOVED % "外部进程开机自启", "invalid_request_error")
        if data.get("watchdog"):
            return self._err(409, POLICY_REMOVED % "进程守护自动拉起", "invalid_request_error")
        # 目录/路径校验放在锁外（纯校验，不碰配置）。
        # 注意：局部名不要叫 `upstream_dir` —— 那会遮蔽本模块从 integrations.upstream
        # 导入的同名函数（响应体还要用它回显当前目录）。
        want_dir = None
        if "dir" in data:
            d = str(data.get("dir") or "").strip()
            if len(d) > MAX_UPSTREAM_DIR:
                return self._err(400, "目录路径过长（上限 %d 字符）" % MAX_UPSTREAM_DIR)
            if d and not os.path.isdir(d):
                return self._err(400, "目录不存在: %s" % d)
            want_dir = d

        # 持锁 read-modify-write：与并发 POST /api/settings、/api/keys 等互不覆盖
        def _apply(cfg):
            if want_dir is not None:
                if want_dir:
                    cfg["upstream_dir"] = want_dir
                else:
                    cfg.pop("upstream_dir", None)
            if "enabled" in data:
                cfg["upstream_enabled"] = bool(data["enabled"])
            if "proxy_enabled" in data:
                cfg["upstream_proxy_enabled"] = bool(data["proxy_enabled"])
            if "autostart" in data:
                cfg["upstream_autostart"] = False     # 只可能显式传 false（true 已在上面拒绝）
            if "watchdog" in data:
                cfg["upstream_watchdog"] = False
            if "wb_autologin" in data:
                cfg["wb_autologin"] = bool(data["wb_autologin"])
            if "wb_password" in data:
                # 安全（AGENTS.md §五，不因面板恢复而放宽）：上游面板口令**永不落盘**。
                # 收到即丢弃，并清理历史遗留值 —— 口令只由浏览器端 localStorage
                # （key `wb_upstream_pwd`）保管，自动登录时由前端填进同源 iframe。
                # 日志、错误回显、状态响应里只出现 `wb_password_set` 布尔，绝不出现口令本身。
                cfg.pop("wb_password", None)
            return True

        ok, _result = update_settings(_apply)
        if not ok:
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        return self._json(200, {
            "ok": True,
            "reg_error": "",
            "autostart_supported": UPSTREAM_AUTOSTART_SUPPORTED,
            "watchdog_supported": WATCHDOG_SUPPORTED,
            "policy_notice": POLICY_REMOVED % "外部进程开机自启 / 进程守护",
            "control_supported": EXTERNAL_CONTROL_SUPPORTED,
            "proxy_enabled": proxy_enabled(),
            "proxy_url": PROXY_PREFIX + "/",
            "autostart_registered": autostart_registered(),
            "upstream_dir": upstream_dir(),
        })

    def _api_upstream_setdir(self):
        data = self._payload() or {}
        if not isinstance(data, dict):
            return self._err(400, "bad json")
        d = str(data.get("dir") or "").strip()
        if len(d) > MAX_UPSTREAM_DIR:
            return self._err(400, "目录路径过长（上限 %d 字符）" % MAX_UPSTREAM_DIR)
        if d and not os.path.isdir(d):
            return self._err(400, "目录不存在: %s" % d)

        # 持锁 read-modify-write：只改 upstream_dir，不动其它字段
        def _apply(cfg):
            if d:
                cfg["upstream_dir"] = d
            else:
                cfg.pop("upstream_dir", None)
            return True

        ok, _result = update_settings(_apply)
        if not ok:
            return self._err(503, "配置未保存：只读模式或写盘失败（原配置保持不变）",
                             "server_error")
        return self._json(200, {"ok": True, "upstream_dir": upstream_dir()})

    # ---- 启停能力仍未恢复（R6：上游项目只读，不写不改不启停）----
    def _api_upstream_control_removed(self):
        """旧的 /api/upstream/start|stop 入口：不执行任何操作，只回策略错误。"""
        return self._err(409, POLICY_CONTROL_REMOVED, "invalid_request_error")


def _log_proxy_failure(url, exc):
    """面板代理失败写一条日志：只记目标 host:port 与脱敏后的原因，不回显上游原文。"""
    try:
        parts = urlsplit(url)
        _log("[WBRelay] 面板代理失败 %s:%s —— %s"
             % (parts.hostname, parts.port or "", redact(exc, 200)))
    except Exception:
        pass
