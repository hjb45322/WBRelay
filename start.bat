@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WBRelay 多供应商网关
echo [WBRelay] 正在启动网关（端口 8789）...
if not exist "%~dp0dist\WBRelay.exe" (
  echo [WBRelay] 未找到 dist\WBRelay.exe。
  echo             先打包：pyinstaller WBRelay.spec
  echo             或改用源码方式：python relay.py
  pause
  exit /b 1
)
start "WBRelay" /min "%~dp0dist\WBRelay.exe"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8789/"
echo [WBRelay] 看板已打开。如未自动打开，请手动访问 http://127.0.0.1:8789/
pause
