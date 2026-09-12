$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
Write-Host "IT 运维工单智能体 - 一键部署上线" -ForegroundColor Cyan
Write-Host "将启动本机源站并连接 Cloudflare Tunnel。"
& (Join-Path $PSScriptRoot "start_online.ps1") @args
