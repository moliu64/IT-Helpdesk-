$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "IT 运维工单智能体 - 一键启动入口" -ForegroundColor Cyan
Write-Host "用户入口：http://127.0.0.1:8787/"
Write-Host "后台管理：http://127.0.0.1:8787/backend"
& (Join-Path $PSScriptRoot "start.ps1") @args
