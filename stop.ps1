$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Import-DeploymentEnv {
    $envFile = Join-Path $root ".env"
    if (-not (Test-Path -LiteralPath $envFile)) { return }
    $allowed = @("HELPDESK_ONLINE_PORT", "HELPDESK_PORT")
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -notmatch '^\s*([^=\s]+)\s*=\s*(.*)\s*$') { continue }
        $name = $Matches[1]
        if ($allowed -notcontains $name -or (Test-Path "Env:$name")) { continue }
        [Environment]::SetEnvironmentVariable($name, $Matches[2].Trim().Trim('"').Trim("'"), "Process")
    }
}

Import-DeploymentEnv
$logDir = Join-Path $root "output"
$port = if ($env:HELPDESK_ONLINE_PORT) { [int]$env:HELPDESK_ONLINE_PORT } elseif ($env:HELPDESK_PORT) { [int]$env:HELPDESK_PORT } else { 8787 }

function Stop-ProjectProcess([int]$processId, [string]$reason) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $process) { return }
    if ($process.ProcessName -notin @("python", "python3", "cloudflared")) { return }
    Write-Host "停止 $($process.ProcessName) PID=$processId（$reason）"
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

# 1) 优先按 pid 文件精确停止（最快、最安全）。
$pidFiles = @((Join-Path $logDir "online-app.pid"), (Join-Path $logDir "cloudflared.pid"))
foreach ($pidFile in $pidFiles) {
    if (-not (Test-Path -LiteralPath $pidFile)) { continue }
    $processId = 0
    $rawPid = Get-Content -Raw -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($rawPid -and [int]::TryParse($rawPid.Trim(), [ref]$processId)) {
        Stop-ProjectProcess $processId "pid 文件"
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# 2) 兜底：只清理本仓库、且确实属于本项目端口的进程，避免误杀仓库里
#    正在运行的测试/临时服务。CIM 查询加超时，防止 WMI 卡死整个停止流程。
$ownedProcesses = @()
try {
    $ownedProcesses = Get-CimInstance Win32_Process `
        -Filter "Name = 'cloudflared.exe' OR Name = 'python.exe' OR Name = 'python3.exe'" `
        -OperationTimeoutSec 5 -ErrorAction SilentlyContinue
} catch {
    Write-Host "WMI 进程查询超时，跳过兜底清理。"
}
foreach ($owned in $ownedProcesses) {
    if (-not $owned -or $owned.ProcessId -eq $PID) { continue }
    $commandLine = [string]$owned.CommandLine
    if (-not $commandLine -or $commandLine -notlike "*$root*") { continue }
    $isCloudflared = $owned.Name -like "cloudflared*"
    $isProjectPython = ($owned.Name -like "python*") -and
        ($commandLine -like "*$port*") -and
        (($commandLine -like "*scripts/start.py*") -or ($commandLine -like "*ui/server.py*"))
    if ($isCloudflared -or $isProjectPython) {
        Stop-ProjectProcess $owned.ProcessId "命令行匹配本项目"
    }
}

# 3) 最后清理仍监听项目端口的本项目进程；非本项目进程只提示，不杀。
$connections = @()
try {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
} catch { }
foreach ($connection in $connections) {
    $ownerPid = [int]$connection.OwningProcess
    if ($ownerPid -eq $PID) { continue }
    $commandLine = ""
    try {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -OperationTimeoutSec 3 -ErrorAction SilentlyContinue
        if ($info) { $commandLine = [string]$info.CommandLine }
    } catch { }
    if ($commandLine -and $commandLine -like "*$root*") {
        Stop-ProjectProcess $ownerPid "占用端口 $port"
    } else {
        Write-Host "端口 $port 被非本项目进程占用（PID=$ownerPid），已跳过：$commandLine"
    }
}

Write-Host "Helpdesk 服务和已登记的 Cloudflare Tunnel 已停止。"
exit 0
