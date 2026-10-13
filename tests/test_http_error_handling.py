# -*- coding: utf-8 -*-
"""HTTP handler 异常收敛的隔离测试。

背景：`http.server.BaseServer.handle_error` 的默认行为是把**完整 traceback** 写到 stderr。
源码模式下会泄漏内部绝对路径与调用栈；`--noconsole` 的 EXE 模式下这些输出直接丢失，
排障时反而看不到。`wbrelay/app.py` 因此改用 `_GatewayServer`：

  · 客户端主动断开（BrokenPipe / ConnectionReset / ConnectionAborted）→ 静默，不记录
  · 其余异常 → 只记「类型 + 脱敏后的消息」，走统一 `_log()` 出口（REDACT + 落盘），
    **不打印堆栈**

断言：
  E1  handler 抛异常被收敛：服务器不崩、stderr 无 traceback、wb.log 有一条脱敏记录
      （含异常类型名、不含堆栈、不含未脱敏的假 Key）
  E2  客户端断开不记录：BrokenPipeError / ConnectionResetError / ConnectionAbortedError
      被静默处理（不写日志），且真实的 RST 断开也不会打堆栈、不会打崩服务器
  E3  只读模式零写入：`--readonly` 等价环境下触发异常，数据目录下没有任何文件
      （READONLY 在导入期定死，只能在独立子进程里切换，见 `_readonly_child`）
  E4  隔离自检：`isolate.assert_isolated()`
  E5  静态回归：app.py 不再实例化裸 ThreadingHTTPServer、未引入 traceback、
      handle_error 走统一 _log 出口、redact import 唯一

本测试只使用**假凭据**（sk-test-FAKE-...），全程在自建临时数据目录里跑，
不读真实 `%APPDATA%\\WBRelay`、不碰 `~/.codex`、不占用 8789。

用法：python tests/test_http_error_handling.py
"""
import contextlib
import http.client
import io
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import isolate

# 只读模式的 READONLY 在 `wbrelay.paths` 导入期就定死了，因此只读用例必须在独立子进程里跑
_CHILD_MODE = "--readonly-child" in sys.argv
isolate.bootstrap(readonly=_CHILD_MODE)
PORT = isolate.port()

from wbrelay import state                        # noqa: E402
from wbrelay.app import _GatewayServer           # noqa: E402
from wbrelay.paths import LOG_PATH, READONLY     # noqa: E402
from wbrelay.web.server import Handler           # noqa: E402

PASS, FAIL = [], []
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 明显的假凭据（绝不能是真实 Key）：既验证脱敏，也验证它不会漏进日志
LEAK_KEY = "sk-test-FAKE-LEAK-1234567890abcdef"

_MISSING = object()


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def http_get(path, timeout=20):
    """普通 GET：返回 (status, body)。"""
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, "<%s>" % type(exc).__name__


def raw_get(path, timeout=20):
    """裸 GET：服务端 handler 抛异常时不会回响应，这里返回 (None, 客户端侧错误类型)。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, "<client:%s>" % type(exc).__name__
    finally:
        try:
            conn.close()
        except Exception:
            pass


def log_size():
    try:
        return os.path.getsize(LOG_PATH)
    except OSError:
        return 0


def log_since(offset):
    """从字节偏移处读日志增量（二进制读，避免文本模式 tell/seek 语义）。"""
    try:
        with open(LOG_PATH, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except Exception:
        return ""


def wait_log(marker, offset, timeout=4.0):
    """等日志出现 marker（服务端线程写盘后客户端才会看到断开，这里再兜一层）。"""
    deadline = time.time() + timeout
    delta = ""
    while time.time() < deadline:
        delta = log_since(offset)
        if marker in delta:
            return delta
        time.sleep(0.05)
    return delta


def install_boom(func):
    """把 Handler 的 API 方法换成会抛异常的函数；返回恢复函数。

    从 MRO 上取原始实现，restore 时原样放回（属性不存在时 delattr）。
    """
    orig = _MISSING
    for klass in Handler.__mro__:
        if "_api_usage" in vars(klass):
            orig = vars(klass)["_api_usage"]
            break
    Handler._api_usage = func

    def _restore():
        if orig is _MISSING:
            delattr(Handler, "_api_usage")
        else:
            Handler._api_usage = orig

    return _restore


def start_server():
    state._SERVER_PORT = PORT
    server = _GatewayServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# E3：只读模式（独立子进程）
# ---------------------------------------------------------------------------

def _readonly_child():
    """只读零写入检查：触发 HTTP handler 异常，断言数据目录下一个文件都没有。"""
    print("[child] 只读子进程：DATA=%s READONLY=%s" % (isolate.data_dir(), READONLY))
    isolate.assert_isolated()
    server = start_server()
    st0, _body0 = http_get("/api/usage")
    check("E3a 只读模式下 GET /api/usage 正常（200）", st0 == 200, "status=%s" % st0)

    def _boom(self):
        raise RuntimeError("readonly boom " + LEAK_KEY)

    restore = install_boom(_boom)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(out_buf):
            st, _body = raw_get("/api/usage")
            time.sleep(0.4)
    finally:
        restore()
    printed = out_buf.getvalue()
    check("E3b 只读下异常仍被记录到控制台（_log 的 print 出口，不落盘）",
          "HTTP handler" in printed and "RuntimeError" in printed, repr(printed[:160]))
    check("E3c 只读下不打印堆栈", "Traceback" not in printed and "Traceback" not in err_buf.getvalue(),
          repr((printed + err_buf.getvalue())[:160]))
    check("E3d 只读下数据目录根本没被创建", not os.path.exists(isolate.data_dir()),
          isolate.data_dir())
    files = isolate.list_files()
    check("E3e 只读模式下零写入（数据目录无任何文件，含 wb.log）", files == [], repr(files))
    st1, _body1 = http_get("/api/usage")
    check("E3f 只读下服务器未被异常打崩（恢复后仍 200）", st1 == 200, "status=%s" % st1)
    isolate.assert_isolated()
    server.shutdown()
    server.server_close()
    print("\n===== 只读子进程：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


def run_readonly_child():
    """在独立子进程里跑只读用例（READONLY 只能在导入期决定）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("WBRELAY_READONLY", None)
    env.pop("WBRELAY_DATA_DIR", None)
    env.pop("WBRELAY_NO_IDE_INJECT", None)
    env.pop("WBRELAY_HEADLESS", None)
    cmd = [sys.executable, os.path.abspath(__file__), "--readonly-child"]
    try:
        proc = subprocess.run(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=180)
    except subprocess.TimeoutExpired:
        check("E3 只读子进程超时", False, "timeout")
        return
    out = proc.stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        print("  [child] " + line)
    check("E3 只读子进程全部通过", proc.returncode == 0, "exit=%s" % proc.returncode)
    check("E3g 只读子进程输出里没有 traceback", "Traceback" not in out, out[-300:])


# ---------------------------------------------------------------------------
# E1 / E2 / E4 / E5
# ---------------------------------------------------------------------------

def _static_checks():
    import ast
    path = os.path.join(REPO, "wbrelay", "app.py")
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    bare = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "ThreadingHTTPServer"):
            bare.append(node.lineno)
    check("E5a app.py 不再直接实例化裸 ThreadingHTTPServer", bare == [], repr(bare))
    check("E5b app.py 未引入 traceback / print_exc（不打印堆栈）",
          "import traceback" not in src and "print_exc" not in src)
    check("E5c app.py 定义了 _GatewayServer.handle_error 且走统一 _log 出口",
          "def handle_error" in src and "_log(" in src)
    check("E5d app.py 只 import 一次 from .redact import redact",
          src.count("from .redact import redact") == 1,
          repr(src.count("from .redact import redact")))
    check("E5e _GatewayServer 是 ThreadingHTTPServer 子类且 daemon_threads=True",
          issubclass(_GatewayServer, __import__("http.server", fromlist=["x"]).ThreadingHTTPServer)
          and _GatewayServer.daemon_threads is True)
    check("E5f app.py 顶部没有重复 import .paths（DATA 行仍唯一）",
          src.count("from .paths import DATA, DIR, _DATA_SOURCE") == 1)


def main():
    if _CHILD_MODE:
        return _readonly_child()

    # 自己的配置（临时目录），不含任何真实供应商/凭据
    isolate.write_settings(isolate.base_settings())
    isolate.assert_isolated()
    server = start_server()

    # ---- E1 handler 抛异常被收敛 ----
    def _boom(self):
        raise RuntimeError("boom " + LEAK_KEY)

    restore = install_boom(_boom)
    try:
        st, body = raw_get("/api/usage")
        check("E1a handler 抛异常时客户端拿到断开而不是假 200", st is None, "status=%s %s" % (st, body[:80]))

        offset = log_size()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            st, body = raw_get("/api/usage")
            time.sleep(0.4)
        delta = wait_log("HTTP handler", offset)
        captured = buf.getvalue()

        check("E1b stderr 没有 traceback", "Traceback" not in captured, repr(captured[:200]))
        check("E1c stderr 没有异常类型回显", "RuntimeError" not in captured, repr(captured[:200]))
        check("E1d stderr 没有内部绝对路径/堆栈帧", "app.py" not in captured and "api_usage.py" not in captured,
              repr(captured[:200]))
        check("E1e stderr 没有泄漏假 Key", LEAK_KEY not in captured, repr(captured[:200]))
        check("E1f wb.log 记了一条 HTTP handler 异常（含异常类型名）",
              "HTTP handler" in delta and "RuntimeError" in delta, repr(delta[:200]))
        check("E1g wb.log 里没有堆栈", "Traceback" not in delta and "File \"" not in delta,
              repr(delta[:200]))
        check("E1h wb.log 里假 Key 已脱敏", LEAK_KEY not in delta and "sk-******" in delta,
              repr(delta[:200]))
        check("E1i wb.log 记录带时间戳前缀", delta.strip().startswith("20"), repr(delta[:40]))
    finally:
        restore()

    st, body = http_get("/api/usage")
    check("E1j 异常后服务器没崩：GET /api/usage → 200", st == 200, "status=%s" % st)
    st, body = http_get("/api/state")
    check("E1k 异常后服务器没崩：GET /api/state → 200", st == 200, "status=%s" % st)
    st, body = http_get("/")
    check("E1l 异常后服务器没崩：GET / → 200", st == 200, "status=%s" % st)

    # ---- E2a 客户端断开异常被静默处理（直接调 handle_error，异常正在活跃）----
    for exc_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        offset = log_size()
        ret = None
        try:
            raise exc_type("client disconnected " + LEAK_KEY)
        except exc_type:
            ret = server.handle_error(None, ("127.0.0.1", 0))
        time.sleep(0.15)
        delta = log_since(offset)
        check("E2a %s 被静默处理（返回 None 且不写日志）" % exc_type.__name__,
              ret is None and "HTTP handler" not in delta and delta.strip() == "",
              repr(delta[:160]))

    # ---- E2b 真实客户端断开（RST）：不打堆栈、不打崩服务器 ----
    def _huge_response(self):
        body = b"x" * (1024 * 1024)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)      # 客户端已 RST / 发送缓冲打满 → 断开异常
        self.wfile.flush()

    restore = install_boom(_huge_response)
    offset = log_size()
    buf = io.StringIO()
    try:
        conn = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        try:
            req = ("GET /api/usage HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "Connection: close\r\n\r\n" % PORT).encode("ascii")
            conn.sendall(req)
            time.sleep(0.3)     # 先把请求交给服务端，再粗暴 RST 断开
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
        finally:
            conn.close()
        with contextlib.redirect_stderr(buf):
            time.sleep(2.5)     # 等服务端线程撞上断开异常
    finally:
        restore()
    delta = log_since(offset)
    check("E2b 客户端 RST 断开不产生 HTTP handler 异常日志",
          "HTTP handler" not in delta, repr(delta[:200]))
    check("E2c 客户端 RST 断开时 stderr 无 traceback", "Traceback" not in buf.getvalue(),
          repr(buf.getvalue()[:200]))

    st, body = http_get("/api/usage", timeout=30)
    check("E2d 客户端断开后服务器仍正常服务（200）", st == 200, "status=%s" % st)

    isolate.assert_isolated()

    # ---- E5 静态回归 ----
    _static_checks()

    # ---- E3 只读零写入（独立子进程）----
    run_readonly_child()

    server.shutdown()
    server.server_close()

    print("\n===== HTTP 异常收敛：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())