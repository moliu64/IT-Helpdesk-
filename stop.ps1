$ErrorActionPreference = "SilentlyContinue"
$port = if ($env:HELPDESK_PORT) { [int]$env:HELPDESK_PORT } else { 8787 }
$connections = Get-NetTCPConnection -LocalPort $port -State Listen
foreach ($connection in $connections) { Stop-Process -Id $connection.OwningProcess -Force }
Write-Host "Helpdesk service on port $port stopped."
