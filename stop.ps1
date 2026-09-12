$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "output"
$pidFiles = @((Join-Path $logDir "online-app.pid"), (Join-Path $logDir "cloudflared.pid"))
foreach ($pidFile in $pidFiles) {
    if (Test-Path $pidFile) {
        $processId = [int](Get-Content -Raw $pidFile)
        Stop-Process -Id $processId -Force
        Remove-Item -LiteralPath $pidFile -Force
    }
}
$port = if ($env:HELPDESK_PORT) { [int]$env:HELPDESK_PORT } else { 8787 }
$connections = Get-NetTCPConnection -LocalPort $port -State Listen
foreach ($connection in $connections) {
    if ($connection.OwningProcess -ne $PID) { Stop-Process -Id $connection.OwningProcess -Force }
}
Write-Host "Helpdesk 服务和已登记的 Cloudflare Tunnel 已停止。"
