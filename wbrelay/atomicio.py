# -*- coding: utf-8 -*-
"""原子写工具：同目录临时文件 + fsync + os.replace。

硬约束（AGENTS.md 第二节）：**配置写入一律原子写**，禁止直接覆写目标文件。
把「临时文件命名 / 失败清理 / 落盘时机」收口在这里，避免各模块各写一份、
漏掉清理或退化成直接覆写。

用法：

    from .atomicio import write_text
    write_text(SETTINGS_PATH, json.dumps(data, ensure_ascii=False, indent=2))

失败时抛异常（由调用方决定降级策略），且**不会留下临时文件**。
"""
import os
import tempfile


def _atomic(path, writer, fsync=True):
    """临时文件 + os.replace 的公共实现：writer(fd) 负责把内容写进临时文件。

    临时文件与目标文件**同目录**（os.replace 才能保证同一卷上的原子替换）。
    """
    path = os.path.abspath(path)
    folder = os.path.dirname(path) or "."
    if not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp", dir=folder)
    try:
        with writer(fd) as fh:
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def write_text(path, text, encoding="utf-8", fsync=True, newline="\n"):
    """原子写文本文件。返回写入的路径；失败抛异常且清理临时文件。

    临时文件与目标文件**同目录**（os.replace 才能保证同一卷上的原子替换）。
    """
    def _writer(fd):
        fh = os.fdopen(fd, "w", encoding=encoding, newline=newline)
        fh.write(text)
        return fh

    return _atomic(path, _writer, fsync=fsync)


def write_bytes(path, data, fsync=True):
    """原子写二进制文件（同目录临时文件 + os.replace）。失败抛异常且不留临时文件。"""
    def _writer(fd):
        fh = os.fdopen(fd, "wb")
        fh.write(data)
        return fh

    return _atomic(path, _writer, fsync=fsync)


def write_json(path, obj, ensure_ascii=False, indent=2, fsync=True):
    """原子写 JSON（UTF-8、无 BOM）。"""
    import json
    return write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent), fsync=fsync)


def cleanup(path):
    """删除文件（不存在/失败都不抛）——用于失败路径。"""
    try:
        os.remove(path)
    except OSError:
        pass