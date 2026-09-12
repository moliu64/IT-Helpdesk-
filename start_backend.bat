@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe -c "import langgraph" >nul 2>&1 || .venv\Scripts\python.exe -m pip install "langgraph>=0.2,<2"
.venv\Scripts\python.exe scripts\start_backend.py %*
endlocal
