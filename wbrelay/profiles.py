# -*- coding: utf-8 -*-
"""模型档案库：能力、档位、上下文等默认值，以及外部 catalog 解析。"""
import io
import json
import os

from .atomicio import write_text
from .paths import PROFILES_PATH, READONLY, ensure_data_dir
from .state import _lock
from .log import _log

_DEFAULT_MODEL_PROFILES = {
    "hy4-preview-f": {"reasoning_efforts": ["high"], "context_window": 960000, "max_output_tokens": 64000, "supports_vision": True},
    "hy3": {"reasoning_efforts": ["low", "high"], "context_window": 192000, "max_output_tokens": 64000, "supports_vision": True},
    "deepseek-v4.1-flash": {"reasoning_efforts": ["low", "high", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "glm-5.3": {"reasoning_efforts": ["low", "high", "max"], "context_window": 1000000, "max_output_tokens": 64000, "supports_vision": True},
    "glm-5.3-flash": {"reasoning_efforts": ["low", "high", "max"], "context_window": 1000000, "max_output_tokens": 131072, "supports_vision": True},
    "glm-5.2": {"reasoning_efforts": ["high", "xhigh"], "context_window": 1000000, "max_output_tokens": 64000, "supports_vision": True},
    "glm-5.1": {"reasoning_efforts": ["medium"], "context_window": 200000, "max_output_tokens": 48000, "supports_vision": True},
    "glm-5v-turbo": {"reasoning_efforts": ["medium"], "context_window": 200000, "max_output_tokens": 64000, "supports_vision": True},
    "minimax-m3": {"reasoning_efforts": ["medium"], "context_window": 512000, "max_output_tokens": 64000, "supports_vision": True},
    "kimi-k3-1": {"reasoning_efforts": ["low", "high", "xhigh"], "context_window": 1000000, "max_output_tokens": 32000, "supports_vision": True},
    "kimi-k2.8-preview": {"reasoning_efforts": ["low", "high", "max"], "context_window": 1000000, "max_output_tokens": 64000, "supports_vision": True},
    "kimi-k2.7": {"reasoning_efforts": ["medium"], "context_window": 256000, "max_output_tokens": 32000, "supports_vision": True},
    "kimi-k2.6": {"reasoning_efforts": ["medium"], "context_window": 256000, "max_output_tokens": 32000, "supports_vision": True},
    "deepseek-v4-pro": {"reasoning_efforts": ["high", "xhigh"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "space-bunny": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "glm-5.3-flashx": {"reasoning_efforts": ["low", "high", "max"], "context_window": 1000000, "max_output_tokens": 131072, "supports_vision": True},
    "gpt-6-astra": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "gpt-6.1-sol": {"reasoning_efforts": ["low", "medium", "high", "xhigh"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "gpt-6-sol": {"reasoning_efforts": ["low", "medium", "high", "xhigh"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "gpt-6-luna": {"reasoning_efforts": ["low", "medium", "high", "xhigh"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    # 以下为常见 gpt 系列补充（上游服务 catalog 未收录，按官方规格合理默认）
    "gpt-5.6-sol": {"reasoning_efforts": ["low", "medium", "high", "xhigh"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "gpt-5.6-terra": {"reasoning_efforts": ["low", "medium", "high"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "gpt-5.5": {"reasoning_efforts": ["low", "medium", "high"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "codex-auto-review": {"reasoning_efforts": ["low", "high"], "context_window": 200000, "max_output_tokens": 64000, "supports_vision": False},
    # Claude Opus 系列：部分中转返回 claude-opus-*-* 连字符 id，OpenRouter 用点号 id 对不上，
    # 因此单独补连字符条目供兜底。规格按 OpenRouter 公开数据：4-6 及以上为
    # 1M 上下文 / 128K 输出 / 5 档思考；4-5 为 200K/64K，官方未给档位，按常见配置给 3 档。
    "claude-opus-4-5": {"reasoning_efforts": ["low", "medium", "high"], "context_window": 200000, "max_output_tokens": 64000, "supports_vision": True},
    "claude-opus-4-6": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "claude-opus-4-7": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "claude-opus-4-8": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "claude-opus-5": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
    "claude-opus-5-5": {"reasoning_efforts": ["low", "medium", "high", "xhigh", "max"], "context_window": 1000000, "max_output_tokens": 128000, "supports_vision": True},
}


_PROFILES_CACHE = {"mtime": None, "data": None}   # 档案文件缓存（导入后失效）
_profiles_meta = {}   # 档案文件顶层 _meta（自动同步状态，落盘持久化）


def load_profiles():
    """读 model_profiles.json；缺文件时用内置默认生成。返回 {model: profile}。"""
    global _profiles_meta
    try:
        mtime = os.path.getmtime(PROFILES_PATH)
    except OSError:
        try:
            save_profiles(_DEFAULT_MODEL_PROFILES)
        except Exception:
            pass
        return dict(_DEFAULT_MODEL_PROFILES)
    if _PROFILES_CACHE["mtime"] == mtime and _PROFILES_CACHE["data"] is not None:
        return _PROFILES_CACHE["data"]
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
        if "_meta" in data:
            _profiles_meta = dict(data["_meta"])
            data = {k: v for k, v in data.items() if k != "_meta"}
        _PROFILES_CACHE["mtime"] = mtime
        _PROFILES_CACHE["data"] = data
        return data
    except Exception:
        return dict(_DEFAULT_MODEL_PROFILES)


def save_profiles(profiles):
    """原子写档案库。返回 True=已落盘；False=未写入（只读模式 / 写盘失败）。"""
    if READONLY:
        return False
    obj = dict(profiles)
    if _profiles_meta:
        obj["_meta"] = dict(_profiles_meta)
    with _lock:
        if ensure_data_dir() is None:
            return False
        try:
            write_text(PROFILES_PATH, json.dumps(obj, ensure_ascii=False, indent=2))
        except Exception as exc:
            _log("[WBRelay] 模型档案保存失败（原文件保持不变）：%s" % exc)
            return False
    _PROFILES_CACHE["mtime"] = None
    _PROFILES_CACHE["data"] = None
    return True


def parse_catalog_profiles(data):
    """解析外部 catalog（models[].slug 等）-> {slug: profile}，不取倍率。"""
    out = {}
    items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out
    for m in items:
        if not isinstance(m, dict):
            continue
        slug = str(m.get("slug") or "").strip()
        if not slug:
            continue
        efforts = []
        for lv in (m.get("supported_reasoning_levels") or []):
            if isinstance(lv, dict) and lv.get("effort"):
                efforts.append(str(lv["effort"]))
        modalities = m.get("input_modalities") or []
        out[slug] = {
            "reasoning_efforts": efforts,
            "context_window": m.get("context_window"),
            "max_output_tokens": m.get("max_output_tokens"),
            "supports_vision": "image" in modalities,
        }
    return out


# ---- 自动档案同步：OpenRouter 公开模型库（免 Key）----


def _persist_profiles_meta():
    """把自动同步状态（_meta）立即写进 model_profiles.json，不依赖 save_profiles。

    只读模式（--readonly）**一律不写盘**：本函数曾在只读下绕过门禁写文件，
    是「只读真正零写入」必须堵住的入口之一。
    """
    if READONLY or not _profiles_meta:
        return False
    try:
        prof = load_profiles()
        obj = dict(prof)
        obj["_meta"] = dict(_profiles_meta)
        with _lock:
            if ensure_data_dir() is None:
                return False
            write_text(PROFILES_PATH, json.dumps(obj, ensure_ascii=False, indent=2))
        _PROFILES_CACHE["mtime"] = None
        _PROFILES_CACHE["data"] = None
        return True
    except Exception:
        return False
