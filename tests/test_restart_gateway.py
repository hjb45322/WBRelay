# -*- coding: utf-8 -*-
"""restart-gateway.py 的隔离测试（第三轮 R7 加固）。

除 G10（只读查询本进程信息，验证非 ASCII 路径的解码往返）外全程 mock：
不 taskkill、不 Popen、不连真实端口、不写仓库文件。
覆盖：
  G1  旧的能力已删除：--force / --cmd / --move 一律被 argparse 拒绝
  G2  隐藏 --worker 无交接文件 / token 不符 / 过期 / 重放 → 拒绝（不结束任何进程）
  G3  worker 独立复核：PID 不再监听 / 身份（CreationDate）变化 → 拒绝
  G4  存在不明后代 → 停止并报告（一个进程都不结束）
  G5  正常重启：逐个结束已确认 PID（子在前、目标在后；不带 /T），再用合法入口拉起
  G6  启动命令白名单：任意命令一律拒绝；只接受本仓库 relay.py / 仓库内 WBRelay.exe
  G7  父进程探测：非 WBRelay / 多监听 PID / 无监听 → 拒绝且不派发值守进程（不做冷启动）
  G8  只凭端口或 basename 不能通过身份识别
  G9  静态审计：源码里没有 taskkill /T、没有 --force/--cmd/--move
  G10 进程信息解码：非 ASCII 路径（本仓库路径含中文）往返后逐字符一致 —— 只读，不碰任何进程

用法：python tests/test_restart_gateway.py
"""
import argparse
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time

import isolate

isolate.bootstrap()          # 独立临时 APPDATA/LOCALAPPDATA/USERPROFILE/HOME（本测试不 import wbrelay）

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "tools", "restart-gateway.py")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def load_tool():
    spec = importlib.util.spec_from_file_location("wbrelay_restart_gateway_probe", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def proc(pid, parent, exe, cmd, created="20261012000000.000000+480"):
    return {"ProcessId": pid, "ParentProcessId": parent, "ExecutablePath": exe,
            "CommandLine": cmd, "CreationDate": created}


def ns(**kw):
    base = {"worker": False, "handoff": "", "token": "", "log": "",
            "port": 19999, "check": True, "cwd": "", "wait": 5}
    base.update(kw)
    return argparse.Namespace(**base)


def main():
    mod = load_tool()
    tmp = tempfile.mkdtemp(prefix="wbrelay-restart-test-")
    python_exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    relay = os.path.join(REPO, "relay.py")
    target_info = proc(1234, 4, python_exe,
                       '"%s" "%s" --data-dir "%s" --no-ide-inject --port 19999'
                       % (python_exe, relay, os.path.join(tmp, "data")))
    snapshot = mod.identity_snapshot(target_info)
    state_ok = {"suppliers": [], "version": "1.0.0", "port": 19999, "api_keys": []}

    # 兜底：任何真实进程操作都直接失败（测试必须全程 mock）
    real_call, real_popen, real_check = mod.subprocess.call, mod.subprocess.Popen, mod.subprocess.check_output
    real_sleep = mod.time.sleep
    real_proc_info = mod.proc_info          # G10 要用真实实现（其它用例都 mock 掉）

    def no_shell(*a, **k):
        raise AssertionError("测试期间不得执行真实进程/命令：%r" % (a,))

    mod.subprocess.call = no_shell
    mod.subprocess.Popen = no_shell
    mod.subprocess.check_output = no_shell
    try:
        # ---- G1 旧能力已删除 ----
        for flag in ("--force", "--cmd", "--move"):
            raised = False
            old_stderr = sys.stderr
            sys.stderr = io.StringIO()
            try:
                mod.main([flag, "x"])
            except SystemExit:
                raised = True
            finally:
                sys.stderr = old_stderr
            check("G1 %s 已删除（argparse 拒绝）" % flag, raised)

        # ---- G2 worker 交接门禁 ----
        killed = []
        mod.kill_pid = lambda pid, fh: killed.append(pid)
        mod.start_process = lambda argv, cwd, fh: killed.append(("start", argv))
        for label, kwargs in (
                ("无交接文件", dict(worker=True, handoff="", token="")),
                ("交接文件不存在", dict(worker=True, handoff=os.path.join(tmp, "nope.json"), token="t")),
        ):
            rc = mod.run_worker(ns(log=os.path.join(tmp, "w1.log"), **kwargs))
            check("G2 %s → 拒绝(4)" % label, rc == 4 and killed == [], "rc=%s" % rc)

        token = "unit-test-token"
        good_path = os.path.join(tmp, "handoff.json")
        payload = {"token": token, "pid": 1234, "port": 19999, "cwd": REPO,
                   "argv": [python_exe, relay, "--data-dir", os.path.join(tmp, "data"),
                            "--no-ide-inject", "--port", "19999"],
                   "identity": snapshot, "marker": os.path.join(tmp, "data"),
                   "parent_pid": 1, "created_at": time.time(), "wait": 5}
        mod.write_handoff(good_path, payload)
        rc = mod.run_worker(ns(worker=True, handoff=good_path, token="wrong-token",
                               log=os.path.join(tmp, "w2.log")))
        check("G2 token 不符 → 拒绝(4)", rc == 4 and killed == [], "rc=%s" % rc)

        old_path = os.path.join(tmp, "expired.json")
        mod.write_handoff(old_path, payload)
        stale = time.time() - (mod.HANDOFF_TTL_SEC + 60)
        os.utime(old_path, (stale, stale))
        rc = mod.run_worker(ns(worker=True, handoff=old_path, token=token,
                               log=os.path.join(tmp, "w3.log")))
        check("G2 交接文件过期 → 拒绝(4)", rc == 4 and killed == [], "rc=%s" % rc)

        # ---- G3 worker 独立复核 ----
        def listener_missing(port):
            return []

        mod.netstat_listeners = listener_missing
        rc = mod.run_worker(ns(worker=True, handoff=good_path, token=token,
                               log=os.path.join(tmp, "w4.log")))
        check("G3 PID 不再监听端口 → 拒绝(5)，不结束任何进程",
              rc == 5 and killed == [], "rc=%s killed=%s" % (rc, killed))

        mod.netstat_listeners = lambda port: [1234]
        mod.proc_info = lambda pid: proc(1234, 4, python_exe,
                                         target_info["CommandLine"],
                                         created="20261012000001.000000+480")   # PID 重用
        rc = mod.run_worker(ns(worker=True, handoff=good_path, token=token,
                               log=os.path.join(tmp, "w5.log")))
        check("G3 启动时间变化（PID 重用）→ 拒绝(5)",
              rc == 5 and killed == [], "rc=%s" % rc)

        # token 仍有效（前两次拒绝发生在消费之前）—— 重新写一份完好交接文件
        good2 = os.path.join(tmp, "handoff2.json")
        mod.write_handoff(good2, payload)
        mod.proc_info = lambda pid: dict(target_info)
        mod.http_json = lambda port, path, timeout=3: (dict(state_ok) if path == "/api/state" else {})
        mod.port_busy = lambda port: False

        # ---- G4 不明后代 → 不结束任何进程 ----
        mod.descendants = lambda pid, procs=None: [proc(2000, 1234, "C:\\Windows\\System32\\svchost.exe",
                                                       "svchost.exe -k netsvcs")]
        rc = mod.run_worker(ns(worker=True, handoff=good2, token=token,
                               log=os.path.join(tmp, "w6.log")))
        check("G4 存在不明后代 → 停止并报告(5)，目标与后代都没被结束",
              rc == 5 and killed == [], "rc=%s killed=%s" % (rc, killed))

        # ---- G5 正常重启 ----
        good3 = os.path.join(tmp, "handoff3.json")
        mod.write_handoff(good3, payload)
        children = [
            proc(2001, 1234, python_exe, '"%s" "%s" --data-dir "%s" --port 19999'
                 % (python_exe, relay, os.path.join(tmp, "data"))),
            proc(2002, 2001, "C:\\Program Files\\Microsoft\\EdgeWebView\\msedgewebview2.exe",
                 '"msedgewebview2.exe" --embedded-browser-webview --user-data-dir="%s"'
                 % os.path.join(tmp, "data")),
        ]
        killed = []
        mod.descendants = lambda pid, procs=None: children
        mod.proc_info = lambda pid: dict(target_info) if pid == 1234 else \
            [c for c in children if c["ProcessId"] == pid][0]
        started = []

        def fake_start(argv, cwd, fh):
            started.append((list(argv), cwd))
            return None

        mod.start_process = fake_start
        mod.kill_pid = lambda pid, fh: killed.append(pid)
        mod.time.sleep = lambda *_a: None
        rc = mod.run_worker(ns(worker=True, handoff=good3, token=token,
                              log=os.path.join(tmp, "w7.log")))
        check("G5 正常重启返回 0", rc == 0, "rc=%s" % rc)
        check("G5 只结束已确认 PID，子进程在前、目标在后（不带 /T）",
              killed == [2002, 2001, 1234], repr(killed))
        check("G5 用合法入口拉起（本仓库 relay.py）",
              len(started) == 1 and started[0][0][1].endswith("relay.py"), repr(started[:1]))

        # ---- G2b 交接文件已消费 → 重放拒绝 ----
        rc = mod.run_worker(ns(worker=True, handoff=good3, token=token,
                               log=os.path.join(tmp, "w8.log")))
        check("G2 交接文件不可重放 → 拒绝(4)", rc == 4, "rc=%s" % rc)

        # ---- G6 启动命令白名单 ----
        cases = [
            ("任意 exe", ["C:\\Windows\\System32\\calc.exe"], False),
            ("cmd 执行", ["cmd.exe", "/c", "calc"], False),
            ("python -c", [python_exe, "-c", "print(1)"], False),
            ("python 跑外部脚本", [python_exe, os.path.join(tmp, "evil.py")], False),
            ("python 跑本仓库 relay.py（相对）", [python_exe, "relay.py", "--port", "19999"], True),
            ("python 跑本仓库 relay.py（绝对）", [python_exe, relay, "--port", "19999"], True),
        ]
        for label, argv, want in cases:
            ok, why, _norm = mod.validate_start_argv(argv, REPO)
            check("G6 %s → %s" % (label, "放行" if want else "拒绝"),
                  ok is want, "ok=%s why=%s" % (ok, why))
        ok, why, _n = mod.validate_start_argv(
            [python_exe, relay, "--data-dir", os.path.join(tmp, "WBRelay-legacy-20260101")], REPO)
        check("G6 数据目录指向归档目录 → 拒绝（R1）", not ok, why)
        ok, why, _n = mod.validate_start_argv(
            [python_exe, relay, "--allow-legacy-data"], REPO)
        check("G6 带 --allow-legacy-data → 拒绝（R1）", not ok, why)

        # ---- G7 父进程探测 ----
        mod.netstat_listeners = lambda port: []
        check("G7 端口没人监听 → 2（只重启，不冷启动）", mod.main(["--port", "19999"]) == 2)
        mod.netstat_listeners = lambda port: [101, 102]
        check("G7 多个监听 PID → 2（不猜目标）", mod.main(["--port", "19999"]) == 2)
        mod.netstat_listeners = lambda port: [1234]
        mod.proc_info = lambda pid: proc(1234, 4, python_exe, target_info["CommandLine"])
        mod.http_json = lambda port, path, timeout=3: (dict(state_ok) if path == "/api/state" else {})
        mod.descendants = lambda pid, procs=None: []
        spawned = []
        mod.subprocess.Popen = lambda *a, **k: spawned.append(a)
        check("G7 正常探测 --check → 0（不做任何改动）",
              mod.main(["--port", "19999", "--check"]) == 0)
        check("G7 --check 不派发值守进程", spawned == [], repr(spawned))
        mod.http_json = lambda port, path, timeout=3: {"hello": "not-wbrelay"}
        check("G7 /api/state 语义不符 → 3（拒绝）",
              mod.main(["--port", "19999", "--check"]) == 3)
        mod.http_json = lambda port, path, timeout=3: (dict(state_ok) if path == "/api/state" else {})
        mod.proc_info = lambda pid: proc(1234, 4, "C:\\Windows\\System32\\svchost.exe",
                                         "svchost.exe -k netsvcs")
        check("G7 非 WBRelay 进程 → 3（拒绝）",
              mod.main(["--port", "19999", "--check"]) == 3)

        # ---- G8 只凭端口 / basename 不通过 ----
        check("G8a 有效 /api/state + svchost → 不通过",
              mod.classify_wbrelay(proc(1, 0, "C:\\Windows\\System32\\svchost.exe", "svchost.exe"),
                                   dict(state_ok), 19999)[0] is False)
        check("G8b 命令行像 relay.py 但 /api/state 不是本网关 → 不通过",
              mod.classify_wbrelay(target_info, {"hello": "world"}, 19999)[0] is False)
        check("G8c 端口对不上 → 不通过",
              mod.classify_wbrelay(target_info, dict(state_ok, port=1), 19999)[0] is False)
        check("G8d 正常组合 → 通过（只凭证据）",
              mod.classify_wbrelay(target_info, dict(state_ok), 19999)[0] is True)
        # onefile 父子：可执行文件绝对路径一致才算家属；只有 basename 相同不算
        frozen_parent = proc(3000, 4, os.path.join(REPO, "dist", "WBRelay.exe"),
                             '"%s" --data-dir "%s" --port 19999'
                             % (os.path.join(REPO, "dist", "WBRelay.exe"), os.path.join(tmp, "data")),
                             created="20261012000002.000000+480")
        fsnap = mod.identity_snapshot(frozen_parent)
        same_exe_child = proc(3001, 3000, os.path.join(REPO, "dist", "WBRelay.exe"),
                              frozen_parent["CommandLine"])
        other_exe_child = proc(3002, 3000, "C:\\Windows\\Temp\\WBRelay.exe",
                               '"C:\\Windows\\Temp\\WBRelay.exe" --port 19999')
        check("G8e onefile 子进程（同绝对路径） → 归 WBRelay",
              mod.classify_owned_child(same_exe_child, fsnap, os.path.join(tmp, "data"))[0] is True)
        check("G8f 仅 basename 相同的其它 WBRelay.exe → 不归本次目标",
              mod.classify_owned_child(other_exe_child, fsnap, os.path.join(tmp, "data"))[0] is False)
        check("G8g 未知后代（cmd.exe） → 不归本次目标",
              mod.classify_owned_child(proc(3003, 3000, "C:\\Windows\\System32\\cmd.exe", "cmd.exe"),
                                       fsnap, os.path.join(tmp, "data"))[0] is False)
        # WebView2 子进程：打包版会拉起一套 msedgewebview2，命令行里带
        # --webview-exe-name=<宿主 exe 名>，但**不带端口 / 数据目录**（旧规则一律拒绝）
        wv_dir = ("C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\1"
                  "\\msedgewebview2.exe")
        prof = "C:\\Windows\\Temp\\wbrelay-fixture\\EBWebView"
        wv_ok = proc(3004, 3000, wv_dir,
                     '"%s" --embedded-browser-webview=1 --webview-exe-name=WBRelay.exe '
                     '--user-data-dir=%s' % (wv_dir, prof),
                     created="20261012000004.000000+480")
        wv_other = proc(3005, 3000, wv_dir,
                        '"%s" --embedded-browser-webview=1 --webview-exe-name=OtherApp.exe' % wv_dir,
                        created="20261012000005.000000+480")
        check("G8j WebView2 子进程（--webview-exe-name 指向目标 exe） → 归 WBRelay",
              mod.classify_owned_child(wv_ok, fsnap, "19999")[0] is True,
              mod.classify_owned_child(wv_ok, fsnap, "19999")[1])
        check("G8k WebView2 子进程（宿主不是本次目标） → 不归本次目标",
              mod.classify_owned_child(wv_other, fsnap, "19999")[0] is False)
        # 两遍核验：crashpad-handler 命令行里没有 --webview-exe-name，也不带端口/数据目录，
        # 只有「与本目标的 WebView 子进程共享同一 --user-data-dir」这一条证据
        crash_ok = proc(3006, 3000, wv_dir,
                        '"%s" --type=crashpad-handler --user-data-dir=%s /prefetch:4 '
                        '--database=%s\\Crashpad' % (wv_dir, prof, prof),
                        created="20261012000006.000000+480")
        crash_other = proc(3007, 3000, wv_dir,
                           '"%s" --type=crashpad-handler --user-data-dir=C:\\Windows\\Temp\\other-app\\EBWebView'
                           % wv_dir, created="20261012000007.000000+480")
        kids = [wv_ok, crash_ok]
        check("G8l 两遍核验：crashpad 与已确认 WebView 共享配置目录 → 全部确认归属",
              mod.classify_own_children(kids, fsnap, "19999")[0] is True,
              repr(mod.classify_own_children(kids, fsnap, "19999")[2]))
        kids_bad = [wv_ok, crash_other]
        ok_bad, _r, bad_pair = mod.classify_own_children(kids_bad, fsnap, "19999")
        check("G8m 两遍核验：配置目录不同的 crashpad → 仍然拒绝",
              ok_bad is False and bad_pair[0].get("ProcessId") == 3007,
              repr(bad_pair[1])[:90] if bad_pair else "?")
        # 在用实例可能无参数启动（端口来自 settings）：端口证据由 /api/state + 监听关系提供
        frozen_inuse = proc(4000, 4, os.path.join(REPO, "dist", "WBRelay.exe"),
                            '"%s"' % os.path.join(REPO, "dist", "WBRelay.exe"),
                            created="20261012000003.000000+480")
        check("G8h 无参数冻结实例（端口来自 /api/state）→ 通过（只凭证据）",
              mod.classify_wbrelay(frozen_inuse, dict(state_ok), 19999)[0] is True)
        # G8i：resolve_cwd 用 os.path.isfile 确认入口是可执行文件。早先这里直接引用
        # REPO/dist/WBRelay.exe，而 dist/ 被 .gitignore 忽略 —— 开发者本机恰好编译过才通过，
        # **全新 clone / CI 上必然失败**。改成自建临时夹具，不依赖构建产物。
        g8i_dir = tempfile.mkdtemp(prefix="wbrelay-g8i-")
        g8i_exe = os.path.join(g8i_dir, "WBRelay.exe")
        try:
            with io.open(g8i_exe, "wb") as fh:
                fh.write(b"MZ")
            check("G8i 冻结实例的 cwd 由可执行文件路径推断（不需要 --cwd）",
                  os.path.normcase(mod.resolve_cwd([g8i_exe], "")) ==
                  os.path.normcase(g8i_dir),
                  mod.resolve_cwd([g8i_exe], ""))
        finally:
            shutil.rmtree(g8i_dir, ignore_errors=True)

        # ---- G9 静态审计 ----
        src = io.open(TOOL, encoding="utf-8").read()
        check("G9a 结束进程用单 PID taskkill，且代码里没有 /T 参数",
              "taskkill /PID" in src and "\"/T\"" not in src and "\x27/T\x27" not in src)
        for flag in ("--force", "--cmd", "--move"):
            check("G9b 没有注册 %s 参数" % flag,
                  "add_argument(\"%s\"" % flag not in src)
    finally:
        mod.subprocess.call, mod.subprocess.Popen, mod.subprocess.check_output = real_call, real_popen, real_check
        mod.time.sleep = real_sleep
        mod.proc_info = real_proc_info
        if os.path.basename(tmp).startswith("wbrelay-restart-test-") and \
                os.path.normcase(os.path.dirname(os.path.abspath(tmp))) == \
                os.path.normcase(os.path.abspath(tempfile.gettempdir())):
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- G10（只读、真实 CIM）：非 ASCII 路径的进程信息往返必须逐字符一致 ----
    # 这一段必须放在 mock 恢复之后：_cim_json 走的是真实 check_output。
    if os.name == "nt":
        real_info = mod.proc_info(os.getpid())
        exe = (real_info or {}).get("ExecutablePath") or ""
        check("G10a proc_info 能取到本进程（PowerShell + CIM，只读）", bool(exe),
              repr(real_info)[:110])
        check("G10b 可执行路径与本机解释器逐字符一致",
              bool(exe) and os.path.normcase(os.path.abspath(exe)) ==
              os.path.normcase(os.path.abspath(sys.executable)),
              "%r vs %r" % (exe, sys.executable))
        # 本次修复的回归点：PowerShell 管道里非 ASCII 不能被按本机代码页解成乱码。
        # 探针本身只用 ASCII 构造（[char]0x4E2D …），因此与调用方代码页无关。
        probe = mod._cim_json("([string][char]0x4E2D + [string][char]0x6587 + "
                              "[string][char]0x8DEF + [string][char]0x5F84)")
        check("G10c 非 ASCII 字符串经 CIM 管道往返逐字符一致（中文路径不乱码）",
              probe == chr(0x4E2D) + chr(0x6587) + chr(0x8DEF) + chr(0x5F84),
              repr(probe))
        check("G10d 命令行往返后仍含本测试脚本名",
              os.path.basename(__file__) in ((real_info or {}).get("CommandLine") or ""),
              ((real_info or {}).get("CommandLine") or "")[:120])
    else:
        print("[SKIP] G10 仅 Windows 有 CIM 进程信息")

    print("\n===== restart-gateway：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
