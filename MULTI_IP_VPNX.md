# Multi-IP VPNX + Castle Farm Handover

## Overview

Grok farm uses 4 VPN Gate instances (JP / KR / TH / JP2) + WARP for multi-IP multi-country proxy rotation.
Castle.io anti-bot is active on x.ai signup — browser farm mints Castle tokens natively (proven).

Goals:
- Unique exit IPs across batches via **round-robin rotate-one** (not only on fail)
- Permanent SOCKS5 **no-auth** baked into image
- Vendored `vpnx-build/` (no submodule; upstream push denied)
- No paid residential proxies / no extra VPS

## Architecture

```
                    ┌──────────────────────────────────┐
                    │       mcs-liam VPS (Indonesia)    │
                    │  public 103.150.61.32             │
                    │  SSH via 103.253.245.148 (mcs-liam)│
                    ├──────────────────────────────────┤
                    │                                  │
  farm.py ────────► GROK_PROXY_POOL (round-robin)      │
  (Camoufox)        │                                  │
                    ├──── WARP socks5://127.0.0.1:1080 │
                    │     exit: 104.28.x.x (CF edge)   │
                    │                                  │
                    ├──── vpnx    JP  :8082 (HTTP)     │
                    ├──── vpnx-kr KR  :8085            │
                    ├──── vpnx-th TH  :8086            │
                    ├──── vpnx-jp2 JP :8087            │
                    │     each: VPN Gate rotating IP   │
                    │     SOCKS5 no-auth :1081+        │
                    │     API :9090+ (rotate/connect)  │
                    └──────────────────────────────────┘
```

SSH: `ssh -i ~/.ssh/mcs-liam mcs-liam` (HostName `103.253.245.148` in `~/.ssh/config`).
Do **not** assume bare `root@103.150.61.32` always routes from every local network.

## VPNX Containers

Built from **vendored** `vpnx-build/` (not a live submodule of `waguriagentic/vpnx` — upstream push 403).

**Patched**:
- `scripts/entrypoint.sh` line 24 — `socksmethod: none` (baked into image)
- `app/vpn.py` CSV parser — `int('-')` crash → `.lstrip('-').isdigit()` guard
- `app/vpn.py` `_try_connect` wait — `range(10)` (10s max per server)
- `app/vpn.py` connect candidates — try up to 8 after `random.shuffle`
- `app/vpn.py` `connect()` / `rotate()` — `random.shuffle(candidates)` so all containers don't pick the same top-speed server
- country filter honored on API `/connect?country=XX` and `/rotate?country=XX`

**Critical**: containers MUST run with `--cap-add=NET_ADMIN --device=/dev/net/tun`.
Without `CAP_NET_ADMIN`, OpenVPN fails with `TUNSETIFF: Operation not permitted` and all connections time out.

| Container | Country | HTTP | SOCKS5 | API  | API Token        |
|-----------|---------|------|--------|------|------------------|
| vpnx      | JP      | 8082 | 1081   | 9090 | liam-vpnx-secret |
| vpnx-kr   | KR      | 8085 | 1085   | 9093 | liam-vpnx-kr     |
| vpnx-th   | TH      | 8086 | 1086   | 9094 | liam-vpnx-th     |
| vpnx-jp2  | JP      | 8087 | 1087   | 9095 | liam-vpnx-jp2    |

**Why JP2 not HK**: VPN Gate free rarely has reachable HK exits. HK always fell back to Japan or stayed dead (direct IP leak). Second JP instance increases Japan IP diversity.

**Credentials**: SOCKS user=`vpnx`, pass=`liam2026` (HTTP proxy auth only; SOCKS5 no-auth after bake).

**Direct IPs (reject fallthrough)**: `103.150.61.32,103.253.245.148` — if proxy returns these, tunnel is dead.

### Rebuild from source

```bash
cd /opt/grok-farm/vpnx-build
docker build -t vpnx:latest .
```

### Recreate containers

```bash
# JP
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

# JP2 (replaces dead HK)
docker run -d --name vpnx-jp2 --network bridge --restart unless-stopped \
  --cap-add=NET_ADMIN --device=/dev/net/tun \
  -p 172.18.0.1:1087:1080 -p 172.18.0.1:8087:8080 -p 172.18.0.1:9095:8000 \
  -e SOCKS_USER=vpnx -e SOCKS_PASS=liam2026 -e API_TOKEN=liam-vpnx-jp2 \
  -e CONNECT_COUNTRY=JP \
  vpnx:latest
```

Or: `docker compose --profile vpnx up -d` (compose uses `vpnx-jp2`, not `vpnx-hk`).

## VPNX Watchdog

**Script**: `scripts/vpnx_watchdog.sh`  
**Timer**: `deploy/vpnx-watchdog.timer` (every 3 min)  
**State**: `/opt/grok-farm/.vpnx_rotate_idx` (0..3 round-robin)

### Flags

| Flag | Behavior |
|------|----------|
| (default) | heal dead only; leave healthy exits alone |
| `--require N` | exit 1 if healthy count < N |
| `--rotate` | force rotate **all** instances (slow, lossy on free VPN Gate) |
| `--rotate-one` | force rotate **one** instance (round-robin) + heal rest |

### Why `--rotate-one` not full `--rotate` every batch

Measured:
- Full `--rotate` of 4: **~84s**, often ~50% drop / IPs collapse
- `--rotate-one` when healthy: **~30s**, keeps 3 tunnels alive, changes 1 exit IP per batch

### Country query + shuffle

- `heal_one` always posts `?country=${country}` on `/rotate` and `/connect`
- bare `/connect` only as last resort (VPN Gate may have zero servers for a country)
- without country query, KR/TH heal reconnected to Japan top servers → all 4 same IP
- `random.shuffle` on candidates stops every container picking the same top-speed server

### Farm loop integration (`scripts/farm_loop.sh`)

```bash
# post-batch
if created == 0:
  vpnx_watchdog.sh --rotate --require 1      # total fail → full rotate
else:
  vpnx_watchdog.sh --rotate-one --require 1  # success → rotate one exit
```

Env used by watchdog:
```bash
VPNX_UPDATE_ENV=/opt/grok-farm/.env
VPNX_KEEP_WARP=1
VPNX_DIRECT_IPS=103.150.61.32,103.253.245.148
```

Watchdog rewrites `GROK_PROXY_POOL` from healthy HTTP ports only (+ WARP if healthy).

### Manual checks

```bash
# health + rewrite pool
VPNX_UPDATE_ENV=/opt/grok-farm/.env VPNX_KEEP_WARP=1 \
  VPNX_DIRECT_IPS=103.150.61.32,103.253.245.148 \
  bash /opt/grok-farm/scripts/vpnx_watchdog.sh --require 1

# timed rotate-one
time timeout 45 env VPNX_UPDATE_ENV=/opt/grok-farm/.env VPNX_KEEP_WARP=1 \
  VPNX_DIRECT_IPS=103.150.61.32,103.253.245.148 \
  bash /opt/grok-farm/scripts/vpnx_watchdog.sh --rotate-one --require 1

# exit IPs
for p in 8082 8085 8086 8087; do
  curl -s -x "http://vpnx:liam2026@172.18.0.1:$p" https://api.ipify.org --max-time 8
  echo "  :$p"
done
```

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

Castle `vpn_access` signal flags known VPN provider IPs. VPN Gate is well-known free VPN. **However**: contextual risk assessment means VPN alone ≠ block. Camoufox fingerprint + humanized mouse + human-like behavior = pass.

### Protocol fallback (Grok-Register)

If browser farm blocked entirely:
1. `/usr/local/bin/grok` (Go binary, built from `github.com/Charles-0509/Grok-Register`)
2. Protocol-based signup: TLS fingerprint `chrome_124`/`chrome_120`, no browser
3. `BuildSignupBodyCastle(...)` — field ready
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

| Node | Provider | IP            | Country  |
|------|----------|---------------|----------|
| 1    | WARP     | 104.28.x.x    | CF edge  |
| 5    | VPNX JP  | (rotating)    | Japan    |
| 6    | VPNX KR  | (rotating)    | Korea    |
| 7    | VPNX TH  | (rotating)    | Thailand |
| 8    | VPNX JP2 | (rotating)    | Japan    |

autoBalance + autoAssign enabled on grok2api gateway.

## farm.py proxy selection

- `next_proxy()` round-robin `_proxy_idx % len(PROXY_POOL)` under asyncio lock
- one proxy per signup attempt; pool of WARP+healthy VPNX repeats within a process lifetime
- **inter-batch** exit IP change depends on VPNX `--rotate-one` after each successful batch

## Known failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Exit IP is `103.150.61.32` or `103.253.245.148` | tunnel dead (VPS leak) | watchdog drops from pool; heal/connect |
| `TUNSETIFF: Operation not permitted` | missing NET_ADMIN | recreate with `--cap-add=NET_ADMIN --device=/dev/net/tun` |
| `invalid literal for int() with base 10: '-'` | CSV parser | `.lstrip('-').isdigit()` guard |
| all 4 same Japan IP | no `?country=` on heal, or no shuffle | country query + `random.shuffle` |
| full rotate ~84s / 50% drop | free VPN Gate | use `--rotate-one` on success path |
| HK always FAIL/direct | no usable HK servers | use `vpnx-jp2` |
| import crash missing logging | only kept `import random` | keep both `logging` + `random` |

## Deploy checklist

```bash
# 1) push local
git push origin main

# 2) pull on server (or scp patched files)
scp scripts/vpnx_watchdog.sh scripts/farm_loop.sh \
  root@mcs-liam:/opt/grok-farm/scripts/
scp vpnx-build/app/vpn.py root@mcs-liam:/opt/grok-farm/vpnx-build/app/vpn.py
# rebuild image only if entrypoint/vpn.py runtime changed
cd /opt/grok-farm/vpnx-build && docker build -t vpnx:latest .

# 3) verify
VPNX_UPDATE_ENV=/opt/grok-farm/.env VPNX_KEEP_WARP=1 \
  bash /opt/grok-farm/scripts/vpnx_watchdog.sh --require 1
systemctl restart farm-loop
journalctl -u farm-loop -n 50 --no-pager
```
