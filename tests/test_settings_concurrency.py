# -*- coding: utf-8 -*-
"""配置层并发安全回归测试：共享可变 dict + read-modify-write 丢更新。

背景（本轮修复的两个真缺陷）：

  A. `load_settings()` 直接返回内部缓存对象（`_settings_cache["data"]`），
     于是「读 → 改 → save_settings(data)」这种 6+ 处都在用的写法，任何一处的
     顺手改动都会污染全局缓存：不落盘、不报错，但下一次保存时又被当成
     「当前状态」整体写回。嵌套结构（suppliers / api_keys / group_models_enabled
     / model_prices）同样共享。
  B. `save_settings()` 只在写盘瞬间持锁，调用方的「读 → 改 → 写」全程无保护。
     ThreadingHTTPServer 下两个并发 POST 即可互相覆盖：A 线程拿着旧快照睡一会，
     B 已清空 api_keys 并保存，A 醒来把含旧 Key 的快照写回 —— B 的删除被静默还原。

覆盖：
  A1~A4  共享 dict：顶层与深层嵌套结构都必须互不影响（**修复前必失败**）
  B1     生产入口 set_* 的 read-modify-write 原子性（竞争窗口实测）
  B2     `update_settings` 原语：并发两个字段都保留（**修复前必失败**）
  B3     mutator 抛异常：不落盘、异常原样上抛
  B4     `set_*` 语义不变：仍返回 True/False；并发不互吃
  B5     核心语义 2「配置保存即清冷却」只在保存成功时发生
  B6     只读模式：`update_settings` 返回 (False, result) 且零写入
  C1~C3  API 层：两个并发 POST /api/settings 改不同字段，两者都生效；
         非法值仍 400 且不落盘
  Z0~Z2  隔离自检（不读真实 %APPDATA%、数据目录在临时根内）

竞争窗口的实现说明（为什么不用 sleep）：
  B1 把 `wbrelay.settings.load_settings` 换成「读完就卡住」的版本，用事件
  握手固定「A 已读完旧快照、B 完整保存完、A 才被放行写回」这三个时刻。
  修复前 A 全程不持锁，B 一定能在 A 写回前保存完，于是 A 的旧快照把 B 的
  修改抹掉；修复后 A 在读配置的那一刻已在锁内，B 只能等 A 写完，且进去后
  重读的是 A 的结果。窗口与机器速度无关，因此修复前稳定失败、修复后稳定通过。

用法：python tests/test_settings_concurrency.py
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import isolate

DATA_DIR = isolate.bootstrap()
PORT = isolate.port()

from wbrelay import settings as wb_settings                            # noqa: E402
from wbrelay import state                                              # noqa: E402
from wbrelay.paths import SETTINGS_PATH                               # noqa: E402
from wbrelay.settings import (load_settings, save_settings,            # noqa: E402
                              set_group_models_enabled, set_suppliers)
from wbrelay.web import api_admin                                     # noqa: E402
from wbrelay.web.server import Handler                                # noqa: E402

try:
    from wbrelay.settings import update_settings                       # noqa: E402
except ImportError:
    # 修复前没有这个原语：相关用例显式失败（而不是 TypeError 掩盖意图）
    update_settings = None                                             # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http(path, body=None, timeout=20):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def on_disk():
    with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def spawn(fn):
    """起一个线程并回收：返回值 / 异常都带回主线程，死锁立即报错。"""
    box = {}

    def _wrap():
        try:
            box["ret"] = fn()
        except BaseException as exc:               # noqa: BLE001 - 原样带给主线程
            box["exc"] = exc

    t = threading.Thread(target=_wrap, daemon=True)
    t.start()
    t.join(60)
    if t.is_alive():
        raise AssertionError("线程 60 秒未结束（疑似死锁：锁没被正确释放）")
    if "exc" in box:
        raise box["exc"]
    return box.get("ret")


def _boxed(fn, outcome, key):
    """线程体：只把异常塞进 outcome（业务字段由 fn 自己写）。

    不在这里写 outcome[key] = fn() —— 那会把 fn 自己写进 outcome 的字段抹成 None。
    """
    try:
        fn()
    except BaseException as exc:               # noqa: BLE001 - 原样带给主线程
        outcome["_exc_" + key] = exc


def raise_thread_errors(outcome, keys):
    for key in keys:
        if "_exc_" + key in outcome:
            raise outcome["_exc_" + key]


def base(**overrides):
    return isolate.base_settings(port=18790, **overrides)


# ===========================================================================
# A 组：load_settings 不得返回共享可变对象
# ===========================================================================
def group_a():
    gbase = "http://127.0.0.1:18791/v1"
    isolate.write_settings(base(
        suppliers=[isolate.demo_supplier("sup_a", "供应商 A", gbase, models=["m1", "m2"])],
        api_keys=[{"id": "k1", "name": "Codex", "key": "sk-test-FAKE-KEY-0001", "enabled": True}],
        group_models_enabled={gbase: {"m1": True}},
        model_prices={"m1": {"in": 1.0, "out": 2.0}}))

    a = load_settings()
    b = load_settings()
    check("A1a 两次 load_settings 返回不同对象（a is not b）", a is not b,
          "a is b = %r" % (a is b))

    a["suppliers"] = []
    check("A1b 顶层改动不互相污染（a['suppliers'] = [] 不影响 b）",
          b.get("suppliers") != [], "b=%d 条" % len(b.get("suppliers") or []))

    a2 = load_settings()
    b2 = load_settings()
    a2["group_models_enabled"][gbase]["m1"] = False
    check("A2a 深层嵌套改动不互相污染（group_models_enabled[base]['m1']）",
          b2["group_models_enabled"][gbase]["m1"] is True,
          "b=%r" % (b2["group_models_enabled"].get(gbase),))

    a3 = load_settings()
    b3 = load_settings()
    a3["api_keys"].append({"id": "intruder", "key": "sk-test-FAKE-KEY-0002"})
    a3["model_prices"]["m1"]["in"] = 999.0
    a3["suppliers"].append(isolate.demo_supplier("sup_intruder", "入侵者",
                                                 "http://127.0.0.1:18799/v1"))
    check("A3a 嵌套 list / dict 改动不互相污染",
          len(b3["api_keys"]) == 1 and len(b3["suppliers"]) == 1
          and b3["model_prices"]["m1"]["in"] == 1.0,
          "keys=%d sups=%d price=%r" % (len(b3["api_keys"]), len(b3["suppliers"]),
                                        b3["model_prices"]["m1"]["in"]))

    check("A4 缓存对象未被污染（重新读回仍是原值）",
          load_settings()["api_keys"] == b3["api_keys"]
          and len(load_settings()["suppliers"]) == 1
          and load_settings()["group_models_enabled"][gbase]["m1"] is True,
          repr(load_settings().get("api_keys")))


# ===========================================================================
# B 组：read-modify-write 必须原子
# ===========================================================================
def group_b():
    gbase = "http://127.0.0.1:18791/v1"

    # ---- B1：生产入口的 read-modify-write 原子性 ----
    isolate.write_settings(base(cooldown_sec=60,
                                suppliers=[isolate.demo_supplier("sup_a", "供应商 A", gbase,
                                                                 models=["m1"])]))
    entered = threading.Event()      # A 已进入读后竞争窗口
    release = threading.Event()      # 主线程放行 A 继续
    armed = threading.Event()        # 慢速补丁已布防
    b_done = threading.Event()       # B 已保存完
    real_load = wb_settings.load_settings

    def slow_load():
        data = real_load()
        if armed.is_set() and not entered.is_set():
            entered.set()
            if not release.wait(30):
                raise AssertionError("等待主线程放行超时（测试故障）")
        return data

    wb_settings.load_settings = slow_load
    outcome = {}
    try:
        def writer_a():
            outcome["a"] = set_suppliers([isolate.demo_supplier("sup_a", "供应商 A 改名",
                                                                gbase, models=["m1"])])

        def writer_b():
            if not entered.wait(30):
                raise AssertionError("B 线程等待 A 进入竞争窗口超时（测试故障）")
            outcome["b"] = set_group_models_enabled({gbase: {"m1": True}})
            b_done.set()

        armed.set()
        ta = threading.Thread(target=lambda: _boxed(writer_a, outcome, "a"), daemon=True)
        tb = threading.Thread(target=lambda: _boxed(writer_b, outcome, "b"), daemon=True)
        ta.start()
        tb.start()
        if not entered.wait(30):
            raise AssertionError("A 线程未进入读后竞争窗口（测试故障）")
        outcome["b_finished_during_stall"] = b_done.wait(1.5)
        release.set()
        ta.join(60)
        tb.join(60)
    finally:
        wb_settings.load_settings = real_load
    raise_thread_errors(outcome, ("a", "b"))
    after = on_disk()
    check("B1a set_suppliers / set_group_models_enabled 都返回 True",
          outcome.get("a") is True and outcome.get("b") is True,
          "a=%r b=%r" % (outcome.get("a"), outcome.get("b")))
    check("B1b A 改的供应商名保留下来",
          [s.get("name") for s in after.get("suppliers") or []] == ["供应商 A 改名"],
          repr([s.get("name") for s in after.get("suppliers") or []]))
    check("B1c B 改的 group_models_enabled 未被 A 的旧快照还原",
          (after.get("group_models_enabled") or {}).get(gbase) == {"m1": True},
          "gem=%r" % (after.get("group_models_enabled"),))
    check("B1d 锁覆盖了 A 的读-改-写（B 无法在 A 持锁期间保存完）",
          outcome.get("b_finished_during_stall") is False,
          "b_finished_during_stall=%r" % (outcome.get("b_finished_during_stall"),))

    # ---- B2：update_settings 原语 —— 并发两个字段都保留 ----
    if update_settings is None:
        check("B2a update_settings 原语存在", False,
              "wbrelay.settings.update_settings 不存在（修复前）")
        check("B2b 两个并发 update_settings 的字段都保留", False, "同上：原语缺失")
    else:
        isolate.write_settings(base(cooldown_sec=60, cooldown_429_sec=30))

        def bump(field, value):
            def _m(data):
                data[field] = value
                return field
            return update_settings(_m)

        ra = spawn(lambda: bump("cooldown_sec", 111))
        rb = spawn(lambda: bump("cooldown_429_sec", 222))
        after = on_disk()
        check("B2a update_settings 返回 (ok, result) 且 ok 为 True",
              ra == (True, "cooldown_sec") and rb == (True, "cooldown_429_sec"),
              "ra=%r rb=%r" % (ra, rb))
        check("B2b 两个并发 update_settings 的字段都保留（互不覆盖）",
              after.get("cooldown_sec") == 111 and after.get("cooldown_429_sec") == 222,
              "cooldown_sec=%r cooldown_429_sec=%r" % (after.get("cooldown_sec"),
                                                       after.get("cooldown_429_sec")))

    # ---- B3：mutator 抛异常 —— 不落盘、异常原样上抛 ----
    isolate.write_settings(base(cooldown_sec=60))
    before = on_disk()
    raised = False
    if update_settings is not None:
        def boom(_data):
            raise ValueError("模拟 mutator 失败")

        try:
            update_settings(boom)
        except ValueError:
            raised = True
        check("B3a mutator 异常原样上抛（不被吞）", raised)
        check("B3b mutator 异常时配置未被写坏", on_disk() == before,
              "cooldown_sec=%r" % on_disk().get("cooldown_sec"))
    else:
        check("B3a mutator 异常原样上抛", False, "update_settings 不存在（修复前）")

    # ---- B4：set_* 原语语义不变（返回 True/False；并发不互吃）----
    isolate.write_settings(base(cooldown_sec=60, suppliers=[
        isolate.demo_supplier("sup_a", "供应商 A", gbase, models=["m1"])]))
    ok_sup = set_suppliers([isolate.demo_supplier("sup_a", "供应商 A", gbase, models=["m1"])])
    check("B4a set_suppliers 保存成功返回 True（既有语义不变）", ok_sup is True, repr(ok_sup))
    ok_gem = set_group_models_enabled({gbase: {"m1": True}})
    check("B4b set_group_models_enabled 保存成功返回 True", ok_gem is True, repr(ok_gem))
    after = on_disk()
    check("B4c 两次 set_* 的字段都保留（互不覆盖）",
          len(after.get("suppliers") or []) == 1
          and (after.get("group_models_enabled") or {}).get(gbase) == {"m1": True},
          "suppliers=%d gem=%r" % (len(after.get("suppliers") or []),
                                   after.get("group_models_enabled")))

    # 并发 set_suppliers / set_group_models_enabled
    gbase2 = "http://127.0.0.1:18792/v1"
    isolate.write_settings(base(cooldown_sec=60, suppliers=[
        isolate.demo_supplier("sup_a", "供应商 A", gbase, models=["m1"])]))

    def _apply_suppliers(data):
        data["suppliers"] = [isolate.demo_supplier("sup_a", "供应商 A", gbase, models=["m1"])]
        return True

    def _apply_gem(data):
        data["group_models_enabled"] = {gbase2: {"m1": True}}
        return True

    def _raw_rmw(mutator):
        # 修复前的写法：读 → 改 → 写，全程不持锁（用于证明它会丢更新）
        data = load_settings()
        mutator(data)
        return save_settings(data)

    def sup_worker():
        return update_settings(_apply_suppliers) if update_settings else _raw_rmw(_apply_suppliers)

    def gem_worker():
        return update_settings(_apply_gem) if update_settings else _raw_rmw(_apply_gem)

    spawn(sup_worker)
    spawn(gem_worker)
    after = on_disk()
    check("B4d 并发写 suppliers 与 group_models_enabled 互不覆盖",
          len(after.get("suppliers") or []) == 1
          and (after.get("group_models_enabled") or {}).get(gbase2) == {"m1": True},
          "suppliers=%d gem=%r" % (len(after.get("suppliers") or []),
                                   after.get("group_models_enabled")))

    # ---- B5：核心语义 2「配置保存即清冷却」只在保存成功时发生 ----
    from wbrelay.state import _cooldowns
    isolate.write_settings(base(cooldown_sec=60))
    _cooldowns.clear()
    _cooldowns["sup_a"] = 9999999999.0
    if update_settings is not None:
        ok, _res = update_settings(lambda d: d.__setitem__("cooldown_sec", 5) or 5)
        check("B5a update_settings 保存成功即清冷却（核心语义 2）",
              ok is True and _cooldowns.get("sup_a") is None,
              "ok=%r cooldown=%r" % (ok, _cooldowns.get("sup_a")))
    else:
        check("B5a update_settings 保存成功即清冷却（核心语义 2）", False,
              "update_settings 不存在（修复前）")

    # ---- B6：只读模式 ----
    isolate.bootstrap(readonly=True, keep_argv=True)
    from wbrelay import paths as _paths
    isolate.write_settings(base(cooldown_sec=60))
    before = on_disk()
    if _paths.READONLY and update_settings is not None:
        ok, res = update_settings(lambda d: d.__setitem__("cooldown_sec", 777) or 777)
        check("B6a 只读下 update_settings 返回 (False, result)（不伪称成功）",
              ok is False and res == 777, "ok=%r res=%r" % (ok, res))
        check("B6b 只读下不落盘", on_disk() == before,
              "cooldown_sec=%r" % on_disk().get("cooldown_sec"))
        check("B6c 只读下 save_settings 仍返回 False（既有语义不变）",
              save_settings({"port": 1}) is False)
    elif not _paths.READONLY:
        check("B6a 只读下 update_settings 返回 (False, result)", True,
              "本轮未进入只读（READONLY=%r），跳过" % _paths.READONLY)
    else:
        check("B6a 只读下 update_settings 返回 (False, result)", False,
              "update_settings 不存在（修复前）")

    # 还原成可写实例，供 C 组起 HTTP 服务
    isolate.bootstrap(readonly=False, keep_argv=True)
    from wbrelay import paths as _paths2
    check("B6d 后续用例恢复为可写模式", _paths2.READONLY is False, "READONLY=%r" % _paths2.READONLY)


# ===========================================================================
# C 组：API 层并发 POST /api/settings
# ===========================================================================
def group_c():
    isolate.write_settings(base(cooldown_sec=60, cooldown_429_sec=30))
    server = None
    real_set = api_admin.set_gateway_autostart
    real_reg = api_admin.gateway_autostart_registered
    try:
        api_admin.set_gateway_autostart = lambda on: True
        api_admin.gateway_autostart_registered = lambda: False
        state._SERVER_PORT = PORT
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # ---- C1：两个并发 POST /api/settings 改不同字段 ----
        # 并发度来自「两个线程同时发请求」。**绝不能在请求 handler 里等另一个线程**
        # （handler 跑在持锁的 update_settings 内，那样会死锁）；这里只把两个请求的
        # 发送时刻对齐，之后由 HTTP 服务端自然交错。
        gate = threading.Event()
        outcome_c = {}

        def post_settings(body, key):
            def _go():
                gate.wait(30)
                outcome_c[key] = http("/api/settings", body)
            return _go

        ta = threading.Thread(target=lambda: _boxed(post_settings({"cooldown_sec": 111}, "a"),
                                                    outcome_c, "a"), daemon=True)
        tb = threading.Thread(target=lambda: _boxed(post_settings({"cooldown_429_sec": 222}, "b"),
                                                    outcome_c, "b"), daemon=True)
        ta.start()
        tb.start()
        gate.set()
        ta.join(60)
        tb.join(60)
        raise_thread_errors(outcome_c, ("a", "b"))
        ra = outcome_c.get("a")
        rb = outcome_c.get("b")
        after = on_disk()
        check("C1a 两个并发 POST /api/settings 都返回 200",
              ra is not None and rb is not None and ra[0] == 200 and rb[0] == 200,
              "A=%s B=%s" % (ra[0] if ra else None, rb[0] if rb else None))
        check("C1b 两个并发 POST 的字段都保留（互不覆盖）",
              after.get("cooldown_sec") == 111 and after.get("cooldown_429_sec") == 222,
              "cooldown_sec=%r cooldown_429_sec=%r" % (after.get("cooldown_sec"),
                                                       after.get("cooldown_429_sec")))

        # ---- C2：并发 POST /api/settings 与 POST /api/keys ----
        isolate.write_settings(base(cooldown_sec=60, cooldown_429_sec=30,
                                    api_keys=[{"id": "k1", "name": "Codex",
                                               "key": "sk-test-FAKE-KEY-0004",
                                               "enabled": True}]))
        gate2 = threading.Event()
        outcome2 = {}

        def post_settings2():
            def _go():
                gate2.wait(30)
                outcome2["c"] = http("/api/settings", {"cooldown_sec": 111})
            return _go

        def post_keys():
            def _go():
                gate2.wait(30)
                outcome2["d"] = http("/api/keys", {"api_keys": []})
            return _go

        tc = threading.Thread(target=lambda: _boxed(post_settings2(), outcome2, "c"),
                              daemon=True)
        td = threading.Thread(target=lambda: _boxed(post_keys(), outcome2, "d"), daemon=True)
        tc.start()
        td.start()
        gate2.set()
        tc.join(60)
        td.join(60)
        raise_thread_errors(outcome2, ("c", "d"))
        rc = outcome2.get("c")
        rd = outcome2.get("d")
        after = on_disk()
        check("C2a 并发 POST /api/settings 与 /api/keys 都返回 200",
              rc is not None and rd is not None and rc[0] == 200 and rd[0] == 200,
              "settings=%s keys=%s" % (rc[0] if rc else None, rd[0] if rd else None))
        check("C2b /api/keys 清空 Key 不被 /api/settings 还原",
              after.get("api_keys") == [], "api_keys=%r" % (after.get("api_keys"),))
        check("C2c /api/settings 改的冷却秒数保留", after.get("cooldown_sec") == 111,
              "cooldown_sec=%r" % after.get("cooldown_sec"))

        # ---- C3：既有校验语义不变（非法值 400 且不落盘）----
        st, _body = http("/api/settings", {"port": 70000})
        check("C3a 非法端口仍 400", st == 400, "status=%s" % st)
        st, _body = http("/api/settings", {"cooldown_sec": "abc"})
        check("C3b 非数字 cooldown_sec 仍 400", st == 400, "status=%s" % st)
        check("C3c 400 之后磁盘上的值仍是上一次成功保存的值",
              on_disk().get("cooldown_sec") == 111,
              "cooldown_sec=%r" % on_disk().get("cooldown_sec"))
    finally:
        api_admin.set_gateway_autostart = real_set
        api_admin.gateway_autostart_registered = real_reg
        if server is not None:
            server.shutdown()
            server.server_close()


def main():
    problems = isolate.isolation_problems()
    check("Z0 数据目录已隔离（临时目录，非真实 APPDATA）", not problems, "；".join(problems))

    group_a()
    group_b()
    group_c()

    problems = isolate.isolation_problems()
    check("Z1 收尾隔离自检", not problems, "；".join(problems))
    isolate.assert_isolated()
    check("Z2 测试数据目录仍在临时根内",
          SETTINGS_PATH.startswith(isolate.tmp_root()), SETTINGS_PATH)

    print("\n===== 配置并发安全：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
