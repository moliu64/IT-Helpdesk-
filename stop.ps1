$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Import-DeploymentEnv {
    $envFile = Join-Path $root ".env"
    if (-not (Test-Path -LiteralPath $envFile)) { return }
    $allowed = @("HELPDESK_ONLINE_PORT")
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -notmatch '^\s*([^=\s]+)\s*=\s*(.*)\s*$') { continue }
        $name = $Matches[1]
        if ($allowed -notcontains $name -or (Test-Path "Env:$name")) { continue }
        [Environment]::SetEnvironmentVariable($name, $Matches[2].Trim().Trim('"').Trim("'"), "Process")
    }
}

Import-DeploymentEnv
$logDir = Join-Path $root "output"
$pidFiles = @((Join-Path $logDir "online-app.pid"), (Join-Path $logDir "cloudflared.pid"))
foreach ($pidFile in $pidFiles) {
    if (Test-Path $pidFile) {
        $processId = 0
        $rawPid = Get-Content -Raw $pidFile
        if ([int]::TryParse($rawPid.Trim(), [ref]$processId)) {
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($process -and $process.ProcessName -in @("python", "python3", "cloudflared")) {
                Stop-Process -Id $processId -Force
            }
        }
        Remove-Item -LiteralPath $pidFile -Force
    }
}
$port = if ($env:HELPDESK_PORT) { [int]$env:HELPDESK_PORT } else { 8787 }
$connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
foreach ($connection in $connections) {
    if ($connection.OwningProcess -ne $PID) { Stop-Process -Id $connection.OwningProcess -Force }
}
Write-Host "Helpdesk 服务和已登记的 Cloudflare Tunnel 已停止。"
