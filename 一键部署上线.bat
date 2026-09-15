@echo off
chcp 65001 >nul
setlocal
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
start "Helpdesk 控制面板" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0一键部署上线.ps1" %*
endlocal
