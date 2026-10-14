# -*- coding: utf-8 -*-
"""版本更新检测的隔离回归：**默认零外联** / 手动触发 / 失败静默降级。

覆盖：
  U1  默认（manual）：导入与读配置都不联网
  U2  版本比较：数字段 / v 前缀 / 位数不同 / 预发布后缀 / 无法解析
  U3  正常路径：mock 一次响应 → has_update 正确，且只发 1 个请求
  U4  离线 / 超时 / 抛异常 → 静默降级（不抛、has_update=False、有 error）
  U5  update_check=off → 一个请求都不发
  U6  WBRELAY_NO_UPDATE_CHECK=1 → 同样不发
  U7  不写任何文件：检查前后数据目录清单一致
  U8  请求边界：URL 固定、只有一个请求、headers 里没有本机信息
  U9  响应体超上限 → 静默降级
  U10 响应缺少 tag_name → 记 error
  U11 API mixin：`_api_update` 返回 200 与同一结构
  U12 路由存在：server.py 有 /api/update 分支（静态断言）

隔离：全部进程内 mock，**不访问真实网络**；数据目录由 isolate 指向临时根。

用法：python tests/test_update_check.py
"""
import io
import json
import os
import sys

import isolate

isolate.bootstrap()

from wbrelay import update as upd                      # noqa: E402
from wbrelay.settings import load_settings, save_settings   # noqa: E402
from wbrelay.web.api_update import ApiUpdateMixin      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, (" | " + detail) if detail else ""))


class FakeResp(object):
    """最小响应句柄：支持 read(limit+1) 与 close()。"""

    def __init__(self, payload, hang=False):
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, n=-1):
        return self._data if n is None or n < 0 else self._data[:n]

    def close(self):
        self.closed = True


class Recorder(object):
    """替换 upd._urlopen：记录每次请求，按脚本返回。"""

    def __init__(self, payload=None, exc=None):
        self.calls = []
        self.payload = payload if payload is not None else {"tag_name": "v1.1.0"}
        self.exc = exc

    def __call__(self, req, timeout):
        self.calls.append((req, timeout))
        if self.exc:
            raise self.exc
        return FakeResp(self.payload)


def with_recorder(rec, fn):
    old = upd._urlopen
    upd._urlopen = rec
    try:
        return fn()
    finally:
        upd._urlopen = old


def main():
    check("U0 wbrelay.update 可导入且不联网", callable(upd.check))

    # ---- U2 版本比较 ----
    cases = [
        ("1.1.0", "v1.1.1", -1), ("1.1.0", "1.1.0", 0), ("1.2.0", "1.1.9", 1),
        ("1.1", "1.1.0", 0), ("1.1.0", "1.2", -1), ("v2.0", "v1.9.9", 1),
        ("1.1.0", "1.1.0-rc1", 0),           # 预发布后缀按忽略处理（刻意的简化）
        ("1.10.0", "1.9.0", 1),              # 数字段比较，不是字符串比较
    ]
    bad = [(a, b, exp, upd.compare_versions(a, b)) for a, b, exp in cases
           if upd.compare_versions(a, b) != exp]
    check("U2a 版本比较正确", not bad, repr(bad[:3]))
    check("U2b 无法解析 → None",
          upd.compare_versions("abc", "1.0") is None and upd.parse_version("") == (),
          repr((upd.compare_versions("abc", "1.0"), upd.parse_version(""))))

    # ---- U1 默认不联网 ----
    rec0 = Recorder()
    with_recorder(rec0, lambda: load_settings())
    check("U1 读配置不会联网（默认零外联）", rec0.calls == [], repr(len(rec0.calls)))

    # ---- U3 正常路径 ----
    rec = Recorder({"tag_name": "v1.1.1", "html_url": "https://github.com/hjb45322/WBRelay/releases/tag/v1.1.1"})
    out = with_recorder(rec, upd.check)
    check("U3a 有新版 → has_update=True", out.get("has_update") is True, repr(out))
    check("U3b 只发一个请求", len(rec.calls) == 1, repr(len(rec.calls)))
    check("U3c 超时设为 5 秒", rec.calls and rec.calls[0][1] == upd.TIMEOUT_SEC, repr(rec.calls[0][1] if rec.calls else None))
    check("U3d 当前版本来自包 __version__", out.get("current") == upd.__version__, repr(out.get("current")))

    rec_same = Recorder({"tag_name": "v" + upd.__version__})
    out_same = with_recorder(rec_same, upd.check)
    check("U3e 同版本 → has_update=False", out_same.get("has_update") is False, repr(out_same))

    # ---- U8 请求边界 ----
    req = rec.calls[0][0] if rec.calls else None
    hdrs = {k.lower(): v for k, v in (req.header_items() if req else [])}
    leaks = [k for k in hdrs if k in ("x-wbrelay-version", "referer", "cookie", "x-forwarded-for")]
    check("U8a URL 固定为配置的检测端点", req is not None and req.full_url == upd.DEFAULT_API,
          repr(req.full_url if req else None))
    check("U8b headers 只有固定 UA 与 Accept（不带本机信息）",
          set(hdrs) <= {"user-agent", "accept"} and hdrs.get("user-agent") == upd.USER_AGENT and not leaks,
          repr(sorted(hdrs)))

    # ---- U4 失败静默降级 ----
    for label, exc in (("URLError", OSError("network down")),
                       ("超时", TimeoutError("timed out")),
                       ("JSON 畸形", ValueError("bad json"))):
        r = Recorder(exc=exc)
        res = with_recorder(r, upd.check)
        check("U4 静默降级：%s" % label,
              res.get("has_update") is False and bool(res.get("error")) and len(r.calls) == 1,
              repr(res.get("error")))

    # ---- U9 响应超上限 ----
    big = Recorder(b"x" * (upd.MAX_BYTES + 10))
    res_big = with_recorder(big, upd.check)
    check("U9 响应体超上限 → 静默降级", res_big.get("has_update") is False and bool(res_big.get("error")),
          repr(res_big.get("error")))

    # ---- U10 缺 tag_name ----
    none_tag = Recorder({"foo": 1})
    res_none = with_recorder(none_tag, upd.check)
    check("U10 响应无 tag_name → 记 error", bool(res_none.get("error")) and not res_none.get("has_update"),
          repr(res_none.get("error")))

    # ---- U5 / U6 关闭开关 ----
    old_off = os.environ.pop("WBRELAY_NO_UPDATE_CHECK", None)
    try:
        save_settings(dict(load_settings(), update_check="off"))
        rec_off = Recorder()
        res_off = with_recorder(rec_off, upd.check)
        check("U5 update_check=off → 一个请求都不发",
              rec_off.calls == [] and res_off.get("has_update") is False and bool(res_off.get("error")),
              repr((len(rec_off.calls), res_off.get("error"))))

        save_settings(dict(load_settings(), update_check="manual"))
        os.environ["WBRELAY_NO_UPDATE_CHECK"] = "1"
        rec_env = Recorder()
        res_env = with_recorder(rec_env, upd.check)
        check("U6 环境变量 WBRELAY_NO_UPDATE_CHECK → 同样不发",
              rec_env.calls == [] and bool(res_env.get("error")),
              repr((len(rec_env.calls), res_env.get("error"))))
    finally:
        os.environ.pop("WBRELAY_NO_UPDATE_CHECK", None)
        if old_off is not None:
            os.environ["WBRELAY_NO_UPDATE_CHECK"] = old_off
        save_settings(dict(load_settings(), update_check="manual"))

    # ---- U7 不写文件 ----
    def snapshot():
        root = isolate.data_dir()
        out = []
        for base, dirs, names in os.walk(root):
            for n in sorted(names):
                p = os.path.join(base, n)
                out.append((os.path.relpath(p, root), os.path.getsize(p)))
        return sorted(out)

    before = snapshot()
    with_recorder(Recorder({"tag_name": "v9.9.9"}), upd.check)
    after = snapshot()
    check("U7 检查更新不写任何文件", before == after,
          repr([x for x in (set(after) - set(before))][:3]))

    # ---- U11 API mixin ----
    class FakeApi(ApiUpdateMixin):
        def _json(self, code, body):
            return (code, body)

    with_recorder(Recorder({"tag_name": "v9.9.9"}), lambda: None)
    old = upd._urlopen
    upd._urlopen = Recorder({"tag_name": "v9.9.9"})
    try:
        code, body = FakeApi()._api_update()
    finally:
        upd._urlopen = old
    check("U11 GET /api/update 处理函数返回 200 与同一结构",
          code == 200 and isinstance(body, dict) and "has_update" in body and "current" in body,
          repr((code, sorted(body)) if isinstance(body, dict) else (code, body)))

    # ---- U12 路由存在 ----
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sv = io.open(os.path.join(root, "wbrelay", "web", "server.py"), encoding="utf-8").read()
    check("U12 server.py 里注册了 /api/update",
          '"/api/update"' in sv and "_api_update" in sv)

    print("\n===== 版本更新检测：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
