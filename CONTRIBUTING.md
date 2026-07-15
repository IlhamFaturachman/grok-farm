# Contributing

Thanks for helping improve this project.

## Local setup

```bash
cp .env.example .env
# edit .env with your IMAP / domain / passwords

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

Browser farm (Camoufox) needs the extra browser deps from `install.sh` / Camoufox docs.  
HTTP farm needs `curl_cffi` and a Turnstile solver (`SOLVER_URL` or CapSolver).

## What to keep out of git

- `.env`
- `results/`, `screenshots/`
- `proxies.txt` (credentials)
- `docker/grok2api/config.yaml` (real secrets)
- local probe/diag scripts

Use the `*.example` files as templates.

## Suggested workflow

1. Open an issue or describe the change
2. Branch from `main`
3. Keep commits focused (one concern per PR when possible)
4. Do not commit tokens, account dumps, or production keys

## Project areas

| Path | Purpose |
|------|---------|
| `farm.py` | Browser-based registration (Camoufox) |
| `farm_http.py` + `xconsole_client/` | HTTP registration flow |
| `farm_panel.py` | Web control panel |
| `public/` + `public_status.py` | Public free-token landing page |
| `traffic_intel.py` | Request capture / intel UI |
| `docker/` + `docker-compose.yml` | grok2api + nginx gate |
| `deploy/` | systemd unit examples |

## Code style

- Match surrounding code: naming, comment density, structure
- Prefer small, reviewable diffs
- Report test failures honestly when you run checks
