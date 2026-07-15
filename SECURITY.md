# Security policy

## Do not publish secrets

Never commit:

- `.env` files with real IMAP / admin / API passwords
- Grok account dumps (`results/**`)
- client keys (`g2a_…`)
- residential proxy credentials (`proxies.txt`)
- `docker/grok2api/config.yaml` with live `jwtSecret` / encryption keys

If a secret was committed by mistake, **rotate it immediately** (treat it as compromised).

## Reporting issues

If you find a vulnerability in this repository (for example accidental secret leakage, unsafe defaults, or auth bypass in a panel), please report it privately to the maintainer instead of opening a public issue with exploit details.

## Operational notes

- Bind admin panels (`farm_panel`, `traffic_intel`, grok2api admin) to trusted networks or protect them with strong passwords + reverse-proxy auth.
- Public landing pages should only expose intentionally shared client keys.
- Prefer short-lived shared keys with RPM / concurrency limits.
