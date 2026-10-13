# -*- coding: utf-8 -*-
"""PyInstaller onefile 父子进程所有权验证（第三轮）。

为什么要单独做：onefile 产物是**两个进程**（父 bootloader + 子应用），两者可执行文件路径与
命令行完全一样。清理时如果按「端口」或「basename」匹配，就会误杀别人的 WBRelay.exe。
本工具的判定规则（**不看端口、不看 basename**）：

    归属 = 可执行文件绝对路径完全一致 + 命令行包含本轮唯一标记 + 创建时间与快照一致

用法：
    python tests/frozen_onefile_ownership.py --self-test          # 纯判定自检（CI 用，不启动任何进程）
    python tests/frozen_onefile_ownership.py --exe <WBRelay.exe>  # 运行自建冻结 EXE：独立临时 DATA + 动态端口
    python tests/frozen_onefile_ownership.py --build              # 先用 PyInstaller 构建到系统临时目录

约束：不删除任何构建目录；--build 的 dist/work/spec 全部在系统临时目录里。
退出码：0=通过，1=失败，2=环境不满足。
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# ---- 控制台编码降级 ----
# 中文 Windows 是 GBK，英文 Windows / GitHub Actions 是 cp1252。编码装不下中文时
# 按 errors="replace" 输出 —— 别让一次 print 把脚本自己打崩（与 tests/ 里 _out() 同源）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


DEFAULT_PORT_FORBIDDEN = 8789
EXE_BASENAME = "wbrelay.exe"


def _isolate_process_env():
    """本测试进程也重定向用户目录（不读真实 APPDATA / ~\.codex）；退出时清理。"""
    root = tempfile.mkdtemp(prefix="wbrelay-frozen-env-")
    for key, sub in (("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                     ("USERPROFILE", "profile"), ("HOME", "profile")):
        os.environ[key] = os.path.join(root, sub)
    for key in ("WBRELAY_DATA_DIR", "WBRELAY_READONLY", "WBRELAY_NO_IDE_INJECT",
                "WBRELAY_HEADLESS", "WBRELAY_UPSTREAM_DIR"):
        os.environ.pop(key, None)
    import atexit
    atexit.register(lambda: shutil.rmtree(root, ignore_errors=True)
                    if os.path.basename(root).startswith("wbrelay-frozen-env-") else None)
    return root


_ISOLATED_ENV_ROOT = _isolate_process_env()


def ok(label, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    return bool(cond)


def norm(path):
    return os.path.normcase(os.path.abspath(str(path or ""))) if path else ""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_json(port, path, timeout=2.0):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as resp:
            return json.loads(resp.read(1024 * 1024).decode("utf-8", "replace"))
    except Exception:
        return None


def list_processes():
    # 编码：中文仓库路径必须原样往返。PowerShell 侧固定 UTF-8 输出、Python 侧按 UTF-8 解码，
    # 否则非 ASCII 路径会变成乱码（归属比对永远不中，还会跳过清理、留下野实例）。
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine,"
          "ExecutablePath,CreationDate | ConvertTo-Json -Compress")
    try:
        out = subprocess.check_output(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                      encoding="utf-8", errors="replace", timeout=30)
        data = json.loads((out or "").strip() or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else []


def identity(info):
    info = info or {}
    return {"pid": info.get("ProcessId"), "exe": norm(info.get("ExecutablePath")),
            "created": str(info.get("CreationDate") or "")}


def owned(entry, info):
    """唯一的归属判定：绝对路径一致 + 唯一标记在命令行里 + 创建时间一致。"""
    if not entry or not info:
        return False, "缺少进程信息"
    if entry.get("pid") != info.get("ProcessId"):
        return False, "PID 不一致"
    if not entry.get("exe") or norm(info.get("ExecutablePath")) != entry["exe"]:
        return False, "可执行文件绝对路径不一致（不按 basename 判定）"
    if not entry.get("marker") or entry["marker"] not in (info.get("CommandLine") or ""):
        return False, "命令行里没有本轮唯一标记"
    if entry.get("created") and str(info.get("CreationDate") or "") != entry["created"]:
        return False, "创建时间不一致（PID 重用）"
    return True, ""


def find_owned(exe, marker, procs=None):
    """只按「绝对路径 + 唯一标记」找本轮进程；不按端口、不按 basename。"""
    procs = list_processes() if procs is None else procs
    exe_n = norm(exe)
    out = []
    for p in procs:
        if norm(p.get("ExecutablePath")) != exe_n:
            continue
        if marker not in (p.get("CommandLine") or ""):
            continue
        entry = identity(p)
        entry["marker"] = marker
        out.append(entry)
    return out


def kill_one(pid):
    """逐个结束已确认 PID（不带 /T，不无差别结束后代）。"""
    try:
        return subprocess.call(["taskkill", "/PID", str(pid), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return -1


def self_test():
    print("== onefile 所有权判定自检（不启动任何进程） ==")
    exe = os.path.join(tempfile.gettempdir(), "wbrelay-selftest", "WBRelay.exe")
    other = os.path.join(tempfile.gettempdir(), "wbrelay-selftest-other", "WBRelay.exe")
    marker = os.path.join(tempfile.gettempdir(), "wbrelay-frozen-selftest-data")
    entry = {"pid": 4242, "exe": norm(exe), "created": "20261012120000.000000+480", "marker": marker}
    base = {"ProcessId": 4242, "ExecutablePath": exe, "CreationDate": entry["created"],
            "CommandLine": '"%s" --data-dir "%s" --port 19999' % (exe, marker)}
    results = [
        ok("同 exe + 同 marker + 同创建时间 → 归本轮（onefile 父子同理）", owned(entry, base)[0]),
        ok("仅 basename 相同、路径不同 → 拒绝（不按 basename 盲杀）",
           not owned(entry, dict(base, ExecutablePath=other))[0]),
        ok("命令行缺本轮 marker → 拒绝（不按端口盲杀）",
           not owned(entry, dict(base, CommandLine='"%s" --port 19999' % exe))[0]),
        ok("PID 不一致 → 拒绝", not owned(entry, dict(base, ProcessId=99))[0]),
        ok("创建时间变化（PID 重用） → 拒绝",
           not owned(entry, dict(base, CreationDate="20261012120001.000000+480"))[0]),
        ok("无关进程（cmd.exe） → 拒绝",
           not owned(entry, {"ProcessId": 4242, "ExecutablePath": "C:\\Windows\\System32\\cmd.exe",
                             "CreationDate": entry["created"], "CommandLine": "cmd.exe"})[0]),
    ]
    found = find_owned(exe, marker, procs=[
        base,
        dict(base, ProcessId=4243),                                   # onefile 子进程：同样匹配
        dict(base, ProcessId=4244, ExecutablePath=other),             # 只是 basename 相同
        dict(base, ProcessId=4245, CommandLine='"%s" --port 19999' % exe),   # 没有 marker
    ])
    results.append(ok("find_owned 只返回路径+marker 都匹配的进程（父子都返回）",
                      sorted(x["pid"] for x in found) == [4242, 4243], repr(found)))
    return 0 if all(results) else 1


def run_exe(exe, wait_secs=25):
    exe = os.path.abspath(exe)
    if not os.path.isfile(exe):
        print("[环境不满足] 找不到 EXE：%s" % exe)
        return 2
    if os.path.basename(exe).lower() != EXE_BASENAME:
        print("[拒绝] 只允许 WBRelay.exe（当前：%s）" % os.path.basename(exe))
        return 2
    port = free_port()
    if port == DEFAULT_PORT_FORBIDDEN:
        port = free_port()
    tmp = tempfile.mkdtemp(prefix="wbrelay-frozen-")
    data = os.path.join(tmp, "data")
    marker = data
    env = dict(os.environ)
    env.update({"APPDATA": tmp, "LOCALAPPDATA": os.path.join(tmp, "localappdata"),
                "USERPROFILE": tmp, "HOME": tmp})
    for k in ("WBRELAY_DATA_DIR", "WBRELAY_READONLY", "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS"):
        env.pop(k, None)
    print("== 冻结 EXE 运行 ==")
    print("  EXE   : %s" % exe)
    print("  大小  : %d B" % os.path.getsize(exe))
    print("  SHA256: %s" % sha256_file(exe))
    print("  端口  : %d（禁止 8789）" % port)
    print("  DATA  : %s" % data)
    procs = []
    failed = 0
    try:
        p = subprocess.Popen([exe, "--data-dir", data, "--no-ide-inject",
                              "--port", str(port), "--headless"],
                             cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("  父进程 PID: %s（等待 /api/state 就绪）" % p.pid)
        state = None
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            state = http_json(port, "/api/state")
            if isinstance(state, dict) and "suppliers" in state:
                break
            time.sleep(0.3)
        if not ok("冻结实例 /api/state 就绪", isinstance(state, dict) and "suppliers" in state,
                  repr(state)[:80] if state else "无响应"):
            failed += 1

        procs = find_owned(exe, marker)
        if not ok("找到本轮 EXE 进程（按绝对路径 + marker）", len(procs) >= 1, repr(procs)):
            failed += 1
        if not ok("DATA 在临时目录内", norm(data).startswith(norm(tmp) + os.sep), data):
            failed += 1
        if not ok("onefile 父子进程都被认出来（至少 2 个）", len(procs) >= 2,
                  "找到 %d 个：%s" % (len(procs), [x["pid"] for x in procs])):
            failed += 1
        if not ok("不再按 basename/端口判定（无关进程不在列表里）",
                  all(x["exe"] == norm(exe) for x in procs), repr(procs)):
            failed += 1

        # 清理：逐个核验后结束（不带 /T）
        for entry in procs:
            info = None
            for p2 in list_processes():
                if p2.get("ProcessId") == entry["pid"]:
                    info = p2
                    break
            good, why = owned(entry, info) if info else (False, "进程已退出")
            if good:
                kill_one(entry["pid"])
            else:
                print("  跳过 PID %s（核验未通过：%s）" % (entry["pid"], why))
        deadline = time.time() + 15
        while time.time() < deadline and not port_free(port):
            time.sleep(0.3)
        remaining = find_owned(exe, marker)
        if not ok("本轮 EXE 进程全部退出", remaining == [], repr(remaining)):
            failed += 1
        if not ok("端口已释放", port_free(port), "port=%d" % port):
            failed += 1
    finally:
        try:
            if p.poll() is None:
                print("  [WARN] 父进程尚未退出（PID %s）—— 不做额外终止" % p.pid)
        except Exception:
            pass
        if os.path.basename(tmp).startswith("wbrelay-frozen-") and \
                norm(os.path.dirname(os.path.abspath(tmp))) == norm(tempfile.gettempdir()):
            shutil.rmtree(tmp, ignore_errors=True)
    return 0 if failed == 0 else 1


def build_to_temp():
    """PyInstaller 只构建到系统临时目录（不碰仓库 dist/build）。返回 EXE 路径。"""
    tmp = tempfile.mkdtemp(prefix="wbrelay-pyinstaller-")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole", "--name", "WBRelay",
           "--distpath", tmp, "--workpath", os.path.join(tmp, "work"),
           "--specpath", tmp, "--add-data",
           os.path.join(repo, "dashboard.html") + os.pathsep + ".",
           os.path.join(repo, "relay.py")]
    print("[构建] %s" % " ".join(cmd))
    rc = subprocess.call(cmd, cwd=tmp)
    if rc != 0:
        return None
    return os.path.join(tmp, "WBRelay.exe")


def main():
    ap = argparse.ArgumentParser(description="PyInstaller onefile 父子进程所有权验证")
    ap.add_argument("--exe", default="", help="要验证的冻结 EXE（绝对路径）")
    ap.add_argument("--build", action="store_true", help="先用 PyInstaller 构建到系统临时目录")
    ap.add_argument("--self-test", action="store_true", help="只跑判定自检（不启动进程）")
    args = ap.parse_args()

    if args.self_test or (not args.exe and not args.build):
        return self_test()
    exe = args.exe
    if args.build:
        exe = build_to_temp() or ""
        if not exe:
            print("[失败] PyInstaller 构建失败")
            return 1
    return run_exe(exe)


if __name__ == "__main__":
    sys.exit(main())