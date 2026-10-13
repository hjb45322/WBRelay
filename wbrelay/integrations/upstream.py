# -*- coding: utf-8 -*-
"""外部上游项目（自建中转服务）的**只读**状态探测。

规则（AGENTS.md R6 / R5）：
  · R6 **上游项目只读**：不写、不改、**不启停** —— 本项目不提供「启动 / 停止上游」的能力，
    也不代为操作它的 Windows 计划任务；这类操作一律由用户在该服务自身完成。
  · R5 **外部进程只能由用户显式操作**：没有进程守护、没有登录自启、没有后台自动拉起。
  · 未配置 `upstream_dir` 时整体不激活；所有探测都不写入外部目录。

移除记录（开源前重构的第二轮发布修复）：
  · `upstream_start()` / `upstream_stop()` / `_find_python_for()` 已删除 ——
    stop 的 PowerShell（Stop-ScheduledTask / Disable-ScheduledTask / Stop-Process）
    与 start 的 `subprocess.Popen` 都属于 R6 禁止的「启停外部上游」操作。
  · 旧接口 `/api/upstream/start|stop` 返回明确的策略错误，见 `web/api_upstream.py`。

安全：
  · 计划任务名 / 上游目录**不拼进 PowerShell 脚本字符串**，改为通过环境变量传入，
    并做字符集校验；只读查询也不会因为脏值而改变行为。
"""
import os
import re
import socket
import subprocess

from ..log import _log
from ..settings import load_settings
from ..state import _NO_WINDOW
from ..validate import check_task_name

UPSTREAM_HOST = "127.0.0.1"       # 外部上游只允许本机回环地址

# 能力开关：供状态接口与看板明确展示「该功能已按发布约束移除」，而不是静默失败
EXTERNAL_CONTROL_SUPPORTED = False      # 外部上游启停（R6：不启停）已在第二轮修复中移除
WATCHDOG_SUPPORTED = False      # 进程守护（自动拉起外部进程）已在开源前重构中移除
UPSTREAM_AUTOSTART_SUPPORTED = False   # 外部上游登录自启（写 HKCU Run）已移除


def upstream_dir():
    """外部上游项目目录：只认显式配置（settings.upstream_dir 或 WBRELAY_UPSTREAM_DIR）。
    不做任何路径猜测，避免在他人机器上误命中无关目录。返回空串表示未配置。"""
    cfg = load_settings().get("upstream_dir")
    if cfg and os.path.isdir(cfg):
        return cfg
    env = os.environ.get("WBRELAY_UPSTREAM_DIR")
    if env and os.path.isdir(env):
        return env
    return ""


def upstream_task_name():
    """上游若由 Windows 计划任务托管，任务名取 settings.upstream_task。

    含非法字符时返回空串（调用方据此跳过计划任务查询），并把原因写日志 ——
    既避免命令注入，也不静默地拿脏值去执行。
    """
    raw = str(load_settings().get("upstream_task") or "")
    name, err = check_task_name(raw)
    if err:
        _log("[WBRelay] 上游计划任务名非法，已忽略：%s" % err)
        return ""
    return name


def upstream_port():
    """从上游目录的启动脚本解析端口（PORT= / --port），解析不到用 wb_proxy.py 默认 8788。"""
    d = upstream_dir()
    if d:
        try:
            for f in os.listdir(d):
                if f.lower().endswith((".bat", ".cmd", ".command", ".sh")):
                    try:
                        with open(os.path.join(d, f), "r", encoding="utf-8", errors="ignore") as fh:
                            txt = fh.read()
                    except Exception:
                        continue
                    m = re.search(r"(?:PORT|port)[\s=:]+(\d{2,5})", txt)
                    if m:
                        return int(m.group(1))
        except Exception:
            pass
    return 8788


def upstream_online():
    """TCP 探测上游端口是否在线（只读：连接后立即关闭，不发送任何报文）。"""
    try:
        s = socket.create_connection((UPSTREAM_HOST, upstream_port()), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def _run_powershell(script, timeout=8):
    """执行**只读** PowerShell 查询片段。用户可配置值一律通过环境变量传入（不拼字符串）。"""
    env = dict(os.environ)
    env["WBRELAY_UPSTREAM_TASK"] = upstream_task_name()
    env["WBRELAY_UPSTREAM_DIR"] = upstream_dir() or ""
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW, env=env)


def upstream_task_state():
    """查询计划任务状态：running / disabled / ready / not_found / unknown / not_configured。

    只读查询（Get-ScheduledTask），绝不 Start/Stop/Disable/Run 任何计划任务（R6）。
    任务名通过环境变量 `WBRELAY_UPSTREAM_TASK` 传给 PowerShell，不拼接字符串。
    """
    if not upstream_task_name():
        return "not_configured"
    try:
        out = _run_powershell(
            "(Get-ScheduledTask -TaskName $env:WBRELAY_UPSTREAM_TASK "
            "-ErrorAction SilentlyContinue).State").stdout or ""
    except Exception:
        return "unknown"
    out = out.strip().lower()
    if not out or "notfound" in out.replace(" ", ""):
        return "not_found"
    if "running" in out:
        return "running"
    if "disabled" in out:
        return "disabled"
    if "ready" in out or "queued" in out:
        return "ready"
    return out or "unknown"


# ---------------------------------------------------------------------------
# 已按发布硬约束移除的能力（保留函数名，调用即报错，避免静默失败）
# ---------------------------------------------------------------------------
def external_control_removed(action):
    """外部上游「启停」类操作的统一拒绝文案（R6：上游项目只读，不启停）。"""
    return ("已移除：WBRelay 不再提供外部上游的%s（规则 R6 —— 上游项目只读、"
            "不写、不改、不启停）。请在外部服务自身完成该操作。" % action)


def set_autostart_reg(on):
    """外部上游登录自启：**已移除**（R5：外部进程只能由用户显式操作）。

    保留函数名以便调用方拿到明确错误，而不是「看起来成功但什么都没做」。
    网关自身的开机自启（settings.autostart）不受影响，见 wbrelay/autostart.py。
    """
    raise RuntimeError(
        "已移除：WBRelay 不再登记外部进程（自建中转服务）的开机启动（规则 R5）。"
        "如需开机启动该外部服务，请用该服务自身的自启方式，或在登录后手动启动。")


def autostart_registered():
    """外部上游自启登记状态：能力已移除，恒为 False。"""
    return False


# 兼容旧引用：不再存在后台守护线程（R5）
def watchdog_thread():
    """返回 None：WBRelay 不启动任何自动拉起外部进程的后台线程。"""
    return None
