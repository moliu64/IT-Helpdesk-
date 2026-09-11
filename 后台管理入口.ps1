$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "IT 运维工单智能体 - 后台管理入口" -ForegroundColor Cyan
Write-Host "工单管理、运行监视、知识库和访问记录"
Write-Host "地址：http://127.0.0.1:8788/backend"
& (Join-Path $PSScriptRoot "start_backend.ps1") @args
