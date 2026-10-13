# -*- coding: utf-8 -*-
"""WBRelay 端到端测试（脚本**自己**拉起 mock 上游 + 隔离网关，全程零真实依赖）。

  mock1      : 127.0.0.1:18788（测试自己启动 tests/mock_server.py）
  mock2      : 127.0.0.1:18789（测试自己启动）
  WBRelay    : 127.0.0.1:18790（--data-dir <临时目录> --no-ide-inject --headless）

全部端口：18788 / 18789（mock）、18790（隔离网关）、18999（「全败」用的**关闭**端口）。

  规则：
  · **不**使用 8789 的在用实例，也**不**触碰 8788 等任何真实外部服务
  · **预检全部端口**（18788/18789/18790/18999）；任一被占用就退出（绝不抢占、绝不按端口盲杀）
  · 目标实例的配置由本脚本在临时数据目录里自建；结束时确保子进程全部退出、端口全部释放
  · **进程所有权**：只能终止本脚本 Popen 出来的 mock/网关子进程；终止前核对
    「进程对象 + PID + 可执行文件 + 命令行」四项，不一致就拒绝并打印所有权信息
  · 不读取、不复制 %APPDATA%\\WBRelay；子进程的 APPDATA / LOCALAPPDATA / USERPROFILE / HOME
    全部指向临时目录 —— 它们既碰不到真实数据目录，也碰不到真实 `~/.codex`
  · **不动注册表**：测试里的配置保存不包含 autostart 字段，且全程不调用网关自启函数
    （回归见 tests/test_settings_autostart.py）

覆盖：配置保存 → API Key 鉴权开关 → 路由与故障切换 → 全败报错 → 无匹配模型 → SSE 流式。

用法：python tests/test_e2e.py
退出码：0=全绿，1=有失败，2=环境不满足（端口被占用等）
"""
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay.py")
MOCK = os.path.join(ROOT, "tests", "mock_server.py")
GW_PORT = 18790
MOCK1_PORT = 18788
MOCK2_PORT = 18789
PORT_0 = 18999          # 「全败」用例用的关闭端口
ALL_PORTS = (MOCK1_PORT, MOCK2_PORT, GW_PORT, PORT_0)
TMP_PREFIX = "wbrelay-e2e-"
KEY = "sk-test-FAKE-DOWNLINK-KEY-0001"
MODEL = "gpt-6-luna"
MODEL2 = "gpt-6-sol"
GW = "http://127.0.0.1:%d" % GW_PORT

passed = 0
failed = 0
skipped = 0
procs = []            # [{"label", "proc", "pid", "script", "marker", "identity"}]
CLEANUP_FAILED = []


def ok(label, detail=""):
    global passed
    passed += 1
    print("  [PASS] %s %s" % (label, ("  " + detail if detail else "")))


def bad(label, detail=""):
    global failed
    failed += 1
    print("  [FAIL] %s %s" % (label, ("  " + detail if detail else "")))


def skip(label, why=""):
    global skipped
    skipped += 1
    print("  [SKIP] %s %s" % (label, why))


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def call(path, method="GET", body=None, bearer=None, timeout=60):
    headers = {}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(GW + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def j(status_raw):
    status, raw = status_raw
    try:
        return status, json.loads(raw)
    except Exception:
        return status, {"_raw": raw[:160]}


def stream_call(path, body, bearer=None, timeout=60):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(GW + path, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def spawn(label, argv, env, script, marker=None):
    print("  启动 %s：%s" % (label, " ".join(argv[-3:])))
    p = subprocess.Popen(argv, cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    procs.append({"label": label, "proc": p, "pid": p.pid, "script": script,
                  "marker": marker or "", "identity": process_identity(p.pid)})
    return p


def process_identity(pid):
    """**只读**查询进程的可执行路径与命令行（仅用于核对「这是我们自己起的子进程」）。

    优先 wmic，取不到再退 PowerShell CIM；两者都不可用时返回空值（此时所有权判定
    退化为「我们自己的 Popen 对象 + 同一个 PID」）。
    """
    exe = cmd = ""
    try:
        out = subprocess.run(["wmic", "process", "where", "ProcessId=%d" % pid,
                              "get", "ExecutablePath,CommandLine", "/format:list"],
                             capture_output=True, text=True, timeout=15)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("CommandLine="):
                cmd = line[len("CommandLine="):]
            elif line.startswith("ExecutablePath="):
                exe = line[len("ExecutablePath="):]
    except Exception:
        pass
    if not cmd and not exe:
        try:
            ps = ("$p = Get-CimInstance Win32_Process -Filter 'ProcessId=%d';"
                  "if ($p) { 'CMD=' + $p.CommandLine; 'EXE=' + $p.ExecutablePath }" % pid)
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=15)
            for line in (out.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("CMD="):
                    cmd = line[4:]
                elif line.startswith("EXE="):
                    exe = line[4:]
        except Exception:
            pass
    return {"exe": exe, "cmdline": cmd}


def pid_alive(pid):
    """只读查询 PID 是否还存在；查询工具不可用时返回 None。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout or ""
    except Exception:
        return None
    return str(pid) in out


def owned_child(entry):
    """返回 (ok, 原因)：逐项核对通过才允许终止该子进程。

    判定依据（**不按端口、不按 basename**）：
      ① Popen 对象 + PID 与创建时一致且进程仍在运行
      ② 可执行文件**绝对路径**与创建时记录的一致（不一致 → 拒绝）
      ③ 命令行里必须出现创建时记录的脚本绝对路径
      ④ 若记录了指纹 marker（网关的临时数据目录），命令行里必须出现它
    wmic/CIM 都不可用时退化为「Popen 对象 + PID」（这不是端口/basename 判定）。
    """
    p = entry["proc"]
    if p.pid != entry["pid"]:
        return False, "PID 与创建时不一致（记录 %s / 当前 %s）" % (entry["pid"], p.pid)
    if p.poll() is not None:
        return False, "进程已退出（无需终止）"
    ident = process_identity(p.pid)
    entry["identity_at_cleanup"] = ident
    if not ident["cmdline"] and not ident["exe"]:
        return True, "命令行不可读（wmic/CIM 都不可用）：仅据 Popen 对象 + PID 判定为本测试子进程"
    if entry["script"] not in (ident["cmdline"] or ""):
        return False, ("命令行不属于本测试：%r 不在 %r"
                       % (entry["script"], (ident["cmdline"] or "")[:160]))
    recorded_exe = (entry.get("identity") or {}).get("exe") or ""
    if recorded_exe and ident["exe"] and \
            os.path.normcase(os.path.abspath(ident["exe"])) != os.path.normcase(os.path.abspath(recorded_exe)):
        return False, "可执行文件绝对路径与创建时不一致：%r != %r" % (ident["exe"], recorded_exe)
    if entry.get("marker") and entry["marker"] not in (ident["cmdline"] or ""):
        return False, "命令行缺少本轮唯一 marker：%r" % entry["marker"]
    return True, ""


def cleanup_children():
    """只终止本脚本自己 Popen 的子进程；任何一项不符合就拒绝并记失败。"""
    global failed
    refused = []
    for entry in procs:
        okc, why = owned_child(entry)
        if not okc:
            if entry["proc"].poll() is None:
                refused.append((entry, why))
            continue
        p = entry["proc"]
        try:
            p.terminate()
        except Exception as exc:
            print("  [WARN] %s 终止请求失败：%s" % (entry["label"], exc))
    deadline = time.time() + 10
    for entry in procs:
        p = entry["proc"]
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if p.poll() is None:
            okc, why = owned_child(entry)
            if okc:
                try:
                    p.kill()
                except Exception:
                    pass
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:
            pass
    if refused:
        failed += 1
        print("  [FAIL] 拒绝终止不属于本测试的进程（未动它们）：")
        for entry, why in refused:
            print("         label=%s pid=%s 请求行=%r 原因=%s"
                  % (entry["label"], entry["pid"], entry["script"], why))
    left = []
    for entry in procs:
        p = entry["proc"]
        if p.poll() is None:
            left.append((entry["label"], entry["pid"], p.poll()))
        elif pid_alive(entry["pid"]):
            left.append((entry["label"], entry["pid"], "Popen 已退出但 PID 仍存在"))
    if left:
        failed += 1
        print("  [FAIL] 子进程清理未完成，资源所有权信息：")
        for entry in procs:
            print("         label=%s pid=%s script=%r identity_at_spawn=%r identity_at_cleanup=%r"
                  % (entry["label"], entry["pid"], entry["script"], entry.get("identity"),
                     entry.get("identity_at_cleanup")))
        print("         未退出：%r" % (left,))
    else:
        print("  所有子进程已退出（%d 个：PID 与端口双重核对）" % len(procs))


def wait_port(proc, port, expect_label, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError("%s 提前退出（code=%s）：\n%s" % (expect_label, proc.returncode, out[-1500:]))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main():
    global failed
    print("=" * 72)
    print(" WBRelay E2E（自建 mock + 隔离实例；不使用 8789 / 不碰真实外部进程）")
    print("=" * 72)

    # ---- 端口预检：**全部端口**（含 18999）被占用直接退出，绝不抢占 ----
    busy = [p for p in ALL_PORTS if not port_free(p)]
    if busy:
        print("端口被占用，拒绝运行（不会抢占或结束占用者）：%s" % busy)
        print("请先释放这些端口，或用 tests/test_routing_semantics.py 跑进程内用例。")
        return 2

    tmp = tempfile.mkdtemp(prefix=TMP_PREFIX)
    # 清理前必须先验证：这是本测试自己 mkdtemp 出来、且位于系统临时根下的目录
    assert os.path.basename(tmp).startswith(TMP_PREFIX), tmp
    assert os.path.normcase(os.path.dirname(os.path.abspath(tmp))) == \
        os.path.normcase(os.path.abspath(tempfile.gettempdir())), tmp
    data_dir = os.path.join(tmp, "data")
    base_env = dict(os.environ)
    # 双保险：即使被测程序丢了 --data-dir，默认数据目录也落在临时根里
    base_env["APPDATA"] = tmp
    base_env["LOCALAPPDATA"] = os.path.join(tmp, "localappdata")
    # 家目录也重定向：子进程即使去算 ~/.codex，也只会落在临时根里
    base_env["USERPROFILE"] = tmp
    base_env["HOME"] = tmp
    base_env["PYTHONIOENCODING"] = "utf-8"
    base_env["PYTHONUTF8"] = "1"
    for k in ("WBRELAY_DATA_DIR", "WBRELAY_UPSTREAM_DIR", "WBRELAY_READONLY",
              "WBRELAY_NO_IDE_INJECT", "WBRELAY_HEADLESS"):
        base_env.pop(k, None)

    try:
        # ---- 1) mock 上游（测试自己启动）----
        m1 = spawn("mock1", [sys.executable, MOCK, "MOCK1", str(MOCK1_PORT)], base_env, MOCK)
        m2 = spawn("mock2", [sys.executable, MOCK, "MOCK2", str(MOCK2_PORT)], base_env, MOCK)
        if not wait_port(m1, MOCK1_PORT, "mock1") or not wait_port(m2, MOCK2_PORT, "mock2"):
            bad("mock 上游启动", "端口未就绪")
            return 1
        ok("mock 上游已就绪（18788 / 18789）")

        # ---- 2) 隔离配置（本脚本自建，含假 Key）----
        os.makedirs(data_dir, exist_ok=True)
        settings = {
            "port": GW_PORT, "default_timeout_sec": 30, "cooldown_sec": 3,
            "cooldown_429_sec": 2, "usage_retention_days": 0, "usage_refresh_sec": 5,
            "require_api_key": False, "api_keys": [{"id": "k1", "name": "Codex",
                                                    "key": KEY, "enabled": True}],
            "suppliers": [
                {"id": "sup_mock_1", "name": "MOCK1", "base_url": "http://127.0.0.1:%d/v1" % MOCK1_PORT,
                 "api_key": "sk-test-FAKE-KEY-AAA", "wire_api": "responses",
                 "models": [], "enabled": True, "timeout_sec": 20},
                {"id": "sup_mock_2", "name": "MOCK2", "base_url": "http://127.0.0.1:%d/v1" % MOCK2_PORT,
                 "api_key": "sk-test-FAKE-KEY-BBB", "wire_api": "responses",
                 "models": [], "enabled": True, "timeout_sec": 20},
            ],
            "model_prices": {}, "models_enabled": {}, "group_models_enabled": {},
            "upstream_enabled": False, "autostart": False,
        }
        write_atomic(os.path.join(data_dir, "settings.json"),
                     json.dumps(settings, ensure_ascii=False, indent=2))
        # 预置模型缓存（免去「拉取模型」这条会访问 OpenRouter 的网络路径）
        suppliers_cache = {}
        for sid, port, name in (("sup_mock_1", MOCK1_PORT, "MOCK1"),
                                ("sup_mock_2", MOCK2_PORT, "MOCK2")):
            suppliers_cache[sid] = {"name": name, "base_url": "http://127.0.0.1:%d/v1" % port,
                                    "models": [{"id": MODEL, "context_window": 1000,
                                                "max_output_tokens": 100,
                                                "reasoning_efforts": ["low"]},
                                               {"id": MODEL2, "context_window": 1000,
                                                "max_output_tokens": 100,
                                                "reasoning_efforts": ["low"]}],
                                    "error": None, "ok": True}
        write_atomic(os.path.join(data_dir, "models_cache.json"),
                     json.dumps({"fetched_at": time.time(),
                                 "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "suppliers": suppliers_cache, "errors": {},
                                 "ok_count": 2, "total_suppliers": 2},
                                ensure_ascii=False, indent=2))
        # 预置模型档案（避免任何模型缺档触发 OpenRouter 自动补缺）
        write_atomic(os.path.join(data_dir, "model_profiles.json"),
                     json.dumps({MODEL: {"reasoning_efforts": ["low"], "context_window": 1000},
                                 MODEL2: {"reasoning_efforts": ["low"], "context_window": 1000}},
                                ensure_ascii=False, indent=2))
        ok("隔离数据目录与配置已创建（本脚本自建，无真实数据）", data_dir)

        # ---- 3) 隔离网关实例 ----
        gw = spawn("gateway", [sys.executable, RELAY, "--data-dir", data_dir,
                               "--no-ide-inject", "--port", str(GW_PORT), "--headless"],
                   base_env, RELAY, marker=data_dir)
        if not wait_port(gw, GW_PORT, "gateway", timeout=60):
            bad("隔离网关启动", "端口 %d 未就绪" % GW_PORT)
            return 1
        up = False
        for _ in range(40):
            st, _b = call("/api/state", timeout=5)
            if st == 200:
                up = True
                break
            time.sleep(0.25)
        if not up:
            bad("隔离网关 API 就绪", "GET /api/state 未返回 200")
            return 1
        ok("隔离网关已就绪（http://127.0.0.1:%d）" % GW_PORT)

        # ---- 4) 面板登录已移除 ----
        st, _ = call("/api/state")
        ok("面板登录已移除（无鉴权即可读状态）", "status=%s" % st) if st == 200 \
            else bad("状态接口", "status=%s" % st)

        # ---- 5) 分组模型启用（默认全启用）----
        st, _ = j(call("/api/models/enabled", "POST", {"group_models_enabled": {}}))
        ok("清空组级启用（默认全启用）", "status=%s" % st) if st == 200 \
            else bad("重置启停", "status=%s" % st)

        # ---- 6) 路由：模型 → mock1 优先 ----
        st, body = j(call("/v1/responses", "POST", {"model": MODEL, "input": "hi"}, bearer=KEY))
        text = ""
        if st == 200:
            try:
                text = body["output"][0]["content"][0]["text"]
            except Exception:
                text = ""
        ok("模型 → mock1 优先", "status=%s text=%s" % (st, text)) if st == 200 and "MOCK1" in text \
            else bad("mock1 优先", "status=%s %s" % (st, str(body)[:160]))

        # ---- 7) 故障切换：把 mock1 指到关闭端口 → 自动切 mock2 ----
        switch = json.loads(json.dumps(settings))
        switch["suppliers"][0]["base_url"] = "http://127.0.0.1:%d/v1" % PORT_0
        st, _ = j(call("/api/suppliers", "POST", {"suppliers": switch["suppliers"]}))
        st2, body = j(call("/v1/responses", "POST", {"model": MODEL, "input": "hi"}, bearer=KEY))
        ok("失败必切换：mock1 不可用 → 自动切 mock2",
           "status=%s" % st2) if st == 200 and st2 == 200 and "MOCK2" in json.dumps(body) \
            else bad("故障切换", "save=%s status=%s %s" % (st, st2, str(body)[:160]))
        st, state = j(call("/api/state"))
        cooling = [s for s in state.get("suppliers", []) if s["id"] == "sup_mock_1"]
        ok("失败的那家进入冷却", str(cooling and cooling[0]["cooling"])) \
            if cooling and cooling[0]["cooling"] else bad("冷却", str(cooling))

        # ---- 8) 配置保存即清冷却 ----
        st, _ = j(call("/api/suppliers", "POST", {"suppliers": switch["suppliers"]}))
        st, state = j(call("/api/state"))
        cooling = [s for s in state.get("suppliers", []) if s["id"] == "sup_mock_1"]
        ok("保存即清冷却（核心语义）") if st == 200 and cooling and not cooling[0]["cooling"] \
            else bad("清冷却", str(cooling))

        # ---- 9) 全败必报错（两家都指向关闭端口）----
        dead = json.loads(json.dumps(settings))
        dead["suppliers"][0]["base_url"] = "http://127.0.0.1:%d/v1" % PORT_0
        dead["suppliers"][1]["base_url"] = "http://127.0.0.1:%d/v1" % PORT_0
        j(call("/api/suppliers", "POST", {"suppliers": dead["suppliers"]}))
        st, body = j(call("/v1/responses", "POST", {"model": MODEL, "input": "hi"}, bearer=KEY))
        ok("全败 → 报错（不正 200）", "status=%s" % st) if st >= 400 else bad("全败", "status=%s" % st)
        ok("全败错误含 all suppliers failed", str(body)[:200]) \
            if "all suppliers failed" in json.dumps(body) else bad("全败信息", str(body)[:200])

        # ---- 10) 无匹配模型 → 503 ----
        st, body = j(call("/v1/responses", "POST", {"model": "no-such-model", "input": "hi"},
                          bearer=KEY))
        ok("无匹配模型 → 503", "status=%s" % st) if st == 503 \
            else bad("无匹配", "status=%s" % st)

        # ---- 11) 恢复正常配置 + 鉴权开关 ----
        j(call("/api/suppliers", "POST", {"suppliers": settings["suppliers"]}))
        st, _ = j(call("/api/settings", "POST", {"require_api_key": True}))
        ok("开启 API Key 校验", "status=%s" % st) if st == 200 else bad("开启校验", "status=%s" % st)
        st, body = j(call("/v1/responses", "POST", {"model": MODEL, "input": "hi"},
                          bearer="wrong-key"))
        ok("错 Key → 401", "status=%s" % st) if st == 401 else bad("鉴权", "status=%s" % st)
        st, _ = j(call("/api/settings", "POST", {"require_api_key": False}))
        st2, body = j(call("/v1/responses", "POST", {"model": MODEL, "input": "hi"},
                           bearer="wrong-key"))
        ok("关闭校验后错 Key 放行", "status=%s" % st2) if st2 == 200 else bad("放行", "status=%s %s" % (st2, str(body)[:120]))

        # ---- 12) 流式 SSE ----
        try:
            st, raw = stream_call("/v1/responses", {"model": MODEL2, "input": "hi",
                                                    "stream": True}, bearer=KEY)
            ok("流式 SSE 完整（含 [DONE]）", "len=%d" % len(raw)) if "[DONE]" in raw \
                else bad("流式", "len=%d" % len(raw))
        except Exception as exc:
            bad("流式 SSE 请求", "%s" % exc)

        # ---- 13) 边界：超长模型名被拒 ----
        st, _ = j(call("/v1/responses", "POST", {"model": "x" * 400, "input": "hi"}, bearer=KEY))
        ok("超长模型名 → 400", "status=%s" % st) if st == 400 else bad("模型名边界", "status=%s" % st)

        # ---- 14) R6：外部上游启停接口已移除（不启动/停止任何进程）----
        st, body = j(call("/api/upstream/start", "POST", {}))
        ok("POST /api/upstream/start → 409（不启动外部进程）",
           "status=%s" % st) if st == 409 and "R6" in json.dumps(body) \
            else bad("R6 启动接口", "status=%s %s" % (st, str(body)[:120]))
        st, body = j(call("/api/upstream/stop", "POST", {}))
        ok("POST /api/upstream/stop → 409（不停止进程/计划任务）",
           "status=%s" % st) if st == 409 and "R6" in json.dumps(body) \
            else bad("R6 停止接口", "status=%s %s" % (st, str(body)[:120]))

        # ---- 15) 进程所有权核对：命令行不匹配就拒绝终止（绝不盲杀）----
        foreign = {"label": "foreign-probe", "proc": gw, "pid": gw.pid,
                   "script": "definitely-not-our-command-line", "identity": {}}
        okc, why = owned_child(foreign)
        ok("所有权核对拒绝命令行不匹配的进程", why) \
            if not okc else bad("所有权核对", "本应拒绝，实际通过")
        okc2, why2 = owned_child(procs[-1])
        ok("所有权核对放行本测试自己的网关子进程", why2) \
            if okc2 else bad("所有权放行", why2)

        # ---- 16) 隔离副作用：配置保存没碰注册表 / Codex / IDE / 上游 ----
        with open(os.path.join(data_dir, "settings.json"), "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        ok("autostart 仍为 False（保存配置不会去动注册表启动项）",
           repr(saved.get("autostart"))) if saved.get("autostart") is False \
            else bad("自启字段", repr(saved.get("autostart")))
        codex_cfg = os.path.join(tmp, ".codex", "config.toml")
        ok("未生成真实 Codex 配置（子进程的 ~ 被指向临时目录）",
           "path=%s" % codex_cfg) if not os.path.exists(codex_cfg) else bad("Codex 配置", codex_cfg)
        ok("--no-ide-inject 下未创建 IDE 备份目录",
           "ide_backups") if not os.path.isdir(os.path.join(data_dir, "ide_backups")) \
            else bad("IDE 备份", os.path.join(data_dir, "ide_backups"))
        ok("上游管理在本测试中关闭（不会触碰外部上游）",
           "upstream_enabled=%s" % saved.get("upstream_enabled")) \
            if saved.get("upstream_enabled") is False else bad("上游开关", repr(saved.get("upstream_enabled")))
    finally:
        # ---- 清理：只终止本脚本自己 Popen 的子进程（四项核对）----
        cleanup_children()
        time.sleep(0.5)
        still = [p for p in ALL_PORTS if not port_free(p)]
        if still:
            failed += 1
            print("  [FAIL] 端口未释放（含 18999）：%s" % still)
        else:
            print("  全部测试端口已释放：%s" % list(ALL_PORTS))
        # 清理前先核对路径：必须是本测试 mkdtemp 出来、且位于系统临时根下的目录
        if (os.path.basename(tmp).startswith(TMP_PREFIX) and
                os.path.normcase(os.path.dirname(os.path.abspath(tmp))) ==
                os.path.normcase(os.path.abspath(tempfile.gettempdir()))):
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            failed += 1
            print("  [FAIL] 拒绝删除不属于本测试的临时目录：%s" % tmp)

    print("\nRESULT: PASS=%d FAIL=%d SKIP=%d" % (passed, failed, skipped))
    return 0 if failed == 0 else 1


def write_atomic(path, text):
    tmp = path + ".e2e.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
