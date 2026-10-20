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
  G11 冷启动：端口已有监听 → 拒绝(2) 且不派发值守进程
  G12 冷启动：端口无监听 → payload.mode=start，入口来自写死白名单，cwd=仓库根
  G13 冷启动 worker：不结束任何进程，只启动一次，就绪即 0
  G14 冷启动竞态：worker 起来时端口已被监听 → 拒绝(5)，不启动
  G15 静态审计：仍无 --force/--cmd/--move；--start 不接受命令/位置参数
  G16 start.bat 指向 dist\WBRelay.exe（防死链回归）
  G18 升级重启：写死新入口、保留旧参数、拒绝相对 --data-dir / 互斥参数

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
            "port": 19999, "check": True, "cwd": "", "wait": 5,
            "start": False, "upgrade": False}
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

        # ---- G11–G16 冷启动（--start）：升级换 EXE 的唯一合规路径 ----
        disp = []

        def fake_popen(cmd, **kw):
            disp.append(([str(c) for c in cmd], kw))
            return None

        saved_popen = mod.subprocess.Popen   # 注意别用 real_popen：那是本测试文件后面恢复用的原名
        mod.subprocess.Popen = fake_popen
        try:
            # G11 端口已有监听 → 拒绝且不派发
            mod.netstat_listeners = lambda port: [1234]
            disp[:] = []
            rc = mod.main(["--start", "--port", "19999"])
            check("G11 端口已有监听时 --start 拒绝(2)且不派发值守进程",
                  rc == 2 and disp == [], "rc=%s disp=%d" % (rc, len(disp)))

            # G12 端口无监听 → plan_cold_start 的 payload 正确
            mod.netstat_listeners = lambda port: []
            mod.proc_info = lambda pid: None
            disp[:] = []
            rc = mod.main(["--start", "--port", "19999"])
            pl, err = None, "未派发"
            if disp:
                cmdline = disp[0][0]
                hop = cmdline[cmdline.index("--handoff") + 1]
                tok = cmdline[cmdline.index("--token") + 1]
                pl, err = mod.read_handoff(hop, tok)
            check("G12 无监听时 --start 派发值守进程且 exit=0",
                  rc == 0 and pl is not None, "rc=%s err=%s" % (rc, err))
            check("G12 payload.mode == start",
                  (pl or {}).get("mode") == "start", repr((pl or {}).get("mode")))
            ok_argv, why_argv, _n = mod.validate_start_argv((pl or {}).get("argv") or [],
                                                           (pl or {}).get("cwd") or "")
            check("G12 入口来自写死白名单（仍无 --cmd）", ok_argv, why_argv)
            check("G12 cwd == 仓库根",
                  os.path.normcase((pl or {}).get("cwd") or "") == os.path.normcase(REPO),
                  repr((pl or {}).get("cwd")))
            cold_payload = pl
        finally:
            mod.subprocess.Popen = saved_popen

        # G13 冷启动 worker：不结束任何进程，只启动一次，就绪即 0
        killed, started = [], []
        cold_path = os.path.join(tmp, "handoff-cold.json")
        mod.write_handoff(cold_path, dict(cold_payload or {}, token=token))
        mod.kill_pid = lambda pid, fh: killed.append(pid)
        mod.start_process = lambda argv, cwd, fh: started.append((list(argv), cwd))
        mod.netstat_listeners = lambda port: []
        mod.http_json = lambda port, path, timeout=3: dict(state_ok)
        mod.time.sleep = lambda *_a: None
        rc = mod.run_worker(ns(worker=True, handoff=cold_path, token=token,
                               log=os.path.join(tmp, "w-cold.log")))
        check("G13 冷启动 worker 返回 0", rc == 0, "rc=%s" % rc)
        check("G13 冷启动不结束任何进程", killed == [], repr(killed))
        check("G13 只启动一次且入口合规",
              len(started) == 1 and mod.validate_start_argv(started[0][0], started[0][1])[0],
              repr(started[:1]))
        check("G13 就绪判据是 /api/state 含 suppliers（与重启模式一致）",
              isinstance(state_ok, dict) and "suppliers" in state_ok)

        # G14 竞态：worker 起来时端口已被监听 → 拒绝(5)，不启动
        killed, started = [], []
        mod.write_handoff(cold_path, dict(cold_payload or {}, token=token))
        mod.netstat_listeners = lambda port: [4321]
        rc = mod.run_worker(ns(worker=True, handoff=cold_path, token=token,
                               log=os.path.join(tmp, "w-cold-race.log")))
        check("G14 冷启动竞态（端口已被监听）→ 拒绝(5) 且不启动",
              rc == 5 and started == [], "rc=%s started=%d" % (rc, len(started)))

        # G15 静态审计：旧能力仍不存在；--start 不接受命令/位置参数
        src = io.open(os.path.join(REPO, "tools", "restart-gateway.py"), encoding="utf-8").read()
        check("G15a 源码里仍无 --cmd / --force / --move",
              all(("add_argument(\"%s\"" % f) not in src for f in ("--cmd", "--force", "--move")))
        raised = False
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            mod.main(["--start", "C:\\Windows\\System32\\calc.exe"])
        except SystemExit:
            raised = True
        finally:
            sys.stderr = old_stderr
        check("G15b --start 不接受命令参数（位置参数被 argparse 拒绝）", raised)

        # G16 start.bat 指向 dist\WBRelay.exe（防死链回归）
        bat = io.open(os.path.join(REPO, "start.bat"), encoding="utf-8", errors="replace").read()
        check("G16a start.bat 指向 dist\\WBRelay.exe",
              "dist\\WBRelay.exe" in bat, repr(bat[:80]))
        check("G16b start.bat 不再指向仓库根的裸 WBRelay.exe",
              'dp0WBRelay.exe' not in bat)
        check("G16c start.bat 在缺文件时给出提示而不是静默失败",
              "if not exist" in bat and "pyinstaller" in bat)

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

        # ---- G17 版本后缀 EXE（Release 常用副本）纳入值守路径 ----
        # 背景：2026-10-17 在用实例由 dist\WBRelay-1.1.0.exe 启动，旧规则只认
        # 精确文件名 wbrelay.exe，导致 R7 唯一合规重启路径整条失效。
        ver_in_repo = proc(5000, 4, os.path.join(REPO, "dist", "WBRelay-1.1.0.exe"),
                           '"%s"' % os.path.join(REPO, "dist", "WBRelay-1.1.0.exe"),
                           created="20261018000001.000000+480")
        check("G17a 仓库内版本后缀 EXE（WBRelay-1.1.0.exe）→ 通过",
              mod.classify_wbrelay(ver_in_repo, dict(state_ok), 19999)[0] is True,
              mod.classify_wbrelay(ver_in_repo, dict(state_ok), 19999)[1])

        ver_outside = proc(5001, 4, "C:\\Windows\\Temp\\WBRelay-1.1.0.exe",
                           '"C:\\Windows\\Temp\\WBRelay-1.1.0.exe"',
                           created="20261018000002.000000+480")
        check("G17b 仓库外的版本后缀 EXE → 仍然拒绝",
              mod.classify_wbrelay(ver_outside, dict(state_ok), 19999)[0] is False)

        ver_bad = proc(5002, 4, os.path.join(REPO, "dist", "WBRelay-abc.exe"),
                       '"%s"' % os.path.join(REPO, "dist", "WBRelay-abc.exe"),
                       created="20261018000003.000000+480")
        check("G17c 非数字版本段（WBRelay-abc.exe）→ 拒绝",
              mod.classify_wbrelay(ver_bad, dict(state_ok), 19999)[0] is False)
        check("G17d 文件名判定：规范名与数字版本后缀通过，其余拒绝",
              mod.is_frozen_exe_name("WBRelay.exe") is True
              and mod.is_frozen_exe_name("wbrelay-1.1.0.exe") is True
              and mod.is_frozen_exe_name("WBRelay-.exe") is False
              and mod.is_frozen_exe_name("notwbrelay.exe") is False
              and mod.is_frozen_exe_name("WBRelay.exe.bak") is False)

        # validate_start_argv 也要接受版本后缀（重启沿用的是旧进程原命令行）
        g17_repo = tempfile.mkdtemp(prefix="wbrelay-g17-repo-")
        g17_old_root = mod.REPO_ROOT
        try:
            os.makedirs(os.path.join(g17_repo, "dist"), exist_ok=True)
            g17_exe = os.path.join(g17_repo, "dist", "WBRelay-1.1.0.exe")
            with io.open(g17_exe, "wb") as fh:
                fh.write(b"MZ")
            mod.REPO_ROOT = g17_repo
            ok17, why17, argv17 = mod.validate_start_argv([g17_exe], os.path.join(g17_repo, "dist"))
            check("G17e 启动白名单接受仓库内版本后缀 EXE",
                  ok17 is True and argv17 == [g17_exe], why17)
            outside17 = os.path.join(tempfile.gettempdir(), "WBRelay-1.1.0.exe")
            check("G17f 启动白名单仍拒绝仓库外的版本后缀 EXE",
                  mod.validate_start_argv([outside17], tempfile.gettempdir())[0] is False)
        finally:
            mod.REPO_ROOT = g17_old_root
            shutil.rmtree(g17_repo, ignore_errors=True)

        check("G17g 冷启动入口仍是规范名 dist\\WBRelay.exe（重启一次即自愈回规范名）",
              'os.path.join(REPO_ROOT, "dist", "WBRelay.exe")' in src)

        # ---- G18 升级重启（--upgrade）：停旧后切写死新入口 ----------------
        g18_repo = tempfile.mkdtemp(prefix="wbrelay-g18-repo-")
        g18_old_root = mod.REPO_ROOT
        try:
            g18_dist = os.path.join(g18_repo, "dist")
            os.makedirs(g18_dist, exist_ok=True)
            g18_new = os.path.join(g18_dist, "WBRelay.exe")
            g18_old = os.path.join(g18_dist, "WBRelay-1.1.0.exe")
            g18_relay = os.path.join(g18_repo, "relay.py")
            for p in (g18_new, g18_old, g18_relay):
                with io.open(p, "wb") as fh:
                    fh.write(b"MZ" if p.endswith(".exe") else b"# test entry\n")
            mod.REPO_ROOT = g18_repo
            data_arg = os.path.join(tmp, "data-upgrade")
            argv18, cwd18, why18 = mod.upgrade_start_argv(
                [g18_old, "--data-dir", data_arg, "--no-ide-inject", "--port", "19999"])
            check("G18a 升级入口切到规范名并保留旧参数",
                  argv18[0] == g18_new
                  and argv18[1:] == ["--data-dir", data_arg, "--no-ide-inject", "--port", "19999"],
                  "%r / %r" % (argv18, why18))
            check("G18b 升级入口 cwd 为仓库根",
                  os.path.normcase(cwd18) == os.path.normcase(g18_repo), repr(cwd18))

            argv18py, _cwd18py, why18py = mod.upgrade_start_argv(
                [python_exe, g18_relay, "--data-dir", data_arg, "--port", "19999"])
            check("G18c 源码入口升级也切到规范名并保留参数",
                  argv18py[0] == g18_new
                  and argv18py[1:] == ["--data-dir", data_arg, "--port", "19999"],
                  "%r / %r" % (argv18py, why18py))

            argv_bad, _cwd_bad, why_bad = mod.upgrade_start_argv(
                [g18_old, "--data-dir", "relative-data"])
            check("G18d 相对 --data-dir 在升级模式下拒绝",
                  argv_bad == [] and "绝对路径" in why_bad, repr(why_bad))

            raised = False
            old_stderr = sys.stderr
            sys.stderr = io.StringIO()
            try:
                mod.main(["--start", "--upgrade"])
            except SystemExit:
                raised = True
            finally:
                sys.stderr = old_stderr
            check("G18e --start 与 --upgrade 互斥", raised)
            check("G18f 仍无任意 --cmd / --force / --move",
                  all(("add_argument(\"%s\"" % f) not in src
                      for f in ("--cmd", "--force", "--move")))
        finally:
            mod.REPO_ROOT = g18_old_root
            shutil.rmtree(g18_repo, ignore_errors=True)
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
