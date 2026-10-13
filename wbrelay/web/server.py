# -*- coding: utf-8 -*-
"""HTTP 骨架：Handler 主类（helpers + HTTP 方法路由），聚合各 API 分组 mixin。"""
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .. import state
from ..paths import DASHBOARD_PATH, MAX_PAYLOAD, READONLY
from ..settings import admin_token_ok, auth_key_ok

from .api_admin import ApiAdminMixin
from .api_state import ApiStateMixin
from .api_upstream import ApiUpstreamMixin
from .api_usage import ApiUsageMixin
from .api_v1 import ApiV1Mixin


# /v1/* 入口：默认无鉴权（本机单用户设计的一部分，见 SECURITY.md），
# 但仍需回环 Host / 同源 Origin 门禁 —— 浏览器里的任意页面都能对本机 127.0.0.1 发跨站请求。
V1_MODELS_PATHS = ("/v1/models", "/models")
V1_CHAT_PATHS = ("/v1/chat/completions", "/chat/completions", "/v1/responses", "/responses")
V1_INDEX_PATHS = ("/v1", "/v1/")
V1_PATHS = V1_MODELS_PATHS + V1_CHAT_PATHS + V1_INDEX_PATHS


class Handler(ApiV1Mixin, ApiUpstreamMixin, ApiUsageMixin, ApiAdminMixin,
              ApiStateMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---- helpers ----
    def _json(self, code, obj, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, message, etype="invalid_request_error"):
        self._json(code, {"error": {"message": message, "type": etype, "code": code}})

    def _payload(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > MAX_PAYLOAD:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None

    def _bearer(self):
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return ""

    def _api_ok(self):
        return auth_key_ok(self._bearer())

    def _admin_ok(self):
        # 管理 API 可选令牌（默认关）；开启后需 X-Admin-Token 头
        return admin_token_ok(self.headers.get("X-Admin-Token") or "")

    # ---- 本机门禁（Host / Origin）----
    # 威胁模型：默认无鉴权是已核准的本机单用户设计，但浏览器里的任意页面都可能
    # 对 127.0.0.1 发跨站请求（CSRF），DNS rebinding 还能用攻击者域名指向本机。
    # 因此对浏览器可直面的 /api/* 与 /proxy-wb 做两道门禁（详见 SECURITY.md）：
    #   ① Host 必须是回环地址 + 当前端口  ② 带 Origin 时必须同源
    _LOCAL_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")

    def _host_ok(self):
        all_hosts = self.headers.get_all("Host") or []
        if len(all_hosts) > 1:
            return False                     # 重复 Host 头：拒绝（避免代理/走私类歧义）
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True                      # HTTP/1.0 无 Host：本机命令行客户端场景
        if host.startswith("["):
            name, _, port = host.partition("]")
            name += "]"
            port = port.lstrip(":")
        else:
            name, _, port = host.partition(":")
        if name not in self._LOCAL_HOSTNAMES:
            return False
        if port and port != str(state._SERVER_PORT):
            return False
        return True

    def _origin_ok(self):
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True                      # curl / 服务端到服务端：无 Origin
        if origin.lower() == "null":
            return False                     # 不透明来源（file://、沙箱 iframe）
        allowed = ("http://127.0.0.1:%d", "http://localhost:%d", "http://[::1]:%d")
        return origin.lower().rstrip("/") in tuple(o % state._SERVER_PORT for o in allowed)

    def _browser_gate(self, path):
        """返回错误文本表示被拦；None 表示放行。

        覆盖浏览器可直接触达的敏感面：管理 API、外部面板代理
        （含 /api/clipboard —— 本机剪贴板内容不得被跨站页面读取）与 /v1/*。

        /v1/* 默认无鉴权（Codex / CLI 开箱可用是本机单用户设计的一部分，见 SECURITY.md），
        所以这里**不引入鉴权**，只要求回环 Host；带 Origin 时必须同源。
        无 Host / 无 Origin 的 CLI 请求照常放行。
        """
        if path.startswith("/api/") or path.startswith("/proxy-wb"):
            if not self._host_ok():
                return "拒绝：Host 不是本机回环地址（防 DNS rebinding / 跨站访问）"
            if not self._origin_ok():
                return "拒绝：跨站来源（Origin 校验未通过）"
            return None
        if path in V1_PATHS:
            if not self._host_ok():
                return "拒绝：Host 不是本机回环地址（/v1 默认无鉴权，必须限制来源）"
            if not self._origin_ok():
                return "拒绝：跨站来源（/v1 默认无鉴权，必须限制来源）"
            return None
        return None

    # ---- HTTP methods ----
    def do_GET(self):
        path = urlparse(self.path).path
        # 容忍尾斜杠：`/v1/models/`、`/v1/` 这类写法不少客户端会拼出来（2026-10-14）
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        blocked = self._browser_gate(path)
        if blocked:
            return self._err(403, blocked)
        if path.startswith("/api/") and not self._admin_ok():
            return self._err(401, "admin token required")
        if path == "/":
            try:
                with open(DASHBOARD_PATH, "rb") as fh:
                    body = fh.read()
            except Exception as exc:
                return self._err(500, "dashboard.html unavailable: %s" % exc)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in V1_INDEX_PATHS:
            if not self._api_ok():
                return self._err(401, "invalid api key")
            return self._v1_index()
        if path in V1_MODELS_PATHS:
            if not self._api_ok():
                return self._err(401, "invalid api key")
            return self._v1_models()
        if path == "/api/keys/current":
            # 明文只在这一条只读端点上出现；开启管理令牌时同样要带令牌
            if not self._admin_ok():
                return self._err(401, "admin token required")
            return self._api_key_current()
        if path == "/api/state":
            return self._api_state()
        if path == "/api/ides":
            return self._api_ides()
        if path == "/api/models":
            return self._api_models()
        if path == "/api/usage":
            return self._api_usage()
        if path == "/api/clipboard":
            return self._api_clipboard()
        if path == "/api/settings":
            return self._api_settings()
        if path == "/api/backups":
            return self._api_backups()
        if path == "/api/profiles":
            return self._api_profiles()
        if path == "/api/upstream/status":
            return self._api_upstream_status()
        if path.startswith("/proxy-wb"):
            return self._proxy_wb(path)
        if path == "/api/upstream/settings":
            return self._api_upstream_settings()
        if path == "/api/upstream/setdir":
            return self._api_upstream_setdir()
        return self._err(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        blocked = self._browser_gate(path)
        if blocked:
            return self._err(403, blocked)
        if path.startswith("/api/") and READONLY:
            return self._err(403, "readonly mode: 管理接口已禁用（--readonly）")
        if path.startswith("/api/") and not self._admin_ok():
            return self._err(401, "admin token required")
        if path == "/api/suppliers":
            return self._save_suppliers()
        if path == "/api/suppliers/clone":
            return self._clone_supplier()
        if path == "/api/suppliers/ping":
            return self._api_supplier_ping()
        if path == "/api/profiles":
            return self._api_profiles()
        if path == "/api/profiles/import":
            return self._api_profiles_import()
        if path == "/api/profiles/auto":
            return self._api_profiles_auto()
        if path == "/api/codex/sync":
            return self._api_codex_sync()
        if path.startswith("/api/ides/"):
            parts = path.split("/")
            # /api/ides/{id}/toggle 或 /api/ides/{id}/restart
            if len(parts) == 5 and parts[4] in ("toggle", "restart"):
                iid = parts[3]
                if parts[4] == "toggle":
                    return self._api_ide_toggle(iid)
                return self._api_ide_restart(iid)
            return self._err(404, "not found")
        if path == "/api/keys":
            return self._save_keys()
        if path == "/api/models/enabled":
            return self._save_models_enabled()
        if path == "/api/settings":
            return self._save_settings()
        if path == "/api/backup":
            return self._api_backup_make()
        if path == "/api/restore":
            return self._api_restore()
        # R6：外部上游启停接口已移除 —— 只回策略错误，不启动/停止任何进程，
        # 也不操作计划任务（保留路径以便旧看板拿到明确拒绝而不是 404）
        if path in ("/api/upstream/start", "/api/upstream/stop"):
            return self._api_upstream_control_removed()
        if path == "/api/upstream/setdir":
            return self._api_upstream_setdir()
        if path == "/api/upstream/settings":
            return self._api_upstream_settings()
        if path.startswith("/proxy-wb"):
            return self._proxy_wb(path)
        if path in V1_CHAT_PATHS:
            if not self._api_ok():
                return self._err(401, "invalid api key")
            payload = self._payload()
            if payload is None:
                return self._err(400, "invalid json")
            return self._dispatch(path, payload)
        return self._err(404, "not found")

    def do_OPTIONS(self):
        # CORS 收紧：只对同源 Origin 返回 CORS 头，其他 Origin 不返回
        # （浏览器会拦截跨源读响应；但同源看板页面不需要 CORS，因为它就是从本服务加载的）。
        # 无 Origin 的请求（curl/服务端到服务端）直接放行 204。
        origin = (self.headers.get("Origin") or "").strip()
        self.send_response(204)
        allow_cors = False
        if not origin:
            allow_cors = True
        else:
            o = origin.lower().rstrip("/")
            if o in ("http://127.0.0.1:%d" % state._SERVER_PORT,
                     "http://localhost:%d" % state._SERVER_PORT):
                allow_cors = True
        if allow_cors:
            self.send_header("Access-Control-Allow-Origin", origin if origin else "null")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()
