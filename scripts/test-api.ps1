# Quick test for the community API endpoint (Windows).
# Verifies that the API is reachable and your key works.
#
# Usage:
#   .\scripts\test-api.ps1                          # prompts for key
#   $env:GROK_API_KEY="sk-xxx"; .\scripts\test-api.ps1   # key via env
$ErrorActionPreference = "Stop"

# ── Config ───────────────────────────────────────────────────────────────────
$ApiBase = if ($env:GROK_API_BASE) { $env:GROK_API_BASE } else { "https://api.example.com" }

# ── Key ──────────────────────────────────────────────────────────────────────
if (-not $env:GROK_API_KEY) {
    $env:GROK_API_KEY = Read-Host "Enter your API key"
}

if (-not $env:GROK_API_KEY) {
    Write-Error "No API key provided."
}

Write-Host "Testing $ApiBase ..."
Write-Host ""

# ── 1. Health check ──────────────────────────────────────────────────────────
Write-Host -NoNewline "[1/3] Health check ........... "
try {
    $resp = Invoke-WebRequest -Uri "$ApiBase/healthz" -Method GET -TimeoutSec 10 -UseBasicParsing
    if ($resp.StatusCode -eq 200) {
        Write-Host "OK ($($resp.Content))" -ForegroundColor Green
    } else {
        Write-Host "FAIL (HTTP $($resp.StatusCode))" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "UNREACHABLE" -ForegroundColor Red
    Write-Host $_.Exception.Message
    exit 1
}

# ── 2. List models ───────────────────────────────────────────────────────────
Write-Host -NoNewline "[2/3] List models (auth) ..... "
$headers = @{ "Authorization" = "Bearer $env:GROK_API_KEY" }
try {
    $resp = Invoke-WebRequest -Uri "$ApiBase/v1/models" -Method GET -Headers $headers -TimeoutSec 15 -UseBasicParsing
    if ($resp.StatusCode -eq 200) {
        $models = $resp.Content | ConvertFrom-Json
        $count = @($models.data).Count
        Write-Host "OK ($count models)" -ForegroundColor Green
    } else {
        Write-Host "FAIL (HTTP $($resp.StatusCode))" -ForegroundColor Red
        exit 1
    }
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 401) {
        Write-Host "FAIL (invalid key - HTTP 401)" -ForegroundColor Red
    } else {
        Write-Host "FAIL (HTTP $code)" -ForegroundColor Red
    }
    exit 1
}

# ── 3. Chat completion ───────────────────────────────────────────────────────
Write-Host -NoNewline "[3/3] Chat completion ........ "
$body = @{
    model    = "grok-3"
    messages = @(@{ role = "user"; content = "Say hi in one word" })
} | ConvertTo-Json -Depth 5

try {
    $resp = Invoke-WebRequest -Uri "$ApiBase/v1/chat/completions" -Method POST `
        -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec 30 -UseBasicParsing
    if ($resp.StatusCode -eq 200) {
        Write-Host "OK" -ForegroundColor Green
        Write-Host ""
        Write-Host "Response:"
        $json = $resp.Content | ConvertFrom-Json
        Write-Host $json.choices[0].message.content
    } else {
        Write-Host "FAIL (HTTP $($resp.StatusCode))" -ForegroundColor Red
        Write-Host $resp.Content
        exit 1
    }
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    Write-Host "FAIL (HTTP $code)" -ForegroundColor Red
    Write-Host $_.Exception.Message
    exit 1
}

Write-Host ""
Write-Host "All checks passed. Endpoint is ready to use:" -ForegroundColor Green
Write-Host "  $ApiBase/v1/chat/completions"
