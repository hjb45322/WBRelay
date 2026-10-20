# -*- coding: utf-8 -*-
"""本地上游 ready 队列回归。

用法：python tests/test_local_ready_pool.py
只使用临时隔离目录与进程内 mock 上游，不访问真实 8788/8789。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import isolate

isolate.bootstrap()

from mock_upstream import MockUpstream  # noqa: E402
from wbrelay import localpool, net  # noqa: E402
from wbrelay.localpool import LocalConnectError, LocalReadyPool  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


class FakeConn(object):
    def __init__(self, n, alive=True):
        self.n = n
        self.alive = alive
        self.closed = False

    def close(self):
        self.closed = True


def make_pool(connector, **kw):
    opts = dict(idle_low=1, idle_target=2, idle_burst=3, idle_max=4,
                burst_at_active=2, acquire_wait=1.0, connect_timeout=0.1,
                is_usable=lambda c: bool(c.alive and not c.closed))
    opts.update(kw)
    return LocalReadyPool(connector, **opts)


def test_pool_prewarm_and_release():
    lock = threading.Lock()
    counter = {"n": 0}

    def connector(_timeout):
        with lock:
            counter["n"] += 1
            return FakeConn(counter["n"])

    pool = make_pool(connector)
    try:
        check("P1 后台预连到目标", wait_for(lambda: pool.stats()["idle"] >= 2),
              repr(pool.stats()))
        conn = pool.acquire()
        check("P2 能领取 ready 连接", isinstance(conn, FakeConn), repr(conn))
        pool.release(conn)
        check("P3 释放后后台补回", wait_for(lambda: pool.stats()["idle"] >= 2),
              repr(pool.stats()))
    finally:
        pool.close()


def test_pool_dynamic_burst():
    lock = threading.Lock()
    n = [0]

    def connector(_timeout):
        with lock:
            n[0] += 1
            return FakeConn(n[0])

    pool = make_pool(connector)
    try:
        check("P4 初始 ready 目标", wait_for(lambda: pool.stats()["idle"] >= 2), repr(pool.stats()))
        a = pool.acquire()
        b = pool.acquire()
        check("P5 active 提升后目标扩容", wait_for(lambda: pool.stats()["target"] >= 3, 2.0),
              repr(pool.stats()))
        pool.release(a)
        pool.release(b)
    finally:
        pool.close()


def test_pool_recover_after_10060():
    lock = threading.Lock()
    failures = [2]
    n = [0]

    def connector(_timeout):
        with lock:
            if failures[0] > 0:
                failures[0] -= 1
                raise OSError(10060, "connection timed out")
            n[0] += 1
            return FakeConn(n[0])

    pool = make_pool(connector, idle_target=1, idle_burst=1, idle_max=1,
                     burst_at_active=1, acquire_wait=2.0)
    try:
        conn = pool.acquire()
        check("P6 补连 10060 后可恢复", isinstance(conn, FakeConn), repr(pool.stats()))
        pool.release(conn)
    finally:
        pool.close()


def test_pool_exhaustion_is_marked_local():
    def connector(_timeout):
        raise OSError(10060, "connection timed out")

    pool = make_pool(connector, idle_target=1, idle_burst=1, idle_max=1,
                     burst_at_active=1, acquire_wait=0.2)
    try:
        try:
            pool.acquire()
            check("P7 连接耗尽抛出 LocalConnectError", False, "没有抛出")
        except LocalConnectError as exc:
            check("P7 连接耗尽抛出 LocalConnectError",
                  getattr(exc, "wbrelay_local_connect", False), repr(exc))
    finally:
        pool.close()


def test_local_connect_does_not_cool():
    from wbrelay.web import api_v1
    from wbrelay import routing

    supplier = {
        "id": "demo-local", "name": "演示本地上游",
        "base_url": "http://127.0.0.1:1/v1",
        "models": ["demo-model"], "enabled": True,
        "timeout_sec": 2, "wire_api": "responses", "api_key": "sk-test-FAKE",
    }
    old_candidates = api_v1.supplier_candidates
    old_cooled = api_v1.supplier_cooled
    old_mark = api_v1.supplier_mark_cool
    marked = []
    api_v1.supplier_candidates = lambda model: []
    api_v1.supplier_cooled = lambda sid: False
    api_v1.supplier_mark_cool = lambda *a, **k: marked.append((a, k))
    try:
        class FakeHandler(api_v1.ApiV1Mixin):
            def __init__(self):
                pass

            def _supplier_send(self, path, payload, supplier):
                raise LocalConnectError("本地 ready 连接耗尽")

            def _err(self, code, message, etype="invalid_request_error", extra_headers=None):
                return code, message

        st, msg = FakeHandler()._supplier_dispatch(
            "/v1/responses", {"model": "demo-model", "stream": False}, supplier)
        check("P8 本地建连失败不触发供应商冷却",
              st == 503 and not marked and "本地" in msg,
              "status=%s marked=%s msg=%s" % (st, marked, msg[:80]))
    finally:
        api_v1.supplier_candidates = old_candidates
        api_v1.supplier_cooled = old_cooled
        api_v1.supplier_mark_cool = old_mark


def test_non_wb_local_upstream_not_pooled():
    net._close_local_pools()
    with MockUpstream("single-threaded", mode="ok") as upstream:
        req = urllib.request.Request(upstream.base_url + "/models", method="GET")
        with net.open_upstream_request(req, 10) as resp:
            resp.read(64)
        origin = net._local_origin(upstream.base_url)
        with net._LOCAL_POOLS_LOCK:
            pooled = origin in net._LOCAL_POOL_ORIGINS
        check("P8b 单线程本地服务不预热", not pooled, repr(pooled))
    net._close_local_pools()


def test_real_mock_upstream():
    net._close_local_pools()
    with MockUpstream("ready-test", mode="responses_sse_no_done") as upstream:
        origin = net._local_origin(upstream.base_url)
        net._mark_local_poolable(origin, {"Server": "wb-proxy/test"})
        pool = net._local_pool(origin)
        check("P8c 探测成功后立即预热",
              wait_for(lambda: pool.stats()["idle"] >= 1, 3.0), repr(pool.stats()))
        body = json.dumps({"model": "demo-model", "stream": True, "input": "hi"}).encode("utf-8")
        for idx in (1, 2):
            req = urllib.request.Request(upstream.base_url + "/responses", data=body,
                                         method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "text/event-stream"})
            try:
                with net.open_upstream_request(req, 10) as resp:
                    raw = resp.read()
                check("P9 mock 本地请求 %d 可用" % idx, b"data:" in raw, repr(raw[:80]))
            except Exception as exc:
                check("P9 mock 本地请求 %d 可用" % idx, False, repr(exc))
        check("P10 mock 请求后 ready 队列补回",
              wait_for(lambda: pool.stats()["idle"] >= 1, 3.0), repr(pool.stats()))
    net._close_local_pools()


def main():
    test_pool_prewarm_and_release()
    test_pool_dynamic_burst()
    test_pool_recover_after_10060()
    test_pool_exhaustion_is_marked_local()
    test_local_connect_does_not_cool()
    test_non_wb_local_upstream_not_pooled()
    test_real_mock_upstream()
    isolate.assert_isolated()
    print("\n本地 ready 队列：%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())