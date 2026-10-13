# -*- coding: utf-8 -*-
"""提取打包产物内的 dashboard.html，确认新看板代码确实进包（只读）

用法：python tests/verify_packed_dashboard.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from PyInstaller.archive.readers import CArchiveReader
except Exception as exc:  # pragma: no cover
    raise SystemExit("无法导入 PyInstaller 归档读取器：%s" % exc)

MARKERS = ["USAGE_REFRESH_SEC", "loadUsageRefreshSetting", "_filtersSig",
           "usage_refresh_sec", "全部供应商", "用量看板自动刷新间隔",
           # 20261011 供应商显示名修复：前端优先用后端解析好的 supplier_label
           "supplier_label", "s.supplier_label || (sObj",
           # 20261012：使用说明、粘贴按钮、模型统计胶囊、供应商操作区分组、编辑弹窗分组
           "btn-help", "help-mask", "/api/clipboard", "paste-btn",
           "mm-chip", "ops-status", "m-group-title"]

for label, exe in (("构建产物 dist\\WBRelay.exe", os.path.join(ROOT, "dist", "WBRelay.exe")),
                   ("分发版 根目录 WBRelay.exe", os.path.join(ROOT, "WBRelay.exe"))):
    if not os.path.exists(exe):
        print("%s：不存在，跳过\n" % label)
        continue
    print("=== %s ===" % label)
    try:
        arc = CArchiveReader(exe)
    except Exception as exc:
        print("  打开失败：%s\n" % exc)
        continue

    toc = arc.toc
    names = list(toc.keys()) if hasattr(toc, "keys") else [t[0] for t in toc]
    dash = [n for n in names if "dashboard" in str(n).lower()]
    print("  归档条目总数：%d；dashboard 相关：%s" % (len(names), dash))
    if not dash:
        print("  未找到 dashboard.html\n")
        continue
    try:
        data = arc.extract(dash[0])
    except Exception as exc:
        print("  提取失败：%s\n" % exc)
        continue
    text = data.decode("utf-8", "replace")
    print("  dashboard.html 解包大小：%d 字节" % len(data))
    for m in MARKERS:
        print("     %-24s %s" % (m, "命中" if m in text else "未命中"))
    print("")
