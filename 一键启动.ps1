$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
Write-Host "此入口已更名为：一键部署上线.ps1" -ForegroundColor Yellow
& (Join-Path $PSScriptRoot "一键部署上线.ps1") @args
