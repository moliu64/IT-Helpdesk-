@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe scripts\start.py %*
endlocal
