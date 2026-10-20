# -*- coding: utf-8 -*-
"""本地上游 ready 连接队列：把瞬时的 TCP 建连挪到请求路径之外。

背景与边界见 ``docs/方案-本地上游预热连接池.md``。

这里刻意使用“one-shot”连接模型：上游的流式响应会发送
``Connection: close``，所以连接完成一次请求后不再复用；后台负责持续补充
已经完成 TCP 握手的 idle 连接。请求线程只从 ready 队列领取。
"""
import select
import socket
import threading
import time

from .log import _log
from .redact import redact


IDLE_LOW = 4
IDLE_TARGET = 8
IDLE_BURST = 12
IDLE_MAX = 16
BURST_AT_ACTIVE = 8
ACQUIRE_WAIT = 10.0
CONNECT_TIMEOUT = 2.0


class LocalConnectError(OSError):
    """在请求发送前没有取得本地上游连接。

    标记 ``wbrelay_local_connect=True``，让路由层不要把本机 TCP 的瞬时建连
    失败当成供应商故障并冷却 60 秒。
    """

    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason
        self.wbrelay_local_connect = True


def connection_is_usable(conn):
    """检查一个已完成 TCP 握手的 idle 连接是否仍可用。

    HTTP/1.1 的 idle 连接正常情况下没有待读数据；如果 ``select`` 读到 EOF
    或意外数据，说明连接已被关闭或状态不可信，应丢弃。
    """
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return True
        # EOF 或意外的服务端数据都不要冒险复用这个 one-shot 连接。
        sock.recv(1, socket.MSG_PEEK)
        return False
    except (OSError, ValueError, select.error):
        return False


class PooledHTTPResponse(object):
    """把 ``http.client.HTTPResponse`` 和它租用的连接绑定在一起。

    ``api_v1`` 只需要文件类接口（read / readline / iteration / close）；
    这里统一代理，close 时把连接交还给队列并关闭。
    """

    def __init__(self, response, connection, pool):
        self._response = response
        self._connection = connection
        self._pool = pool
        self._closed = False

    def read(self, *args):
        return self._response.read(*args)

    def readline(self, *args):
        return self._response.readline(*args)

    def readinto(self, *args):
        return self._response.readinto(*args)

    def __iter__(self):
        return iter(self._response)

    def __next__(self):
        return next(self._response)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._pool.release(self._connection)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._response, name)


class LocalReadyPool(object):
    """按 origin 维护一组已建连、尚未发请求的本地连接。

    连接器由调用方提供，签名为 ``connector(timeout) -> connection``。
    生产环境传入 ``http.client.HTTPConnection``/``HTTPSConnection`` 的
    ``connect()`` 结果；测试可传轻量 fake。
    """

    def __init__(self, connector, name="", idle_low=IDLE_LOW,
                 idle_target=IDLE_TARGET, idle_burst=IDLE_BURST,
                 idle_max=IDLE_MAX, burst_at_active=BURST_AT_ACTIVE,
                 acquire_wait=ACQUIRE_WAIT, connect_timeout=CONNECT_TIMEOUT,
                 is_usable=None):
        self._connector = connector
        self._name = name or "local"
        self._idle_low = max(1, int(idle_low))
        self._idle_target = max(self._idle_low, int(idle_target))
        self._idle_burst = max(self._idle_target, int(idle_burst))
        self._idle_max = max(self._idle_burst, int(idle_max))
        self._burst_at_active = max(1, int(burst_at_active))
        self._acquire_wait = max(0.1, float(acquire_wait))
        self._connect_timeout = max(0.1, float(connect_timeout))
        self._is_usable = is_usable or connection_is_usable
        self._ready = []
        self._active = 0
        self._closed = False
        self._last_error = None
        self._last_failure_log = 0.0
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._refill_loop,
                                        name="wbrelay-local-ready-%s" % self._name,
                                        daemon=True)
        self._thread.start()

    def _desired_idle_locked(self):
        if self._active >= self._burst_at_active:
            return self._idle_burst
        return self._idle_target

    def _log_failure(self, exc):
        now = time.monotonic()
        if now - self._last_failure_log < 15.0:
            return
        self._last_failure_log = now
        try:
            detail = redact(exc, 180)
        except Exception:
            detail = type(exc).__name__
        _log("[WBRelay] 本地 ready 队列 %s 补连失败（后台重试）：%s"
             % (self._name, detail))

    def _refill_loop(self):
        while not self._stop.is_set():
            with self._cond:
                if self._closed:
                    return
                target = self._desired_idle_locked()
                if len(self._ready) >= target:
                    self._cond.wait(0.5)
                    continue
            try:
                conn = self._connector(self._connect_timeout)
            except Exception as exc:
                with self._cond:
                    self._last_error = exc
                    self._cond.notify_all()
                self._log_failure(exc)
                self._stop.wait(0.25)
                continue

            with self._cond:
                if self._closed:
                    should_close = True
                elif len(self._ready) >= self._idle_max:
                    should_close = True
                else:
                    self._ready.append(conn)
                    self._last_error = None
                    self._cond.notify_all()
                    should_close = False
            if should_close:
                try:
                    conn.close()
                except Exception:
                    pass
                if self._stop.is_set():
                    return
            # 轻微错开建连，避免启动/补连本身形成一个短连接尖峰。
            self._stop.wait(0.02)

    def acquire(self, timeout=None):
        """领取一个 ready 连接；没有时等待后台补连。"""
        wait = self._acquire_wait if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait
        with self._cond:
            while True:
                if self._closed:
                    raise LocalConnectError("本地 ready 队列已关闭")
                while self._ready:
                    conn = self._ready.pop()
                    if self._is_usable(conn):
                        self._active += 1
                        self._cond.notify_all()
                        return conn
                    try:
                        conn.close()
                    except Exception:
                        pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = self._last_error
                    detail = ""
                    if reason is not None:
                        try:
                            detail = ": " + str(redact(reason, 160))
                        except Exception:
                            detail = ": " + type(reason).__name__
                    raise LocalConnectError(
                        "本地 ready 连接耗尽，补连未在 %.1fs 内完成%s"
                        % (wait, detail), reason=reason)
                # 不忙等：后台补连成功或 active 变化都会唤醒。
                self._cond.wait(min(0.5, remaining))

    def release(self, conn):
        """关闭一个 one-shot 租约并唤醒补连线程。"""
        try:
            conn.close()
        except Exception:
            pass
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()

    def stats(self):
        with self._cond:
            return {
                "name": self._name,
                "idle": len(self._ready),
                "active": self._active,
                "target": self._desired_idle_locked(),
                "idle_low": self._idle_low,
                "idle_max": self._idle_max,
                "closed": self._closed,
                "last_error": str(self._last_error or ""),
            }

    def close(self):
        self._stop.set()
        with self._cond:
            if self._closed:
                return
            self._closed = True
            ready, self._ready = self._ready, []
            self._cond.notify_all()
        for conn in ready:
            try:
                conn.close()
            except Exception:
                pass
        self._thread.join(timeout=self._connect_timeout + 1.0)