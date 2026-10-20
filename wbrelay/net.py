# -*- coding: utf-8 -*-
"""网络层：系统代理探测、带代理回退的 urlopen、OpenRouter 档案同步。"""
import atexit
import http.client
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import winreg

from .state import _lock
from .log import _log
from .redact import redact
from .settings import load_settings
from .localpool import (ACQUIRE_WAIT, LocalConnectError, LocalReadyPool,
                         PooledHTTPResponse)
from . import profiles

_wb_cookiejar = None  # 全局上游服务代理 cookie jar，保持登录态


OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
_openrouter_cache = {"fetched_at": 0.0, "profiles": {}}
AUTO_PROFILE_TTL = 24 * 3600   # 源缓存：24h 拉一次
OPENROUTER_TIMEOUT_SEC = 30

# 上游 JSON 响应体上限（/models、OpenRouter 档案同步、用户提交的 catalog URL 导入统一走这里）
MAX_UPSTREAM_JSON_BYTES = 8 * 1024 * 1024


class ResponseTooLarge(Exception):
    """上游响应体超过上限（多读 1 字节判定，不整体读进内存）。"""


def close_quietly(resp):
    """尽力关闭响应句柄（None / 已关闭 / 抛异常都不会再抛）。"""
    try:
        if resp is not None:
            resp.close()
    except Exception:
        pass


def read_limited(resp, limit=MAX_UPSTREAM_JSON_BYTES):
    """按上限读取响应体；超限抛 ResponseTooLarge。
    调用方负责关闭响应句柄（用 with 包住 _urlopen 的返回值）。"""
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise ResponseTooLarge("上游响应体超过上限 %d 字节" % limit)
    return data


_proxy_cache = {"at": 0.0, "value": None}
PROXY_CACHE_TTL = 30.0   # 30 秒缓存，避免每个请求都打开注册表


def _system_proxy():
    """读 Windows 系统代理设置（注册表 ProxyEnable/ProxyServer），返回 {http,https} 或 None。30 秒缓存。"""
    now = time.time()
    if now - _proxy_cache["at"] < PROXY_CACHE_TTL:
        return _proxy_cache["value"]
    result = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
        if not (enable and server):
            result = None
        else:
            server = server.strip()
            if "=" in server:   # 形如 "http=127.0.0.1:7890;https=127.0.0.1:7890"
                pairs = {}
                for part in server.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        pairs[k.strip().lower()] = v.strip()
                h = pairs.get("https") or pairs.get("http")
                result = {"http": h, "https": h} if h else None
            else:
                if "://" not in server:
                    server = "http://" + server
                result = {"http": server, "https": server}
    except Exception:
        result = None
    _proxy_cache["at"] = now
    _proxy_cache["value"] = result
    return result


def _urlopen(req, timeout):
    """带系统代理的 urlopen。本地地址（127.0.0.1/localhost）直连不走代理，其他走系统代理。"""
    host = urllib.request.urlparse(req.full_url).hostname or ""
    is_local = host in ("127.0.0.1", "localhost", "::1")
    if not is_local:
        prx = _system_proxy()
        if prx:
            try:
                return urllib.request.build_opener(urllib.request.ProxyHandler(prx)).open(req, timeout=timeout)
            except Exception as exc:
                _log("[WBRelay] 系统代理 %s 请求 %s 失败，回退直连: %s" % (prx, host, exc))
    return urllib.request.urlopen(req, timeout=timeout)


def _local_origin(url):
    """返回本地上游的 (scheme, host, port)，非本地/非法 URL 返回 None。"""
    try:
        parts = urlsplit(str(url or ""))
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if scheme not in ("http", "https"):
            return None
        if host not in ("127.0.0.1", "localhost", "::1"):
            return None
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, host, int(port)
    except (TypeError, ValueError):
        return None


_LOCAL_POOLS = {}
_LOCAL_POOL_ORIGINS = set()
_LOCAL_POOLS_LOCK = threading.Lock()


def _make_local_connector(origin):
    scheme, host, port = origin

    def connect(timeout):
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=timeout)
        conn.connect()
        return conn

    return connect


def _local_pool(origin):
    with _LOCAL_POOLS_LOCK:
        pool = _LOCAL_POOLS.get(origin)
        if pool is None:
            pool = LocalReadyPool(
                _make_local_connector(origin),
                name="%s://%s:%d" % origin,
            )
            _LOCAL_POOLS[origin] = pool
        return pool


def _mark_local_poolable(origin, headers):
    """只有已知支持并发 idle 连接的本地上游才启用 ready 队列。

    单线程 HTTPServer 会在第一个 idle TCP 连接上阻塞；无条件预热会把它饿死。
    支持并发的本地上游会明确发送 ``Server: wb-proxy/...``，因此以该能力标记作为
    开关；未知本地服务继续走原来的 _urlopen 路径。
    """
    try:
        server = str(headers.get("Server") or "").lower()
    except Exception:
        server = ""
    if "wb-proxy" not in server:
        return False
    with _LOCAL_POOLS_LOCK:
        _LOCAL_POOL_ORIGINS.add(origin)
    # 探测成功就立即开始预热；不能等下一次请求才创建队列，否则
    # “探测后到下一请求前”的空窗仍然没有 ready 连接可用。
    try:
        _local_pool(origin)
    except Exception:
        # 预热线程创建失败不应影响本次已经成功的上游请求；下一次
        # 请求会重新尝试创建队列。
        pass
    return True


def _close_local_pools():
    with _LOCAL_POOLS_LOCK:
        pools = list(_LOCAL_POOLS.values())
        _LOCAL_POOLS.clear()
        _LOCAL_POOL_ORIGINS.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


atexit.register(_close_local_pools)


def _open_local_request(req, timeout, origin):
    """走本地上游 ready 队列，返回 PooledHTTPResponse。"""
    parts = urlsplit(req.full_url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    pool = _local_pool(origin)
    try:
        conn = pool.acquire(min(float(timeout), ACQUIRE_WAIT))
    except LocalConnectError:
        raise
    except Exception as exc:
        raise LocalConnectError("本地 ready 队列不可用", reason=exc) from exc

    try:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.settimeout(float(timeout))
        headers = dict(req.header_items())
        # one-shot：响应结束就关闭，后台补新连接；避免依赖上游 keep-alive。
        headers["Connection"] = "close"
        conn.request(req.get_method(), path, body=req.data, headers=headers)
        resp = conn.getresponse()
        wrapped = PooledHTTPResponse(resp, conn, pool)
        if getattr(resp, "status", 0) >= 400:
            err = urllib.error.HTTPError(
                req.full_url, resp.status, resp.reason, resp.headers, wrapped)
            err.wbrelay_body_bytes = len(req.data or b"")
            raise err
        return wrapped
    except urllib.error.HTTPError:
        raise
    except Exception:
        pool.release(conn)
        raise


def open_upstream_request(req, timeout):
    """按目标主机选择本地上游 ready 队列或原 urllib 路径。

    ``127.0.0.1`` / ``localhost`` / ``::1`` 的模型请求走预热连接；其他
    供应商、模型目录和面板代理继续沿用 ``_urlopen`` 的系统代理回退语义。
    """
    origin = _local_origin(req.full_url)
    if origin is None:
        return _urlopen(req, timeout)
    with _LOCAL_POOLS_LOCK:
        poolable = origin in _LOCAL_POOL_ORIGINS
    if poolable:
        return _open_local_request(req, timeout, origin)
    # 第一次本地请求先走正常 urllib，既保持单线程本地服务兼容，也读取
    # Server 头确认上游是否支持并发 idle 连接。
    try:
        resp = _urlopen(req, timeout)
    except urllib.error.HTTPError as exc:
        _mark_local_poolable(origin, getattr(exc, "headers", {}))
        raise
    _mark_local_poolable(origin, getattr(resp, "headers", {}))
    return resp


def fetch_openrouter_profiles(force=False):
    """拉 OpenRouter 全量模型清单 → {去前缀 id: profile}。失败返回 {}（下次再试）。"""
    now = time.time()
    if (not force and _openrouter_cache["profiles"]
            and (now - _openrouter_cache["fetched_at"]) < AUTO_PROFILE_TTL):
        return _openrouter_cache["profiles"]
    try:
        req = urllib.request.Request(OPENROUTER_URL, headers={
            "Accept": "application/json", "User-Agent": "WBRelay/1.0"})
        with _urlopen(req, OPENROUTER_TIMEOUT_SEC) as resp:
            obj = json.loads(read_limited(resp).decode("utf-8", "replace"))
        items = obj.get("data") or []
        out = {}
        order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
        for m in items:
            if not isinstance(m, dict):
                continue
            rid = str(m.get("id") or "").strip()
            if not rid:
                continue
            key = rid.split("/", 1)[-1]   # openai/gpt-6.1-sol → gpt-6.1-sol
            arch = m.get("architecture") or {}
            mods = arch.get("input_modalities") or []
            tp = m.get("top_provider") or {}
            rsn = m.get("reasoning") or {}
            efforts = list(rsn.get("supported_efforts") or [])
            efforts.sort(key=lambda e: order.get(str(e), 9))
            params = m.get("supported_parameters") or []
            out[key] = {
                "reasoning_efforts": efforts,
                "context_window": m.get("context_length"),
                "max_output_tokens": tp.get("max_completion_tokens"),
                "supports_vision": "image" in mods,
                "supports_tool_calls": "tools" in params,
                "supports_reasoning": bool(rsn) or bool(efforts),
                "prompt_price": float((m.get("pricing") or {}).get("prompt") or 0),
                "completion_price": float((m.get("pricing") or {}).get("completion") or 0),
                "cache_read_price": float((m.get("pricing") or {}).get("input_cache_read") or 0),
            }
        _openrouter_cache["fetched_at"] = now
        _openrouter_cache["profiles"] = out
        profiles._profiles_meta["auto_synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                         time.localtime(now))
        profiles._profiles_meta["auto_known"] = len(out)
        profiles._persist_profiles_meta()
        return out
    except Exception as exc:
        # 不向日志或调用方回显上游原文（可能含凭据）
        _log("[WBRelay] OpenRouter 档案同步失败（保持本地档案）：%s" % redact(exc, 200))
        return {}


def ensure_auto_profiles(model_ids):
    """自动补缺：档案库里没有的模型名，从 OpenRouter 查并合并进 model_profiles.json。返回新增数。"""
    prof = profiles.load_profiles()
    missing = [m for m in model_ids if m and m not in prof]
    if not missing:
        return 0
    src = fetch_openrouter_profiles()
    if not src:
        return 0
    merged = dict(prof)
    added = 0
    for m in missing:
        if m in src:
            merged[m] = src[m]
            added += 1
    if added:
        profiles.save_profiles(merged)
    return added


def auto_sync_info():
    synced_at = profiles._profiles_meta.get("auto_synced_at") or ""
    known = profiles._profiles_meta.get("auto_known") or 0
    if _openrouter_cache["profiles"] and _openrouter_cache["fetched_at"]:
        synced_at = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(_openrouter_cache["fetched_at"]))
        known = len(_openrouter_cache["profiles"])
    return {
        "source": "OpenRouter 公开模型库（免 Key）",
        "synced_at": synced_at,
        "known": known,
    }


# ---------------------------------------------------------------------------
# Codex 联动：同步模型配置（catalog）并重启 Codex
# ---------------------------------------------------------------------------
