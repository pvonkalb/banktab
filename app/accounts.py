"""Accounts: households, logins, one-time links (verify / reset / invite) and email.

Layout on disk (BUDGET_DATA_DIR, default ./data):
    accounts.db              users, households, sessions, tokens
    households/<id>.db       one household's budget (app/schema.sql)

Keeping each household in its own file means a query can never return another
household's data, and backing up / exporting / deleting a household is one file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
import smtplib
import sqlite3
import threading
from email.message import EmailMessage
from pathlib import Path

from . import core

SCHEMA = Path(__file__).with_name("accounts_schema.sql")
log = logging.getLogger("budget")


class AccountError(ValueError):
    """Message is safe to show the user."""


# ---------------------------------------------------------------- storage

def accounts_path(data_dir: Path) -> Path:
    return data_dir / "accounts.db"


# New accounts start on the Bank theme with the Gold accent (people can change it in Settings).
# Set explicitly on every new user, because older accounts.db files keep their original column default.
DEFAULT_THEME = "bank"
DEFAULT_ACCENT = "gold"


def household_path(data_dir: Path, household_id: int) -> Path:
    return data_dir / "households" / f"{int(household_id)}.db"


def open_accounts(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = core.connect(accounts_path(data_dir))
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    _ensure_autoincrement(conn)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn) -> None:
    """Columns added after the first release."""
    add = {"households": [("currency", "TEXT NOT NULL DEFAULT 'USD'")],
           "users": [("theme", f"TEXT NOT NULL DEFAULT '{DEFAULT_THEME}'"), ("accent", f"TEXT NOT NULL DEFAULT '{DEFAULT_ACCENT}'")]}
    for table, cols in add.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def backup_all(data_dir: Path, dest: Path, keep: int) -> tuple[Path, int, int]:
    """Consistent copies of accounts.db and every household into dest/backup-<time>/.
    Safe while the app runs. Keeps the newest `keep` backups. Returns (folder, files, removed)."""
    import datetime as _dt
    import shutil
    out = dest / f"backup-{_dt.datetime.now():%Y%m%d-%H%M%S}"
    files = [p for p in [accounts_path(data_dir), *sorted((data_dir / "households").glob("*.db"))] if p.exists()]
    for f in files:
        target = out / f.relative_to(data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        src, dst = sqlite3.connect(f), sqlite3.connect(target)
        with dst:
            src.backup(dst)
        dst.close(); src.close()
    runs = sorted(p for p in dest.glob("backup-*") if p.is_dir())
    old = runs[:-keep] if keep > 0 else []
    for d in old:
        shutil.rmtree(d, ignore_errors=True)
    return out, len(files), len(old)


def _ensure_autoincrement(conn) -> None:
    """Older accounts.db files created households/users without AUTOINCREMENT; rebuild them
    (same rows, same ids) so ids are never reused."""
    schema = SCHEMA.read_text(encoding="utf-8")
    for table in ("households", "users"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        if "AUTOINCREMENT" in sql.upper():
            continue
        create = schema[schema.index(f"CREATE TABLE IF NOT EXISTS {table} ("):]
        create = create[:create.index(");") + 2].replace(f"IF NOT EXISTS {table} (", f"{table}_new (")
        cols = ", ".join(r["name"] for r in conn.execute(f"PRAGMA table_info({table})"))
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with core.tx(conn):
                conn.execute(create)
                conn.execute(f"INSERT INTO {table}_new({cols}) SELECT {cols} FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_users_household ON users(household_id)")


def open_household(data_dir: Path, household_id: int) -> sqlite3.Connection:
    path = household_path(data_dir, household_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = core.connect(path)
    core.init_db(conn)
    return conn


def create_household(acct: sqlite3.Connection, data_dir: Path, name: str, setup_done: bool = False) -> int:
    hid = acct.execute("INSERT INTO households(name, setup_done) VALUES (?,?)",
                       (name.strip() or "My budget", int(setup_done))).lastrowid
    path = household_path(data_dir, hid)
    if path.exists():  # leftover from a deleted household with a reused id
        path.unlink()
    open_household(data_dir, hid).close()
    return hid


def sync_member(hconn: sqlite3.Connection, user: dict) -> None:
    """Each household file keeps a small copy of its members (id + name) so entries can say
    who added them. Passwords live only in accounts.db."""
    hconn.execute(
        """INSERT INTO users(id, username, display_name, password_hash) VALUES (?,?,?,'')
           ON CONFLICT(id) DO UPDATE SET username=excluded.username, display_name=excluded.display_name""",
        (user["id"], user["username"], user["display_name"]))


# ---------------------------------------------------------------- passwords

COMMON = {"password", "password1", "password123", "123456789", "1234567890", "qwertyuiop", "iloveyou12",
          "letmein123", "welcome123", "budget1234", "abc1234567", "passw0rd12", "1q2w3e4r5t"}


def check_new_password(pw: str) -> None:
    if len(pw) < 10:
        raise AccountError("Use at least 10 characters for your password.")
    if len(pw) > 200:
        raise AccountError("That password is too long.")
    if pw.lower() in COMMON or len(set(pw)) < 4:
        raise AccountError("That password is too easy to guess. Try a short phrase.")


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=2**26)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**15, r=8, p=1, maxmem=2**26)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_email(email: str) -> str:
    email = (email or "").strip()
    if len(email) > 254 or email.count("@") != 1:
        raise AccountError("Enter a valid email address.")
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith(".") or any(c.isspace() for c in email):
        raise AccountError("Enter a valid email address.")
    return email


# ---------------------------------------------------------------- one-time links

TOKEN_HOURS = {"verify": 48, "reset": 1, "invite": 24 * 7}


def make_token(conn, kind: str, *, user_id=None, household_id=None, email=None, created_by=None) -> str:
    raw = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TOKEN_HOURS[kind])
    if kind in ("verify", "reset") and user_id:
        # Only the newest link of each kind works.
        conn.execute("DELETE FROM tokens WHERE kind=? AND user_id=? AND used_at IS NULL", (kind, user_id))
    conn.execute(
        "INSERT INTO tokens(token_hash, kind, user_id, household_id, email, created_by, expires_at) VALUES (?,?,?,?,?,?,?)",
        (sha(raw), kind, user_id, household_id, email, created_by, expires.strftime("%Y-%m-%d %H:%M:%S")))
    return raw


def peek_token(conn, raw: str, kind: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM tokens WHERE token_hash=? AND kind=? AND used_at IS NULL AND expires_at > datetime('now')",
        (sha(raw or ""), kind)).fetchone()
    if row is None:
        raise AccountError("That link has expired or was already used.")
    return row


def use_token(conn, raw: str, kind: str) -> sqlite3.Row:
    row = peek_token(conn, raw, kind)
    conn.execute("UPDATE tokens SET used_at=datetime('now') WHERE token_hash=?", (row["token_hash"],))
    return row


# ---------------------------------------------------------------- email

def email_enabled() -> bool:
    return bool(os.environ.get("BUDGET_SMTP_HOST"))


def send_email(to: str, subject: str, body: str) -> None:
    """Send in the background. Without SMTP settings the message goes to the server log,
    which is how links work during local testing."""
    if not email_enabled():
        log.warning("Email (SMTP not configured) to %s: %s\n%s", to, subject, body)
        print(f"\n--- email to {to}: {subject}\n{body}\n---", flush=True)
        return

    def _send():
        try:
            msg = EmailMessage()
            msg["From"] = os.environ.get("BUDGET_SMTP_FROM") or os.environ.get("BUDGET_SMTP_USER", "")
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            port = int(os.environ.get("BUDGET_SMTP_PORT", "587"))
            host = os.environ["BUDGET_SMTP_HOST"]
            with (smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)) as s:
                if port != 465:
                    s.starttls()
                if os.environ.get("BUDGET_SMTP_USER"):
                    s.login(os.environ["BUDGET_SMTP_USER"], os.environ.get("BUDGET_SMTP_PASSWORD", ""))
                s.send_message(msg)
        except Exception:
            log.exception("Sending email to %s failed", to)

    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------- moving the old single-household database

def migrate_legacy(legacy_db: Path, data_dir: Path, household_name: str) -> int:
    """Turn a pre-households database (users + budget in one file) into accounts.db plus
    households/<id>.db. Existing logins keep working and become owners of the household."""
    if accounts_path(data_dir).exists():
        existing = sqlite3.connect(accounts_path(data_dir)).execute("SELECT count(*) FROM households").fetchone()[0]
        if existing:
            raise AccountError(f"{accounts_path(data_dir)} already has households; not migrating again.")
    acct = open_accounts(data_dir)
    src = core.connect(legacy_db)
    core.init_db(src)  # bring the old file up to date first
    users = [dict(r) for r in src.execute("SELECT id, username, display_name, password_hash FROM users ORDER BY id")]
    hid = acct.execute("INSERT INTO households(name, setup_done) VALUES (?, 1)", (household_name,)).lastrowid
    dest_path = household_path(data_dir, hid)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(dest_path)
    src.backup(dest)
    dest.close()
    src.close()
    with core.tx(acct):
        for u in users:  # same ids, so entries keep pointing at the right person
            acct.execute(
                "INSERT INTO users(id, username, display_name, password_hash, household_id, role, theme, accent) "
                "VALUES (?,?,?,?,?,'owner',?,?)",
                (u["id"], u["username"], u["display_name"], u["password_hash"], hid, DEFAULT_THEME, DEFAULT_ACCENT))
    h = core.connect(dest_path)
    h.execute("UPDATE users SET password_hash=''")  # passwords now live only in accounts.db
    h.execute("DELETE FROM sessions")
    h.close()
    acct.close()
    return hid
