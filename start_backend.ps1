$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    & $python -m pip install -r requirements.txt
}
$hasLangGraph = & $python -c "import importlib.util; print('1' if importlib.util.find_spec('langgraph') else '0')"
if ($hasLangGraph -ne "1") {
    & $python -m pip install "langgraph>=0.2,<2"
}
& $python scripts/start_backend.py @args
