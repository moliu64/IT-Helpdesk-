$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$tunnelConfig = Join-Path $PSScriptRoot "deploy\060115.top\cloudflared-local-config.yml"
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$logDir = Join-Path $PSScriptRoot "output"

if (-not (Test-Path $python)) { throw "未找到 .venv，请先运行 一键启动.ps1 安装依赖。" }
if (-not (Test-Path $tunnelConfig)) { throw "未找到本机 Tunnel 配置：$tunnelConfig" }
if (-not (Test-Path $cloudflared)) { throw "未找到 cloudflared：$cloudflared" }

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:HELPDESK_HOST = "127.0.0.1"
$env:HELPDESK_PORT = "8787"
$env:HELPDESK_RAG_PREWARM = "0"

Start-Process -FilePath $python -ArgumentList @("scripts/start.py", "--host", "127.0.0.1", "--port", "8787") -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "online-app.log") -RedirectStandardError (Join-Path $logDir "online-app.err")
Start-Process -FilePath $cloudflared -ArgumentList @("--config", $tunnelConfig, "tunnel", "run") -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "cloudflared.log") -RedirectStandardError (Join-Path $logDir "cloudflared.err")

Write-Host "IT 运维工单智能体已启动" -ForegroundColor Green
Write-Host "用户入口：https://060115.top/"
Write-Host "后台管理：https://060115.top/backend"
Write-Host "健康检查：https://060115.top/healthz"
Write-Host "日志目录：$logDir"
