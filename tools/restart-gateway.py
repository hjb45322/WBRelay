# -*- coding: utf-8 -*-
r"""网关重启工具：停掉占用端口的 WBRelay 网关，然后立刻用原命令行重启。

为什么需要它
    网关是本机 Codex 的上游（config.toml 指向 http://127.0.0.1:8789/v1）。
    手工「托盘退出 → 再启动」期间 Codex 会整段失联。本工具把
    「停旧进程 → 等端口释放 → 用原命令行拉起 → 轮询直到就绪」
    交给一个**脱离调用方会话的值守进程**执行：
    即使调用它的终端/会话中途断开，重启流程照样跑完。

安全约束（AGENTS.md 第三轮加固，R7）
    * 只操作「显式指定端口 + 命令行/可执行路径 + /api/state 语义全部对得上」的 WBRelay 进程；
      认不出来一律拒绝 —— **没有 --force，也没有任意 --cmd**。
    * 隐藏 --worker 只能由本工具父进程通过**一次性交接文件**（随机 token）拉起；
      外部直接 `--worker` 一律拒绝。
    * worker 在真正结束进程前，独立再一次核验 PID / 可执行路径 / 命令行 / 启动时间（CreationDate，
      防 PID 重用）/ 目标端口监听 + /api/state；核验与执行之间身份变了就停止。
    * 只逐个结束「已确认归 WBRelay 所有」的进程（`taskkill /PID <pid>`，**不带 /T**）；
      出现任何不明后代就停止并报告，绝不无差别结束后代。
    * 启动命令只允许「本仓库 relay.py 的 Python 解释器入口」或「本仓库内的 WBRelay.exe」。
    * 不做通用文件搬运（历史上那个任意路径的 --move 已删除）。
    * 不写仓库内任何文件；日志固定落在 %TEMP%\wbrelay-restart-<时间戳>.log。

用法
    python tools/restart-gateway.py --check          # 只探测当前监听进程，不动它
    python tools/restart-gateway.py                  # 重启 8789 上的网关
    python tools/restart-gateway.py --start          # 冷启动：端口无监听时从零拉起（升级换 EXE 用）
    python tools/restart-gateway.py --port 18789 --wait 60
    python tools/restart-gateway.py --cwd "<网关启动时的工作目录>"

冷启动（--start，2026-10-14 新增，见 docs/方案-网关冷启动.md）
    * 只在「端口无监听」时可用；已有监听一律拒绝，提示改用普通重启；
    * 入口**写死**：<仓库>\dist\WBRelay.exe 优先，其次 <仓库>\relay.py —— 仍然没有 --cmd；
    * 与重启共用「一次性交接文件 + 随机 token + 脱离会话的 worker + 就绪轮询」全套机制。
"""
import argparse
import datetime
import hashlib
import json
import os
import secrets
import subprocess
import sys
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


DEFAULT_PORT = 8789
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
HANDOFF_TTL_SEC = 180
INTERPRETER_BASENAMES = ("python.exe", "pythonw.exe", "py.exe")
FROZEN_EXE_BASENAME = "wbrelay.exe"
# 只有这些后代类型可以被认定为「归 WBRelay 所有」（核验规则见 classify_owned_child）
WEBVIEW_BASENAMES = ("msedgewebview2.exe",)


# --------------------------------------------------------------------------- 工具
def temp_dir():
    return os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd()


def log_line(fh, msg):
    line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    fh.write(line + "\n")
    fh.flush()
    return line


def norm(path):
    """规范化为可比较的绝对路径（大小写不敏感由 normcase 处理）。"""
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:
        return os.path.normcase(str(path))


def norm_cmdline(cmd):
    return " ".join(str(cmd or "").split())


def split_cmdline(s):
    """按 Windows CRT 规则拆分命令行字符串。"""
    args, cur, in_q, started = [], [], False, False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            j = i
            while j < n and s[j] == "\\":
                j += 1
            nbs = j - i
            if j < n and s[j] == '"':
                cur.append("\\" * (nbs // 2))
                if nbs % 2:
                    cur.append('"')
                    i = j + 1
                    started = True
                    continue
                in_q = not in_q
                i = j + 1
                started = True
                continue
            cur.append("\\" * nbs)
            i = j
            started = True
            continue
        if c == '"':
            in_q = not in_q
            started = True
            i += 1
            continue
        if c in " \t" and not in_q:
            if started:
                args.append("".join(cur))
                cur, started = [], False
            i += 1
            continue
        cur.append(c)
        started = True
        i += 1
    if started:
        args.append("".join(cur))
    return args


def netstat_listeners(port):
    """返回监听该端口的 PID 列表（netstat -ano，只读）。"""
    pids = []
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore",
                                      creationflags=CREATE_NO_WINDOW)
    except Exception:
        return pids
    for line in out.splitlines():
        s = line.split()
        if len(s) >= 5 and s[0].upper() == "TCP" and "LISTENING" in line:
            if s[1].endswith(":%d" % port):
                pid = s[-1]
                if pid.isdigit() and int(pid) not in pids:
                    pids.append(int(pid))
    return pids


def port_busy(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def http_json(port, path, timeout=3):
    try:
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(1024 * 1024).decode("utf-8", "replace"))
    except Exception:
        return None


# 进程信息里的路径可能含非 ASCII（本仓库路径就是）。PowerShell 在管道里按 UTF-8 写、
# Python 默认按本机 ANSI 代码页解码（这里 cp936），中文会被解成乱码 —— 于是身份核验里的
# 路径比对必然失败（实测：中文仓库路径被解成乱码，例如「代码仓库」→「浠ｇ爜浠撳簱」，
# 工具对**正在运行的打包版实例**一律拒绝重启，R7 入口等于失效）。
# 解法：让 PowerShell 先把 JSON 里所有非 ASCII 字符转成 \\uXXXX 转义，管道里只剩 ASCII，
# 任何代码页下都读得对；json.loads 再把转义还原成原字符。
_ASCII_JSON_PS = ("$r=$(%s);"
                  "$s=($r | ConvertTo-Json -Compress);"
                  "[Regex]::Replace($s,'[^\\x20-\\x7E]',"
                  "{param($m)('\\u{0:x4}' -f [int][char]$m.Value)})")


def _cim_json(select_expr):
    """用 CIM 取进程信息并解析成 JSON（只读）。

    `select_expr` 是「返回进程对象的 PowerShell 表达式」；序列化与转义由本函数统一负责
    （调用方不要再自己拼 ConvertTo-Json）。
    """
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _ASCII_JSON_PS % select_expr],
            text=True, encoding="ascii", errors="replace", timeout=20)
    except Exception:
        return None
    out = (out or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def proc_info(pid):
    """用 CIM 取单个进程的命令行/父进程/启动时间（Win10+ 自带 PowerShell）。"""
    d = _cim_json("$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%d';"
                  "if($p){$p | Select-Object ProcessId,ParentProcessId,CommandLine,"
                  "ExecutablePath,CreationDate}" % pid)
    return d if isinstance(d, dict) else {}


def list_processes():
    """一次取全量进程快照（只读；用于后代归属核验）。"""
    d = _cim_json("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
                  "CommandLine,ExecutablePath,CreationDate")
    if isinstance(d, dict):
        return [d]
    return d if isinstance(d, list) else []


def descendants(pid, procs=None):
    """递归返回该 PID 的全部后代（宽优先；同一进程只出现一次）。"""
    procs = list_processes() if procs is None else procs
    by_parent = {}
    for p in procs:
        if not isinstance(p, dict):
            continue
        by_parent.setdefault(p.get("ParentProcessId"), []).append(p)
    out, seen, stack = [], set(), list(by_parent.get(pid, []))
    while stack:
        p = stack.pop(0)
        cpid = p.get("ProcessId")
        if cpid in seen or cpid == pid:
            continue
        seen.add(cpid)
        out.append(p)
        stack.extend(by_parent.get(cpid, []))
    return out


def identity_snapshot(info):
    """进程身份快照：PID + 可执行路径 + 命令行 + 启动时间（防 PID 重用）。"""
    info = info or {}
    return {
        "pid": info.get("ProcessId"),
        "exe": norm(info.get("ExecutablePath")),
        "cmdline": norm_cmdline(info.get("CommandLine")),
        "created": str(info.get("CreationDate") or ""),
    }


def same_identity(a, b):
    """两份身份快照是否是同一个进程（PID 重用会被 created/exe 的差异抓到）。"""
    if not a or not b:
        return False
    if a.get("pid") != b.get("pid"):
        return False
    for key in ("exe", "cmdline", "created"):
        if a.get(key) and b.get(key) and a.get(key) != b.get(key):
            return False
    return bool(a.get("exe") or a.get("cmdline") or a.get("created"))


def classify_wbrelay(info, state, port):
    """只凭证据确认「这就是本项目的 WBRelay 网关」。返回 (ok, reason)。"""
    if not isinstance(info, dict) or not info:
        return False, "取不到进程信息"
    exe = info.get("ExecutablePath") or ""
    cmd = info.get("CommandLine") or ""
    base = os.path.basename(exe).lower()

    if not isinstance(state, dict):
        return False, "/api/state 无响应：不能确认它是 WBRelay（拒绝按端口猜）"
    if state.get("port") not in (None, port):
        return False, "/api/state 报告的端口与目标端口不一致"
    if "suppliers" not in state or not isinstance(state.get("version"), str) or not state.get("version"):
        return False, "/api/state 缺少 suppliers/version：不是本项目的状态协议"

    if base == FROZEN_EXE_BASENAME:
        if not norm(exe).startswith(norm(REPO_ROOT) + os.sep):
            return False, "WBRelay.exe 不在本仓库目录内，拒绝重启"
        # 端口证据来自 /api/state（state.port == 目标端口）+ 该 PID 正在监听该端口，
        # 不要求命令行里出现 --port（在用实例可能是无参数启动、端口来自 settings）。
        return True, "冻结 WBRelay.exe（仓库内）+ /api/state 协议一致"

    if base in INTERPRETER_BASENAMES:
        argv, err = _split_or_error(cmd)
        if err:
            return False, err
        ok, why, relay = _find_relay_entry(argv)
        if not ok:
            return False, why
        if norm(relay) != norm(os.path.join(REPO_ROOT, "relay.py")):
            return False, "解释器运行的 relay.py 不是本仓库入口：%s" % relay
        return True, "Python 解释器 + 本仓库 relay.py + /api/state 协议一致"

    return False, "可执行路径既不是 Python 解释器也不是 WBRelay.exe：%s" % (exe or "?")


def _split_or_error(cmd):
    if not cmd:
        return [], "命令行取不到"
    argv = split_cmdline(cmd)
    if not argv:
        return [], "命令行解析为空"
    return argv, ""


def _find_relay_entry(argv):
    """从 argv 里找脚本入口；只接受唯一一个 .py/.pyw。"""
    scripts = [a for a in argv[1:] if str(a).lower().endswith((".py", ".pyw"))]
    if len(scripts) != 1:
        return False, "命令行里的 Python 脚本入口不唯一（找到 %d 个）" % len(scripts), ""
    return True, "", scripts[0]


def resolve_cwd(argv, explicit):
    if explicit:
        return os.path.abspath(explicit)
    if argv:
        first = str(argv[0])
        if os.path.isabs(first) and os.path.isfile(first):
            return os.path.dirname(first)      # 冻结 EXE / 绝对路径入口
    for a in argv[1:]:
        low = a.lower()
        if not low.endswith(".py"):
            continue
        if os.path.isabs(a):
            return os.path.dirname(a)
        cand = os.path.join(REPO_ROOT, a)
        if os.path.isfile(cand):
            return REPO_ROOT
    return ""


def validate_start_argv(argv, cwd):
    """启动命令白名单：本仓库 relay.py 的 Python 入口 或 本仓库内的 WBRelay.exe。

    返回 (ok, reason, normalized_argv)。**不接受任意命令**（旧的 --cmd 已删除）。
    """
    if not argv:
        return False, "启动命令为空", []
    cwd = cwd or REPO_ROOT
    prog = str(argv[0])
    base = os.path.basename(prog).lower()
    if base in INTERPRETER_BASENAMES:
        ok, why, script = _find_relay_entry(argv)
        if not ok:
            return False, why, []
        cand = script if os.path.isabs(script) else os.path.join(cwd, script)
        if norm(cand) != norm(os.path.join(REPO_ROOT, "relay.py")):
            return False, "只允许本仓库 relay.py 作为 Python 入口：%s" % cand, []
        if not os.path.isfile(cand):
            return False, "入口文件不存在：%s" % cand, []
    elif base == FROZEN_EXE_BASENAME:
        cand = prog if os.path.isabs(prog) else os.path.join(cwd, prog)
        if not os.path.isfile(cand):
            return False, "WBRelay.exe 不存在：%s" % cand, []
        if not norm(cand).startswith(norm(REPO_ROOT) + os.sep):
            return False, "WBRelay.exe 必须位于本仓库目录内：%s" % cand, []
    else:
        return False, "启动命令不是 WBRelay 入口（只允许 relay.py 解释器入口或仓库内 WBRelay.exe）", []
    joined = " ".join(argv)
    low = joined.lower()
    if "--allow-legacy-data" in low:
        return False, "拒绝重启带 --allow-legacy-data 的实例（R1：历史/归档数据只读）", []
    if "--data-dir" in argv:
        try:
            idx = argv.index("--data-dir")
            d = str(argv[idx + 1])
        except (ValueError, IndexError):
            d = ""
        dl = d.lower()
        if "wbrelay-oss" in dl or "wbrelay-legacy" in dl:
            return False, "拒绝重启数据目录指向历史/归档目录的实例（R1）：%s" % d, []
    return True, "", [str(a) for a in argv]


def cold_start_argv():
    """冷启动的固定入口：**不接受任何外部命令**。

    优先打包版 `<仓库>\\dist\\WBRelay.exe`（升级换的就是它），其次源码入口
    `<仓库>\\relay.py`（用当前解释器）。返回 (argv, cwd, reason)；reason 非空表示不可用。
    """
    exe = os.path.join(REPO_ROOT, "dist", "WBRelay.exe")
    if os.path.isfile(exe):
        return [exe], REPO_ROOT, ""
    relay = os.path.join(REPO_ROOT, "relay.py")
    if os.path.isfile(relay):
        return [sys.executable, relay], REPO_ROOT, ""
    return [], "", ("仓库内既没有 dist\\WBRelay.exe 也没有 relay.py，无法冷启动"
                    "（先打包：pyinstaller WBRelay.spec）")


def plan_cold_start(args, pids):
    """冷启动规划：返回 (payload 或 None, 退出码)。

    端口已有监听 → 拒绝（那不是冷启动）；`--check` 时只打印计划不派发。
    """
    if pids:
        print("[退出] 端口 %d 已有监听 PID %s：这不是冷启动；请用普通重启（不带 --start）。"
              % (args.port, pids))
        return None, 2
    argv, cwd, why = cold_start_argv()
    if not argv:
        print("[退出] %s" % why)
        return None, 2
    ok, why2, argv = validate_start_argv(argv, cwd)
    if not ok:
        print("[退出] 冷启动入口不合规：%s" % why2)
        return None, 3
    print("[计划] 冷启动命令行: %s" % " ".join('"%s"' % a if " " in a else a for a in argv))
    print("[计划] 工作目录    : %s" % cwd)
    print("[计划] 动作        : 拉起 → 等 /api/state 就绪（端口 %d）" % args.port)
    if args.check:
        print("[check] 仅探测，未做任何改动。")
        return None, 0
    return {
        "mode": "start", "port": args.port, "cwd": cwd, "argv": argv,
        "parent_pid": os.getpid(), "created_at": time.time(),
        "argv_hash": hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode("utf-8")).hexdigest(),
    }, None


def dispatch_worker(payload, args, cold=False):
    """派发脱离会话的值守进程（重启 / 冷启动共用）。"""
    token = secrets.token_hex(32)
    payload["token"] = token
    payload["wait"] = args.wait
    log_path = os.path.join(temp_dir(), "wbrelay-restart-%s.log"
                            % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    path = handoff_path()
    write_handoff(path, payload)
    cmd = [sys.executable, os.path.abspath(__file__), "--worker",
           "--handoff", path, "--token", token, "--log", log_path]
    subprocess.Popen(cmd, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)
    print("[值守] 已派出值守进程，日志: %s" % log_path)
    if cold:
        print("[值守] 正在拉起网关（通常 2–8 秒），无需人工再启动。")
    else:
        print("[值守] 网关将短暂中断（通常 2–5 秒），无需人工再启动。")
    return 0


def start_and_wait(argv, cwd, port, wait, fh):
    """启动 + 轮询 /api/state 就绪（重启 / 冷启动共用）。"""
    try:
        start_process(argv, cwd, fh)
    except Exception as exc:
        log_line(fh, "[worker] 失败：启动异常 %s" % exc)
        return 7
    deadline = time.time() + max(5, int(wait or 60))
    state = None
    while time.time() < deadline:
        state = http_json(port, "/api/state")
        if isinstance(state, dict) and "suppliers" in state:
            break
        time.sleep(0.5)
    if not isinstance(state, dict) or "suppliers" not in state:
        log_line(fh, "[worker] 失败：等待 /api/state 就绪超时，请查看该实例数据目录的 wb.log")
        return 7
    log_line(fh, "[worker] 就绪 ✅ 完成")
    return 0


def command_marker(argv, port):
    """同一实例后代的唯一标记：优先 --data-dir 值，其次端口。"""
    if "--data-dir" in argv:
        try:
            return str(argv[argv.index("--data-dir") + 1])
        except IndexError:
            pass
    return str(port)


def webview_profile_dir(cmd):
    """从 WebView2 子进程命令行里取 `--user-data-dir`（规范化后的绝对路径）。

    WebView2 的 crashpad-handler / gpu / utility 兄弟进程共享同一个配置目录；
    该目录在打包版里位于一次性解包目录下（`...\\tmpXXXX\\EBWebView`），
    是「同一宿主实例」的可靠证据。
    """
    text = str(cmd or "")
    low = text.lower()
    key = "--user-data-dir="
    idx = low.find(key)
    if idx < 0:
        return ""
    rest = text[idx + len(key):].lstrip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        value = rest[1:end] if end > 0 else rest[1:]
    else:
        value = rest.split(" ")[0]
    return norm(value) if value else ""


def classify_own_children(children, parent_identity, marker):
    """后代归属核验（两遍）。返回 (ok, reasons, bad)。

    第一遍按各自命令行证据判定；第二遍让「与已确认的 WebView2 兄弟进程共享同一
    配置目录」的 WebView2 进程跟随确认 —— 打包版的 crashpad-handler 命令行里没有
    `--webview-exe-name`、也不带端口/数据目录标记，只有第一遍的话会在**正在运行的
    打包版实例**上必然失败（实测 2026-10-13）。
    """
    reasons = {}
    first_reason = {}
    profiles = set()
    for child in children or []:
        pid = child.get("ProcessId")
        good, why = classify_owned_child(child, parent_identity, marker)
        first_reason[pid] = why
        if good:
            reasons[pid] = why
            prof = webview_profile_dir(child.get("CommandLine"))
            if prof:
                profiles.add(prof)
    if profiles:
        for child in children or []:
            pid = child.get("ProcessId")
            if pid in reasons:
                continue
            base = os.path.basename(child.get("ExecutablePath") or "").lower()
            prof = webview_profile_dir(child.get("CommandLine"))
            if base in WEBVIEW_BASENAMES and prof and prof in profiles:
                reasons[pid] = "WebView 子进程：与本目标的 WebView 子进程共享配置目录"
    for child in children or []:
        pid = child.get("ProcessId")
        if pid not in reasons:
            return False, reasons, (child, first_reason.get(pid) or "无法确认归属")
    return True, reasons, None


def classify_owned_child(child, parent_identity, marker):
    """后代归属核验：只有能证明属于本次重启目标的进程才返回 True。"""
    exe = child.get("ExecutablePath") or ""
    cmd = child.get("CommandLine") or ""
    base = os.path.basename(exe).lower()
    if base == FROZEN_EXE_BASENAME and norm(exe) == parent_identity.get("exe"):
        return True, "onefile 子进程：可执行文件与目标完全一致"
    if base in INTERPRETER_BASENAMES:
        argv, err = _split_or_error(cmd)
        if not err:
            ok, _why, script = _find_relay_entry(argv)
            if ok:
                cand = script if os.path.isabs(script) else os.path.join(REPO_ROOT, script)
                if norm(cand) == norm(os.path.join(REPO_ROOT, "relay.py")):
                    return True, "Python 子进程：本仓库 relay.py 入口"
    if base in WEBVIEW_BASENAMES:
        # 打包版 GUI 会拉起一套 msedgewebview2 子进程，命令行里带
        # `--webview-exe-name=WBRelay.exe`（宿主 exe 名），但**不带端口号** —— 旧规则只认
        # marker（端口 / 数据目录），于是「打包版 + 无参数启动」的在用实例被一律拒绝重启。
        host = os.path.basename(parent_identity.get("exe") or "")
        if host and ("--webview-exe-name=%s" % host).lower() in cmd.lower():
            return True, "WebView 子进程：--webview-exe-name 指向本次目标 %s" % host
        if marker and marker.lower() in cmd.lower():
            return True, "WebView 子进程：命令行带本次目标标记"
    return False, "无法确认归属（exe=%s）" % (exe or "?")


def kill_pid(pid, fh):
    """结束**单个**已确认 PID：`taskkill /PID <pid> /F`，不带 /T（不无差别结束后代）。"""
    return subprocess.call(["taskkill", "/PID", str(pid), "/F"],
                           stdout=fh, stderr=fh, shell=False)


def start_process(argv, cwd, fh):
    return subprocess.Popen(argv, cwd=cwd or None,
                            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                            stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, close_fds=True)


# --------------------------------------------------------------------------- 交接文件
def handoff_path():
    name = "wbrelay-restart-handoff-%s-%s.json" % (
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), secrets.token_hex(4))
    return os.path.join(temp_dir(), name)


def write_handoff(path, payload):
    """同目录临时文件 + os.replace 原子写（与项目配置写入同一约定）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_handoff(path, token):
    """读取并校验一次性交接文件。返回 (payload, error)。"""
    if not path or not os.path.isfile(path):
        return None, "缺少交接文件（--worker 只能由本工具的父进程拉起）"
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError as exc:
        return None, "交接文件不可读：%s" % exc
    if age > HANDOFF_TTL_SEC:
        return None, "交接文件已过期（%.0fs > %ds）" % (age, HANDOFF_TTL_SEC)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        return None, "交接文件损坏：%s" % exc
    if not isinstance(payload, dict):
        return None, "交接文件格式不正确"
    want = str(payload.get("token") or "")
    if not token or not want or not secrets.compare_digest(want, str(token)):
        return None, "交接 token 不匹配（外部直接调用 --worker？）"
    # 必填字段按模式区分：冷启动（mode=start）没有旧进程，因此没有 pid / identity
    mode = str(payload.get("mode") or "restart")
    need = ("port", "argv", "cwd") if mode == "start" else ("pid", "port", "identity", "argv", "cwd")
    for key in need:
        if key not in payload:
            return None, "交接文件缺少字段：%s" % key
    return payload, ""


def consume_handoff(path):
    """消费交接文件（改名），防止重放。"""
    try:
        os.replace(path, path + ".consumed")
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- worker
def verify_target(payload, logfh):
    """worker 独立核验目标身份（不信任父进程结论）。返回 (info, children, reason)。"""
    pid, port = int(payload["pid"]), int(payload["port"])
    listeners = netstat_listeners(port)
    if pid not in listeners:
        return None, [], "PID %d 当前没有监听端口 %d（已退出 / PID 重用 / 端口易主）" % (pid, port)
    info = proc_info(pid)
    if not info:
        return None, [], "取不到 PID %d 的进程信息" % pid
    snap = identity_snapshot(info)
    if not same_identity(snap, payload.get("identity") or {}):
        return None, [], "进程身份与交接快照不一致（PID 重用 / 进程已换）"
    state = http_json(port, "/api/state")
    ok, why = classify_wbrelay(info, state, port)
    if not ok:
        return None, [], why
    kids = descendants(pid)
    marker = str(payload.get("marker") or "")
    owned, reasons, bad = classify_own_children(kids, snap, marker)
    if not owned:
        return None, [], "存在无法确认归属的后代 PID %s：%s" % (bad[0].get("ProcessId"), bad[1])
    own_children = [(child, reasons.get(child.get("ProcessId")) or "已确认归属") for child in kids]
    log_line(logfh, "[worker] 身份核验通过：%s（后代 %d 个，全部已确认归属）"
             % (why, len(own_children)))
    return {"info": info, "snapshot": snap, "state": state}, own_children, ""


def run_worker(args):
    log_path = args.log or os.path.join(temp_dir(), "wbrelay-restart-worker.log")
    fh = open(log_path, "a", encoding="utf-8")
    try:
        log_line(fh, "[worker] 值守进程已启动（与本会话解耦）")
        payload, err = read_handoff(args.handoff, args.token)
        if err:
            log_line(fh, "[worker] 拒绝：%s" % err)
            return 4
        ok, why, argv = validate_start_argv(payload.get("argv") or [], payload.get("cwd") or "")
        if not ok:
            log_line(fh, "[worker] 拒绝：启动命令不合规：%s" % why)
            return 4
        port = int(payload["port"])
        log_line(fh, "[worker] 启动命令: %s" % " ".join(argv))

        if str(payload.get("mode") or "restart") == "start":
            # 冷启动：没有旧进程可停；但 worker 起来时端口可能已被别的东西占上（竞态）
            log_line(fh, "[worker] 模式: 冷启动（端口 %d）" % port)
            listening = netstat_listeners(port)
            if listening:
                log_line(fh, "[worker] 拒绝：端口 %d 已被 PID %s 监听（竞态），不启动" % (port, listening))
                return 5
            if not consume_handoff(args.handoff):
                log_line(fh, "[worker] 拒绝：交接文件已被消费或不可写（防重放）")
                return 4
            return start_and_wait(argv, payload.get("cwd") or None, port,
                                  payload.get("wait"), fh)

        pid = int(payload["pid"])
        log_line(fh, "[worker] 目标: pid=%s port=%d cwd=%s" % (pid, port, payload.get("cwd")))

        verified, children, err = verify_target(payload, fh)
        if err:
            log_line(fh, "[worker] 拒绝：%s（不结束任何进程）" % err)
            return 5
        if not consume_handoff(args.handoff):
            log_line(fh, "[worker] 拒绝：交接文件已被消费或不可写（防重放）")
            return 4

        # 停止：只结束已逐一确认的 PID（不带 /T）。每个 PID 执行前再核验一次身份。
        for child, creason in reversed(children):
            cpid = child.get("ProcessId")
            cur = proc_info(cpid)
            if not cur or not same_identity(identity_snapshot(cur), identity_snapshot(child)):
                log_line(fh, "[worker] 中止：后代 PID %s 身份在核验后发生变化" % cpid)
                return 5
            log_line(fh, "[worker] 结束后代 PID %s（%s）" % (cpid, creason))
            kill_pid(cpid, fh)

        cur = proc_info(pid)
        if (not cur or not same_identity(identity_snapshot(cur), verified["snapshot"])
                or pid not in netstat_listeners(port)):
            log_line(fh, "[worker] 中止：目标 PID %s 身份在核验后发生变化" % pid)
            return 5
        log_line(fh, "[worker] 结束目标 PID %s" % pid)
        kill_pid(pid, fh)

        killed = set([pid] + [c.get("ProcessId") for c, _r in children])
        deadline = time.time() + 15
        while time.time() < deadline and port_busy(port):
            time.sleep(0.5)
        if port_busy(port):
            unknown = [p for p in netstat_listeners(port) if p not in killed]
            log_line(fh, "[worker] 失败：端口 %d 仍被占用；非本次确认 PID：%s" % (port, unknown or "无"))
            return 6

        # 启动 + 等就绪
        return start_and_wait(argv, payload.get("cwd") or None, port, payload.get("wait"), fh)
    finally:
        try:
            fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- 入口
def main(argv=None):
    ap = argparse.ArgumentParser(description="WBRelay 网关重启（停 → 起 → 等就绪）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--check", "--dry-run", action="store_true", dest="check",
                    help="只探测监听进程与启动方式，不做任何操作")
    ap.add_argument("--cwd", default="", help="显式指定工作目录（默认按旧进程脚本路径推断）")
    ap.add_argument("--start", action="store_true",
                    help="冷启动：端口没有网关时用写死入口（dist\\WBRelay.exe 或 relay.py）拉起并等就绪")
    ap.add_argument("--wait", type=int, default=60, help="就绪等待上限（秒）")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--handoff", default="", help=argparse.SUPPRESS)
    ap.add_argument("--token", default="", help=argparse.SUPPRESS)
    ap.add_argument("--log", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")      # 路径含生僻字符也不让打印把工具打崩
    except Exception:
        pass

    if args.worker:
        # 外部直接调用隐藏 --worker（没有父进程交接）必须拒绝
        return run_worker(args)

    pids = netstat_listeners(args.port)
    print("[探测] 端口 %d 监听 PID: %s" % (args.port, pids or "（无）"))
    if args.start:
        payload, rc = plan_cold_start(args, pids)
        if payload is None:
            return rc
        return dispatch_worker(payload, args, cold=True)
    if not pids:
        print("[退出] 没有进程监听该端口；本工具只做「重启」。")
        print("       如果是要冷启动（例如升级后第一次拉起），请加 --start。")
        return 2
    if len(pids) != 1:
        print("[退出] 端口 %d 有多个监听 PID %s：无法确定目标，拒绝操作。" % (args.port, pids))
        return 2
    pid = pids[0]
    if pid == os.getpid():
        print("[退出] 目标 PID 就是本进程，拒绝执行。")
        return 2
    info = proc_info(pid)
    state = http_json(args.port, "/api/state")
    print("[探测] 命令行  : %s" % (info.get("CommandLine") or "?"))
    print("[探测] 可执行  : %s" % (info.get("ExecutablePath") or "?"))
    print("[探测] 启动时间: %s" % (info.get("CreationDate") or "?"))
    print("[探测] /api/state: %s" % ("OK（含 suppliers）" if isinstance(state, dict) and "suppliers" in state
                                      else "无响应或非本网关"))
    ok, why = classify_wbrelay(info, state, args.port)
    if not ok:
        print("[退出] 无法确认 PID %d 是本项目网关：%s" % (pid, why))
        print("       本工具没有 --force；认不出来就不碰（R7）。")
        return 3

    snap = identity_snapshot(info)
    argv = split_cmdline(info.get("CommandLine") or "")
    marker = command_marker(argv, args.port)
    cwd = resolve_cwd(argv, args.cwd)
    if not cwd or not os.path.isdir(cwd):
        print("[退出] 推断不出工作目录（旧进程用相对路径启动时无法定位），请用 --cwd 指定。")
        return 3
    ok, why, argv = validate_start_argv(argv, cwd)
    if not ok:
        print("[退出] 启动命令不合规：%s" % why)
        return 3

    children = descendants(pid)
    owned, _reasons, bad = classify_own_children(children, snap, marker)
    if not owned:
        print("[退出] 存在无法确认归属的后代 PID %s：%s" % (bad[0].get("ProcessId"), bad[1]))
        return 3

    print("[计划] 启动命令行: %s" % " ".join('"%s"' % a if " " in a else a for a in argv))
    print("[计划] 工作目录  : %s" % cwd)
    print("[计划] 后代进程  : %s" % ([c.get("ProcessId") for c in children] or "无"))
    print("[计划] 动作      : 停 pid=%d → 用上面命令重启 → 等 /api/state 就绪" % pid)
    if args.check:
        print("[check] 仅探测，未做任何改动。")
        return 0

    payload = {
        "mode": "restart", "pid": pid, "port": args.port, "cwd": cwd, "argv": argv,
        "identity": snap, "marker": marker,
        "parent_pid": os.getpid(), "created_at": time.time(),
        "argv_hash": hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode("utf-8")).hexdigest(),
    }
    return dispatch_worker(payload, args, cold=False)


if __name__ == "__main__":
    sys.exit(main())
