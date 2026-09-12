param(
    [switch]$NoBrowser,
    [switch]$SkipInstall
)
$ErrorActionPreference = "Stop"

# Force UTF-8 for PowerShell 5, Windows Terminal and child Python processes.
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = "utf-8"
Set-Location $PSScriptRoot

function Import-DeploymentEnv {
    $envFile = Join-Path $PSScriptRoot ".env"
    if (-not (Test-Path -LiteralPath $envFile)) { return }
    $allowed = @("HELPDESK_ONLINE_HOST", "HELPDESK_ONLINE_PORT", "HELPDESK_PUBLIC_URL", "HELPDESK_TUNNEL_CONFIG", "CLOUDFLARED_PATH")
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -notmatch '^\s*([^=\s]+)\s*=\s*(.*)\s*$') { continue }
        $name = $Matches[1]
        if ($allowed -notcontains $name -or (Test-Path "Env:$name")) { continue }
        [Environment]::SetEnvironmentVariable($name, $Matches[2].Trim().Trim('"').Trim("'"), "Process")
    }
}

function Ensure-Environment {
    param([string]$PythonPath)
    if (Test-Path -LiteralPath $PythonPath) { return }
    if ($SkipInstall) { throw "未找到虚拟环境，请先安装依赖，或不要使用 -SkipInstall。" }
    $bootstrap = Get-Command python -ErrorAction SilentlyContinue
    if (-not $bootstrap) { throw "未找到 Python，请安装 Python 3.10+ 后重试。" }
    Write-Host "正在创建 Python 虚拟环境并安装依赖，请等待首次部署完成..." -ForegroundColor Yellow
    & $bootstrap.Source -m venv (Join-Path $PSScriptRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败。" }
    & $PythonPath -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "安装 Python 依赖失败，请查看终端输出。" }
}

function Stop-StartedProcess {
    param([System.Diagnostics.Process]$Process, [string]$PidFile)
    if ($Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue }
    if ($PidFile -and (Test-Path -LiteralPath $PidFile)) { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue }
}

Import-DeploymentEnv
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$tunnelConfig = if ($env:HELPDESK_TUNNEL_CONFIG) { $env:HELPDESK_TUNNEL_CONFIG } else { Join-Path $PSScriptRoot "deploy\060115.top\cloudflared-local-config.yml" }
$cloudflared = if ($env:CLOUDFLARED_PATH) { $env:CLOUDFLARED_PATH } else { (Get-Command cloudflared -ErrorAction SilentlyContinue).Source }
if (-not $cloudflared -and (Test-Path "C:\Program Files (x86)\cloudflared\cloudflared.exe")) { $cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
$hostName = if ($env:HELPDESK_ONLINE_HOST) { $env:HELPDESK_ONLINE_HOST } else { "127.0.0.1" }
$port = if ($env:HELPDESK_ONLINE_PORT) { [int]$env:HELPDESK_ONLINE_PORT } else { 8787 }
$publicUrl = if ($env:HELPDESK_PUBLIC_URL) { $env:HELPDESK_PUBLIC_URL.TrimEnd('/') } else { "https://060115.top" }
$logDir = Join-Path $PSScriptRoot "output"
$appPidFile = Join-Path $logDir "online-app.pid"
$tunnelPidFile = Join-Path $logDir "cloudflared.pid"
$app = $null
$tunnel = $null

Ensure-Environment $python
if (-not (Test-Path -LiteralPath $tunnelConfig)) { throw "未找到本机 Tunnel 配置：$tunnelConfig`n请复制模板并填写 tunnel 与 credentials-file。" }
if (-not $cloudflared -or -not (Test-Path -LiteralPath $cloudflared)) { throw "未找到 cloudflared，请安装后设置 CLOUDFLARED_PATH。" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$existingListener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($existingListener) { throw "端口 $port 已被占用，请先运行 stop.ps1。" }
$existingTunnel = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
if ($existingTunnel) { throw "已检测到运行中的 cloudflared，请先运行 stop.ps1。" }
$env:HELPDESK_HOST = $hostName
$env:HELPDESK_PORT = [string]$port
$env:HELPDESK_RAG_PREWARM = "0"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"

try {
    $app = Start-Process -FilePath $python -ArgumentList @("scripts/start.py", "--host", $hostName, "--port", $port) -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "online-app.log") -RedirectStandardError (Join-Path $logDir "online-app.err") -PassThru
    $app.Id | Set-Content -Path $appPidFile -Encoding ascii
    $healthUrl = "http://127.0.0.1:$port/healthz"
    $healthy = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if ($app.HasExited) { break }
        try { $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2; if ($response.StatusCode -eq 200) { $healthy = $true; break } } catch { }
    }
    if (-not $healthy) { throw "应用健康检查失败，请查看 $logDir\online-app.err。" }
    $quotedTunnelConfig = '"' + $tunnelConfig + '"'
    $tunnel = Start-Process -FilePath $cloudflared -ArgumentList @("--config", $quotedTunnelConfig, "tunnel", "run") -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "cloudflared.log") -RedirectStandardError (Join-Path $logDir "cloudflared.err") -PassThru
    $tunnel.Id | Set-Content -Path $tunnelPidFile -Encoding ascii
    Start-Sleep -Milliseconds 800
    if ($tunnel.HasExited) { throw "Cloudflare Tunnel 启动失败，请查看 $logDir\cloudflared.err。" }
    Write-Host "IT 运维工单智能体已部署上线" -ForegroundColor Green
    Write-Host "用户入口：$publicUrl/"
    Write-Host "后台管理：$publicUrl/backend"
    Write-Host "健康检查：$publicUrl/healthz"
    Write-Host "日志目录：$logDir"
    if (-not $NoBrowser) { Start-Process $publicUrl }
} catch {
    Stop-StartedProcess $tunnel $tunnelPidFile
    Stop-StartedProcess $app $appPidFile
    throw
}
