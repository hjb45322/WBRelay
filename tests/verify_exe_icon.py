# -*- coding: utf-8 -*-
"""打包取证：确认 dist\\WBRelay.exe 内嵌了图标资源（只看图标）

做法（纯标准库，只读）：
  解析 assets/wbrelay-icon.ico 的各帧图像数据，在 EXE 二进制里做字节级搜索
  对照组：根目录旧 WBRelay.exe

注意：**看板代码是否进包由 tests/verify_packed_dashboard.py 负责**（它会从
CArchive 里解包 dashboard.html 再比对标记）。本脚本不再在裸 EXE 里搜 JS 标识符 ——
dashboard.html 在归档里是压缩存放的，搜不到会误报「未命中」。

用法：python tests/verify_exe_icon.py
"""
import os
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICO = os.path.join(ROOT, "assets", "wbrelay-icon.ico")
TARGETS = [
    ("当前分发 WBRelay.exe（根目录）", os.path.join(ROOT, "WBRelay.exe")),
    ("构建产物 dist\\WBRelay.exe", os.path.join(ROOT, "dist", "WBRelay.exe")),
    ("修复前旧包 WBRelay.exe.bak_20261011", os.path.join(ROOT, "WBRelay.exe.bak_20261011")),
]

def read_ico_frames(path):
    data = open(path, "rb").read()
    if data[:4] != b"\x00\x00\x01\x00":
        raise SystemExit("不是 ICO 文件：%s" % path)
    n = struct.unpack("<H", data[4:6])[0]
    frames = []
    for i in range(n):
        off = 6 + i * 16
        w, h, _cc, _rsv, _pl, bpp, size, imgoff = struct.unpack("<BBBBHHII", data[off:off + 16])
        frames.append(((w or 256), (h or 256), bpp, data[imgoff:imgoff + size]))
    return frames


def probe(exe, label, frames):
    if not os.path.exists(exe):
        print("=== %s：文件不存在，跳过 ===" % label)
        return
    blob = open(exe, "rb").read()
    print("=== %s（%d 字节 ≈ %.2f MB）===" % (label, len(blob), len(blob) / 1048576.0))

    hit_cnt = 0
    for (w, h, bpp, f) in frames:
        fp = f[:96]
        pos = blob.find(fp)
        ok = pos >= 0
        hit_cnt += 1 if ok else 0
        print("  ICO 帧 %3dx%-3d %2dbpp %7d 字节 → %s%s"
              % (w, h, bpp, len(f), "内嵌命中" if ok else "未命中",
                 ("  @偏移 %d" % pos) if ok else ""))
    print("  → 图标帧命中 %d/%d" % (hit_cnt, len(frames)))

    print("  看板代码是否进包：见 tests/verify_packed_dashboard.py（本脚本只查图标）")


def main():
    frames = read_ico_frames(ICO)
    print("源图标 %s：%d 帧\n" % (os.path.basename(ICO), len(frames)))
    for label, exe in TARGETS:
        probe(exe, label, frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
