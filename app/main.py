"""Banktab: HTTP API + static front end.

Run locally:  .venv/Scripts/python -m uvicorn app.main:app --reload

Settings (environment variables):
  BUDGET_DATA_DIR       folder for accounts.db + households/ (default ./data)
  BUDGET_SECURE_COOKIE  1 when served over HTTPS (required in production)
  BUDGET_SIGNUP         "open" lets anyone create a household; default "closed" (invites still work)
  BUDGET_BASE_URL       public address used in emailed links, e.g. https://budget.example.com
  BUDGET_MAX_HOUSEHOLDS cap on households created by sign-up (default 100)
  BUDGET_SMTP_HOST / _PORT / _USER / _PASSWORD / _FROM   outgoing email; without it links go to the log
  BUDGET_BACKUP_KEEP    daily automatic backups to keep in <data>/backups (default 14, 0 = off)
  BUDGET_SOURCE_URL     where this copy's source code lives (AGPL-3.0: set it if you changed the code)

Banktab is free software under the GNU Affero General Public License v3.0 (see LICENSE).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import secrets
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import accounts, core, totp
from .accounts import check_password, hash_password, sha

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("BUDGET_DATA_DIR", ROOT / "data"))
SECURE_COOKIE = os.environ.get("BUDGET_SECURE_COOKIE", "0") == "1"
SIGNUP_OPEN = os.environ.get("BUDGET_SIGNUP", "closed").lower() == "open"
BASE_URL = os.environ.get("BUDGET_BASE_URL", "").rstrip("/")
MAX_HOUSEHOLDS = int(os.environ.get("BUDGET_MAX_HOUSEHOLDS", "100"))
BACKUP_KEEP = int(os.environ.get("BUDGET_BACKUP_KEEP", "14"))
VERSION = "1.0.0"
SOURCE_URL = os.environ.get("BUDGET_SOURCE_URL", "")
THEMES = ("system", "light", "dark", "noir", "paper", "bank")
ACCENTS = ("teal", "blue", "violet", "rose", "amber", "green", "graphite", "gold")
MAX_BODY = 3 * 1024 * 1024  # bytes; CSV imports are the biggest requests
COOKIE = "budget_session"
SESSION_DAYS = 90
STATIC = Path(__file__).with_name("static")

@__import__("contextlib").asynccontextmanager
async def _lifespan(_app):
    # Open the accounts database at startup (not on the first visit), so a new install prints
    # its setup code right away and backups start on schedule.
    with _lock:
        acct()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
_acct = None
_households: dict[int, object] = {}
_synced: set[tuple[int, str]] = set()
_lock = threading.Lock()


def _locked():
    """One request at a time. Plenty for a handful of households on SQLite."""
    with _lock:
        yield


def acct(_=Depends(_locked)):
    global _acct
    if _acct is None:
        first_run = not accounts.accounts_path(DATA_DIR).exists()
        legacy = DATA_DIR / "budget.db"
        if first_run and legacy.exists():
            # Upgrading from the single-household version: move it over automatically.
            accounts.migrate_legacy(legacy, DATA_DIR, os.environ.get("BUDGET_LEGACY_NAME", "Our budget"))
        _acct = accounts.open_accounts(DATA_DIR)
        _first_run_check(_acct)
        _start_backups()
    return _acct


_setup_code: str | None = None


def _first_run_check(conn) -> None:
    """No logins yet: print a one-time code to the server log. Whoever creates the first
    account must enter it, so a stranger who finds a fresh install first can't claim it."""
    global _setup_code
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        _setup_code = None
        return
    raw = secrets.token_hex(4).upper()
    _setup_code = f"{raw[:4]}-{raw[4:]}"
    msg = f"No accounts yet. Open the app and create the first one with this setup code: {_setup_code}"
    # print, not log: one clear banner even before logging is configured
    print(f"\n{'=' * 72}\n  {msg}\n{'=' * 72}\n", flush=True)


_backup_thread = None


def _start_backups() -> None:
    """Daily copies of every database into <data>/backups, kept for BACKUP_KEEP days."""
    global _backup_thread
    if BACKUP_KEEP <= 0 or _backup_thread is not None:
        return
    dest = DATA_DIR / "backups"

    def loop():
        time.sleep(60)  # let the app finish starting
        while True:
            try:
                runs = sorted(p for p in dest.glob("backup-*") if p.is_dir())
                if not runs or time.time() - runs[-1].stat().st_mtime > 20 * 3600:
                    with _lock:
                        accounts.backup_all(DATA_DIR, dest, BACKUP_KEEP)
            except Exception:
                accounts.log.exception("Automatic backup failed")
            time.sleep(3600)

    _backup_thread = threading.Thread(target=loop, name="backups", daemon=True)
    _backup_thread.start()


def household_conn(hid: int):
    conn = _households.get(hid)
    if conn is None:
        conn = _households[hid] = accounts.open_household(DATA_DIR, hid)
    return conn


def close_household(hid: int) -> None:
    conn = _households.pop(hid, None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------- rate limits

_hits: dict[str, list[float]] = defaultdict(list)
_failures = _hits  # name kept for tests


def _limited(key: str, limit: int, window: float) -> bool:
    now = time.time()
    if len(_hits) > 20000:  # a flood of junk keys can't grow memory forever
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
            del _hits[k]
    _hits[key] = [t for t in _hits[key] if now - t < window]
    return len(_hits[key]) >= limit


def _hit(key: str) -> None:
    _hits[key].append(time.time())


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


_DUMMY_HASH = hash_password(secrets.token_hex(8))


# ---------------------------------------------------------------- sessions

USER_SQL = """SELECT u.id, u.username, u.email, u.email_verified_at, u.display_name, u.household_id, u.role,
                     u.theme, u.accent, u.totp_on_at, h.name AS household_name, h.setup_done, h.currency
              FROM users u JOIN households h ON h.id = u.household_id"""


def current_user(request: Request, conn=Depends(acct)):
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Please log in")
    row = conn.execute(USER_SQL + " JOIN sessions s ON s.user_id = u.id WHERE s.token_hash=? AND s.expires_at > datetime('now')",
                       (sha(token),)).fetchone()
    if row is None:
        raise HTTPException(401, "Please log in")
    # Cross-site requests can't set custom headers, so this blocks CSRF on writes.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-budget") != "1":
        raise HTTPException(403, "Missing request header")
    return dict(row)


def owner(user=Depends(current_user)):
    if user["role"] != "owner":
        raise HTTPException(403, "Only the household owner can do that.")
    return user


def db(user=Depends(current_user)):
    """The logged-in user's household budget. Nothing else is reachable from here."""
    conn = household_conn(user["household_id"])
    key = (user["id"], user["display_name"])
    if key not in _synced:
        accounts.sync_member(conn, user)
        _synced.add(key)
    return conn


def _https(request: Request) -> bool:
    return SECURE_COOKIE or request.url.scheme == "https"


def _start_session(conn, user_id: int, response: Response, request: Request) -> None:
    token = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=SESSION_DAYS)
    conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
    conn.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?,?,?)",
                 (sha(token), user_id, expires.strftime("%Y-%m-%d %H:%M:%S")))
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=_https(request), samesite="strict", path="/")


def _require_header(request: Request) -> None:
    if request.headers.get("x-budget") != "1":
        raise HTTPException(403, "Missing request header")


def _link(request: Request, route: str, token: str) -> str:
    if not BASE_URL and accounts.email_enabled():
        # The Host header is attacker-controlled; never put it in an emailed link.
        raise HTTPException(500, "The server needs BUDGET_BASE_URL set before it can send email links.")
    base = BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/#/{route}?token={token}"


def _purge_unconfirmed(conn) -> None:
    """Sign-ups that never confirmed their email within 7 days: remove the login, and the
    household too if nobody else is in it (keeps spam from filling the sign-up cap)."""
    stale = conn.execute(
        """SELECT u.id, u.household_id FROM users u
           WHERE u.verify_required=1 AND u.email_verified_at IS NULL AND u.created_at < datetime('now', '-7 days')""").fetchall()
    for u in stale:
        conn.execute("DELETE FROM users WHERE id=?", (u["id"],))
        if not conn.execute("SELECT 1 FROM users WHERE household_id=?", (u["household_id"],)).fetchone():
            conn.execute("DELETE FROM tokens WHERE household_id=?", (u["household_id"],))
            conn.execute("DELETE FROM households WHERE id=?", (u["household_id"],))
            close_household(u["household_id"])
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(accounts.household_path(DATA_DIR, u["household_id"])) + suffix)
                if p.exists():
                    p.unlink()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        length = request.headers.get("content-length")
        if length is None and request.headers.get("transfer-encoding"):
            # Streamed bodies could skip the size check below.
            return JSONResponse({"detail": "Content-Length required"}, status_code=411)
        if int(length or 0) > MAX_BODY:
            return JSONResponse({"detail": "That's too big to upload."}, status_code=413)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if _https(request):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        # Always revalidate (cheap 304 via ETag) so a normal refresh picks up new versions of the app.
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(core.BudgetError)
async def budget_error(_req, exc: core.BudgetError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(accounts.AccountError)
async def account_error(_req, exc: accounts.AccountError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------- log in / out

class Login(BaseModel):
    username: str = Field(max_length=254)   # username or email
    password: str = Field(max_length=200)


@app.post("/api/login")
def login(body: Login, request: Request, response: Response, conn=Depends(acct)):
    _require_header(request)  # stops other sites logging you into their account
    who = body.username.strip()
    key = f"login|{_ip(request)}|{who.lower()}"
    acct_key = f"login-acct|{who.lower()}"  # across all IPs, against spread-out guessing
    if _limited(key, 5, 900) or _limited(f"login-ip|{_ip(request)}", 30, 900) or _limited(acct_key, 20, 3600):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    row = conn.execute("SELECT id, password_hash, email, email_verified_at, verify_required FROM users "
                       "WHERE username=? OR email=?", (who, who)).fetchone()
    ok = check_password(body.password, row["password_hash"] if row else _DUMMY_HASH) and row is not None
    if not ok:
        _hit(key); _hit(f"login-ip|{_ip(request)}"); _hit(acct_key)
        raise HTTPException(401, "Wrong username or password")
    _hits.pop(key, None)
    if row["verify_required"] and not row["email_verified_at"]:
        if not _limited(f"resend|{row['id']}", 3, 3600):
            _hit(f"resend|{row['id']}")
            _send_verify(conn, request, row["id"], row["email"])
        raise HTTPException(403, "Please confirm your email first. We just sent you a new link.")
    if _twofa_on(conn, row["id"]):
        # Right password; the session only starts after the code (POST /api/login/2fa).
        return {"twofa": True, "ticket": _new_challenge(conn, row["id"])}
    _start_session(conn, row["id"], response, request)
    return {"ok": True}


# ---------------------------------------------------------------- two-step login

CHALLENGE_MINUTES = 5
CHALLENGE_TRIES = 5


def _twofa_on(conn, user_id: int) -> bool:
    row = conn.execute("SELECT totp_on_at FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["totp_on_at"])


def _new_challenge(conn, user_id: int) -> str:
    raw = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=CHALLENGE_MINUTES)
    conn.execute("DELETE FROM login_challenges WHERE expires_at <= datetime('now') OR user_id=?", (user_id,))
    conn.execute("INSERT INTO login_challenges(token_hash, user_id, expires_at) VALUES (?,?,?)",
                 (sha(raw), user_id, expires.strftime("%Y-%m-%d %H:%M:%S")))
    return raw


def _check_second_factor(conn, user_id: int, code: str) -> str | None:
    """'totp' or 'recovery' if the code is good (and uses it up), else None."""
    row = conn.execute("SELECT totp_secret, totp_last_step FROM users WHERE id=? AND totp_on_at IS NOT NULL",
                       (user_id,)).fetchone()
    if row is None:
        return None
    step = totp.verify(row["totp_secret"], code, last_step=row["totp_last_step"])
    if step is not None:
        conn.execute("UPDATE users SET totp_last_step=? WHERE id=?", (step, user_id))
        return "totp"
    norm = totp.normalize_recovery(code)
    if len(norm) == 12:
        used = conn.execute("UPDATE recovery_codes SET used_at=datetime('now') WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                            (user_id, sha(norm))).rowcount
        if used:
            return "recovery"
    return None


def _recovery_left(conn, user_id: int) -> int:
    return conn.execute("SELECT count(*) FROM recovery_codes WHERE user_id=? AND used_at IS NULL", (user_id,)).fetchone()[0]


def _new_recovery_codes(conn, user_id: int) -> list[str]:
    codes = totp.new_recovery_codes()
    conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
    conn.executemany("INSERT INTO recovery_codes(code_hash, user_id) VALUES (?,?)",
                     [(sha(totp.normalize_recovery(c)), user_id) for c in codes])
    return codes


class SecondStep(BaseModel):
    ticket: str = Field(max_length=200)
    code: str = Field(max_length=40)


@app.post("/api/login/2fa")
def login_second_step(body: SecondStep, request: Request, response: Response, conn=Depends(acct)):
    _require_header(request)
    # Errors are raised after the transaction commits, so a wrong guess is always counted.
    error = None
    with core.tx(conn):
        ch = conn.execute("SELECT * FROM login_challenges WHERE token_hash=? AND expires_at > datetime('now')",
                          (sha(body.ticket),)).fetchone()
        if ch is None:
            error = (401, "That took too long. Log in again.")
        elif _limited(f"2fa|{ch['user_id']}", 10, 900):
            error = (429, "Too many attempts. Try again in 15 minutes.")
        else:
            uid = ch["user_id"]
            kind = _check_second_factor(conn, uid, body.code)
            if kind is None:
                _hit(f"2fa|{uid}")
                if ch["tries"] + 1 >= CHALLENGE_TRIES:
                    conn.execute("DELETE FROM login_challenges WHERE token_hash=?", (ch["token_hash"],))
                    error = (401, "Too many wrong codes. Log in again.")
                else:
                    conn.execute("UPDATE login_challenges SET tries=tries+1 WHERE token_hash=?", (ch["token_hash"],))
                    error = (401, "That code isn't right. Check the time on your phone and try the newest code.")
            else:
                conn.execute("DELETE FROM login_challenges WHERE token_hash=?", (ch["token_hash"],))
                _start_session(conn, uid, response, request)
                out = {"ok": True}
                if kind == "recovery":
                    out["recovery_left"] = _recovery_left(conn, uid)
    if error:
        raise HTTPException(*error)
    return out


@app.post("/api/logout")
def logout(request: Request, response: Response, user=Depends(current_user), conn=Depends(acct)):
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (sha(request.cookies.get(COOKIE, "")),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/config")
def config(conn=Depends(acct)):
    """Public: what the login screen should offer."""
    return {"signup": SIGNUP_OPEN, "email": accounts.email_enabled(), "first_run": _setup_code is not None,
            "version": VERSION, "source": SOURCE_URL}


@app.get("/api/health")
def health(conn=Depends(acct)):
    conn.execute("SELECT 1").fetchone()
    return {"ok": True, "version": VERSION}


class FirstRunIn(BaseModel):
    code: str = Field(max_length=20)
    name: str = Field(min_length=1, max_length=60)
    login: str = Field(min_length=2, max_length=254)   # an email address or a plain username
    password: str = Field(max_length=200)
    household: str = Field("", max_length=60)


@app.post("/api/first-run")
def first_run(body: FirstRunIn, request: Request, response: Response, conn=Depends(acct)):
    """Create the very first account on a new install (needs the code from the server log)."""
    global _setup_code
    _require_header(request)
    if _setup_code is None:
        raise HTTPException(403, "This server is already set up. Log in instead.")
    if _limited(f"first-run|{_ip(request)}", 10, 3600):
        raise HTTPException(429, "Too many tries. Try again later.")
    if not secrets.compare_digest(body.code.strip().upper().replace(" ", ""), _setup_code):
        _hit(f"first-run|{_ip(request)}")
        raise accounts.AccountError("That setup code isn't right. It's printed in the server log.")
    login = body.login.strip()
    email = accounts.clean_email(login) if "@" in login else None
    if not email and not all(c.isalnum() or c in "._-" for c in login):
        raise accounts.AccountError("Usernames can use letters, numbers, dots, dashes and underscores.")
    accounts.check_new_password(body.password)
    with core.tx(conn):
        # An existing household nobody can log in to (e.g. moved over from the old version) gets this owner.
        orphan = conn.execute("""SELECT id FROM households h WHERE NOT EXISTS
                                 (SELECT 1 FROM users u WHERE u.household_id = h.id) ORDER BY id LIMIT 1""").fetchone()
        hid = orphan["id"] if orphan else accounts.create_household(conn, DATA_DIR, body.household.strip() or "Our budget")
        uid = conn.execute(
            """INSERT INTO users(username, email, email_verified_at, display_name, password_hash, household_id, role, theme, accent)
               VALUES (?,?,?,?,?,?,'owner',?,?)""",
            (login, email, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if email else None,
             body.name.strip(), hash_password(body.password), hid, accounts.DEFAULT_THEME, accounts.DEFAULT_ACCENT)).lastrowid
        _start_session(conn, uid, response, request)
    _setup_code = None
    return {"ok": True}


# ---------------------------------------------------------------- sign up, verify, reset

def _send_verify(conn, request: Request, user_id: int, email: str) -> None:
    token = accounts.make_token(conn, "verify", user_id=user_id, email=email)
    accounts.send_email(email, "Confirm your email for Banktab",
                        f"Confirm your email address to finish setting up your budget:\n\n{_link(request, 'verify', token)}\n\n"
                        "The link works for 48 hours. If you didn't sign up, you can ignore this email.")


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    household: str = Field("", max_length=60)
    website: str = ""  # honeypot: people leave it empty, form-filling bots don't


def _new_user(conn, *, name, email, password, household_id, role, verified: bool) -> int:
    accounts.check_new_password(password)
    if conn.execute("SELECT 1 FROM users WHERE username=? OR email=?", (email, email)).fetchone():
        raise accounts.AccountError("There's already an account with that email. Try logging in or resetting your password.")
    return conn.execute(
        """INSERT INTO users(username, email, email_verified_at, verify_required, display_name, password_hash, household_id, role,
                            theme, accent)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (email, email, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if verified else None,
         int(not verified), name.strip(), hash_password(password), household_id, role,
         accounts.DEFAULT_THEME, accounts.DEFAULT_ACCENT)).lastrowid


@app.post("/api/signup")
def signup(body: SignupIn, request: Request, conn=Depends(acct)):
    _require_header(request)
    if not SIGNUP_OPEN:
        raise HTTPException(403, "Sign-ups are closed. Ask someone for an invite.")
    if body.website:
        return {"ok": True}  # quietly ignore bots
    if _limited(f"signup|{_ip(request)}", 5, 3600):
        raise HTTPException(429, "Too many sign-ups from here. Try again later.")
    email = accounts.clean_email(body.email)
    with core.tx(conn):
        _purge_unconfirmed(conn)
    if conn.execute("SELECT count(*) FROM households").fetchone()[0] >= MAX_HOUSEHOLDS:
        raise HTTPException(403, "Sign-ups are full right now. Ask for an invite.")
    _hit(f"signup|{_ip(request)}")
    with core.tx(conn):
        accounts.check_new_password(body.password)
        if conn.execute("SELECT 1 FROM users WHERE username=? OR email=?", (email, email)).fetchone():
            raise accounts.AccountError("There's already an account with that email. Try logging in or resetting your password.")
        hid = accounts.create_household(conn, DATA_DIR, body.household.strip() or f"{body.name.strip()}'s budget")
        uid = _new_user(conn, name=body.name, email=email, password=body.password, household_id=hid, role="owner", verified=False)
        _send_verify(conn, request, uid, email)
    return {"ok": True, "verify": True}


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


@app.post("/api/verify")
def verify(body: TokenIn, request: Request, response: Response, conn=Depends(acct)):
    _require_header(request)
    with core.tx(conn):
        t = accounts.use_token(conn, body.token, "verify")
        if t["email"]:
            taken = conn.execute("SELECT 1 FROM users WHERE (email=? OR username=?) AND id != ?",
                                 (t["email"], t["email"], t["user_id"])).fetchone()
            if taken:
                raise accounts.AccountError("Another account already uses that email.")
            old = conn.execute("SELECT email FROM users WHERE id=?", (t["user_id"],)).fetchone()["email"]
            conn.execute("UPDATE users SET email=? WHERE id=?", (t["email"], t["user_id"]))
            if old and old.lower() != t["email"].lower():
                accounts.send_email(old, "Your Banktab email address was changed",
                                    f"The email on your Banktab account was changed to {t['email']}.\n\n"
                                    "If that wasn't you, reset your password right away and tell whoever runs your Banktab server.")
        conn.execute("UPDATE users SET email_verified_at=datetime('now') WHERE id=?", (t["user_id"],))
        if _twofa_on(conn, t["user_id"]):
            return {"ok": True, "login": True}  # an email link alone never skips the two-step code
        _start_session(conn, t["user_id"], response, request)
    return {"ok": True}


class EmailIn(BaseModel):
    email: str = Field(max_length=254)


@app.post("/api/forgot")
def forgot(body: EmailIn, request: Request, conn=Depends(acct)):
    """Always says the same thing, so it can't be used to find out who has an account."""
    _require_header(request)
    if _limited(f"forgot|{_ip(request)}", 5, 3600):
        raise HTTPException(429, "Too many requests. Try again later.")
    _hit(f"forgot|{_ip(request)}")
    row = conn.execute("SELECT id, email FROM users WHERE email=? AND email_verified_at IS NOT NULL",
                       (body.email.strip(),)).fetchone()
    if row:
        token = accounts.make_token(conn, "reset", user_id=row["id"])
        accounts.send_email(row["email"], "Reset your Banktab password",
                            f"Someone asked to reset your password. To choose a new one, open:\n\n{_link(request, 'reset', token)}\n\n"
                            "The link works for 1 hour. If it wasn't you, ignore this email and nothing changes.")
    return {"ok": True}


class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=200)


@app.post("/api/reset")
def reset(body: ResetIn, request: Request, response: Response, conn=Depends(acct)):
    _require_header(request)
    accounts.check_new_password(body.password)
    with core.tx(conn):
        t = accounts.use_token(conn, body.token, "reset")
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), t["user_id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (t["user_id"],))  # log out everywhere else
        if _twofa_on(conn, t["user_id"]):
            return {"ok": True, "login": True}  # a reset link proves the email, not the second step
        _start_session(conn, t["user_id"], response, request)
    return {"ok": True}


# ---------------------------------------------------------------- invites

@app.get("/api/invite")
def invite_info(token: str, conn=Depends(acct)):
    t = accounts.peek_token(conn, token, "invite")
    h = conn.execute("SELECT name FROM households WHERE id=?", (t["household_id"],)).fetchone()
    by = conn.execute("SELECT display_name FROM users WHERE id=?", (t["created_by"],)).fetchone()
    return {"household": h["name"], "email": t["email"] or "", "invited_by": by["display_name"] if by else ""}


class JoinIn(BaseModel):
    token: str = Field(max_length=200)
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


@app.post("/api/join")
def join(body: JoinIn, request: Request, response: Response, conn=Depends(acct)):
    _require_header(request)
    if _limited(f"signup|{_ip(request)}", 10, 3600):
        raise HTTPException(429, "Too many tries from here. Try again later.")
    _hit(f"signup|{_ip(request)}")
    email = accounts.clean_email(body.email)
    with core.tx(conn):
        t = accounts.use_token(conn, body.token, "invite")
        # An invite emailed to this exact address proves they own it.
        verified = bool(t["email"]) and t["email"].lower() == email.lower()
        uid = _new_user(conn, name=body.name, email=email, password=body.password,
                        household_id=t["household_id"], role="member", verified=verified)
        if verified:
            _start_session(conn, uid, response, request)
        else:
            _send_verify(conn, request, uid, email)
    return {"ok": True, "verify": not verified}


# ---------------------------------------------------------------- household & account settings

@app.get("/api/household")
def household(user=Depends(current_user), conn=Depends(acct)):
    members = [dict(r) for r in conn.execute(
        "SELECT id, display_name, email, role, created_at FROM users WHERE household_id=? ORDER BY id", (user["household_id"],))]
    invites = [dict(r) for r in conn.execute(
        """SELECT rowid AS id, email, created_at, expires_at FROM tokens
           WHERE kind='invite' AND household_id=? AND used_at IS NULL AND expires_at > datetime('now') ORDER BY created_at""",
        (user["household_id"],))] if user["role"] == "owner" else []
    return {"id": user["household_id"], "name": user["household_name"], "role": user["role"],
            "members": members, "invites": invites}


class HouseholdPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$")


@app.patch("/api/household")
def rename_household(body: HouseholdPatch, user=Depends(owner), conn=Depends(acct)):
    if body.name:
        conn.execute("UPDATE households SET name=? WHERE id=?", (body.name.strip(), user["household_id"]))
    if body.currency:
        conn.execute("UPDATE households SET currency=? WHERE id=?", (body.currency, user["household_id"]))
    return {"ok": True}


@app.get("/api/household/backup")
def household_backup(user=Depends(owner)):
    """The whole budget as one SQLite file (a consistent copy, safe while in use)."""
    import tempfile
    from starlette.background import BackgroundTask
    src = household_conn(user["household_id"])
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dst = __import__("sqlite3").connect(tmp)
    with dst:
        src.backup(dst)
    dst.close()
    safe = "-".join("".join(ch if ch.isalnum() else " " for ch in user["household_name"]).split()) or "budget"
    return FileResponse(tmp, media_type="application/vnd.sqlite3",
                        filename=f"{safe}-{dt.date.today().isoformat()}.db",
                        background=BackgroundTask(lambda: os.path.exists(tmp) and os.unlink(tmp)))


class InviteIn(BaseModel):
    email: str = Field("", max_length=254)


@app.post("/api/household/invites")
def create_invite(body: InviteIn, request: Request, user=Depends(owner), conn=Depends(acct)):
    if _limited(f"invite|{user['id']}", 20, 86400):
        raise HTTPException(429, "That's a lot of invites for one day. Try again tomorrow.")
    _hit(f"invite|{user['id']}")
    email = accounts.clean_email(body.email) if body.email.strip() else None
    token = accounts.make_token(conn, "invite", household_id=user["household_id"], email=email, created_by=user["id"])
    link = _link(request, "join", token)
    if email and accounts.email_enabled():
        accounts.send_email(email, f"{user['display_name']} invited you to {user['household_name']}",
                            f"{user['display_name']} invited you to share their budget, \"{user['household_name']}\".\n\n"
                            f"Create your login here:\n\n{link}\n\nThe link works for 7 days.")
    return {"link": link, "emailed": bool(email and accounts.email_enabled())}


@app.delete("/api/household/invites/{rowid}")
def cancel_invite(rowid: int, user=Depends(owner), conn=Depends(acct)):
    conn.execute("DELETE FROM tokens WHERE rowid=? AND kind='invite' AND household_id=?", (rowid, user["household_id"]))
    return {"ok": True}


class MemberPatch(BaseModel):
    role: Literal["owner", "member"]


@app.patch("/api/household/members/{uid}")
def set_role(uid: int, body: MemberPatch, user=Depends(owner), conn=Depends(acct)):
    with core.tx(conn):
        m = conn.execute("SELECT id FROM users WHERE id=? AND household_id=?", (uid, user["household_id"])).fetchone()
        if m is None:
            raise HTTPException(404)
        conn.execute("UPDATE users SET role=? WHERE id=?", (body.role, uid))
        if not conn.execute("SELECT 1 FROM users WHERE household_id=? AND role='owner'", (user["household_id"],)).fetchone():
            raise accounts.AccountError("A household needs at least one owner.")
    return {"ok": True}


@app.delete("/api/household/members/{uid}")
def remove_member(uid: int, user=Depends(owner), conn=Depends(acct)):
    """Removes the login. Their past entries stay, still labeled with their name."""
    with core.tx(conn):
        if uid == user["id"]:
            raise accounts.AccountError("You can't remove yourself. Make someone else owner first, or delete the household.")
        if conn.execute("DELETE FROM users WHERE id=? AND household_id=?", (uid, user["household_id"])).rowcount == 0:
            raise HTTPException(404)
    return {"ok": True}


class PasswordConfirm(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/household/delete")
def delete_household(body: PasswordConfirm, request: Request, response: Response, user=Depends(owner), conn=Depends(acct)):
    """Permanently deletes the household's budget file and every login in it."""
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if _limited(f"pw|{user['id']}", 5, 900):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    if not check_password(body.password, row["password_hash"]):
        _hit(f"pw|{user['id']}")
        raise accounts.AccountError("That password isn't right.")
    hid = user["household_id"]
    with core.tx(conn):
        conn.execute("DELETE FROM tokens WHERE household_id=? OR user_id IN (SELECT id FROM users WHERE household_id=?)", (hid, hid))
        conn.execute("DELETE FROM users WHERE household_id=?", (hid,))
        conn.execute("DELETE FROM households WHERE id=?", (hid,))
    close_household(hid)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(accounts.household_path(DATA_DIR, hid)) + suffix)
        if p.exists():
            p.unlink()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


class AccountPatch(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=60)
    email: str | None = Field(None, max_length=254)
    theme: Literal["system", "light", "dark", "noir", "paper", "bank"] | None = None
    accent: Literal["teal", "blue", "violet", "rose", "amber", "green", "graphite", "gold"] | None = None


@app.patch("/api/account")
def edit_account(body: AccountPatch, request: Request, user=Depends(current_user), conn=Depends(acct)):
    out = {"ok": True}
    if body.theme:
        conn.execute("UPDATE users SET theme=? WHERE id=?", (body.theme, user["id"]))
    if body.accent:
        conn.execute("UPDATE users SET accent=? WHERE id=?", (body.accent, user["id"]))
    if body.display_name:
        conn.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name.strip(), user["id"]))
        hconn = household_conn(user["household_id"])
        accounts.sync_member(hconn, {**user, "display_name": body.display_name.strip()})
    if body.email is not None and body.email.strip() != (user["email"] or ""):
        email = accounts.clean_email(body.email)
        if conn.execute("SELECT 1 FROM users WHERE (email=? OR username=?) AND id != ?", (email, email, user["id"])).fetchone():
            raise accounts.AccountError("Another account already uses that email.")
        if _limited(f"resend|{user['id']}", 3, 3600):
            raise HTTPException(429, "Too many emails sent. Try again in an hour.")
        _hit(f"resend|{user['id']}")
        _send_verify(conn, request, user["id"], email)  # the address changes once they click the link
        out["verify_sent"] = True
    return out


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


@app.post("/api/account/password")
def change_password(body: PasswordChange, request: Request, user=Depends(current_user), conn=Depends(acct)):
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if _limited(f"pw|{user['id']}", 5, 900):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    if not check_password(body.current, row["password_hash"]):
        _hit(f"pw|{user['id']}")
        raise accounts.AccountError("Your current password isn't right.")
    accounts.check_new_password(body.new)
    token = request.cookies.get(COOKIE, "")
    with core.tx(conn):
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.new), user["id"]))
        # Sign out other devices, keep this one.
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (user["id"], sha(token)))
    return {"ok": True}


@app.post("/api/account/logout-others")
def logout_others(request: Request, user=Depends(current_user), conn=Depends(acct)):
    n = conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?",
                     (user["id"], sha(request.cookies.get(COOKIE, "")))).rowcount
    return {"signed_out": n}


# ---------------------------------------------------------------- two-step login settings

class PasswordOnly(BaseModel):
    password: str = Field(max_length=200)


class CodeOnly(BaseModel):
    code: str = Field(max_length=40)


class PasswordAndCode(BaseModel):
    password: str = Field(max_length=200)
    code: str = Field(max_length=40)


def _confirm_password(conn, user, password: str) -> None:
    if _limited(f"pw|{user['id']}", 5, 900):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not check_password(password, row["password_hash"]):
        _hit(f"pw|{user['id']}")
        raise accounts.AccountError("Your password isn't right.")


def _confirm_code(conn, user, code: str) -> None:
    if _limited(f"2fa|{user['id']}", 10, 900):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    with core.tx(conn):
        kind = _check_second_factor(conn, user["id"], code)
    if kind is None:
        _hit(f"2fa|{user['id']}")
        raise accounts.AccountError("That code isn't right. Use the newest code from your app, or a recovery code.")


@app.get("/api/account/2fa")
def twofa_status(user=Depends(current_user), conn=Depends(acct)):
    return {"on": bool(user["totp_on_at"]), "since": user["totp_on_at"],
            "recovery_left": _recovery_left(conn, user["id"]) if user["totp_on_at"] else 0}


@app.post("/api/account/2fa/start")
def twofa_start(body: PasswordOnly, user=Depends(current_user), conn=Depends(acct)):
    """Step 1 of turning it on: a fresh secret to scan. Nothing changes until it's confirmed."""
    if user["totp_on_at"]:
        raise accounts.AccountError("Two-step login is already on.")
    _confirm_password(conn, user, body.password)
    secret = totp.new_secret()
    conn.execute("UPDATE users SET totp_secret=?, totp_last_step=NULL WHERE id=?", (secret, user["id"]))
    link = totp.uri(secret, user["email"] or user["username"])
    return {"secret": secret, "uri": link, "qr": totp.qr_svg_data_uri(link)}


@app.post("/api/account/2fa/enable")
def twofa_enable(body: CodeOnly, request: Request, user=Depends(current_user), conn=Depends(acct)):
    """Step 2: prove the app works by typing a code. Returns the recovery codes, shown once."""
    if user["totp_on_at"]:
        raise accounts.AccountError("Two-step login is already on.")
    if _limited(f"2fa|{user['id']}", 10, 900):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    row = conn.execute("SELECT totp_secret FROM users WHERE id=?", (user["id"],)).fetchone()
    if not row["totp_secret"]:
        raise accounts.AccountError("Start again: scan the code first.")
    step = totp.verify(row["totp_secret"], body.code)
    if step is None:
        _hit(f"2fa|{user['id']}")
        raise accounts.AccountError("That code isn't right. Check the time on your phone and try the newest code.")
    with core.tx(conn):
        conn.execute("UPDATE users SET totp_on_at=datetime('now'), totp_last_step=? WHERE id=?", (step, user["id"]))
        codes = _new_recovery_codes(conn, user["id"])
        # Devices that logged in without a code are signed out; this one stays.
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (user["id"], sha(request.cookies.get(COOKIE, ""))))
    return {"recovery_codes": codes}


@app.post("/api/account/2fa/recovery-codes")
def twofa_new_codes(body: PasswordAndCode, user=Depends(current_user), conn=Depends(acct)):
    if not user["totp_on_at"]:
        raise accounts.AccountError("Two-step login is off.")
    _confirm_password(conn, user, body.password)
    _confirm_code(conn, user, body.code)
    with core.tx(conn):
        codes = _new_recovery_codes(conn, user["id"])
    return {"recovery_codes": codes}


@app.post("/api/account/2fa/disable")
def twofa_disable(body: PasswordAndCode, user=Depends(current_user), conn=Depends(acct)):
    if not user["totp_on_at"]:
        raise accounts.AccountError("Two-step login is already off.")
    _confirm_password(conn, user, body.password)
    _confirm_code(conn, user, body.code)
    with core.tx(conn):
        conn.execute("UPDATE users SET totp_secret=NULL, totp_on_at=NULL, totp_last_step=NULL WHERE id=?", (user["id"],))
        conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (user["id"],))
    return {"ok": True}


# ---------------------------------------------------------------- first-run setup

class SetupCategory(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    grp: str | None = Field(None, max_length=40)
    budget_cents: int = Field(0, ge=0, lt=10**10)
    is_investment: bool = False


class SetupIn(BaseModel):
    categories: list[SetupCategory] = Field(default_factory=list, max_length=80)
    income_cents: int = Field(0, ge=0, lt=10**10)


@app.post("/api/setup")
def setup(body: SetupIn, user=Depends(current_user), conn=Depends(db), aconn=Depends(acct)):
    if user["setup_done"]:
        raise accounts.AccountError("This budget is already set up.")
    with core.tx(conn):
        if conn.execute("SELECT count(*) FROM categories").fetchone()[0] == 0:
            for i, c in enumerate(body.categories, 1):
                if conn.execute("SELECT 1 FROM categories WHERE name=?", (c.name.strip(),)).fetchone():
                    continue
                conn.execute("INSERT INTO categories(name, grp, sort, default_budget_cents, is_investment) VALUES (?,?,?,?,?)",
                             (c.name.strip(), c.grp or None, i, c.budget_cents, int(c.is_investment)))
            if body.income_cents:
                conn.execute("INSERT INTO income(name, amount_cents) VALUES ('Take-home pay', ?)", (body.income_cents,))
        core.ensure_months(conn)
        for c in conn.execute("SELECT id, default_budget_cents FROM categories").fetchall():
            for ym in core.open_months(conn):
                conn.execute("INSERT OR IGNORE INTO month_budgets(ym, category_id, amount_cents) VALUES (?,?,?)",
                             (ym, c["id"], c["default_budget_cents"]))
    aconn.execute("UPDATE households SET setup_done=1 WHERE id=?", (user["household_id"],))
    return {"ok": True}


# ---------------------------------------------------------------- state

def _cats(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, name, grp, sort, default_budget_cents, budget_from_bills, archived, is_investment, goal_cents, goal_ym "
        "FROM categories ORDER BY archived, sort, name")]


def _tags(conn):
    return [dict(r) for r in conn.execute("SELECT id, name, kind, archived FROM tags ORDER BY archived, name")]


@app.get("/api/state")
def state(ym: str | None = None, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        core.ensure_months(conn)
    months = [dict(r) for r in conn.execute("SELECT ym, status FROM months ORDER BY ym")]
    if ym is None:
        opens = [m["ym"] for m in months if m["status"] == "open"]
        ym = opens[-1] if opens else months[-1]["ym"]
    core.valid_ym(ym)
    return {
        "user": {**{k: user[k] for k in ("id", "username", "email", "display_name", "role", "theme", "accent")},
                 "twofa": bool(user["totp_on_at"])},
        "household": {"id": user["household_id"], "name": user["household_name"], "setup_done": bool(user["setup_done"]),
                      "currency": user["currency"]},
        "today": dt.date.today().isoformat(),
        "months": months,
        "summary": core.month_summary(conn, ym),
        "categories": _cats(conn),
        "tags": _tags(conn),
        "users": [dict(r) for r in conn.execute("SELECT id, display_name FROM users")],
    }


# ---------------------------------------------------------------- transactions

TX_SELECT = """
SELECT t.*, c.name AS category, u.display_name AS entered_by_name,
       (SELECT group_concat(tag_id) FROM transaction_tags tt WHERE tt.transaction_id = t.id) AS tag_csv,
       (SELECT c2.name FROM transactions o JOIN categories c2 ON c2.id = o.category_id
         WHERE o.move_id = t.move_id AND o.id != t.id) AS move_other
FROM transactions t
LEFT JOIN categories c ON c.id = t.category_id
LEFT JOIN users u ON u.id = t.entered_by
"""


def _tx_out(rows):
    out = []
    for r in rows:
        d = dict(r)
        d["tag_ids"] = [int(x) for x in (d.pop("tag_csv") or "").split(",") if x]
        out.append(d)
    return out


@app.get("/api/transactions")
def transactions(ym: str | None = None, category_id: int | None = None, q: str | None = None,
                 tag_id: int | None = None, user=Depends(current_user), conn=Depends(db)):
    where, args = [], []
    if ym:
        where.append("t.ym = ?"); args.append(core.valid_ym(ym))
    if category_id:
        where.append("t.category_id = ?"); args.append(category_id)
    if tag_id:
        where.append("t.id IN (SELECT transaction_id FROM transaction_tags WHERE tag_id=?)"); args.append(tag_id)
    if q:
        where.append("(t.description LIKE ? OR t.notes LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    if not where:
        raise HTTPException(400, "Pick a month, category, tag or search")
    sql = TX_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY t.ym DESC, t.sort DESC, t.id DESC LIMIT 2000"
    return _tx_out(conn.execute(sql, args))


class EntryIn(BaseModel):
    kind: Literal["spend", "add"] = "spend"
    ym: str
    category_id: int
    amount_cents: int = Field(gt=0, lt=10**10)
    description: str = Field("", max_length=200)
    notes: str = Field("", max_length=1000)
    date: str | None = None
    intent: str | None = None
    tag_ids: list[int] = []
    reimbursed: bool = False


class MoveIn(BaseModel):
    ym: str
    from_id: int
    to_id: int
    amount_cents: int = Field(gt=0, lt=10**10)
    notes: str = Field("", max_length=1000)
    date: str | None = None


class RecordIn(BaseModel):
    amount_cents: int = Field(gt=-10**10, lt=10**10)
    description: str = Field("", max_length=200)
    notes: str = Field("", max_length=1000)
    date: str | None = None
    tag_ids: list[int] = []
    reimbursed: bool = False


class EntryPatch(BaseModel):
    kind: Literal["spend", "add"] | None = None
    ym: str | None = None
    category_id: int | None = None
    amount_cents: int | None = Field(None, gt=-10**10, lt=10**10)
    description: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=1000)
    date: str | None = None
    intent: str | None = None
    tag_ids: list[int] | None = None
    reimbursed: bool | None = None


@app.get("/api/entries/{tx_id}")
def get_entry(tx_id: int, user=Depends(current_user), conn=Depends(db)):
    rows = _tx_out(conn.execute(TX_SELECT + " WHERE t.id=?", (tx_id,)))
    if not rows:
        raise HTTPException(404, "That entry no longer exists")
    return rows[0]


@app.get("/api/entries/{tx_id}/other")
def get_move_other(tx_id: int, user=Depends(current_user), conn=Depends(db)):
    """The other half of a move."""
    rows = _tx_out(conn.execute(
        TX_SELECT + " WHERE t.move_id = (SELECT move_id FROM transactions WHERE id=?) AND t.id != ?", (tx_id, tx_id)))
    if not rows:
        raise HTTPException(404, "Not a move")
    return rows[0]


@app.post("/api/entries")
def create_entry(body: EntryIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        tid = core.add_entry(conn, **body.model_dump(), user_id=user["id"])
    return {"id": tid}


@app.post("/api/moves")
def create_move(body: MoveIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        mid = core.add_move(conn, **body.model_dump(), user_id=user["id"])
    return {"move_id": mid}


@app.post("/api/records")
def create_record(body: RecordIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        tid = core.add_record(conn, **body.model_dump(), user_id=user["id"])
    return {"id": tid}


class BatchRow(BaseModel):
    ym: str = Field(max_length=7)
    category_id: int
    amount_cents: int = Field(gt=-10**10, lt=10**10)   # + = spending, - = money added (like the sheet)
    description: str = Field("", max_length=200)
    date: str | None = Field(None, max_length=10)


class BatchIn(BaseModel):
    rows: list[BatchRow] = Field(min_length=1, max_length=500)


@app.post("/api/entries/batch")
def create_batch(body: BatchIn, user=Depends(current_user), conn=Depends(db)):
    """Several entries at once (the weekly catch-up grid). All or nothing: if any row is
    bad, nothing is saved and the error says which row."""
    ids = []
    with core.tx(conn):
        for i, r in enumerate(body.rows, 1):
            if r.amount_cents == 0:
                raise core.BudgetError(f"Row {i}: amount can't be zero")
            try:
                ids.append(core.add_entry(conn, ym=core.valid_ym(r.ym), category_id=r.category_id,
                                          amount_cents=abs(r.amount_cents), kind="spend" if r.amount_cents > 0 else "add",
                                          description=r.description, date=r.date, user_id=user["id"]))
            except core.BudgetError as e:
                raise core.BudgetError(f"Row {i}: {e}")
    return {"ids": ids}


@app.patch("/api/entries/{tx_id}")
def patch_entry(tx_id: int, body: EntryPatch, user=Depends(current_user), conn=Depends(db)):
    fields = body.model_dump(exclude_unset=True)
    with core.tx(conn):
        core.update_entry(conn, tx_id, **fields)
    return {"ok": True}


@app.delete("/api/entries/{tx_id}")
def remove_entry(tx_id: int, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        core.delete_entry(conn, tx_id)
    return {"ok": True}


# ---------------------------------------------------------------- months & budgets

@app.post("/api/months/{ym}/close")
def close(ym: str, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        core.close_month(conn, core.valid_ym(ym), user["id"])
    return {"ok": True}


@app.post("/api/months/{ym}/reopen")
def reopen(ym: str, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        core.reopen_month(conn, core.valid_ym(ym))
    return {"ok": True}


class Amount(BaseModel):
    amount_cents: int = Field(ge=0, lt=10**10)


@app.put("/api/months/{ym}/budgets/{category_id}")
def set_budget(ym: str, category_id: int, body: Amount, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        core.set_month_budget(conn, core.valid_ym(ym), category_id, body.amount_cents)
    return {"ok": True}


@app.get("/api/history/{category_id}")
def category_history(category_id: int, user=Depends(current_user), conn=Depends(db)):
    """Budget / spent / running balance for one category, every month."""
    rows = conn.execute(
        """SELECT mb.ym, mb.amount_cents AS budget,
                  COALESCE((SELECT sum(amount_cents) FROM transactions t
                            WHERE t.ym = mb.ym AND t.category_id = mb.category_id), 0) AS spent
           FROM month_budgets mb WHERE mb.category_id=? ORDER BY mb.ym""", (category_id,)).fetchall()
    out, bal = [], 0
    for r in rows:
        bal += r["budget"] - r["spent"]
        out.append({"ym": r["ym"], "budget": r["budget"], "spent": r["spent"], "balance": bal})
    return out


@app.get("/api/reports")
def reports(start: str, end: str, user=Depends(current_user), conn=Depends(db)):
    """Per month and category: purchases ('spend' entries), money added ('add' entries) and the
    month's budget, for months start..end inclusive. Moves are left out: they aren't spending."""
    if not (core.valid_ym(start) and core.valid_ym(end)) or start > end:
        raise HTTPException(400, "Bad month range")
    rows = conn.execute(
        """SELECT ym, category_id, sum(budget) AS budget, sum(spent) AS spent, sum(added) AS added FROM (
             SELECT ym, category_id, amount_cents AS budget, 0 AS spent, 0 AS added
               FROM month_budgets WHERE ym BETWEEN ?1 AND ?2
             UNION ALL
             SELECT ym, category_id, 0,
                    sum(CASE WHEN kind='spend' THEN amount_cents ELSE 0 END),
                    -sum(CASE WHEN kind='add' THEN amount_cents ELSE 0 END)
               FROM transactions WHERE ym BETWEEN ?1 AND ?2 AND kind IN ('spend','add')
               GROUP BY ym, category_id)
           GROUP BY ym, category_id ORDER BY ym""", (start, end)).fetchall()
    intents = conn.execute(
        """SELECT COALESCE(intent, '') AS intent, sum(amount_cents) AS spent, count(*) AS n FROM transactions
           WHERE ym BETWEEN ? AND ? AND kind='spend' GROUP BY 1""", (start, end)).fetchall()
    return {"rows": [dict(r) for r in rows], "intents": [dict(r) for r in intents]}


# ---------------------------------------------------------------- settings: categories, bills, income

class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    grp: str | None = None
    default_budget_cents: int = Field(0, ge=0, lt=10**10)
    budget_from_bills: bool = False


class CategoryPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    grp: str | None = None
    sort: int | None = None
    default_budget_cents: int | None = Field(None, ge=0, lt=10**10)
    budget_from_bills: bool | None = None
    archived: bool | None = None
    is_investment: bool | None = None
    # Savings goal: send goal_cents null to remove the goal (its month goes with it).
    goal_cents: int | None = Field(None, gt=0, lt=10**10)
    goal_ym: str | None = Field(None, max_length=7)


@app.post("/api/categories")
def add_category(body: CategoryIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        sort = conn.execute("SELECT COALESCE(max(sort),0)+1 FROM categories WHERE archived=0").fetchone()[0]
        cid = conn.execute(
            "INSERT INTO categories(name, grp, sort, budget_from_bills) VALUES (?,?,?,?)",
            (body.name.strip(), body.grp or None, sort, int(body.budget_from_bills))).lastrowid
        core.set_default_budget(conn, cid, body.default_budget_cents)
        core.refresh_bill_budgets(conn)
    return {"id": cid}


@app.patch("/api/categories/{cid}")
def edit_category(cid: int, body: CategoryPatch, user=Depends(current_user), conn=Depends(db)):
    f = body.model_dump(exclude_unset=True)
    with core.tx(conn):
        for k in ("name", "grp", "sort"):
            if k in f:
                conn.execute(f"UPDATE categories SET {k}=? WHERE id=?", (f[k].strip() if k == "name" else f[k], cid))
        if "budget_from_bills" in f:
            conn.execute("UPDATE categories SET budget_from_bills=? WHERE id=?", (int(f["budget_from_bills"]), cid))
        if "is_investment" in f:
            conn.execute("UPDATE categories SET is_investment=? WHERE id=?", (int(f["is_investment"]), cid))
        if "archived" in f:
            conn.execute("UPDATE categories SET archived=? WHERE id=?", (int(f["archived"]), cid))
            if f["archived"]:  # stop budgeting it in open months
                for ym in core.open_months(conn):
                    core.set_month_budget(conn, ym, cid, 0)
        if f.get("default_budget_cents") is not None:
            core.set_default_budget(conn, cid, f["default_budget_cents"])
        if "goal_cents" in f:
            conn.execute("UPDATE categories SET goal_cents=? WHERE id=?", (f["goal_cents"], cid))
            if f["goal_cents"] is None:
                conn.execute("UPDATE categories SET goal_ym=NULL WHERE id=?", (cid,))
        if "goal_ym" in f and f.get("goal_cents", 0) is not None:
            conn.execute("UPDATE categories SET goal_ym=? WHERE id=?",
                         (core.valid_ym(f["goal_ym"]) if f["goal_ym"] else None, cid))
        core.refresh_bill_budgets(conn)
    return {"ok": True}


class BillIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    amount_cents: int = Field(ge=0, lt=10**10)
    frequency: Literal["monthly", "quarterly", "yearly"]
    renews: str = Field("", max_length=40)
    category_id: int | None = None
    notes: str = Field("", max_length=500)
    active: bool = True


@app.get("/api/bills")
def bills(user=Depends(current_user), conn=Depends(db)):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM bills ORDER BY category_id, CASE frequency WHEN 'monthly' THEN 0 WHEN 'quarterly' THEN 1 ELSE 2 END, name")]


@app.post("/api/bills")
def add_bill(body: BillIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        bid = conn.execute(
            "INSERT INTO bills(name, amount_cents, frequency, renews, category_id, notes, active) VALUES (?,?,?,?,?,?,?)",
            (body.name, body.amount_cents, body.frequency, body.renews, body.category_id, body.notes, int(body.active)),
        ).lastrowid
        core.refresh_bill_budgets(conn)
    return {"id": bid}


@app.put("/api/bills/{bid}")
def edit_bill(bid: int, body: BillIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        conn.execute(
            "UPDATE bills SET name=?, amount_cents=?, frequency=?, renews=?, category_id=?, notes=?, active=? WHERE id=?",
            (body.name, body.amount_cents, body.frequency, body.renews, body.category_id, body.notes, int(body.active), bid))
        core.refresh_bill_budgets(conn)
    return {"ok": True}


@app.delete("/api/bills/{bid}")
def delete_bill(bid: int, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        conn.execute("DELETE FROM bills WHERE id=?", (bid,))
        core.refresh_bill_budgets(conn)
    return {"ok": True}


class IncomeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    amount_cents: int = Field(ge=0, lt=10**10)
    active: bool = True


@app.get("/api/income")
def income(user=Depends(current_user), conn=Depends(db)):
    return [dict(r) for r in conn.execute("SELECT * FROM income ORDER BY id")]


@app.post("/api/income")
def add_income(body: IncomeIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        iid = conn.execute("INSERT INTO income(name, amount_cents, active) VALUES (?,?,?)",
                           (body.name, body.amount_cents, int(body.active))).lastrowid
    return {"id": iid}


@app.put("/api/income/{iid}")
def edit_income(iid: int, body: IncomeIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        conn.execute("UPDATE income SET name=?, amount_cents=?, active=? WHERE id=?",
                     (body.name, body.amount_cents, int(body.active), iid))
    return {"ok": True}


@app.delete("/api/income/{iid}")
def delete_income(iid: int, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        conn.execute("DELETE FROM income WHERE id=?", (iid,))
    return {"ok": True}


# ---------------------------------------------------------------- trackers (tags)

class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["tax", "project"] = "project"
    archived: bool = False


@app.get("/api/tags")
def tags_with_totals(user=Depends(current_user), conn=Depends(db)):
    """Each tag with totals per year. Year = purchase date, else the entry's month."""
    rows = conn.execute(
        """SELECT tg.id, tg.name, tg.kind, tg.archived,
                  COALESCE(substr(t.date,1,4), substr(t.ym,1,4), CAST(t.year AS TEXT)) AS yr,
                  sum(t.amount_cents) AS total, count(t.id) AS n,
                  sum(CASE WHEN t.reimbursed THEN t.amount_cents ELSE 0 END) AS reimbursed
           FROM tags tg LEFT JOIN transaction_tags tt ON tt.tag_id = tg.id
           LEFT JOIN transactions t ON t.id = tt.transaction_id
           -- alias must not be "year": SQLite would group by the transactions.year column instead
           GROUP BY tg.id, yr ORDER BY tg.archived, tg.name, yr""").fetchall()
    out = {}
    for r in rows:
        t = out.setdefault(r["id"], {"id": r["id"], "name": r["name"], "kind": r["kind"],
                                     "archived": r["archived"], "years": [], "total": 0, "n": 0})
        if r["n"]:
            t["years"].append({"year": r["yr"] or "No date", "total": r["total"], "n": r["n"],
                               "reimbursed": r["reimbursed"]})
            t["total"] += r["total"]
            t["n"] += r["n"]
    return list(out.values())


@app.post("/api/tags")
def add_tag(body: TagIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        tid = conn.execute("INSERT INTO tags(name, kind, archived) VALUES (?,?,?)",
                           (body.name.strip(), body.kind, int(body.archived))).lastrowid
    return {"id": tid}


@app.put("/api/tags/{tid}")
def edit_tag(tid: int, body: TagIn, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):
        conn.execute("UPDATE tags SET name=?, kind=?, archived=? WHERE id=?",
                     (body.name.strip(), body.kind, int(body.archived), tid))
    return {"ok": True}


@app.delete("/api/tags/{tid}")
def delete_tag(tid: int, user=Depends(current_user), conn=Depends(db)):
    """Delete a tracker. Envelope entries just lose the tag; tracking-only records that
    belong to no other tracker are deleted too (they'd count toward nothing otherwise).
    Records have no month, so closed months never block this."""
    with core.tx(conn):
        if conn.execute("SELECT 1 FROM tags WHERE id=?", (tid,)).fetchone() is None:
            raise HTTPException(404)
        orphan = """SELECT t.id FROM transactions t JOIN transaction_tags tt ON tt.transaction_id = t.id
                    WHERE tt.tag_id = ? AND t.kind = 'record'
                      AND NOT EXISTS (SELECT 1 FROM transaction_tags o WHERE o.transaction_id = t.id AND o.tag_id != ?)"""
        ids = [r[0] for r in conn.execute(orphan, (tid, tid))]
        conn.execute("DELETE FROM transaction_tags WHERE tag_id=?", (tid,))
        conn.executemany("DELETE FROM transactions WHERE id=? AND kind='record'", [(i,) for i in ids])
        conn.execute("DELETE FROM tags WHERE id=?", (tid,))
    return {"ok": True, "records_deleted": len(ids)}


def _cell(v):
    """Spreadsheet apps run cells starting with = + - @ as formulas; a description like
    '=HYPERLINK(...)' must stay text when someone opens the export."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def _csv_response(rows, name):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "month", "category", "description", "amount", "kind", "notes", "type", "reimbursed", "entered_by"])
    for r in rows:
        w.writerow([r["date"] or "", r["ym"] or "", _cell(r["category"]), _cell(r["description"]), f"{r['amount_cents'] / 100:.2f}",
                    r["kind"], _cell(r["notes"]), r["intent"] or "", "yes" if r["reimbursed"] else "", _cell(r["entered_by_name"])])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/tags/{tid}/export.csv")
def tag_csv(tid: int, year: str | None = None, user=Depends(current_user), conn=Depends(db)):
    tag = conn.execute("SELECT name FROM tags WHERE id=?", (tid,)).fetchone()
    if tag is None:
        raise HTTPException(404)
    sql = TX_SELECT + " WHERE t.id IN (SELECT transaction_id FROM transaction_tags WHERE tag_id=?)"
    args: list = [tid]
    if year:
        sql += " AND COALESCE(substr(t.date,1,4), substr(t.ym,1,4), CAST(t.year AS TEXT)) = ?"
        args.append(year)
    rows = conn.execute(sql + " ORDER BY COALESCE(t.date, t.ym)", args).fetchall()
    safe = "".join(ch for ch in tag["name"] if ch.isalnum() or ch in " -_").strip().replace(" ", "-")
    return _csv_response(rows, f"{safe}{'-' + year if year else ''}.csv")


@app.get("/api/export.csv")
def export_all(user=Depends(current_user), conn=Depends(db)):
    rows = conn.execute(TX_SELECT + " ORDER BY t.ym, t.sort, t.id").fetchall()
    return _csv_response(rows, f"budget-export-{dt.date.today().isoformat()}.csv")


@app.get("/api/suggestions")
def suggestions(user=Depends(current_user), conn=Depends(db)):
    """Recent descriptions with the category they were last used with (for autocomplete)."""
    rows = conn.execute(
        """WITH recent AS (
               SELECT lower(trim(description)) AS k, trim(description) AS description, category_id,
                      count(*) OVER (PARTITION BY lower(trim(description))) AS n,
                      row_number() OVER (PARTITION BY lower(trim(description)) ORDER BY ym DESC, sort DESC, id DESC) AS rn
               FROM transactions
               WHERE kind IN ('spend','add') AND description != '' AND ym >= ?)
           SELECT description, category_id, n FROM recent WHERE rn = 1 ORDER BY n DESC LIMIT 400""",
        (f"{dt.date.today().year - 2}-01",)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- bank CSV import
# The browser reads the CSV and maps its columns; the server checks for entries that are
# already logged and then adds the chosen rows like normal entries (source = 'csv').

class ImportRow(BaseModel):
    date: str = Field(max_length=10)
    description: str = Field("", max_length=200)
    amount_cents: int = Field(gt=-10**10, lt=10**10)   # + = spending, - = money in


class ImportCheck(BaseModel):
    rows: list[ImportRow] = Field(max_length=5000)


@app.post("/api/import/check")
def import_check(body: ImportCheck, user=Depends(current_user), conn=Depends(db)):
    """For each row: is something with the same amount, going the same way (spent vs. money in),
    already logged within 4 days of it? (Card purchases often post a day or two after you log them.)"""
    out = []
    for r in body.rows:
        try:
            d = dt.date.fromisoformat(r.date)
        except ValueError:
            out.append({"duplicate_of": None}); continue
        lo, hi = (d - dt.timedelta(days=4)).isoformat(), (d + dt.timedelta(days=4)).isoformat()
        m = conn.execute(
            """SELECT t.id, t.description, t.date, t.ym, c.name AS category FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               WHERE t.kind IN ('spend','add') AND t.amount_cents = ?
                 AND (COALESCE(t.posted_date, t.date) BETWEEN ? AND ?
                      OR (t.date IS NULL AND t.ym = ?))
               ORDER BY t.id DESC LIMIT 1""",
            (r.amount_cents, lo, hi, r.date[:7])).fetchone()
        out.append({"duplicate_of": dict(m) if m else None})
    return out


class ImportCommitRow(ImportRow):
    category_id: int
    ym: str = Field(max_length=7)


class ImportCommit(BaseModel):
    rows: list[ImportCommitRow] = Field(max_length=2000)


@app.post("/api/import/commit")
def import_commit(body: ImportCommit, user=Depends(current_user), conn=Depends(db)):
    with core.tx(conn):  # all or nothing
        for r in body.rows:
            if r.amount_cents == 0:
                continue
            tid = core.add_entry(conn, ym=core.valid_ym(r.ym), category_id=r.category_id,
                                 amount_cents=abs(r.amount_cents), kind="spend" if r.amount_cents > 0 else "add",
                                 description=r.description, date=r.date, user_id=user["id"], source="csv")
            conn.execute("UPDATE transactions SET posted_date=date WHERE id=?", (tid,))
    return {"added": len([r for r in body.rows if r.amount_cents])}


# ---------------------------------------------------------------- front end

@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
