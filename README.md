# Budget

A self-hosted envelope budget for households. Each category gets money every month, whatever
you don't spend rolls over ("the bank") so you can save up, months are closed (locked) once
everything has posted, and trackers total up tagged spending for taxes or projects.

One server can host several households (friends and family), each completely separate.
FastAPI + SQLite backend, plain-JS front end with no build step, installable on a phone.

## Self-host it (Docker Compose)

```sh
cp .env.example .env              # optional settings: sign-ups, email, backups
docker compose up -d
docker compose logs budget        # look for "setup code: XXXX-XXXX"
```

Open the app, enter the setup code, and create the first account. It becomes the owner of the
first household; invite others from **Settings → Household**.

Put it behind something that adds HTTPS: Caddy, a Cloudflare Tunnel, or `tailscale serve`.
The container only listens on 127.0.0.1:8000. Never publish port 8000 to the internet directly.

* **Private use** (just your household): `tailscale serve` in front of it.
* **Open to friends**: a Cloudflare Tunnel (no open ports at home), `BUDGET_SIGNUP=open` or
  invite-only, email configured, `BUDGET_BASE_URL` set, and backups copied off the server.

Backups run daily into the data volume (`BUDGET_BACKUP_KEEP`). Household owners can also
download a full copy of their budget from **Settings → Household**. `GET /api/health` is
there for uptime checks.

## Features

* Envelope budgeting with rollover, month close/reopen (including closing early), per-month budgets
* Dashboard table, month view, category history, reports by month / quarter / year
* Fast entry: single entries with category auto-fill, or "Add several" in a spreadsheet-style grid
* Bank CSV import (US Bank, Capital One and most banks): review every row, duplicates and card payments flagged
* Bills that drive a category's budget (monthly / quarterly / yearly)
* Trackers for taxes, HSA, or projects, with yearly totals and CSV export
* Households with invites, owners and members; sign-up with email confirmation; password reset
* Themes (Match device, Light, Dark, Noir, Paper) and accent colors, saved per person
* Currency per household

## Run locally (development)

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Windows; use .venv/bin on Linux
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000 (the setup code is printed in the terminal).
Tests: `.venv/Scripts/python -m pytest -q tests`

## How data is stored

```
data/
  accounts.db            logins, households, sessions, one-time links (verify / reset / invite)
  households/<id>.db     one household's budget: categories, months, entries, bills, trackers
  backups/               daily automatic backups
```

Each household lives in its own SQLite file, so one household's data can never appear in
another's queries, and exporting or deleting a household is a single file.

## Command-line admin

```sh
python tools/manage.py households                       # list
python tools/manage.py create-household "The Smiths"
python tools/manage.py adduser sam "Sam" 1              # username, display name, household id
python tools/manage.py adduser alex "Alex" 1 --member --email alex@example.com
python tools/manage.py passwd sam
python tools/manage.py users
python tools/manage.py delete-household 2
python tools/backup.py                                  # one-off backup
```

With Docker, prefix these with `docker compose exec budget`.

Coming from a spreadsheet or another app? Export your transactions as CSV and bring them in
with **Settings → Import**. A budget database made elsewhere (same schema as
`app/schema.sql`) can be added as a new household with
`python tools/manage.py import-household budget.db "Name"`.

Upgrading from the single-household version: start the app with the old `data/budget.db` in
the data folder and it moves itself into `households/1.db` automatically (or run
`manage.py migrate-legacy data/budget.db "Name"`). Existing logins keep working.

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `BUDGET_DATA_DIR` | `./data` (`/data` in Docker) | where `accounts.db` and `households/` live |
| `BUDGET_SIGNUP` | `closed` | `open` lets anyone create a household; invites work either way |
| `BUDGET_MAX_HOUSEHOLDS` | `100` | cap on sign-ups |
| `BUDGET_BASE_URL` | request URL | public address for emailed links; **required once email is on** |
| `BUDGET_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` | unset | outgoing email; port 465 = SSL, otherwise STARTTLS |
| `BUDGET_BACKUP_KEEP` | `14` | daily automatic backups kept in `<data>/backups`; `0` turns them off |
| `BUDGET_SECURE_COOKIE` | auto | cookies are HTTPS-only whenever the app sees HTTPS (directly or via `X-Forwarded-Proto`); `1` forces it |
| `BUDGET_SOURCE_URL` | unset | link to this copy's source code, shown in Settings → Account |
| `FORWARDED_ALLOW_IPS` | `*` in Docker | proxies allowed to set `X-Forwarded-*`; narrow it if port 8000 is reachable directly |

Without SMTP settings, emails (confirm address, reset password, invites) are printed to the
server log instead of sent. That's fine for a single household, but sign-ups can't confirm
their email without it. Any transactional email service works (Postmark, Resend, Brevo,
Amazon SES, or a Gmail app password for low volume).

## How the money works

* Every amount is stored in cents. Positive = money out of an envelope, negative = money into
  it (gifts, refunds, the receiving side of a move).
* Each month keeps its own copy of every category's budget. Changing a default (Settings →
  Categories, or a bill) updates open months and future months only.
* Envelope balance = sum of (budget − spent) over every month up to now.
* Closing a month locks its entries and budgets. Months close in order; only the latest closed
  month can be reopened. The current month can be closed early, which opens next month.
* Bill-driven categories get monthly + quarterly ÷ 3 + yearly ÷ 12 from their bills.
* "Tracking-only" records count toward trackers but not toward any envelope.

## Security

* A fresh install can only be claimed with a one-time setup code printed in the server log.
* Passwords hashed with scrypt (10+ characters, very common ones refused). Sessions are random
  tokens stored hashed in HttpOnly, SameSite=Strict cookies (HTTPS-only over HTTPS), 90 days.
  Changing or resetting a password signs out other devices; Settings → Account can too.
* Rate limits: 5 wrong passwords per username/IP per 15 minutes, 30 per IP, and 20 per
  account per hour from any number of IPs; 5 sign-ups and 5 reset emails per IP per hour;
  20 invites per owner per day. Sign-up has a honeypot field and a household cap, and
  unconfirmed sign-ups are removed after 7 days.
* Email links (verify 48 h, reset 1 h, invite 7 days) are one-use and stored hashed. "Forgot
  password" answers the same whether or not the email exists. Emailed links use
  `BUDGET_BASE_URL`, never the request's Host header. Changing your email notifies the old address.
* Every write, including login, requires an `X-Budget: 1` header (blocks cross-site posts).
* Strict Content-Security-Policy, HSTS over HTTPS, COOP/CORP and Permissions-Policy headers.
  No third-party scripts, fonts, analytics or trackers. Request bodies over 3 MB, or with no
  declared length, are refused.
* CSV exports neutralize cells that start with `= + - @` so they can't run as spreadsheet formulas.
* Bank CSVs are read in the browser; only the rows you tick are sent to the server.
* The Docker image runs as a non-root user; the Compose file adds a read-only filesystem,
  no Linux capabilities and no-new-privileges.
* Dependencies are pinned; check them with `pip-audit -r requirements.txt`.
* The database, sheet exports, CSVs and `.env` are git-ignored.

## License

Budget is free software: you can redistribute it and/or modify it under the terms of the
[GNU Affero General Public License v3.0](LICENSE).

In short: you can use it, host it for others, and change it. If you run a **modified**
version that other people use over a network, you must offer them its source code (set
`BUDGET_SOURCE_URL` and a link appears in Settings → Account). Running it unchanged for your
own household needs nothing extra.
