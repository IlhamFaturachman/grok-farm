# Multi-IP VPNX + Castle Farm Handover

## Overview

Grok farm uses 4 VPN Gate instances (JP/KR/TH/HK) + WARP for multi-IP multi-country proxy rotation. Castle.io anti-bot is active on x.ai signup — browser farm mints Castle tokens natively (proven).

## Architecture

```
                    ┌──────────────────────────────────┐
                    │       mcs-liam VPS (Indonesia)    │
                    │       103.150.61.32               │
                    ├──────────────────────────────────┤
                    │                                  │
  farm.py ────────► GROK_PROXY_POOL (round-robin)      │
  (Camoufox)        │                                  │
                    ├──── WARP socks5://127.0.0.1:1080 │
                    │     exit: 104.28.x.x (CF edge)   │
                    │                                  │
                    ├──── vpnx  JP  :8082 (HTTP)       │
                    ├──── vpnx-kr KR :8085             │
                    ├──── vpnx-th TH :8086             │
                    ├──── vpnx-hk HK :8087             │
                    │     each: VPN Gate rotating IP   │
                    │     SOCKS5 no-auth :1081+        │
                    │     API :9090+ (rotate/connect)  │
                    └──────────────────────────────────┘
```

## VPNX Containers

All built from `vpnx-build/` (submodule of `github.com/waguriagentic/vpnx`).

**Patched**:
- `scripts/entrypoint.sh` line 24 — `socksmethod: none` (baked into image, not ephemeral)
- `app/vpn.py` lines 71-73 — `int('-')` parser crash fix (`.lstrip('-').isdigit()` guard)
- `app/vpn.py` line 278 — per-server connect timeout 30s→10s
- `app/vpn.py` line 345 — candidates 5→8

**Critical**: containers MUST run with `--cap-add=NET_ADMIN --device=/dev/net/tun`.
Without `CAP_NET_ADMIN`, OpenVPN fails with `TUNSETIFF: Operation not permitted` and all connections time out.

| Container  | Country | HTTP  | SOCKS5 | API  | API Token           |
|------------|---------|-------|--------|------|---------------------|
| vpnx       | JP      | 8082  | 1081   | 9090 | liam-vpnx-secret    |
| vpnx-kr    | KR      | 8085  | 1085   | 9093 | liam-vpnx-kr        |
| vpnx-th    | TH      | 8086  | 1086   | 9094 | liam-vpnx-th        |
| vpnx-hk    | HK      | 8087  | 1087   | 9095 | liam-vpnx-hk        |

**Verified exit IPs** (2026-08-06):
| Proxy     | Exit IP         | Country  |
|-----------|-----------------|----------|
| WARP      | 104.28.163.28   | CF edge  |
| VPNX JP   | 219.104.133.188 | Japan    |
| VPNX KR   | 210.179.77.38   | Korea    |
| VPNX TH   | 184.82.117.41   | Thailand |
| VPNX HK   | 14.133.57.43    | HK       |

**Credentials**: SOCKS user=`vpnx`, pass=`liam2026` (HTTP proxy auth only; SOCKS5 no-auth after bake).

### Rebuild from source

```bash
cd /opt/grok-farm/vpnx-build
docker build -t vpnx:latest .
# Recreate containers (see docker run commands in deploy section)
```

### Recreate containers

```bash
# JP — MUST include --cap-add=NET_ADMIN --device=/dev/net/tun
docker run -d --name vpnx --network bridge --restart unless-stopped \
  --cap-add=NET_ADMIN --device=/dev/net/tun \
  -p 172.18.0.1:1081:1080 -p 172.18.0.1:8082:8080 -p 172.18.0.1:9090:8000 \
  -e SOCKS_USER=vpnx -e SOCKS_PASS=liam2026 -e API_TOKEN=liam-vpnx-secret \
  -e CONNECT_COUNTRY=JP \
  vpnx:latest

# KR
docker run -d --name vpnx-kr --network bridge --restart unless-stopped \
  --cap-add=NET_ADMIN --device=/dev/net/tun \
  -p 172.18.0.1:1085:1080 -p 172.18.0.1:8085:8080 -p 172.18.0.1:9093:8000 \
  -e SOCKS_USER=vpnx -e SOCKS_PASS=liam2026 -e API_TOKEN=liam-vpnx-kr \
  -e CONNECT_COUNTRY=KR \
  vpnx:latest

# TH
docker run -d --name vpnx-th --network bridge --restart unless-stopped \
  --cap-add=NET_ADMIN --device=/dev/net/tun \
  -p 172.18.0.1:1086:1080 -p 172.18.0.1:8086:8080 -p 172.18.0.1:9094:8000 \
  -e SOCKS_USER=vpnx -e SOCKS_PASS=liam2026 -e API_TOKEN=liam-vpnx-th \
  -e CONNECT_COUNTRY=TH \
  vpnx:latest

# HK
docker run -d --name vpnx-hk --network bridge --restart unless-stopped \
  --cap-add=NET_ADMIN --device=/dev/net/tun \
  -p 172.18.0.1:1087:1080 -p 172.18.0.1:8087:8080 -p 172.18.0.1:9095:8000 \
  -e SOCKS_USER=vpnx -e SOCKS_PASS=liam2026 -e API_TOKEN=liam-vpnx-hk \
  -e CONNECT_COUNTRY=HK \
  vpnx:latest

## VPNX Watchdog

**Script**: `scripts/vpnx_watchdog.sh`
**Timer**: `deploy/vpnx-watchdog.timer` (every 3 min)

Features:
- Parallel health check all 4 instances (check HTTP proxy → ipify)
- If dead: connect → rotate → docker restart (escalating)
- Rewrite `GROK_PROXY_POOL` in `.env` with healthy proxies only
- `VPNX_DIRECT_IPS=103.150.61.32,103.253.245.148` — reject fallthrough to VPS direct
- `VPNX_KEEP_WARP=1` — prepend WARP SOCKS5 if healthy

**Farm loop integration**:
- Pre-batch: `vpnx_watchdog.sh --require 1` (heal dead, don't rotate working)
- Post-batch success: `vpnx_watchdog.sh --require 1` (heal dead only)
- Post-batch fail: `vpnx_watchdog.sh --rotate --require 1` (rotate = new exit IP)

## Castle.io

### Status: SOLVED (browser-native)

x.ai signup page has Castle.io SDK bundled in Next.js chunks:
- `enableCastle: true`
- `castlePk: pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz`
- `improvedCastleFlow: true`

**Browser farm (Camoufox) generates valid Castle tokens natively.** The x.ai frontend calls `createRequestToken()` internally, embeds result as `castleRequestToken` in Server Action POST body to `accounts.x.ai/sign-up`.

**Proof** (from `results/castle_sample_*.txt`):
```
castleRequestToken":"IBYIll|UwnPCzA2NXEPcPHJC6w0LKhyrkozTQzXd7bUTvbxs3TxMfAtts0SDZD1cE1MZgr2iGJzSA9lDwnOZy0siC6PYKvIYSn..."
```
Token is ~200+ chars, present in every successful signup. x.ai accepts it (OTP received, account created).

### Castle monitor in farm.py

`_castle_request_monitor()` uses `page.route("**/*")` (not `page.on("request")` — Camoufox/Firefox skips that for fetch/Server Actions). Intercepts POST to `accounts.x.ai`, searches body for `castleRequestToken`, dumps sample to `results/castle_sample_{attempt}.txt`.

### Castle + VPN Gate risk

Castle June 2026: `vpn_access` signal flags known VPN provider IPs. VPN Gate is well-known free VPN. **However**: contextual risk assessment means VPN alone ≠ block. Camoufox fingerprint + humanized mouse + human-like behavior = pass. Current success rate: 3/4 (75%) with VPN Gate.

### Protocol fallback (Grok-Register)

If browser farm blocked entirely:
1. `/usr/local/bin/grok` (Go binary, built from `github.com/Charles-0509/Grok-Register`)
2. Protocol-based signup: TLS fingerprint `chrome_124`/`chrome_120`, no browser
3. `BuildSignupBodyCastle(email, password, code, turnstileToken, castleToken)` — field ready
4. Castle token from browser sample → pass to protocol call
5. `grok reoauth accounts.txt` — re-login expired SSO tokens

## Config

### Server .env key values

```env
GROK_PROXY_POOL=socks5://127.0.0.1:1080,http://vpnx:liam2026@172.18.0.1:8082,...
GROK_CONCURRENT=2
FARM_LOOP_CONCURRENT=2
FARM_LOOP_BATCH_N=4
FARM_LOOP_DAILY_CAP=999999
GROK_TURNSTILE_PARALLEL=4
GROK_GEOIP=false
GROK_EMAIL_DOMAINS=aduhteh.my.id,clique.web.id,gwidojaya.my.id,...
```

### Grok-Register config (`/root/.grok/config.env`)

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=aduhteh.my.id
REGISTER_PROXY=http://vpnx:liam2026@172.18.0.1:8082
CLEARANCE_ENABLED=0
CLEARANCE_MODE=never
CF_IMPERSONATE=chrome_131
TURNSTILE_PROVIDER=browser
TURNSTILE_MODE=offscreen
```

## Gateway Egress Nodes (serving path)

| Node | Provider  | IP              | Country |
|------|-----------|-----------------|---------|
| 1    | WARP      | 104.28.163.28   | CF edge |
| 5    | VPNX JP   | 221.45.120.53   | Japan   |
| 6    | VPNX KR   | (rotating)      | Korea   |
| 7    | VPNX TH   | (rotating)      | Thailand|
| 8    | VPNX HK   | (rotating)      | HK      |

autoBalance + autoAssign enabled on grok2api gateway.

## Known Limitations

- VPN Gate free servers drop randomly (JP most stable, KR/TH/HK flaky)
- `NS_ERROR_UNKNOWN_HOST` = VPN tunnel drop (watchdog heals before next batch)
- Castle `vpn_access` flag = VPN Gate IP risk signal (mitigated by fingerprint quality)
- SOCKS5 auth incompatible with Camoufox (use HTTP proxy for farming, SOCKS5 no-auth for gateway egress)
