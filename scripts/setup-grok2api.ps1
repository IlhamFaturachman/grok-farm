# Generate docker/grok2api/config.yaml with random secrets (Windows).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Example = Join-Path $Root "docker\grok2api\config.example.yaml"
$Dest = Join-Path $Root "docker\grok2api\config.yaml"

if (Test-Path $Dest) {
    Write-Host "Already exists: $Dest (not overwriting)"
    exit 0
}
if (-not (Test-Path $Example)) {
    Write-Error "Missing $Example"
}

$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$Jwt = -join ($bytes | ForEach-Object { $_.ToString("x2") })
$EncBytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($EncBytes)
$Enc = [Convert]::ToBase64String($EncBytes)
$AdminUser = if ($env:GROK2API_ADMIN_USER) { $env:GROK2API_ADMIN_USER } else { "admin" }
if ($env:GROK2API_ADMIN_PASS) {
    $AdminPass = $env:GROK2API_ADMIN_PASS
} else {
    $pbytes = New-Object byte[] 16
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($pbytes)
    $AdminPass = [Convert]::ToBase64String($pbytes).TrimEnd("=") -replace "[/+=]", "x"
}

$t = Get-Content -Raw -Path $Example
$t = $t.Replace("replace-with-at-least-32-characters", $Jwt)
$t = $t.Replace("replace-with-base64-key", $Enc)
$t = $t.Replace("replace-with-a-strong-password", $AdminPass)
$t = $t -replace '(?m)(bootstrapAdmin:\r?\n(?:.*\r?\n)*?\s*username:\s*)"[^"]*"', "`${1}`"$AdminUser`""
Set-Content -Path $Dest -Value $t -Encoding UTF8

Write-Host "Wrote $Dest"
Write-Host "  admin user: $AdminUser"
Write-Host "  admin pass: $AdminPass"

$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    $envText = Get-Content -Raw $EnvFile
    if ($envText -notmatch "GROK2API_URL=") {
        Add-Content $EnvFile @"

# ── grok2api (auto-export / import) ──────────────────────────────────────────
GROK2API_URL=http://127.0.0.1:8000
GROK2API_ADMIN_USER=$AdminUser
GROK2API_ADMIN_PASS=$AdminPass
GROK2API_EXPORT=true
GROK2API_AUTO_IMPORT=true
"@
        Write-Host "Appended GROK2API_* to .env"
    }
}

Write-Host ""
Write-Host "Next:"
Write-Host "  docker compose up -d"
Write-Host "  open http://127.0.0.1:8000  (login $AdminUser)"
Write-Host "  python farm.py -n 1 -c 1 -y"
