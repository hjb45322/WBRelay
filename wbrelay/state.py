# -*- coding: utf-8 -*-
"""进程内共享运行态。

集中放置可变全局，避免各模块各自持有一份导致状态不一致。
"""
import subprocess
import threading

from .paths import DEFAULT_PORT


# RLock：save_settings 内部已持锁，read-modify-write 外层再拿同一把锁不会死锁
_lock = threading.RLock()
_cooldowns = {}             # supplier id -> unix until（进程内）
_cooldown_reasons = {}      # supplier id -> 最近一次失败原因（进程内，仅用于展示/排错）
_stats = {"requests": 0, "errors": 0, "by_model": {}}


# Windows 下隐藏子进程控制台窗口（--noconsole EXE 时 subprocess 调用不弹黑框）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# 实际监听端口（main() 在端口被占用时会顺延）
_SERVER_PORT = DEFAULT_PORT
