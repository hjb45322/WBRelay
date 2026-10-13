# -*- coding: utf-8 -*-
"""网关自身的开机自启（HKCU Run 注册表项）。"""
import os
import sys
import winreg

from .log import _log
from .paths import DIR, READONLY

# ---- 上游服务开机启动（注册表 HKCU Run）与进程守护（WBRelay 后台线程）----
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---- WBRelay 网关本体开机自启动（注册表 HKCU Run，与上游服务启动项并列）----
GATEWAY_AUTOSTART_NAME = "WBRelayGateway"


def gateway_autostart_cmd():
    """登录自动启动命令：EXE 指向当前运行文件（换目录自动跟随）；源码运行用 python relay.py。"""
    if getattr(sys, "frozen", False):
        return '"%s"' % os.path.abspath(sys.executable)
    script = os.path.join(DIR, "relay.py")
    return '"%s" "%s"' % (sys.executable, script)


def set_gateway_autostart(on):
    """写/删 HKCU Run 注册表项，登录时自动启动 WBRelay 网关。

    只读模式（`--readonly`）**不动注册表**：零写入承诺的一部分（返回 False）。
    第二道防线：即使调用方忘了判只读，这里也不会写。
    """
    if READONLY:
        _log("[WBRelay] 只读模式：不同步网关开机自启注册表项")
        return False
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        if on:
            winreg.SetValueEx(key, GATEWAY_AUTOSTART_NAME, 0, winreg.REG_SZ,
                              gateway_autostart_cmd())
        else:
            try:
                winreg.DeleteValue(key, GATEWAY_AUTOSTART_NAME)
            except OSError:
                pass
    finally:
        winreg.CloseKey(key)
    return True


def gateway_autostart_registered():
    """注册表是否已登记 WBRelay 本体开机启动。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        try:
            winreg.QueryValueEx(key, GATEWAY_AUTOSTART_NAME)
            return True
        except OSError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False
