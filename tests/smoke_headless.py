# -*- coding: utf-8 -*-
"""无副作用启动冒烟测试。

在隔离的临时数据目录中启动 relay.py（--no-ide-inject --readonly --headless），验证：

  S1  GET  /                  -> 200
  S2  GET  /api/state         -> 200
  S3  POST /api/settings      -> 403（只读模式拒绝管理写入）
  S4  临时数据目录**根本没被创建**（只读 = 零写入、零目录创建）
  S5  进程可被正常终止，端口释放
  S6  数据目录确实在系统临时目录内（隔离断言）

本测试不会触碰：
  %APPDATA%\\WBRelay（既有实例数据）、~/.codex（Codex 配置）、任何外部进程。

用法： python tests/smoke_headless.py [端口]
退出码： 0 = 全部通过
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay.py")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18789


def http(method, path, body=None, timeout=5):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def main():
    results = []
    tmp = tempfile.mkdtemp(prefix="wbrelay-smoke-")
    data_dir = os.path.join(tmp, "data")
    # 关键：**不预建**数据目录，这样「只读模式零写入」才能连目录创建一起断言

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 子进程的用户目录全部指向临时根：即使代码路径去算 ~/.codex，也不会碰到真实 Codex 配置
    env["APPDATA"] = tmp
    env["LOCALAPPDATA"] = os.path.join(tmp, "localappdata")
    env["USERPROFILE"] = tmp
    env["HOME"] = tmp
    for _k in ("WBRELAY_DATA_DIR", "WBRELAY_UPSTREAM_DIR", "WBRELAY_READONLY",
               "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS"):
        env.pop(_k, None)
    proc = subprocess.Popen(
        [sys.executable, RELAY, "--data-dir", data_dir, "--no-ide-inject",
         "--readonly", "--port", str(PORT), "--headless"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    try:
        up = False
        for _ in range(60):
            time.sleep(0.25)
            if proc.poll() is not None:
                break
            if http("GET", "/api/state", timeout=2) == 200:
                up = True
                break

        if not up:
            out = b""
            try:
                proc.terminate()
                out = proc.communicate(timeout=5)[0] or b""
            except Exception:
                pass
            print("启动失败，子进程输出：")
            print(out.decode("utf-8", "replace"))
            return 1

        s1 = http("GET", "/")
        s2 = http("GET", "/api/state")
        s3 = http("POST", "/api/settings", {"cooldown_sec": 99})
        results.append(("S1 GET  /", s1, s1 == 200))
        results.append(("S2 GET  /api/state", s2, s2 == 200))
        results.append(("S3 POST /api/settings", s3, s3 == 403))

        files = []
        for root, _dirs, names in os.walk(data_dir):
            for n in names:
                files.append(os.path.join(root, n))
        created = os.path.exists(data_dir)
        results.append(("S4a 只读模式未创建数据目录", created, not created))
        results.append(("S4b 只读模式零文件写入", len(files), not files))
        in_temp = os.path.normcase(os.path.abspath(data_dir)).startswith(
            os.path.normcase(os.path.abspath(tempfile.gettempdir())))
        results.append(("S6 数据目录在系统临时目录内", in_temp, in_temp))

        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        time.sleep(0.5)
        gone = http("GET", "/api/state", timeout=2) is None
        results.append(("S5 进程已终止", "stopped" if gone else "still up", gone))
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    ok = True
    print("=" * 56)
    for name, got, passed in results:
        print("  %-30s got=%-10s %s" % (name, got, "PASS" if passed else "FAIL"))
        ok = ok and passed
    print("=" * 56)
    print("结果：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())