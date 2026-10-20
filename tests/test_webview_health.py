# -*- coding: utf-8 -*-
"""wbrelay/webviewhealth.py 的纯逻辑测试（不碰真实进程操作）。

覆盖：
  W1  analyze：能认出「以本进程为父」的 WebView2 根，并数出它的子进程
  W2  analyze：没有根 → root=None / children=0
  W3  analyze：快照为 None（探测失败）→ root=None / children=0，不抛异常
  W4  analyze：父进程不是本进程的 msedgewebview2 不计入
  W5  filter_renderers：只认 --type=renderer；给了 host_exe 还要 --webview-exe-name 匹配
  W6  filter_renderers：单条 dict / None / 非法输入都能安全处理
  W7  RecoveryPolicy：子进程数增长不触发；持平不触发；变少才触发
  W8  RecoveryPolicy：曾经见过进程树、之后根消失 → 触发
  W9  RecoveryPolicy：冷却期内不重复触发
  W10 RecoveryPolicy：恢复次数有上限，用完不再触发
  W11 RecoveryPolicy：recovered() 重新学习基线并补满次数
  W12 snapshot_processes：Windows 上返回快照且包含本进程（只读）
  W13 webview_tree：不存在的 PID → root=None / children=0

用法：python tests/test_webview_health.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wbrelay import webviewhealth as wh   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def p(pid, ppid, name):
    return {"pid": pid, "ppid": ppid, "name": name}


def main():
    # ---- W1 / W2 / W3 / W4：analyze ----
    procs = [
        p(10, 1, "explorer.exe"),
        p(20, 10, "msedgewebview2.exe"),      # 根：父是本进程
        p(21, 20, "msedgewebview2.exe"),      # crashpad
        p(22, 20, "msedgewebview2.exe"),      # gpu
        p(23, 20, "msedgewebview2.exe"),      # utility
        p(24, 20, "msedgewebview2.exe"),      # renderer
        p(25, 99, "msedgewebview2.exe"),      # 别的 App 的根，父不是本进程
        p(26, 25, "msedgewebview2.exe"),      # 别的 App 的子进程
    ]
    tree = wh.analyze(procs, 10)
    check("W1 认出 WebView2 根并数出 4 个子进程",
          tree["root"] == 20 and tree["children"] == 4, repr(tree))

    check("W2 没有根 → root=None / children=0",
          wh.analyze([p(1, 0, "system")], 10) == {"root": None, "children": 0})
    check("W3 快照为 None → 安全降级",
          wh.analyze(None, 10) == {"root": None, "children": 0})
    check("W4 父进程不是本进程的 msedgewebview2 不计入",
          wh.analyze(procs, 12345) == {"root": None, "children": 0})

    # 渲染进程消失：子进程从 4 变 3
    missing = [x for x in procs if x["pid"] != 24]
    check("W4b 渲染进程消失后子进程数 -1",
          wh.analyze(missing, 10)["children"] == 3)

    # ---- W5 / W6：filter_renderers ----
    rows = [
        {"ProcessId": 31, "CommandLine": '"x\\msedgewebview2.exe" --type=renderer '
                                          '--webview-exe-name=WBRelay-1.1.0.exe'},
        {"ProcessId": 32, "CommandLine": '"x\\msedgewebview2.exe" --type=gpu-process '
                                          '--webview-exe-name=WBRelay-1.1.0.exe'},
        {"ProcessId": 33, "CommandLine": '"x\\msedgewebview2.exe" --type=renderer '
                                          '--webview-exe-name=OtherApp.exe'},
        {"ProcessId": 34, "CommandLine": '"x\\msedgewebview2.exe" --type=utility'},
    ]
    check("W5a 只认 --type=renderer",
          wh.filter_renderers(rows) == [31, 33])
    check("W5b 指定 host_exe 后还要 --webview-exe-name 匹配",
          wh.filter_renderers(rows, "WBRelay-1.1.0.exe") == [31])
    check("W6a 单条 dict 也能处理",
          wh.filter_renderers(rows[0], "WBRelay-1.1.0.exe") == [31])
    check("W6b None / 非法输入 → 空列表",
          wh.filter_renderers(None) == [] and wh.filter_renderers("x") == [])

    # ---- W7 / W8 / W9 / W10 / W11：RecoveryPolicy ----
    pol = wh.RecoveryPolicy(cooldown=30.0, max_attempts=2)
    check("W7a 首次见到进程树：不触发", pol.observe(20, 5, now=1000)[0] is False)
    check("W7b 数量增长：不触发", pol.observe(20, 6, now=1001)[0] is False)
    check("W7c 数量持平：不触发", pol.observe(20, 6, now=1002)[0] is False)
    suspect, why = pol.observe(20, 5, now=1003)
    check("W7d 数量变少：触发核验", suspect is True and "少于峰值" in why, why)

    pol2 = wh.RecoveryPolicy()
    pol2.observe(20, 5, now=2000)
    check("W8 根进程消失（曾见过）→ 触发",
          pol2.observe(None, 0, now=2001)[0] is True)

    pol3 = wh.RecoveryPolicy(cooldown=30.0, max_attempts=2)
    pol3.observe(20, 5, now=3000)
    pol3.observe(20, 4, now=3001)
    pol3.note_attempt(now=3001)
    check("W9 冷却期内不重复触发", pol3.observe(20, 4, now=3010)[0] is False)
    check("W9b 冷却结束后可再次触发", pol3.observe(20, 4, now=3040)[0] is True)

    check("W10a 次数未用满时允许恢复", pol3.can_attempt() is True)
    pol3.note_attempt(now=3040)
    check("W10b 次数用满后不再恢复", pol3.can_attempt() is False)

    pol4 = wh.RecoveryPolicy(cooldown=30.0, max_attempts=2)
    pol4.note_attempt(now=4000)
    pol4.recovered(children=5, now=4001)
    check("W11 recovered() 补满次数并重置基线",
          pol4.can_attempt() is True and pol4.peak == 5)

    # ---- W12 / W13：真实只读探测 ----
    if os.name == "nt":
        snap = wh.snapshot_processes()
        check("W12 Toolhelp32 快照可用且包含本进程",
              isinstance(snap, list) and len(snap) > 10
              and any(r.get("pid") == os.getpid() for r in snap),
              "rows=%d" % (len(snap) if snap else -1))
    else:
        print("[SKIP] W12 仅 Windows 有 Toolhelp32")
        check("W12 非 Windows 返回 None", wh.snapshot_processes() is None)

    check("W13 不存在的 PID → root=None / children=0",
          wh.webview_tree(99999999) == {"root": None, "children": 0})

    print("\n===== webviewhealth：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
