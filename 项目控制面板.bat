@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Launch the WinForms control panel without a visible console window.
rem The panel itself also hides its hosting console as a second safeguard.
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0项目控制面板.ps1"
if errorlevel 1 (
  echo Failed to start the control panel. Check the PowerShell execution policy.
  pause
)
endlocal
