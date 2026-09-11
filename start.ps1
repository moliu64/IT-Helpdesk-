$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    & $python -m pip install -r requirements.txt
}
& $python scripts/start.py @args
