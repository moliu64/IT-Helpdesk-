@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_backend.ps1" %*
endlocal
