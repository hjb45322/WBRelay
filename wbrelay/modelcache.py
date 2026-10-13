# -*- coding: utf-8 -*-
"""上游模型列表的本地缓存读写（磁盘部分；网络拉取仍在 core）。"""
import json
import os

from .atomicio import write_text
from .paths import MODELS_CACHE_PATH, READONLY, ensure_data_dir
from .state import _lock

def save_models_cache(cache):
    """原子写模型缓存。返回 True=已落盘；False=未写入（只读模式 / 写盘失败）。"""
    if READONLY:
        return False
    with _lock:
        if ensure_data_dir() is None:
            return False
        try:
            write_text(MODELS_CACHE_PATH, json.dumps(cache, ensure_ascii=False, indent=2))
        except Exception:
            return False
    return True


def load_models_cache():
    try:
        with open(MODELS_CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}
