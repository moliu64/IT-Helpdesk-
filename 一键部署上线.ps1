param(
    [switch]$NoBrowser,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
Write-Host "正在打开 IT 运维工单智能体控制面板..." -ForegroundColor Cyan
$controlPanel = Join-Path $PSScriptRoot "项目控制面板.ps1"
$arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $controlPanel + '"'))
if ($NoBrowser) { $arguments += "-NoBrowser" }
if ($SkipInstall) { $arguments += "-SkipInstall" }
Start-Process -FilePath "powershell.exe" -WindowStyle Normal -ArgumentList $arguments -WorkingDirectory $PSScriptRoot
