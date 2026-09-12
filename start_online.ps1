$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$tunnelConfig = Join-Path $PSScriptRoot "deploy\060115.top\cloudflared-local-config.yml"
$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$logDir = Join-Path $PSScriptRoot "output"
$appPidFile = Join-Path $logDir "online-app.pid"
$tunnelPidFile = Join-Path $logDir "cloudflared.pid"

if (-not (Test-Path $python)) { throw "未找到 .venv，请先运行一键启动入口安装依赖。" }
if (-not (Test-Path $tunnelConfig)) { throw "未找到本机 Tunnel 配置：$tunnelConfig" }
if (-not (Test-Path $cloudflared)) { throw "未找到 cloudflared：$cloudflared" }

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$existingListener = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
if ($existingListener) { throw "端口 8787 已被占用，请先运行 stop.ps1 或检查现有服务。" }
$existingTunnel = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
if ($existingTunnel) { throw "已检测到运行中的 cloudflared，请先运行 stop.ps1，避免重复建立 Tunnel。" }
$env:HELPDESK_HOST = "127.0.0.1"
$env:HELPDESK_PORT = "8787"
$env:HELPDESK_RAG_PREWARM = "0"

$app = Start-Process -FilePath $python -ArgumentList @("scripts/start.py", "--host", "127.0.0.1", "--port", "8787") -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "online-app.log") -RedirectStandardError (Join-Path $logDir "online-app.err") -PassThru
$app.Id | Set-Content -Path $appPidFile -Encoding ascii
Start-Sleep -Milliseconds 800
if ($app.HasExited) { throw "应用启动失败，请查看 $logDir\online-app.err" }
# Start-Process joins ArgumentList into one command line on Windows. Quote the
# config path explicitly because this workspace path contains spaces.
$quotedTunnelConfig = '"' + $tunnelConfig + '"'
$tunnel = Start-Process -FilePath $cloudflared -ArgumentList @("--config", $quotedTunnelConfig, "tunnel", "run") -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "cloudflared.log") -RedirectStandardError (Join-Path $logDir "cloudflared.err") -PassThru
$tunnel.Id | Set-Content -Path $tunnelPidFile -Encoding ascii

Write-Host "IT 运维工单智能体已启动" -ForegroundColor Green
Write-Host "用户入口：https://060115.top/"
Write-Host "后台管理：https://060115.top/backend"
Write-Host "健康检查：https://060115.top/healthz"
Write-Host "日志目录：$logDir"
