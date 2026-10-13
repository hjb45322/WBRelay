# -*- coding: utf-8 -*-
"""日志：控制台（源码可见）+ 文件双写。

只读模式下不写文件，只打印。

轮转策略（`_rotate_log`）：超过 `LOG_ROTATE_BYTES` 就把当前日志滚成
`wb.log.1`，历史世代整体后移，最多保留 `LOG_ROTATE_KEEP` 代；再滚时最旧一代
直接删除。世代名固定为 `wb.log.1` / `wb.log.2` / `wb.log.3`（**不带时间戳**），
便于测试断言与人工排查。

历史兼容：早期版本只保留一代、用的是 `wb.log.old`。第一次轮转时若检测到该
文件，会把它迁移成 `wb.log.1`；若 `wb.log.1` 已存在（说明旧文件更老），直接
删除 `wb.log.old`，避免残留一份永远不会被清理的历史日志。
"""
import os
import threading
import time

from .paths import LOG_PATH, READONLY, ensure_data_dir
from .redact import redact

# 单行日志上限：超长内容（如上游错误正文）截断后再落盘
LOG_LINE_LIMIT = 800

# 轮转阈值：wb.log 超过 2MB 就滚一代
LOG_ROTATE_BYTES = 2 * 1024 * 1024
# 最多保留几代历史日志（wb.log.1 ~ wb.log.3）
LOG_ROTATE_KEEP = 3

# 轮转 + 追加必须互斥：`os.replace` 与随后的 `open(append)` 之间原本有窗口，
# 多线程并发写时可能「按旧文件判断大小、却把行写进另一份文件」，导致轮转判断
# 与写入不是同一份文件。这里用一把模块级锁把整个临界区包住。
# 锁内只做一次 getsize + 必要的 rename + 追加写，不扫目录、不额外 stat ——
# 热路径开销可忽略（文件 IO 本来就要做）。
_LOG_LOCK = threading.Lock()


def _rotate_log():
    """滚一代：wb.log → wb.log.1，历史世代后移，丢弃最旧一代。

    调用方必须已持有 `_LOG_LOCK`。任何单步失败都不影响本次日志写入
    （最坏情况就是日志继续涨在同一个文件里）。
    """
    # 迁移兼容：旧命名 wb.log.old
    legacy = LOG_PATH + ".old"
    if os.path.exists(legacy):
        try:
            if os.path.exists(LOG_PATH + ".1"):
                # 新世代已在：旧文件更老，删除以免留下一份永不被清理的历史
                os.remove(legacy)
            else:
                os.replace(legacy, LOG_PATH + ".1")
        except OSError:
            pass
    # 从最旧一代往前推：第 LOG_ROTATE_KEEP 代删除，其余后移一位
    for gen in range(LOG_ROTATE_KEEP, 0, -1):
        src = "%s.%d" % (LOG_PATH, gen)
        if not os.path.exists(src):
            continue
        if gen >= LOG_ROTATE_KEEP:
            try:
                os.remove(src)
            except OSError:
                pass
            continue
        try:
            os.replace(src, "%s.%d" % (LOG_PATH, gen + 1))
        except OSError:
            pass
    os.replace(LOG_PATH, LOG_PATH + ".1")


def _rotate_if_needed():
    """超过阈值就轮转。调用方必须已持有 `_LOG_LOCK`。"""
    try:
        if os.path.getsize(LOG_PATH) > LOG_ROTATE_BYTES:
            _rotate_log()
    except OSError:
        # 文件不存在（首次写）：不轮转，由随后的 open(append) 创建
        pass


def _log(msg):
    """控制台（源码调试可见）+ 文件日志双写；EXE --noconsole 时靠 wb.log 排障。

    统一脱敏：日志出口一律过 `redact()`（掩码 Bearer / API Key / URL 凭据）并截断，
    这样任何调用点即使传进来带凭据的文本，也不会把原文写进日志。
    """
    text = redact(msg, LOG_LINE_LIMIT)
    try:
        print(text)
    except Exception:
        pass
    if READONLY or ensure_data_dir() is None:
        return
    try:
        # 同一把锁内完成「大小判断 → 轮转 → 追加」，消除轮转与写入之间的竞争窗口
        with _LOG_LOCK:
            _rotate_if_needed()
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + text + "\n")
    except Exception:
        pass
