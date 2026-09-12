$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "IT 运维工单智能体 - 一键启动入口" -ForegroundColor Cyan
Write-Host "用户入口：http://127.0.0.1:8787/"
Write-Host "后台管理：http://127.0.0.1:8787/backend"
Write-Host "同一个服务同时启动用户端和后台端，浏览器将自动打开两个页面。"
& (Join-Path $PSScriptRoot "start.ps1") --open-browser @args
