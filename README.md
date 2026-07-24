# Grok Farm

CLI + small web stack for creating **xAI / Grok free CLI** accounts, exporting OIDC tokens, and serving them through **[grok2api](https://github.com/chenyme/grok2api)** (OpenAI/Anthropic-compatible API).

> This repository is a **self-contained shareable package**. Copy secrets into `.env` and `docker/grok2api/config.yaml` from the examples before running.

## Features

- **Browser farm** (`farm.py`) — Camoufox/Playwright registration
- **HTTP farm** (`farm_http.py`) — no browser; curl_cffi + Turnstile solver
- **Web control panel** (`farm_panel.py`) — start/stop jobs, live log, batch list
- **grok2api stack** — Docker Compose API + public nginx gate
- **Public free-token page** (`public/`) — synthlabs-style landing UI
- **Traffic intel** (`traffic_intel.py`) — request capture + dashboard
- **Export / pool import** — `g2a_export.py`, `g2a_pool.py`

## Quick start (farmer)

```bash
# 1) configure
cp .env.example .env
# edit IMAP, email domain, account password, optional proxies

# 2) install
chmod +x install.sh run.sh run_http.sh
./install.sh          # Linux/macOS style; on Windows use a venv + pip

# Windows (PowerShell example)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3) farm
# Browser mode
./run.sh
# or non-interactive
./run.sh -- -n 5 -c 1 -y

# HTTP mode (needs SOLVER_URL or CAPSOLVER_API_KEY)
./run_http.sh -n 5 -c 2 -y
```

Each run writes a new batch folder:

```text
results/
  used_emails.txt
  batch_<timestamp>_<id>/
    accounts.json
    accounts.txt
    failed.json
    farm.log
    batch_meta.json
```

## HTTP farm notes

`farm_http.py` reconstructs the accounts.x.ai signup flow without a browser:

1. Create / verify email OTP (gRPC-web)
2. Solve Turnstile via external solver
3. Create account (Next.js server action)
4. CreateSession → OAuth PKCE → token exchange

Important env:

```env
SOLVER_URL=http://127.0.0.1:8888
# optional fallback
# CAPSOLVER_API_KEY=
GROK_IMPERSONATE=chrome131
```

If your account password starts with `$`, the HTTP client automatically Flight-escapes it for Next.js server actions.

## Control panel

```bash
export FARM_PANEL_PASSWORD='change-me'
python farm_panel.py
# http://127.0.0.1:9000
```

Panel can start **HTTP** or **Browser** jobs (up to 1000 accounts / 20 concurrent for HTTP).

Systemd example: `deploy/farm-panel.service`.

## grok2api (API gateway)

```bash
cp docker/grok2api/config.example.yaml docker/grok2api/config.yaml
# set jwtSecret, credentialEncryptionKey, bootstrapAdmin password

docker compose up -d
# grok2api: 127.0.0.1:8000
# public gate: 127.0.0.1:8080  (landing + /v1/*)
```

Import farmed tokens:

```bash
python g2a_export.py results/batch_xxx/ --import
# or continuous pooler
python g2a_pool.py
```

Community API guide: [`API_GUIDE.md`](./API_GUIDE.md)  
Token lifecycle notes: [`TOKEN_LIFECYCLE.md`](./TOKEN_LIFECYCLE.md)

## Public free-token page

- Static UI: `public/index.html`
- Status API: `public_status.py` (default port `8002`)
- Nginx gate proxies `/` and `/api/public/status`
- Unit: `deploy/public-status.service`

Set the shared key via `results/g2a_client_key.txt` or `PUBLIC_TOKEN_KEY`.

## Layout

```text
grok-farm/
  farm.py / farm_http.py / farm_panel.py
  xconsole_client/          # HTTP signup protocol
  public/ + public_status.py
  traffic_intel.py + static_intel_ui.html
  g2a_export.py / g2a_pool.py
  docker/ + docker-compose.yml
  deploy/                   # systemd units
  scripts/                  # setup + tests + tunnel helpers
```

## Requirements

See `requirements.txt`:

- `camoufox[geoip]` + `playwright` (browser farm)
- `curl_cffi` + `requests` (HTTP farm / solvers)
- `python-dotenv`

## Safety / sharing

Before publishing:

1. Never commit `.env`, `results/`, `proxies.txt`, or live `config.yaml`
2. Rotate any keys that ever touched a private machine
3. Read [`SECURITY.md`](./SECURITY.md)

## License

MIT — see [`LICENSE`](./LICENSE).
