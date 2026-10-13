# -*- coding: utf-8 -*-
"""主窗口几何状态的持久化（大小 / 位置 / 是否最大化）。

为什么单独成模块：GUI 依赖（pywebview）是可缺省的，而这段是纯标准库逻辑 ——
放这里既能被 app.py 复用，也能被单元测试直接覆盖，不需要真开窗口。

落盘：`%APPDATA%\\WBRelay\\window.json`（本项目自己的数据目录），**原子写**（同目录 tmp + os.replace）。
读盘一律做范围校验：文件损坏 / 数值越界 / 窗口跑到屏幕外，都退回默认值，不把窗口开到看不见的地方。
"""
import json
import os
import tempfile

from .paths import DATA, READONLY

FILENAME = "window.json"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 950
# 与 create_window 的 min_size 同口径：小于这个尺寸看板会挤成一团
MIN_WIDTH = 900
MIN_HEIGHT = 600
MAX_WIDTH = 10000
MAX_HEIGHT = 10000
# 位置校验：至少要留这么多像素在屏幕内，避免窗口「跑到天边」
KEEP_X = 120
KEEP_Y = 80


def state_path():
    return os.path.join(DATA, FILENAME)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def screen_size():
    """主屏像素尺寸（Windows）。取不到返回 None —— 调用方据此跳过位置校验。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        w, h = int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _position_ok(x, y, w, h, screen):
    if not screen:
        return True                       # 屏幕尺寸未知：不拦，交给窗口管理器
    sw, sh = screen
    if not sw or not sh:
        return True
    return (-w + KEEP_X) <= x <= (sw - KEEP_X) and (-20) <= y <= (sh - KEEP_Y)


def normalize(raw, screen=None):
    """把任意来源的字典收敛成可直接喂给 create_window 的键值。"""
    raw = raw if isinstance(raw, dict) else {}
    width = _as_int(raw.get("width")) or DEFAULT_WIDTH
    height = _as_int(raw.get("height")) or DEFAULT_HEIGHT
    width = max(MIN_WIDTH, min(width, MAX_WIDTH))
    height = max(MIN_HEIGHT, min(height, MAX_HEIGHT))
    out = {"width": width, "height": height, "maximized": bool(raw.get("maximized"))}
    x, y = _as_int(raw.get("x")), _as_int(raw.get("y"))
    if x is not None and y is not None and _position_ok(x, y, width, height, screen):
        out["x"], out["y"] = x, y
    return out


def load(path=None, screen=None):
    """读状态；文件缺失 / 损坏 / 越界一律落到默认值（不抛异常）。"""
    target = path or state_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        raw = {}
    return normalize(raw, screen)


def save(state, path=None):
    """原子写状态。失败只返回 False（窗口状态丢了不影响使用），不抛异常。

    只读模式（--readonly）一律不写盘 —— 只读必须零写入，窗口状态也不例外。
    """
    if READONLY:
        return False
    target = path or state_path()
    data = normalize(state)
    tmp = None
    try:
        folder = os.path.dirname(target) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".window-", suffix=".tmp", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        tmp = None
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
