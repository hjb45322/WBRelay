# -*- coding: utf-8 -*-
"""版本更新检测：**只有用户点了才发**的一次只读查询（见 docs/方案-版本更新检测.md）。

边界（写死在实现里，改之前先改方案）：

  * 默认 `manual`：只有看板上的「检查更新」按钮会触发 —— **没有后台轮询**，
    启动时也不会联网；
  * 只发一个 GET，**不带任何本机信息**（不传版本号 / 路径 / 供应商 / 用量 / Key，
    只有一个固定的 User-Agent）；
  * 5 秒超时 + 64 KiB 响应上限；离线 / 被墙 / 任何异常一律**静默降级**，
    绝不影响网关功能；
  * 只读响应里的 `tag_name` 与 `html_url`：**不下载、不安装、不改任何文件**，结果不落盘；
  * 关闭方式：`settings.json` 里 `"update_check": "off"`，或环境变量 `WBRELAY_NO_UPDATE_CHECK=1`；
  * 只用标准库（urllib，复用 `net._urlopen` 的系统代理处理）。
"""
import json
import os
import re
import urllib.request

from . import __version__
from .log import _log
from .net import ResponseTooLarge, _urlopen, close_quietly, read_limited
from .settings import load_settings

# 只查「最新发布」这一个只读端点；内网镜像可用 settings.update_check_url 覆盖
DEFAULT_API = "https://api.github.com/repos/hjb45322/WBRelay/releases/latest"
TIMEOUT_SEC = 5
MAX_BYTES = 64 * 1024
USER_AGENT = "WBRelay-version-check"


def disabled_reason():
    """为什么不做检查；可以检查时返回空串。"""
    if os.environ.get("WBRELAY_NO_UPDATE_CHECK"):
        return "已关闭（环境变量 WBRELAY_NO_UPDATE_CHECK）"
    v = str(load_settings().get("update_check") or "manual").strip().lower()
    if v == "off":
        return "已关闭（settings.json 的 update_check = off）"
    return ""


def api_url():
    """检测地址；`settings.update_check_url` 可覆盖（内网镜像 / 离线自测用）。"""
    v = str(load_settings().get("update_check_url") or "").strip()
    return v or DEFAULT_API


def parse_version(text):
    """`v1.2.3` / `1.2` / `1.2.3-rc1` → (1, 2, 3) / (1, 2) / (1, 2, 3)；解析不出返回 ()。

    预发布后缀按**忽略**处理（只比较数字段）—— 这是刻意的简化，如实写在方案里。
    """
    m = re.match(r"^v?(\d+(?:\.\d+)*)", str(text or "").strip())
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def compare_versions(current, latest):
    """返回 -1 / 0 / 1（current 相对 latest）；任一解析不出返回 None。"""
    a, b = parse_version(current), parse_version(latest)
    if not a or not b:
        return None
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def check():
    """执行一次检查并返回结构化结果；**永不抛异常**。

    返回：{enabled, current, latest, has_update, url, error}
    """
    cur = __version__
    result = {"enabled": False, "current": cur, "latest": "", "has_update": False,
              "url": "", "error": ""}
    why = disabled_reason()
    if why:
        result["error"] = why
        return result

    req = urllib.request.Request(api_url(), headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    resp = None
    try:
        resp = _urlopen(req, TIMEOUT_SEC)
        data = json.loads(read_limited(resp, MAX_BYTES).decode("utf-8", "replace"))
        latest = str(data.get("tag_name") or "").strip()
        html = str(data.get("html_url") or "").strip()
        cmp_ = compare_versions(cur, latest)
        result.update({"enabled": True, "latest": latest.lstrip("v"), "url": html})
        if not latest:
            result["error"] = "响应里没有版本号"
        elif cmp_ is None:
            result["error"] = "版本号无法比较"
        else:
            result["has_update"] = cmp_ < 0
    except ResponseTooLarge:
        result["enabled"] = True
        result["error"] = "响应体超过上限"
    except Exception as exc:
        # 离线 / 超时 / DNS / TLS / JSON 畸形 …… 一律静默降级：只记一行脱敏日志
        result["enabled"] = True
        result["error"] = "检查失败（网络不可用或超时）"
        _log("[更新检测] 查询失败：%s" % type(exc).__name__)
    finally:
        close_quietly(resp)
    return result
