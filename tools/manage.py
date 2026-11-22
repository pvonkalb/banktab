"""Manage households and logins (run on the server).

    python tools/manage.py households
    python tools/manage.py create-household "<Name>"
    python tools/manage.py import-household <budget.db> "<Name>"     # a budget file made elsewhere
    python tools/manage.py migrate-legacy <old-budget.db> "<Name>"    # pre-households database
    python tools/manage.py adduser <username> "<Display Name>" <household id> [--member] [--email you@x.com]
    python tools/manage.py passwd <username>
    python tools/manage.py disable-2fa <username>                    # lost phone AND recovery codes
    python tools/manage.py users
    python tools/manage.py logout-all
    python tools/manage.py delete-household <id>

Data folder: BUDGET_DATA_DIR (default ./data). Passwords are read from a hidden prompt
(or BUDGET_PASSWORD for scripted setups).
"""
from __future__ import annotations

import getpass
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import accounts, core  # noqa: E402
from app.main import DATA_DIR  # noqa: E402


def ask_password() -> str:
    pw = os.environ.get("BUDGET_PASSWORD")
    if pw:
        accounts.check_new_password(pw)
        return pw
    while True:
        pw = getpass.getpass("Password (10+ characters): ")
        try:
            accounts.check_new_password(pw)
        except accounts.AccountError as e:
            print(e)
            continue
        if pw != getpass.getpass("Again: "):
            print("Didn't match.")
            continue
        return pw


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    cmd, args = argv[0], argv[1:]
    if cmd == "migrate-legacy" and len(args) == 2:
        hid = accounts.migrate_legacy(Path(args[0]), DATA_DIR, args[1])
        print(f"Moved {args[0]} into household {hid} ({accounts.household_path(DATA_DIR, hid)})")
        return
    conn = accounts.open_accounts(DATA_DIR)
    if cmd == "households":
        for r in conn.execute("""SELECT h.id, h.name, h.created_at, count(u.id) AS n FROM households h
                                 LEFT JOIN users u ON u.household_id = h.id GROUP BY h.id ORDER BY h.id"""):
            print(f"{r['id']:4} {r['name']:30} {r['n']} login(s)  created {r['created_at']}")
    elif cmd == "create-household" and len(args) == 1:
        hid = accounts.create_household(conn, DATA_DIR, args[0])
        print(f"Created household {hid}")
    elif cmd == "import-household" and len(args) == 2:
        src = sqlite3.connect(args[0])
        hid = conn.execute("INSERT INTO households(name, setup_done) VALUES (?, 1)", (args[1],)).lastrowid
        path = accounts.household_path(DATA_DIR, hid)
        path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(path)
        src.backup(dst)
        dst.close(); src.close()
        h = core.connect(path); core.init_db(h)
        h.execute("UPDATE users SET password_hash=''"); h.execute("DELETE FROM sessions"); h.close()
        print(f"Imported {args[0]} as household {hid}. Add a login with: adduser <username> \"<Name>\" {hid}")
    elif cmd == "adduser" and len(args) >= 3:
        username, name, hid = args[0], args[1], int(args[2])
        role = "member" if "--member" in args else "owner"
        email = args[args.index("--email") + 1] if "--email" in args else None
        if not conn.execute("SELECT 1 FROM households WHERE id=?", (hid,)).fetchone():
            raise SystemExit(f"No household {hid}")
        uid = conn.execute(
            "INSERT INTO users(username, email, email_verified_at, display_name, password_hash, household_id, role, theme, accent) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (username, email, "now" if email else None, name, accounts.hash_password(ask_password()), hid, role,
             accounts.DEFAULT_THEME, accounts.DEFAULT_ACCENT)).lastrowid
        conn.execute("UPDATE users SET email_verified_at=datetime('now') WHERE id=? AND email IS NOT NULL", (uid,))
        h = accounts.open_household(DATA_DIR, hid)
        accounts.sync_member(h, {"id": uid, "username": username, "display_name": name})
        h.close()
        print(f"Added {username} ({role}) to household {hid}")
    elif cmd == "passwd" and len(args) == 1:
        n = conn.execute("UPDATE users SET password_hash=? WHERE username=? OR email=?",
                         (accounts.hash_password(ask_password()), args[0], args[0])).rowcount
        conn.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username=? OR email=?)", (args[0], args[0]))
        print("Password changed, sessions signed out" if n else f"No user {args[0]}")
    elif cmd == "disable-2fa" and len(args) == 1:
        n = conn.execute("UPDATE users SET totp_secret=NULL, totp_on_at=NULL, totp_last_step=NULL WHERE username=? OR email=?",
                         (args[0], args[0])).rowcount
        conn.execute("DELETE FROM recovery_codes WHERE user_id IN (SELECT id FROM users WHERE username=? OR email=?)", (args[0], args[0]))
        conn.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username=? OR email=?)", (args[0], args[0]))
        print(f"Two-step login turned off for {args[0]}; they can turn it on again in Settings" if n else f"No user {args[0]}")
    elif cmd == "users":
        for r in conn.execute("""SELECT u.username, u.display_name, u.role, u.email, u.email_verified_at, h.id AS hid, h.name
                                 FROM users u JOIN households h ON h.id = u.household_id ORDER BY h.id, u.id"""):
            mail = f"{r['email']}{'' if r['email_verified_at'] else ' (unconfirmed)'}" if r["email"] else ""
            print(f"{r['hid']:4} {r['name']:24} {r['username']:28} {r['display_name']:18} {r['role']:7} {mail}")
    elif cmd == "logout-all":
        conn.execute("DELETE FROM sessions")
        print("Everyone signed out")
    elif cmd == "delete-household" and len(args) == 1:
        hid = int(args[0])
        row = conn.execute("SELECT name FROM households WHERE id=?", (hid,)).fetchone()
        if not row:
            raise SystemExit(f"No household {hid}")
        if input(f'Type the household name ("{row["name"]}") to delete it and all its logins: ') != row["name"]:
            raise SystemExit("Not deleted.")
        with core.tx(conn):
            conn.execute("DELETE FROM tokens WHERE household_id=? OR user_id IN (SELECT id FROM users WHERE household_id=?)", (hid, hid))
            conn.execute("DELETE FROM users WHERE household_id=?", (hid,))
            conn.execute("DELETE FROM households WHERE id=?", (hid,))
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(accounts.household_path(DATA_DIR, hid)) + suffix)
            if p.exists():
                p.unlink()
        print(f"Deleted household {hid}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
