@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WBRelay 多供应商网关
echo [WBRelay] 正在启动网关（端口 8789）...
start "WBRelay" /min "%~dp0WBRelay.exe"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8789/"
echo [WBRelay] 看板已打开。如未自动打开，请手动访问 http://127.0.0.1:8789/
pause
