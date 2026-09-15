@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
start "Helpdesk 控制面板" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0项目控制面板.ps1"
endlocal
