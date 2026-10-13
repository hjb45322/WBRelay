# -*- coding: utf-8 -*-
"""配置备份与恢复。"""
import json
import os
import time

from .atomicio import write_text
from .paths import (BACKUP_DIR, SETTINGS_PATH, READONLY, data_dir_error,
                    ensure_data_dir)
from .log import _log
from .settings import update_settings

def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if fn.startswith("settings-") and fn.endswith(".json"):
            full = os.path.join(BACKUP_DIR, fn)
            try:
                out.append({"file": fn, "size": os.path.getsize(full),
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(os.path.getmtime(full)))})
            except Exception:
                pass
    return out


def make_backup():
    """把当前 settings.json 备份到 backups/。返回备份文件名；失败或只读返回 None。

    原子写（同目录临时文件 + os.replace）：**不再直接覆写备份文件** ——
    直接覆写在断电/异常时会留下半截 JSON，备份本身反而不可用。
    """
    if READONLY:
        return None
    if ensure_data_dir() is None:
        _log("[WBRelay] 备份未执行：数据目录不可用（%s）" % data_dir_error())
        return None
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = fh.read()
    except Exception as exc:
        _log("[WBRelay] 备份失败（读不到当前配置）：%s" % exc)
        return None
    fn = "settings-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
    full = os.path.join(BACKUP_DIR, fn)
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        write_text(full, data)
        return fn
    except Exception as exc:
        _log("[WBRelay] 备份写入失败：%s" % exc)
        return None


def restore_backup(fn):
    if not fn or ".." in fn or "/" in fn or "\\" in fn:
        return None
    full = os.path.join(BACKUP_DIR, fn)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # 恢复 = 一次「整体替换配置」的写入。走持锁的 update_settings：与并发
    # POST /api/settings、/api/keys、/api/suppliers 互不交错（不会出现
    # 「恢复刚写完就被别人的旧快照覆盖回去」）。冷却由 save_settings 统一清
    # （核心语义 2：只在保存成功时发生），这里不再自己清。
    def _apply(_current):
        return data

    ok, restored = update_settings(_apply)
    if not ok:
        # 只读模式 / 写盘失败：不能对调用方伪称恢复成功
        return None
    return restored


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
