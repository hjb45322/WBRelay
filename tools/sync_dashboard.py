# -*- coding: utf-8 -*-
"""把仓库根的 dashboard.html 同步到包内（wbrelay/static/），供 wheel / 安装版使用。

为什么需要副本：wheel 只能携带「包目录内」的文件。根目录的 dashboard.html 是
唯一编辑源（PyInstaller 与源码运行都用它），包内副本是**生成物**，由本脚本产出，
并由 tests/test_package_layout.py 断言两者逐字节一致 —— 防止副本静默漂移。

用法：
    python tools/sync_dashboard.py            # 写入/更新包内副本
    python tools/sync_dashboard.py --check    # 只校验是否一致（CI 用；不一致返回 1）
"""
import argparse
import os
import shutil
import sys

# ---- 控制台编码降级 ----
# 中文 Windows 是 GBK，英文 Windows / GitHub Actions 是 cp1252。编码装不下中文时
# 按 errors="replace" 输出 —— 别让一次 print 把脚本自己打崩（与 tests/ 里 _out() 同源）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dashboard.html")
DST = os.path.join(ROOT, "wbrelay", "static", "dashboard.html")


def main():
    ap = argparse.ArgumentParser(description="同步 dashboard.html 到包内静态资源目录")
    ap.add_argument("--check", action="store_true", help="只校验一致性，不写入")
    args = ap.parse_args()

    if not os.path.isfile(SRC):
        print("找不到源文件：%s" % SRC)
        return 2
    src_bytes = open(SRC, "rb").read()
    dst_bytes = open(DST, "rb").read() if os.path.isfile(DST) else None

    if args.check:
        if dst_bytes == src_bytes:
            print("包内 dashboard.html 与源文件一致（%d 字节）" % len(src_bytes))
            return 0
        print("包内 dashboard.html 与源文件不一致，请运行：python tools/sync_dashboard.py")
        print("  源文件：%s（%d 字节）" % (SRC, len(src_bytes)))
        print("  包内  ：%s（%s）" % (DST, "缺失" if dst_bytes is None else "%d 字节" % len(dst_bytes)))
        return 1

    os.makedirs(os.path.dirname(DST), exist_ok=True)
    shutil.copyfile(SRC, DST)
    print("已同步 → %s（%d 字节）" % (DST, len(src_bytes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())