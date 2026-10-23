param(
    [switch]$NoBrowser,
    [switch]$SkipInstall,
    [switch]$SkipTunnelDownload
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

function Resolve-Cloudflared {
    # The one-click panel must survive an uninstalled or moved cloudflared.exe.
    # Check every location this machine has plausibly stored it in, newest
    # user-scoped location first, so deployment never depends on PATH alone.
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:CLOUDFLARED_PATH) { $candidates.Add($env:CLOUDFLARED_PATH) }
    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { $candidates.Add($command.Source) }
    if ($env:USERPROFILE) {
        $candidates.Add((Join-Path $env:USERPROFILE ".cloudflared\cloudflared.exe"))
    }
    $candidates.Add((Join-Path $PSScriptRoot "deploy\060115.top\cloudflared.exe"))
    $candidates.Add("C:\Program Files (x86)\cloudflared\cloudflared.exe")
    $candidates.Add("C:\Program Files\cloudflared\cloudflared.exe")
    if ($env:LOCALAPPDATA) {
        $wingetRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        if (Test-Path -LiteralPath $wingetRoot) {
            Get-ChildItem -LiteralPath $wingetRoot -Filter "cloudflared.exe" -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 5 |
                ForEach-Object { $candidates.Add($_.FullName) }
        }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Install-Cloudflared {
    # Self-healing path used by the control panel: fetch the official Windows
    # build into the user profile, which needs no administrator rights.
    if ($SkipTunnelDownload) { return $null }
    $target = Join-Path $env:USERPROFILE ".cloudflared\cloudflared.exe"
    $targetDir = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    $url = if ($env:CLOUDFLARED_DOWNLOAD_URL) { $env:CLOUDFLARED_DOWNLOAD_URL } else { "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" }
    $temp = "$target.download"
    Write-Host "未检测到 cloudflared，正在从 Cloudflare 官方发布页下载（约 50 MB）..." -ForegroundColor Yellow
    $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
    $previousProgress = $ProgressPreference
    try {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
        [Net.ServicePointManager]::SecurityProtocol = $previousProtocol -bor [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temp -TimeoutSec 600
        if (-not (Test-Path -LiteralPath $temp) -or (Get-Item -LiteralPath $temp).Length -lt 5MB) {
            throw "下载文件不完整"
        }
        Move-Item -LiteralPath $temp -Destination $target -Force
        return $target
    } catch {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
        throw "自动下载 cloudflared 失败：$($_.Exception.Message)`n请手动下载 cloudflared-windows-amd64.exe 放到 $target，或在 .env 中设置 CLOUDFLARED_PATH 指向已有的 cloudflared.exe。"
    } finally {
        $ProgressPreference = $previousProgress
        [Net.ServicePointManager]::SecurityProtocol = $previousProtocol
    }
}

function Assert-TunnelConfiguration {
    param([string]$ConfigPath, [int]$AppPort)
    $text = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
    if ($text -match 'credentials-file:\s*"?([^"\r\n]+)"?') {
        $credentials = $Matches[1].Trim().Trim('"') -replace '\\\\', '\'
        if (-not (Test-Path -LiteralPath $credentials -PathType Leaf)) {
            throw "Tunnel 凭据文件不存在：$credentials`n请重新执行 cloudflared tunnel login，或修改 $ConfigPath 中的 credentials-file。"
        }
    }
    if ($text -match 'service:\s*http://(?:127\.0\.0\.1|localhost):(\d+)') {
        $configPort = [int]$Matches[1]
        if ($configPort -ne $AppPort) {
            Write-Host "警告：Tunnel 配置指向端口 $configPort，但本机源站使用端口 $AppPort，公网访问可能返回 502。请同步修改 $ConfigPath。" -ForegroundColor Yellow
        }
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
$cloudflared = Resolve-Cloudflared
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
Assert-TunnelConfiguration $tunnelConfig $port
if (-not $cloudflared) { $cloudflared = Install-Cloudflared }
if (-not $cloudflared) {
    throw "未找到 cloudflared，请把 cloudflared-windows-amd64.exe 下载到 $env:USERPROFILE\.cloudflared\cloudflared.exe，或在 .env 中设置 CLOUDFLARED_PATH。"
}
Write-Host "使用 cloudflared：$cloudflared"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$existingListener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($existingListener) { throw "端口 $port 已被占用，请先运行 stop.ps1。" }
$existingTunnel = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
if ($existingTunnel) { throw "已检测到运行中的 cloudflared，请先运行 stop.ps1。" }
$env:HELPDESK_HOST = $hostName
$env:HELPDESK_PORT = [string]$port
$env:HELPDESK_RAG_PREWARM = "1"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"

try {
    $app = Start-Process -FilePath $python -ArgumentList @("scripts/start.py", "--host", $hostName, "--port", $port) -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "online-app.log") -RedirectStandardError (Join-Path $logDir "online-app.err") -PassThru
    $app.Id | Set-Content -Path $appPidFile -Encoding ascii
    $healthUrl = "http://127.0.0.1:$port/healthz"
    $healthy = $false
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep -Milliseconds 500
        if ($app.HasExited) { break }
        try { $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2; if ($response.StatusCode -eq 200) { $healthy = $true; break } } catch { }
    }
    if (-not $healthy) {
        $appErrorLog = Join-Path $logDir "online-app.err"
        $appErrorTail = ""
        if (Test-Path -LiteralPath $appErrorLog) {
            $appErrorTail = (Get-Content -LiteralPath $appErrorLog -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() } | Select-Object -Last 5) -join "`n"
        }
        if ($appErrorTail) { throw "应用健康检查失败（60 秒内未就绪）：`n$appErrorTail`n完整日志：$appErrorLog" }
        throw "应用健康检查失败（60 秒内未就绪），请查看 $appErrorLog。"
    }
    # On Windows, the venv python launcher can leave the actual interpreter
    # with a different PID. Record the process that really owns the port so
    # stop.ps1 and the control panel can always stop the complete service.
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { throw "应用已返回健康检查，但未找到端口 $port 的监听进程。" }
    $listenerProcess = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if (-not $listenerProcess) { throw "无法识别端口 $port 的监听进程。" }
    $listenerCommandLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue).CommandLine
    if ($listenerCommandLine -and ($listenerCommandLine -notlike "*start.py*" -or $listenerCommandLine -notlike "*$PSScriptRoot*")) {
        throw "健康检查命中了非本项目进程，请先检查端口 $port。"
    }
    $listener.OwningProcess | Set-Content -Path $appPidFile -Encoding ascii
    $quotedTunnelConfig = '"' + $tunnelConfig + '"'
    $tunnel = Start-Process -FilePath $cloudflared -ArgumentList @("--config", $quotedTunnelConfig, "tunnel", "run") -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $logDir "cloudflared.log") -RedirectStandardError (Join-Path $logDir "cloudflared.err") -PassThru
    $tunnel.Id | Set-Content -Path $tunnelPidFile -Encoding ascii
    Start-Sleep -Milliseconds 800
    if ($tunnel.HasExited) { throw "Cloudflare Tunnel 启动失败，请查看 $logDir\cloudflared.err。" }
    # A tunnel process can stay alive while the connector never registers
    # (bad credentials, DNS route missing). Wait for the registration line so
    # the control panel only reports "上线" when the edge is really connected.
    $tunnelErrorLog = Join-Path $logDir "cloudflared.err"
    $tunnelReady = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if ($tunnel.HasExited) { break }
        try {
            $tunnelOutput = Get-Content -LiteralPath $tunnelErrorLog -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
            if ($tunnelOutput -match "Registered tunnel connection") { $tunnelReady = $true; break }
        } catch { }
    }
    if (-not $tunnelReady) {
        $tunnelTail = ""
        try {
            $tunnelTail = (Get-Content -LiteralPath $tunnelErrorLog -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() } | Select-Object -Last 5) -join "`n"
        } catch { }
        if ($tunnelTail) { throw "Cloudflare Tunnel 未能连接边缘节点：`n$tunnelTail`n完整日志：$tunnelErrorLog" }
        throw "Cloudflare Tunnel 未能连接边缘节点，请查看 $tunnelErrorLog。"
    }
    Write-Host "IT 运维工单智能体已部署上线" -ForegroundColor Green
    Write-Host "用户入口：$publicUrl/"
    Write-Host "后台管理：$publicUrl/backend"
    Write-Host "健康检查：$publicUrl/healthz"
    Write-Host "日志目录：$logDir"
    # Verify the public entry too; a local-only success would still look broken
    # in the browser. Cloudflare may need a few seconds to propagate.
    $publicHealthy = $false
    for ($i = 0; $i -lt 20; $i++) {
        try {
            $publicResponse = Invoke-WebRequest -UseBasicParsing -Uri "$publicUrl/healthz" -TimeoutSec 5
            if ($publicResponse.StatusCode -eq 200) { $publicHealthy = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 1000
    }
    if ($publicHealthy) {
        Write-Host "公网健康检查：$publicUrl/healthz 正常" -ForegroundColor Green
    } else {
        Write-Host "警告：本机服务与 Tunnel 已启动，但公网 $publicUrl/healthz 暂未通过。请稍后刷新页面，或检查 Cloudflare DNS 路由。" -ForegroundColor Yellow
    }
    if (-not $NoBrowser) { Start-Process $publicUrl }
} catch {
    Stop-StartedProcess $tunnel $tunnelPidFile
    Stop-StartedProcess $app $appPidFile
    throw
}
