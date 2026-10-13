# -*- coding: utf-8 -*-
"""WBRelay 入口。

保持与 start.bat、PyInstaller 打包命令的兼容：
    python relay.py
    pyinstaller --onefile --noconsole --add-data "dashboard.html;." relay.py
"""
from wbrelay.app import main

if __name__ == "__main__":
    main()
