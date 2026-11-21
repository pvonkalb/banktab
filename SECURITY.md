# Security policy

Budget holds people's financial records, so security reports are very welcome.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Include what you found, how to
reproduce it, and what an attacker could do with it.

You'll get a reply within a week. Once a fix is released you're welcome to be credited in the
release notes.

## Supported versions

Only the latest release gets security fixes. Self-hosters should update regularly:

```sh
git pull && docker compose up -d --build
```

## Scope

In scope: the app in this repository (`app/`, `tools/`, the Dockerfile and Compose file).

Out of scope: how a particular server is deployed (reverse proxy, TLS, host OS), denial of
service by flooding, and anything needing access to the server's data folder, which by
design holds every household's data.

## Hardening checklist for self-hosters

- Serve it over HTTPS only (Cloudflare Tunnel, Caddy or `tailscale serve`); never expose port 8000.
- Set `BUDGET_BASE_URL` before turning on email.
- Leave `BUDGET_SIGNUP=closed` unless you mean to host strangers; invites work either way.
- Copy `<data>/backups` somewhere off the server.
- Run `pip-audit -r requirements.txt` (CI does this on every push) and rebuild when it reports anything.
