# -*- mode: python ; coding: utf-8 -*-

import os
import re

# ---- 版本资源（Windows「属性 → 详细信息」）------------------------------------
# 为什么：没有版本资源 + 没有数字签名时，Windows 把 EXE 当成「来路不明的程序」——
# 属性面板里 CompanyName / ProductName / FileVersion 全是空白。
# 单一来源：wbrelay/__init__.py 的 __version__，版本号只在这一处维护（不手改第二处）。
# 生成物落在 build/（构建目录，不入库）；不涉及任何用户运行数据（见 test_package_layout.py P6）。
# 首行写 coding cookie：PyInstaller 以字节读取该文件、按 cookie/BOM 解码，避免中文在别的语言环境乱码。
_ver_src = open(os.path.join(SPECPATH, 'wbrelay', '__init__.py'), encoding='utf-8').read()
APP_VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', _ver_src).group(1)
VER_TUPLE = tuple(([int(x) for x in APP_VERSION.split('.')] + [0, 0, 0, 0])[:4])
VERSION_FILE = os.path.join(SPECPATH, 'build', 'version_info.txt')
os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
with open(VERSION_FILE, 'w', encoding='utf-8') as _fh:
    _fh.write(
        "# -*- coding: utf-8 -*-\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(filevers=%r, prodvers=%r, mask=0x3f, flags=0x0,\n"
        "                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
        "  kids=[StringFileInfo([StringTable('040904B0', [\n"
        "        StringStruct('CompanyName', 'WBRelay contributors'),\n"
        "        StringStruct('FileDescription', 'WBRelay - 多供应商自动路由网关'),\n"
        "        StringStruct('FileVersion', %r),\n"
        "        StringStruct('InternalName', 'WBRelay'),\n"
        "        StringStruct('LegalCopyright', 'MIT License, Copyright (c) 2026 WBRelay contributors'),\n"
        "        StringStruct('OriginalFilename', 'WBRelay.exe'),\n"
        "        StringStruct('ProductName', 'WBRelay'),\n"
        "        StringStruct('ProductVersion', %r)\n"
        "      ])]),\n"
        "      VarFileInfo([VarStruct('Translation', [1033, 1200])])]\n"
        ")\n"
        % (VER_TUPLE, VER_TUPLE, APP_VERSION, APP_VERSION)
    )

a = Analysis(
    ['relay.py'],
    pathex=[],
    binaries=[],
    datas=[('dashboard.html', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WBRelay',
    icon=os.path.join(SPECPATH, 'assets', 'wbrelay-icon.ico'),
    version=VERSION_FILE,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
