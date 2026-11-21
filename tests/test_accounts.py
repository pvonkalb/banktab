"""Households, sign-up, email links, invites, isolation, setup, CSV import, migration."""
import re

import pytest
from fastapi.testclient import TestClient

from app import accounts, core, main
from tests.test_api import H, make_household, reset_app

PW = "correct horse battery"


@pytest.fixture
def env(tmp_path, monkeypatch):
    reset_app(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(accounts, "send_email", lambda to, subject, body: sent.append((to, subject, body)))
    yield tmp_path, sent
    for conn in list(main._households.values()) + [main._acct]:
        if conn:
            conn.close()


def link_token(body):
    return re.search(r"token=([\w-]+)", body).group(1)


def client_for(username, password=PW):
    c = TestClient(main.app)
    assert c.post("/api/login", headers=H, json={"username": username, "password": password}).status_code == 200
    return c


def test_households_are_isolated(env):
    data, _ = env
    make_household(data, "Sam & Alex", "sam")
    make_household(data, "Friends", "friend")
    p, f = client_for("sam"), client_for("friend")
    ym = p.get("/api/state").json()["summary"]["ym"]
    tid = p.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 4321,
                                                  "description": "secret costco"}).json()["id"]
    tag = p.post("/api/tags", headers=H, json={"name": "Sam only"}).json()["id"]

    assert f.get("/api/state").json()["household"]["name"] == "Friends"
    assert f.get(f"/api/transactions?q=secret").json() == []
    assert f.get(f"/api/entries/{tid}").status_code == 404
    assert f.patch(f"/api/entries/{tid}", headers=H, json={"amount_cents": 1}).status_code == 400
    assert f.delete(f"/api/entries/{tid}", headers=H).status_code == 400
    assert all(t["name"] != "Sam only" for t in f.get("/api/tags").json())
    assert f.delete(f"/api/tags/{tag}", headers=H).status_code == 404
    assert "secret costco" not in f.get("/api/export.csv").text
    # Sam's entry is untouched
    assert p.get(f"/api/entries/{tid}").json()["amount_cents"] == 4321
    # and the household endpoint only lists your own members
    assert [m["display_name"] for m in f.get("/api/household").json()["members"]] == ["Friend"]


def test_signup_closed_by_default(env):
    c = TestClient(main.app)
    cfg = c.get("/api/config").json()
    assert cfg["signup"] is False and cfg["email"] is False
    r = c.post("/api/signup", headers=H, json={"name": "Sam", "email": "sam@example.com", "password": "a long passphrase"})
    assert r.status_code == 403


def test_signup_verify_setup(env, monkeypatch):
    data, sent = env
    monkeypatch.setattr(main, "SIGNUP_OPEN", True)
    c = TestClient(main.app)
    body = {"name": "Sam", "email": "sam@example.com", "password": "a long passphrase", "household": "Sam & Alex"}
    assert c.post("/api/signup", json=body).status_code == 403            # CSRF header required
    assert c.post("/api/signup", headers=H, json={**body, "password": "short"}).status_code == 400
    assert c.post("/api/signup", headers=H, json=body).json() == {"ok": True, "verify": True}
    assert c.post("/api/signup", headers=H, json=body).status_code == 400  # same email again

    # can't log in until the email is confirmed; a new link is sent
    r = c.post("/api/login", headers=H, json={"username": "sam@example.com", "password": "a long passphrase"})
    assert r.status_code == 403 and len(sent) == 2
    assert c.post("/api/verify", headers=H, json={"token": "nope"}).status_code == 400
    assert c.post("/api/verify", headers=H, json={"token": link_token(sent[-1][2])}).status_code == 200
    assert c.post("/api/verify", headers=H, json={"token": link_token(sent[-1][2])}).status_code == 400  # one use

    st = c.get("/api/state").json()   # verify logged them in
    assert st["household"] == {"id": st["household"]["id"], "name": "Sam & Alex", "setup_done": False, "currency": "USD"}
    assert st["user"]["role"] == "owner"
    r = c.post("/api/setup", headers=H, json={"categories": [{"name": "Groceries", "grp": "Needs", "budget_cents": 60000},
                                                             {"name": "Roth IRA", "grp": "Investing - Long", "budget_cents": 50000,
                                                              "is_investment": True}],
                                              "income_cents": 400000})
    assert r.status_code == 200
    st = c.get("/api/state").json()
    assert st["household"]["setup_done"] is True
    assert {x["name"]: x["budget"] for x in st["summary"]["categories"]} == {"Groceries": 60000, "Roth IRA": 50000}
    assert st["summary"]["unassigned"] == 400000 - 110000
    assert c.post("/api/setup", headers=H, json={}).status_code == 400  # only once
    # honeypot: looks fine to a bot, creates nothing
    assert c.post("/api/signup", headers=H, json={**body, "email": "bot@example.com", "website": "x"}).json() == {"ok": True}
    assert len(sent) == 2


def test_signup_rate_limit_and_cap(env, monkeypatch):
    monkeypatch.setattr(main, "SIGNUP_OPEN", True)
    monkeypatch.setattr(main, "MAX_HOUSEHOLDS", 2)
    c = TestClient(main.app)
    mk = lambda i: c.post("/api/signup", headers=H, json={"name": "X", "email": f"x{i}@example.com", "password": "a long passphrase"})
    assert mk(1).status_code == 200 and mk(2).status_code == 200
    assert mk(3).status_code == 403  # full
    monkeypatch.setattr(main, "MAX_HOUSEHOLDS", 100)
    for i in range(4, 7):
        mk(i)
    assert mk(9).status_code == 429


def test_password_reset(env):
    data, sent = env
    make_household(data, "Sam & Alex", "sam")
    p = client_for("sam")
    # add + confirm an email so reset can work
    assert p.patch("/api/account", headers=H, json={"email": "sam@example.com"}).json()["verify_sent"]
    p.post("/api/verify", headers=H, json={"token": link_token(sent[-1][2])})
    assert p.get("/api/state").json()["user"]["email"] == "sam@example.com"

    anon = TestClient(main.app)
    n = len(sent)
    assert anon.post("/api/forgot", headers=H, json={"email": "nobody@example.com"}).json() == {"ok": True}
    assert len(sent) == n  # no email, same answer
    anon.post("/api/forgot", headers=H, json={"email": "sam@example.com"})
    token = link_token(sent[-1][2])
    assert anon.post("/api/reset", headers=H, json={"token": token, "password": "short"}).status_code == 400
    assert anon.post("/api/reset", headers=H, json={"token": token, "password": "brand new passphrase"}).status_code == 200
    assert anon.get("/api/state").status_code == 200           # logged in by the reset
    assert p.get("/api/state").status_code == 401              # old sessions signed out
    assert anon.post("/api/reset", headers=H, json={"token": token, "password": "another passphrase"}).status_code == 400
    client_for("sam@example.com", "brand new passphrase")   # email works as login too


def test_invite_join_and_members(env):
    data, sent = env
    make_household(data, "Sam & Alex", "sam")
    p = client_for("sam")
    inv = p.post("/api/household/invites", headers=H, json={"email": "alex@example.com"}).json()
    token = link_token(inv["link"])
    anon = TestClient(main.app)
    info = anon.get(f"/api/invite?token={token}").json()
    assert info["household"] == "Sam & Alex" and info["email"] == "alex@example.com"
    r = anon.post("/api/join", headers=H, json={"token": token, "name": "Alex", "email": "alex@example.com",
                                                "password": "alexs passphrase"})
    assert r.json() == {"ok": True, "verify": False}   # invited address -> trusted, logged in
    st = anon.get("/api/state").json()
    assert st["household"]["name"] == "Sam & Alex" and st["user"]["role"] == "member"
    # members share the budget
    ym = st["summary"]["ym"]
    tid = anon.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 500, "description": "grocer"}).json()["id"]
    assert p.get(f"/api/entries/{tid}").json()["entered_by_name"] == "Alex"
    # only owners manage the household
    assert anon.post("/api/household/invites", headers=H, json={}).status_code == 403
    assert anon.patch("/api/household", headers=H, json={"name": "Hacked"}).status_code == 403
    # invite links are one-use
    assert anon.post("/api/join", headers=H, json={"token": token, "name": "X", "email": "x@example.com",
                                                   "password": "another passphrase"}).status_code == 400
    # a link-only invite (no email) needs the joiner to confirm their address
    t2 = link_token(p.post("/api/household/invites", headers=H, json={}).json()["link"])
    assert TestClient(main.app).post("/api/join", headers=H, json={"token": t2, "name": "Kid", "email": "kid@example.com",
                                                                   "password": "kids passphrase!"}).json()["verify"] is True
    members = p.get("/api/household").json()["members"]
    rid = next(m["id"] for m in members if m["display_name"] == "Alex")
    assert p.delete(f"/api/household/members/{p.get('/api/state').json()['user']['id']}", headers=H).status_code == 400
    assert p.delete(f"/api/household/members/{rid}", headers=H).status_code == 200
    assert anon.get("/api/state").status_code == 401
    assert p.get(f"/api/entries/{tid}").json()["entered_by_name"] == "Alex"  # history keeps the name
    p.patch("/api/household", headers=H, json={"name": "The House"})
    assert p.get("/api/state").json()["household"]["name"] == "The House"


def test_delete_household(env):
    data, _ = env
    hid, _ = make_household(data, "Gone", "gone")
    make_household(data, "Stays", "stays")
    c = client_for("gone")
    c.get("/api/state")
    assert c.post("/api/household/delete", headers=H, json={"password": "wrong"}).status_code == 400
    assert c.post("/api/household/delete", headers=H, json={"password": PW}).status_code == 200
    assert not accounts.household_path(data, hid).exists()
    assert c.get("/api/state").status_code == 401
    assert TestClient(main.app).post("/api/login", headers=H, json={"username": "gone", "password": PW}).status_code == 401
    assert client_for("stays").get("/api/state").status_code == 200


def test_change_password(env):
    data, _ = env
    make_household(data, "H", "sam")
    a, b = client_for("sam"), client_for("sam")
    assert a.post("/api/account/password", headers=H, json={"current": "nope", "new": "new passphrase!"}).status_code == 400
    assert a.post("/api/account/password", headers=H, json={"current": PW, "new": "new passphrase!"}).status_code == 200
    assert a.get("/api/state").status_code == 200   # this device stays logged in
    assert b.get("/api/state").status_code == 401   # other devices signed out
    a.patch("/api/account", headers=H, json={"display_name": "P"})
    assert a.get("/api/state").json()["user"]["display_name"] == "P"


def test_csv_import(env):
    data, _ = env
    make_household(data, "H", "sam")
    c = client_for("sam")
    ym = c.get("/api/state").json()["summary"]["ym"]
    d = f"{ym}-05"
    c.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 4210, "description": "costco",
                                            "date": f"{ym}-03"})
    chk = c.post("/api/import/check", headers=H, json={"rows": [
        {"date": d, "description": "COSTCO WHSE #0001", "amount_cents": 4210},
        {"date": d, "description": "KING SOOPERS", "amount_cents": 1899},
        {"date": "garbage", "description": "x", "amount_cents": 1}]}).json()
    assert chk[0]["duplicate_of"]["description"] == "costco"
    assert chk[1]["duplicate_of"] is None and chk[2]["duplicate_of"] is None
    r = c.post("/api/import/commit", headers=H, json={"rows": [
        {"date": d, "description": "KING SOOPERS", "amount_cents": 1899, "category_id": 1, "ym": ym},
        {"date": d, "description": "REFUND", "amount_cents": -500, "category_id": 2, "ym": ym}]})
    assert r.json() == {"added": 2}
    txs = {t["description"]: t for t in c.get(f"/api/transactions?ym={ym}").json()}
    assert txs["KING SOOPERS"]["source"] == "csv" and txs["KING SOOPERS"]["posted_date"] == d
    assert txs["REFUND"]["kind"] == "add" and txs["REFUND"]["amount_cents"] == -500
    # a bad row rolls back the whole import
    bad = c.post("/api/import/commit", headers=H, json={"rows": [
        {"date": d, "description": "ok", "amount_cents": 100, "category_id": 1, "ym": ym},
        {"date": d, "description": "bad", "amount_cents": 100, "category_id": 999, "ym": ym}]})
    assert bad.status_code == 400
    assert "ok" not in {t["description"] for t in c.get(f"/api/transactions?ym={ym}").json()}


def test_migrate_legacy(env):
    data, _ = env
    legacy = data / "old.db"
    conn = core.connect(legacy)
    core.init_db(conn)
    conn.execute("INSERT INTO users(id, username, display_name, password_hash) VALUES (7, 'sam', 'Sam', ?)",
                 (accounts.hash_password(PW),))
    conn.execute("INSERT INTO categories(id, name, sort, default_budget_cents) VALUES (1, 'Food', 1, 1000)")
    core.ensure_months(conn)
    ym = core.open_months(conn)[-1]
    core.add_entry(conn, ym=ym, category_id=1, amount_cents=250, description="old entry", user_id=7)
    conn.close()
    dest = data / "new"
    hid = accounts.migrate_legacy(legacy, dest, "Sam & Alex")
    with pytest.raises(accounts.AccountError):
        accounts.migrate_legacy(legacy, dest, "again")
    main.DATA_DIR = dest  # restored by the fixture's monkeypatch
    c = client_for("sam")
    st = c.get("/api/state").json()
    assert st["household"] == {"id": hid, "name": "Sam & Alex", "setup_done": True, "currency": "USD"}
    tx = c.get(f"/api/transactions?q=old").json()[0]
    assert tx["entered_by_name"] == "Sam" and tx["amount_cents"] == 250
    h = core.connect(accounts.household_path(dest, hid))
    assert h.execute("SELECT password_hash FROM users").fetchone()[0] == ""  # no password copies left behind


def test_unconfirmed_signups_expire(env, monkeypatch):
    data, sent = env
    monkeypatch.setattr(main, "SIGNUP_OPEN", True)
    c = TestClient(main.app)
    c.post("/api/signup", headers=H, json={"name": "Spam", "email": "spam@example.com", "password": "a long passphrase"})
    acct = accounts.open_accounts(data)
    hid = acct.execute("SELECT household_id FROM users WHERE email='spam@example.com'").fetchone()[0]
    acct.execute("UPDATE users SET created_at=datetime('now','-8 days')")
    acct.close()
    c.post("/api/signup", headers=H, json={"name": "Real", "email": "real@example.com", "password": "a long passphrase"})
    a = main._acct
    assert a.execute("SELECT count(*) FROM users WHERE email='spam@example.com'").fetchone()[0] == 0
    assert a.execute("SELECT count(*) FROM households WHERE id=?", (hid,)).fetchone()[0] == 0
    assert not accounts.household_path(data, hid).exists()
    new_hid = a.execute("SELECT household_id FROM users WHERE email='real@example.com'").fetchone()[0]
    assert new_hid > hid  # ids are never reused


def test_email_links_need_base_url(env, monkeypatch):
    data, _ = env
    monkeypatch.setenv("BUDGET_SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(main, "BASE_URL", "")
    make_household(data, "H", "sam")
    p = client_for("sam")
    r = p.post("/api/household/invites", headers=H, json={})
    assert r.status_code == 500 and "BUDGET_BASE_URL" in r.json()["detail"]
    monkeypatch.setattr(main, "BASE_URL", "https://budget.example.com")
    assert p.post("/api/household/invites", headers=H, json={}).json()["link"].startswith("https://budget.example.com/#/join?token=")


def test_old_accounts_db_gets_autoincrement(env):
    data, _ = env
    import sqlite3
    old = sqlite3.connect(accounts.accounts_path(data))
    old.executescript("""CREATE TABLE households (id INTEGER PRIMARY KEY, name TEXT NOT NULL, setup_done INTEGER NOT NULL DEFAULT 0,
                           created_at TEXT NOT NULL DEFAULT (datetime('now')));
                         INSERT INTO households(id, name, setup_done) VALUES (1, 'Old', 1);""")
    old.close()
    conn = accounts.open_accounts(data)
    assert "AUTOINCREMENT" in conn.execute("SELECT sql FROM sqlite_master WHERE name='households'").fetchone()[0]
    assert conn.execute("SELECT name FROM households WHERE id=1").fetchone()[0] == "Old"
    conn.execute("DELETE FROM households WHERE id=1")
    assert conn.execute("INSERT INTO households(name) VALUES ('New')").lastrowid == 2


def test_security_hardening(env):
    data, _ = env
    make_household(data, "H", "sam")
    anon = TestClient(main.app)
    # login needs the anti-forgery header like every other write
    assert anon.post("/api/login", json={"username": "sam", "password": PW}).status_code == 403
    # spread-out guessing: per-account limit holds even if every try comes from a new IP
    for i in range(20):
        main._hits.pop(f"login|testclient|sam", None); main._hits.pop("login-ip|testclient", None)
        anon.post("/api/login", headers=H, json={"username": "sam", "password": f"wrong{i}"})
    main._hits.pop("login|testclient|sam", None); main._hits.pop("login-ip|testclient", None)
    assert anon.post("/api/login", headers=H, json={"username": "sam", "password": PW}).status_code == 429
    main._hits.clear()
    p = client_for("sam")
    # streamed body with no length is refused
    r = p.post("/api/entries", headers={**H, "transfer-encoding": "chunked"}, content=iter([b"{}"]))
    assert r.status_code == 411
    # CSV export neutralizes formulas
    ym = p.get("/api/state").json()["summary"]["ym"]
    p.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 100,
                                            "description": '=HYPERLINK("http://evil","x")', "notes": "+1+1"})
    csv = p.get("/api/export.csv").text
    assert "'=HYPERLINK" in csv and "'+1+1" in csv
    # extra headers
    h = p.get("/api/state").headers
    assert h["cross-origin-opener-policy"] == "same-origin" and "camera=()" in h["permissions-policy"]
    # sign out other devices
    other = client_for("sam")
    assert p.post("/api/account/logout-others", headers=H).json()["signed_out"] >= 1
    assert other.get("/api/state").status_code == 401 and p.get("/api/state").status_code == 200


def test_secure_cookie_over_https(env):
    data, _ = env
    make_household(data, "H", "sam")
    c = TestClient(main.app, base_url="https://budget.example.com")
    r = c.post("/api/login", headers=H, json={"username": "sam", "password": PW})
    assert "secure" in r.headers["set-cookie"].lower()
    assert "strict-transport-security" in r.headers
    plain = TestClient(main.app).post("/api/login", headers=H, json={"username": "sam", "password": PW})
    assert "secure" not in plain.headers["set-cookie"].lower()


def test_first_run_needs_setup_code(env):
    data, _ = env
    c = TestClient(main.app)
    cfg = c.get("/api/config").json()            # opening the accounts db generates the code
    assert cfg["first_run"] is True
    code = main._setup_code
    body = {"code": "WRONG-CODE", "name": "Admin", "login": "admin", "password": "admin passphrase", "household": "Home"}
    assert c.post("/api/first-run", headers=H, json=body).status_code == 400
    assert c.post("/api/first-run", json={**body, "code": code}).status_code == 403   # header required
    assert c.post("/api/first-run", headers=H, json={**body, "code": code.lower()}).status_code == 200
    st = c.get("/api/state").json()
    assert st["user"]["role"] == "owner" and st["household"]["name"] == "Home"
    assert c.get("/api/config").json()["first_run"] is False
    assert TestClient(main.app).post("/api/first-run", headers=H, json={**body, "code": code}).status_code == 403


def test_first_run_claims_orphan_household(env):
    data, _ = env
    acct = accounts.open_accounts(data)
    hid = accounts.create_household(acct, data, "Sam & Alex", setup_done=True)   # moved over, no logins
    acct.close()
    c = TestClient(main.app)
    c.get("/api/config")
    r = c.post("/api/first-run", headers=H, json={"code": main._setup_code, "name": "Sam", "login": "sam",
                                                  "password": "sams passphrase"})
    assert r.status_code == 200
    assert c.get("/api/state").json()["household"] == {"id": hid, "name": "Sam & Alex", "setup_done": True, "currency": "USD"}


def test_theme_currency_backup_health(env):
    data, _ = env
    make_household(data, "H", "sam")
    p = client_for("sam")
    assert p.get("/api/health").json()["ok"] is True
    assert p.patch("/api/account", headers=H, json={"theme": "noir", "accent": "violet"}).status_code == 200
    assert p.patch("/api/account", headers=H, json={"theme": "neon"}).status_code == 422
    st = p.get("/api/state").json()
    assert st["user"]["theme"] == "noir" and st["user"]["accent"] == "violet"
    assert p.patch("/api/household", headers=H, json={"currency": "eur"}).status_code == 422
    p.patch("/api/household", headers=H, json={"currency": "EUR"})
    assert p.get("/api/state").json()["household"]["currency"] == "EUR"
    r = p.get("/api/household/backup")
    assert r.status_code == 200 and r.content[:16] == b"SQLite format 3\x00"
    out, n, _ = accounts.backup_all(data, data / "backups", keep=2)
    assert n == 2 and (out / "accounts.db").exists()
