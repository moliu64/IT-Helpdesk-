@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem Launch the WinForms panel via WScript so no console/terminal window is created.
rem The panel also hides its hosting console as a fallback and enforces a single
rem instance, so double-clicking this file again only focuses the existing panel.
wscript.exe //nologo "%~dp0scripts\launch_panel.vbs" "%~dp0项目控制面板.ps1"
if errorlevel 1 (
  echo Failed to start the control panel. Check the PowerShell execution policy.
  pause
)
endlocal
