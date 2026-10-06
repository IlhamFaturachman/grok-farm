# Grok Farm + Liam API Shop — Agent Context

## What this is
Production stack: farm OIDC accounts → grok2api pool → traffic_intel edge → shop billing.

## Critical paths
- farm.py — Camoufox farmer + IMAP OTP (persistent poller)
- traffic_intel.py — boost, vision, billing 402, images bridge, stream no-fake-DONE
- shop/backend — FastAPI JWT shop; tiers PRICE 5000
- scripts/farm_loop.sh, fix_reauth_accounts.py, g2a_pool.py
- HANDOVER.md — ops bible

## Services (systemd / docker)
farm-loop, g2a-pool, traffic-intel, shop, fix-reauth.timer, cloudflared; docker: grok2api, api-gate

## Rules
- Don't disable GROK_ENHANCE without product decision
- Don't raise MC max-players/Xmx without free -h
- Deploy: scp/rsync + restart relevant unit; py_compile farm/traffic
- Secrets only in .env — never commit
- Shop keys ≠ admin opencode-farm key

## Smoke
curl -sS https://api.liamnevalackin.my.id/healthz
curl -sS https://shop.liamnevalackin.my.id/api/public/config
