# -*- coding: utf-8 -*-
"""本机剪贴板读取（Windows / 仅标准库）。

看板跑在浏览器里，浏览器读剪贴板会弹「允许读取剪贴板」权限框；而网关本来就在同一台
机器上，所以改由网关进程直接读系统剪贴板，前端不再触发权限提示。

约定：
  - 只读，不写剪贴板；
  - 非 Windows 或读取失败返回 None（调用方据此回退浏览器剪贴板 API）；
  - 剪贴板本身为空返回 ""（说明「支持但没内容」，调用方不要再去弹权限）；
  - 内容不写日志。
"""
import sys

# 只取前 64K 字符：异常大的剪贴板内容（比如整篇文档）没有粘贴价值，也不该塞进接口响应
MAX_CHARS = 65536

CF_UNICODETEXT = 13


def read_text():
    """返回剪贴板文本；None = 当前环境不支持/读取失败，"" = 支持但剪贴板为空。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        # 剪贴板同一时刻只能被一个进程打开；被别的程序占着（返回 0）时按「读取失败」处理
        if not user32.OpenClipboard(None):
            return None
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                text = ctypes.c_wchar_p(ptr).value or ""
            finally:
                kernel32.GlobalUnlock(handle)
            return text[:MAX_CHARS]
        finally:
            user32.CloseClipboard()
    except Exception:
        return None
