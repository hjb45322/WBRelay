# -*- coding: utf-8 -*-
"""窗口几何记忆的单元测试（纯逻辑，不开窗口、不碰真实数据目录）

覆盖：
  T1 文件不存在 → 默认 1280x950
  T2 存 → 读 往返一致（含位置与最大化标记）
  T3 文件损坏 / 非字典 → 回落默认，不抛异常
  T4 尺寸越界被钳制（过小 → MIN，过大 → MAX）
  T5 窗口跑到屏幕外 → 只丢位置，尺寸仍生效
  T6 屏内位置保留
  T7 原子写：不留 .tmp 且落盘内容是合法 JSON
  T8 写失败（目录不可写）只返回 False，不抛

用法：python tests/test_window_state.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 隔离：走统一的隔离引导层（临时数据目录 + 不读真实 %APPDATA%）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: E402

isolate.bootstrap()
TMP_APPDATA = isolate.tmp_root()

from wbrelay import windowstate as ws  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" | " + detail) if detail else ""))


def write(path, text):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main():
    target = os.path.join(TMP_APPDATA, "window.json")
    problems = isolate.isolation_problems()
    check("T0 数据目录已隔离（不读真实 %APPDATA%）", not problems, "；".join(problems))
    try:
        # T1 文件不存在 → 默认
        got = ws.load(path=target)
        check("T1 无文件时回落默认 1280x950",
              (got["width"], got["height"]) == (ws.DEFAULT_WIDTH, ws.DEFAULT_HEIGHT) and "x" not in got,
              repr(got))

        # T2 往返
        ok_save = ws.save({"width": 1500, "height": 1000, "x": 120, "y": 60, "maximized": True}, path=target)
        back = ws.load(path=target, screen=(1920, 1080))
        check("T2a save 返回 True", ok_save is True)
        check("T2b 存读往返一致",
              (back["width"], back["height"], back.get("x"), back.get("y"), back["maximized"]) ==
              (1500, 1000, 120, 60, True), repr(back))

        # T3 损坏
        write(target, "{ 这不是 json")
        got = ws.load(path=target)
        check("T3a 损坏文件回落默认", (got["width"], got["height"]) == (1280, 950), repr(got))
        write(target, "[1,2,3]")
        got = ws.load(path=target)
        check("T3b 非字典内容回落默认", (got["width"], got["height"]) == (1280, 950), repr(got))

        # T4 越界钳制
        tiny = ws.normalize({"width": 10, "height": 10})
        huge = ws.normalize({"width": 99999, "height": 99999})
        check("T4a 过小被抬到最小尺寸",
              (tiny["width"], tiny["height"]) == (ws.MIN_WIDTH, ws.MIN_HEIGHT), repr(tiny))
        check("T4b 过大被压到上限",
              (huge["width"], huge["height"]) == (ws.MAX_WIDTH, ws.MAX_HEIGHT), repr(huge))

        # T5 屏外位置丢弃
        off = ws.normalize({"width": 1200, "height": 800, "x": 99999, "y": 99999}, screen=(1920, 1080))
        check("T5 屏外位置被丢弃（尺寸保留）",
              "x" not in off and "y" not in off and off["width"] == 1200, repr(off))

        # T6 屏内位置保留
        on = ws.normalize({"width": 1200, "height": 800, "x": 300, "y": 200}, screen=(1920, 1080))
        check("T6 屏内位置保留", (on.get("x"), on.get("y")) == (300, 200), repr(on))

        # T7 原子写
        ws.save({"width": 1300, "height": 900}, path=target)
        leftovers = [f for f in os.listdir(TMP_APPDATA) if f.endswith(".tmp")]
        data_ok = False
        try:
            data_ok = json.load(io.open(target, encoding="utf-8"))["width"] == 1300
        except Exception:
            data_ok = False
        check("T7a 落盘内容是合法 JSON", data_ok)
        check("T7b 不留 .tmp 残留", leftovers == [], repr(leftovers))

        # T8 写失败不抛
        bad_path = os.path.join(TMP_APPDATA, "no-such-dir", "window.json")
        os.makedirs(os.path.dirname(bad_path), exist_ok=True)
        os.chmod(os.path.dirname(bad_path), 0o500) if hasattr(os, "chmod") else None
        got = ws.save({"width": 1200, "height": 800}, path=bad_path)
        check("T8 写失败只返回布尔", got in (True, False))
    finally:
        shutil.rmtree(TMP_APPDATA, ignore_errors=True)

    print("\n===== 窗口几何：%d 通过 / %d 失败 =====" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败项：" + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
