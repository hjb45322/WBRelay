# -*- coding: utf-8 -*-
"""看板 WebView2 渲染进程的健康探测（只读；标准库实现）。

为什么需要（2026-10-17 实录）
    pywebview 窗口背后的 msedgewebview2.exe **渲染进程**消失后，宿主窗口、托盘、
    窗口几何事件全都还正常，但看板变成永久白屏 —— GUI 层没有任何「渲染进程没了」
    的感知，只能整进程重启才恢复；而 R7 规定重启网关必须走值守进程，代价很大。

本模块只做**只读**探测，不结束、不启动任何进程，不写任何文件：
    snapshot_processes()       Toolhelp32 全量进程快照（不起子进程，几乎零成本）
    analyze(procs, own_pid)    纯函数：算出 WebView2 根 PID 与其子进程数
    webview_tree(own_pid)      快照 + analyze
    filter_renderers(rows, ..) 纯函数：从 CIM 结果里筛出 renderer
    renderer_pids(host_exe)    CIM 权威核验（较慢，**只在校验时调用**）
    RecoveryPolicy             边沿触发：只有「进程树曾经更完整、现在变少」才要求核验

探测失败（非 Windows / 快照失败 / 超时）一律返回 None，调用方据此**不做任何动作** ——
宁可漏修一次，也不误判后去重载用户正在填写的表单。
"""
import ctypes
import json
import os
import subprocess
import time

WEBVIEW_EXE = "msedgewebview2.exe"
TH32CS_SNAPPROCESS = 0x00000002
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def snapshot_processes():
    """返回 [{"pid","ppid","name"}]；非 Windows 或失败返回 None。"""
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        handle = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        try:
            k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
            k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            rows = []
            ok = k32.Process32FirstW(ctypes.c_void_p(handle), ctypes.byref(entry))
            while ok:
                rows.append({"pid": int(entry.th32ProcessID),
                             "ppid": int(entry.th32ParentProcessID),
                             "name": entry.szExeFile or ""})
                entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                ok = k32.Process32NextW(ctypes.c_void_p(handle), ctypes.byref(entry))
            return rows
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return None


def analyze(procs, own_pid):
    """纯函数：返回 {"root": int|None, "children": int}。

    根 = 直接以本进程为父的 msedgewebview2.exe（WebView2 浏览器进程）。
    子进程数 = 父为该根的 msedgewebview2.exe 个数（renderer / gpu / utility / crashpad）。
    渲染进程消失时，这个数会比健康时少 1 —— 这正是边沿触发的判据。
    """
    if not procs:
        return {"root": None, "children": 0}
    root = None
    for p in procs:
        if not isinstance(p, dict):
            continue
        if p.get("ppid") == own_pid and str(p.get("name") or "").lower() == WEBVIEW_EXE:
            root = p.get("pid")
            break
    if root is None:
        return {"root": None, "children": 0}
    children = 0
    for p in procs:
        if not isinstance(p, dict):
            continue
        if p.get("ppid") == root and str(p.get("name") or "").lower() == WEBVIEW_EXE:
            children += 1
    return {"root": root, "children": children}


def webview_tree(own_pid):
    """Toolhelp32 快照 + analyze。快照失败时 root=None，调用方不得据此下结论。"""
    return analyze(snapshot_processes(), own_pid)


def filter_renderers(rows, host_exe=None):
    """纯函数：从 CIM 结果里筛出 --type=renderer 的进程号。

    host_exe 给定时还要求 --webview-exe-name 指向它，避免把别的 App 的 WebView
    渲染进程当成自己的（源码运行时会退化成 python.exe，命中的概率更高）。
    """
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    want_host = ("--webview-exe-name=%s" % host_exe).lower() if host_exe else ""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cmd = str(row.get("CommandLine") or "").lower()
        if "--type=renderer" not in cmd:
            continue
        if want_host and want_host not in cmd:
            continue
        pid = row.get("ProcessId")
        if isinstance(pid, int):
            out.append(pid)
    return out


def renderer_pids(host_exe=None):
    """用 CIM 查 --type=renderer。返回 list（可能为空）；探测失败返回 None。"""
    if os.name != "nt":
        return None
    script = ("Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress" % WEBVIEW_EXE)
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True, errors="replace", timeout=15, creationflags=NO_WINDOW)
        data = json.loads(raw) if raw.strip() else []
    except Exception:
        return None
    return filter_renderers(data, host_exe)


class RecoveryPolicy:
    """边沿触发：只有「进程树曾经更完整、现在变少」才要求做一次权威核验。

    不做定时 CIM 轮询：CIM 每次都要起 PowerShell，长期每 10 秒一次太重。
    Toolhelp32 快照几乎免费，适合常态观察；只有它报「变少」时才升级为 CIM 核验。
    """

    def __init__(self, cooldown=30.0, max_attempts=2):
        self.cooldown = float(cooldown)
        self.max_attempts = int(max_attempts)
        self.peak = 0
        self.attempts = 0
        self.next_check = 0.0

    def observe(self, root, children, now=None):
        """返回 (suspect, reason)。"""
        now = time.time() if now is None else now
        if now < self.next_check:
            return False, ""
        if root is None:
            if self.peak > 0:
                return True, "WebView2 根进程已消失"
            return False, ""
        if children > self.peak:
            self.peak = children
            return False, ""
        if children < self.peak:
            return True, "WebView2 子进程 %d 个，少于峰值 %d 个" % (children, self.peak)
        return False, ""

    def can_attempt(self):
        return self.attempts < self.max_attempts

    def note_attempt(self, now=None):
        self.attempts += 1
        self.next_check = (time.time() if now is None else now) + self.cooldown

    def recovered(self, children=0, now=None):
        """重载成功 / 确认其实没坏之后调用：重新学习基线、补满次数并进入冷却。"""
        self.peak = max(0, int(children or 0))
        self.attempts = 0
        self.next_check = (time.time() if now is None else now) + self.cooldown
