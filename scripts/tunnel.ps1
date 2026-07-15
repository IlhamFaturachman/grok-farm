# Open an SSH tunnel to reach grok2api admin panel + traffic-intel dashboard
# on your local machine (Windows). Both services are locked to 127.0.0.1 on the server.
#
# While this is running, open in your browser:
#   Admin panel     ->  http://localhost:8000
#   Intel dashboard ->  http://localhost:8001
#
# Close the PowerShell window or press Ctrl-C to tear down.
$ErrorActionPreference = "Stop"

# ── Config ───────────────────────────────────────────────────────────────────
$Server    = if ($env:TUNNEL_SERVER)       { $env:TUNNEL_SERVER }       else { "user@your-server.example.com" }
$AdminPort = if ($env:TUNNEL_ADMIN_PORT)    { $env:TUNNEL_ADMIN_PORT }    else { 8000 }
$IntelPort = if ($env:TUNNEL_INTEL_PORT)    { $env:TUNNEL_INTEL_PORT }    else { 8001 }

# ── Pre-flight ───────────────────────────────────────────────────────────────
$ssh = Get-Command ssh -ErrorAction SilentlyContinue
if (-not $ssh) {
    Write-Error "ssh not found. Install OpenSSH Client: Settings > Apps > Optional features."
}

# Check if local ports are already in use
foreach ($p in @($AdminPort, $IntelPort)) {
    $busy = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host "WARN: local port $p is already in use (admin/intel may fail to forward)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  SSH tunnel - grok2api admin + intel             " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Admin panel   ->  http://localhost:$AdminPort"
Write-Host "  Intel dash    ->  http://localhost:$IntelPort"
Write-Host ""
Write-Host "  Ctrl-C to close the tunnel."
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

# -N  no remote command (just forward ports)
ssh -N `
    -L "${AdminPort}:127.0.0.1:8000" `
    -L "${IntelPort}:127.0.0.1:8001" `
    $Server
