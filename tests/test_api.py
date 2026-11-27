import pytest
from fastapi.testclient import TestClient

from app import accounts, core, main

H = {"X-Budget": "1"}


def reset_app(monkeypatch, data_dir):
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "_acct", None)
    monkeypatch.setattr(main, "_households", {})
    monkeypatch.setattr(main, "_synced", set())
    monkeypatch.setattr(main, "BACKUP_KEEP", 0)
    monkeypatch.setattr(main, "_setup_code", None)
    main._hits.clear()


def make_household(data_dir, name, username, password="correct horse battery", hid_cats=True):
    acct = accounts.open_accounts(data_dir)
    hid = accounts.create_household(acct, data_dir, name, setup_done=True)
    uid = acct.execute("INSERT INTO users(username, display_name, password_hash, household_id, role) VALUES (?,?,?,?,'owner')",
                       (username, username.title(), accounts.hash_password(password), hid)).lastrowid
    acct.close()
    conn = accounts.open_household(data_dir, hid)
    if hid_cats:
        conn.execute("INSERT INTO categories(id, name, grp, sort, default_budget_cents) VALUES (1,'Food/Drink','Needs',1,120000)")
        conn.execute("INSERT INTO categories(id, name, grp, sort, default_budget_cents) VALUES (2,'Misc','Fun',2,44000)")
        conn.execute("INSERT INTO income(name, amount_cents) VALUES ('work', 500000)")
    conn.close()
    return hid, uid


@pytest.fixture
def client(tmp_path, monkeypatch):
    reset_app(monkeypatch, tmp_path)
    make_household(tmp_path, "Sam & Alex", "sam")
    c = TestClient(main.app)
    yield c
    for conn in list(main._households.values()) + [main._acct]:
        if conn:
            conn.close()


def login(c):
    r = c.post("/api/login", headers=H, json={"username": "sam", "password": "correct horse battery"})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()


def test_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/entries", json={}, headers=H).status_code == 401


def test_wrong_password_and_throttle(client):
    for _ in range(5):
        assert client.post("/api/login", headers=H, json={"username": "sam", "password": "nope"}).status_code == 401
    r = client.post("/api/login", headers=H, json={"username": "sam", "password": "correct horse battery"})
    assert r.status_code == 429


def test_writes_need_custom_header(client):
    login(client)
    st = client.get("/api/state").json()
    ym = st["summary"]["ym"]
    body = {"ym": ym, "category_id": 1, "amount_cents": 1234, "description": "costco"}
    assert client.post("/api/entries", json=body).status_code == 403
    assert client.post("/api/entries", json=body, headers=H).status_code == 200


def test_full_flow(client):
    login(client)
    st = client.get("/api/state").json()
    ym = st["summary"]["ym"]
    assert st["summary"]["unassigned"] == 500000 - 164000

    r = client.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 5000,
                                                     "description": "grocer", "intent": "need"})
    tid = r.json()["id"]
    client.post("/api/moves", headers=H, json={"ym": ym, "from_id": 2, "to_id": 1, "amount_cents": 1000})
    client.post("/api/entries", headers=H, json={"kind": "add", "ym": ym, "category_id": 2, "amount_cents": 40000,
                                                 "description": "gift"})
    cats = {c["name"]: c for c in client.get(f"/api/state?ym={ym}").json()["summary"]["categories"]}
    assert cats["Food/Drink"]["left"] == 120000 - 5000 + 1000
    assert cats["Misc"]["left"] == 44000 - 1000 + 40000

    assert client.patch(f"/api/entries/{tid}", headers=H, json={"amount_cents": 6000}).status_code == 200
    assert client.get(f"/api/entries/{tid}").json()["amount_cents"] == 6000

    txs = client.get(f"/api/transactions?ym={ym}").json()
    assert len(txs) == 4
    assert client.get("/api/suggestions").json()[0]["category_id"] in (1, 2)

    # tags + tracking-only record + CSV
    tag = client.post("/api/tags", headers=H, json={"name": "Medical", "kind": "tax"}).json()["id"]
    client.post("/api/records", headers=H, json={"amount_cents": 12500, "description": "clinic", "tag_ids": [tag],
                                                 "date": "2026-03-02"})
    client.patch(f"/api/entries/{tid}", headers=H, json={"tag_ids": [tag], "reimbursed": True})
    t = next(x for x in client.get("/api/tags").json() if x["id"] == tag)
    assert t["total"] == 12500 + 6000 and t["n"] == 2
    csv = client.get(f"/api/tags/{tag}/export.csv").text
    assert "clinic" in csv and "125.00" in csv

    # month budget override for this month only
    client.put(f"/api/months/{ym}/budgets/1", headers=H, json={"amount_cents": 100000})
    cats = {c["name"]: c for c in client.get(f"/api/state?ym={ym}").json()["summary"]["categories"]}
    assert cats["Food/Drink"]["budget"] == 100000

    # a month that hasn't started can't be closed
    nxt = f"{int(ym[:4]) + (ym[5:] == '12')}-{int(ym[5:]) % 12 + 1:02d}"
    assert client.post(f"/api/months/{nxt}/close", headers=H).status_code == 400


def test_security_headers_and_spa(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/api/nope").status_code in (401, 404)


def test_tracker_years_and_delete(client):
    login(client)
    ym = client.get("/api/state").json()["summary"]["ym"]
    tag = client.post("/api/tags", headers=H, json={"name": "House repairs", "kind": "tax"}).json()["id"]
    other = client.post("/api/tags", headers=H, json={"name": "Remodel"}).json()["id"]
    # records from different years (the app also stamps each record with the current year)
    for d, amt in [("2023-05-01", 100), ("2024-06-01", 200), ("2025-07-01", 300)]:
        client.post("/api/records", headers=H, json={"amount_cents": amt, "date": d, "tag_ids": [tag]})
    shared = client.post("/api/records", headers=H, json={"amount_cents": 50, "date": "2025-01-01",
                                                          "tag_ids": [tag, other]}).json()["id"]
    spend = client.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 999,
                                                         "description": "lowes", "tag_ids": [tag]}).json()["id"]
    t = next(x for x in client.get("/api/tags").json() if x["id"] == tag)
    assert {y["year"]: y["total"] for y in t["years"]} == {"2023": 100, "2024": 200, "2025": 350, ym[:4]: 999}

    # close (archive) and reopen
    client.put(f"/api/tags/{tag}", headers=H, json={"name": "House repairs", "kind": "tax", "archived": True})
    assert next(x for x in client.get("/api/tags").json() if x["id"] == tag)["archived"]

    # delete: its own records go, shared record and envelope purchase stay
    assert client.delete(f"/api/tags/{tag}").status_code == 403  # needs X-Budget header
    r = client.delete(f"/api/tags/{tag}", headers=H).json()
    assert r["records_deleted"] == 3
    assert all(x["id"] != tag for x in client.get("/api/tags").json())
    assert client.get(f"/api/entries/{shared}").status_code == 200
    assert client.get(f"/api/entries/{spend}").json()["amount_cents"] == 999
    assert next(x for x in client.get("/api/tags").json() if x["id"] == other)["total"] == 50
    assert client.delete(f"/api/tags/{tag}", headers=H).status_code == 404


def test_reports(client):
    login(client)
    ym = client.get("/api/state").json()["summary"]["ym"]
    client.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 5000, "description": "grocer", "intent": "need"})
    client.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 700, "description": "x"})
    client.post("/api/entries", headers=H, json={"kind": "add", "ym": ym, "category_id": 2, "amount_cents": 40000, "description": "gift"})
    client.post("/api/moves", headers=H, json={"ym": ym, "from_id": 2, "to_id": 1, "amount_cents": 1000})
    r = client.get(f"/api/reports?start={ym}&end={ym}").json()
    rows = {x["category_id"]: x for x in r["rows"]}
    assert rows[1] == {"ym": ym, "category_id": 1, "budget": 120000, "spent": 5700, "added": 0}  # move not counted
    assert rows[2]["spent"] == 0 and rows[2]["added"] == 40000
    assert {x["intent"]: x["spent"] for x in r["intents"]} == {"need": 5000, "": 700}
    assert client.get(f"/api/reports?start={ym}&end=2001-01").status_code == 400
    assert client.get("/api/reports?start=nope&end=2001-01").status_code == 400


def test_batch_entries(client):
    login(client)
    ym = client.get("/api/state").json()["summary"]["ym"]
    rows = [{"ym": ym, "category_id": 1, "amount_cents": 4210, "description": "costco"},
            {"ym": ym, "category_id": 1, "amount_cents": 1899, "description": "grocer", "date": f"{ym}-02"},
            {"ym": ym, "category_id": 2, "amount_cents": -40000, "description": "gift"}]
    r = client.post("/api/entries/batch", headers=H, json={"rows": rows})
    assert len(r.json()["ids"]) == 3
    cats = {c["name"]: c for c in client.get(f"/api/state?ym={ym}").json()["summary"]["categories"]}
    assert cats["Food/Drink"]["spent"] == 4210 + 1899 and cats["Misc"]["spent"] == -40000
    txs = {t["description"]: t for t in client.get(f"/api/transactions?ym={ym}").json()}
    assert txs["gift"]["kind"] == "add" and txs["grocer"]["date"] == f"{ym}-02" and txs["costco"]["date"] is None
    # one bad row -> nothing saved, error names the row
    bad = client.post("/api/entries/batch", headers=H, json={"rows": [
        {"ym": ym, "category_id": 1, "amount_cents": 100, "description": "fine"},
        {"ym": ym, "category_id": 99, "amount_cents": 100, "description": "bad"}]})
    assert bad.status_code == 400 and bad.json()["detail"].startswith("Row 2")
    assert "fine" not in {t["description"] for t in client.get(f"/api/transactions?ym={ym}").json()}


def test_goal_progress_math():
    g = core.goal_progress
    # no date: how long at the current budget
    r = g(balance=50000, budget=25000, goal_cents=200000, goal_ym=None, ym="2026-11")
    assert (r["status"], r["need"], r["eta_ym"], r["pct"]) == ("open", 150000, "2027-05", 0.25)
    # dated: months after the viewed month count; per month rounds up to the cent
    r = g(balance=0, budget=10000, goal_cents=100000, goal_ym="2027-02", ym="2026-11")
    assert (r["months_left"], r["per_month"], r["status"]) == (3, 33334, "behind")
    assert g(balance=0, budget=40000, goal_cents=100000, goal_ym="2027-02", ym="2026-11")["status"] == "on_track"
    # goal month reached and still short; reached goal
    assert g(balance=10, budget=0, goal_cents=100, goal_ym="2026-11", ym="2026-11")["status"] == "due"
    r = g(balance=300, budget=0, goal_cents=100, goal_ym="2026-11", ym="2026-11")
    assert (r["status"], r["need"], r["pct"]) == ("done", 0, 1.0)
    # a negative bank never shows negative progress
    assert g(balance=-500, budget=0, goal_cents=1000, goal_ym=None, ym="2026-11")["pct"] == 0.0


def test_savings_goal_api(client):
    login(client)
    ym = client.get("/api/state").json()["summary"]["ym"]
    goal_ym = core.add_months(ym, 4)
    assert client.patch("/api/categories/2", headers=H, json={"goal_cents": 500000, "goal_ym": goal_ym}).status_code == 200
    st = client.get("/api/state").json()
    misc = next(c for c in st["summary"]["categories"] if c["id"] == 2)
    # Misc budgets 440.00 a month, so it holds 440.00 now and needs 4,560.00 over 4 more months
    assert misc["goal"]["need"] == 500000 - 44000 and misc["goal"]["months_left"] == 4
    assert misc["goal"]["per_month"] == 114000 and misc["goal"]["status"] == "behind"
    assert next(c for c in st["categories"] if c["id"] == 2)["goal_ym"] == goal_ym
    food = next(c for c in st["summary"]["categories"] if c["id"] == 1)
    assert food["goal"] is None
    # bad month and non-positive amounts are rejected
    assert client.patch("/api/categories/2", headers=H, json={"goal_ym": "2027-13"}).status_code == 400
    assert client.patch("/api/categories/2", headers=H, json={"goal_cents": 0}).status_code == 422
    # date only can be cleared; removing the amount removes the whole goal
    client.patch("/api/categories/2", headers=H, json={"goal_ym": None})
    assert next(c for c in client.get("/api/state").json()["categories"] if c["id"] == 2)["goal_ym"] is None
    client.patch("/api/categories/2", headers=H, json={"goal_cents": None})
    misc = next(c for c in client.get("/api/state").json()["summary"]["categories"] if c["id"] == 2)
    assert misc["goal"] is None


def test_goal_columns_added_to_older_databases(tmp_path):
    conn = core.connect(tmp_path / "old.db")
    old = core.SCHEMA.read_text(encoding="utf-8").replace(
        "    is_investment        INTEGER NOT NULL DEFAULT 0,  -- reports count it as invested, not spent\n"
        "    goal_cents           INTEGER,           -- savings goal: target bank balance (NULL = no goal)\n"
        "    goal_ym              TEXT               -- optional target month 'YYYY-MM' for the goal\n",
        "    is_investment        INTEGER NOT NULL DEFAULT 0\n")
    assert "goal_cents" not in old
    conn.executescript(old)
    core.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
    assert {"goal_cents", "goal_ym"} <= cols
    conn.close()


# ---------------------------------------------------------------- two-step login

from app import totp  # noqa: E402

PW = "correct horse battery"


def _turn_on_2fa(c):
    assert c.post("/api/account/2fa/start", headers=H, json={"password": "wrong"}).status_code == 400
    r = c.post("/api/account/2fa/start", headers=H, json={"password": PW}).json()
    assert r["qr"].startswith("data:image/svg+xml") and r["uri"].startswith("otpauth://totp/Banktab%3Asam?")
    secret = r["secret"]
    assert c.post("/api/account/2fa/enable", headers=H, json={"code": "000000"}).status_code == 400
    step = totp.current_step()
    codes = c.post("/api/account/2fa/enable", headers=H, json={"code": totp.code_at(secret, step)}).json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    return secret, step, codes


def test_two_step_login(client):
    login(client)
    other = TestClient(main.app)
    login(other)  # a second device, signed in before 2FA was turned on
    secret, step, codes = _turn_on_2fa(client)
    assert client.get("/api/state").json()["user"]["twofa"] is True
    assert other.get("/api/state").status_code == 401           # other devices were signed out
    assert client.get("/api/account/2fa").json()["recovery_left"] == 10

    # password alone no longer logs in
    fresh = TestClient(main.app)
    r = fresh.post("/api/login", headers=H, json={"username": "sam", "password": PW}).json()
    assert r["twofa"] is True and "ticket" in r
    assert fresh.get("/api/state").status_code == 401
    # the code used to turn it on can't be replayed
    bad = fresh.post("/api/login/2fa", headers=H, json={"ticket": r["ticket"], "code": totp.code_at(secret, step)})
    assert bad.status_code == 401
    ok = fresh.post("/api/login/2fa", headers=H, json={"ticket": r["ticket"], "code": totp.code_at(secret, step + 1)})
    assert ok.status_code == 200 and fresh.get("/api/state").status_code == 200
    # a ticket works once
    assert fresh.post("/api/login/2fa", headers=H, json={"ticket": r["ticket"], "code": "123456"}).status_code == 401

    # recovery codes: any formatting, one use each
    c2 = TestClient(main.app)
    t = c2.post("/api/login", headers=H, json={"username": "sam", "password": PW}).json()["ticket"]
    r = c2.post("/api/login/2fa", headers=H, json={"ticket": t, "code": " " + codes[0].upper().replace("-", " ") + " "})
    assert r.status_code == 200 and r.json()["recovery_left"] == 9
    c3 = TestClient(main.app)
    t = c3.post("/api/login", headers=H, json={"username": "sam", "password": PW}).json()["ticket"]
    assert c3.post("/api/login/2fa", headers=H, json={"ticket": t, "code": codes[0]}).status_code == 401


def test_two_step_wrong_codes_end_the_attempt(client):
    login(client)
    _turn_on_2fa(client)
    c = TestClient(main.app)
    t = c.post("/api/login", headers=H, json={"username": "sam", "password": PW}).json()["ticket"]
    for i in range(4):
        r = c.post("/api/login/2fa", headers=H, json={"ticket": t, "code": "000000"})
        assert r.status_code == 401 and "isn't right" in r.json()["detail"]
    r = c.post("/api/login/2fa", headers=H, json={"ticket": t, "code": "000000"})
    assert "Log in again" in r.json()["detail"]
    assert c.post("/api/login/2fa", headers=H, json={"ticket": t, "code": "000000"}).json()["detail"].startswith("That took too long")


def test_reset_link_does_not_skip_two_step(client, tmp_path):
    login(client)
    _turn_on_2fa(client)
    conn = accounts.open_accounts(tmp_path)
    uid = conn.execute("SELECT id FROM users WHERE username='sam'").fetchone()[0]
    token = accounts.make_token(conn, "reset", user_id=uid)
    conn.close()
    c = TestClient(main.app)
    r = c.post("/api/reset", headers=H, json={"token": token, "password": "a brand new passphrase"})
    assert r.json() == {"ok": True, "login": True}
    assert c.get("/api/state").status_code == 401
    assert c.post("/api/login", headers=H, json={"username": "sam", "password": "a brand new passphrase"}).json()["twofa"] is True


def test_turn_off_and_new_codes_need_password_and_code(client):
    login(client)
    secret, step, codes = _turn_on_2fa(client)
    assert client.post("/api/account/2fa/disable", headers=H, json={"password": PW, "code": "000000"}).status_code == 400
    assert client.post("/api/account/2fa/disable", headers=H, json={"password": "nope", "code": codes[1]}).status_code == 400
    new = client.post("/api/account/2fa/recovery-codes", headers=H, json={"password": PW, "code": codes[1]}).json()["recovery_codes"]
    assert len(new) == 10 and codes[2] not in new
    assert client.post("/api/account/2fa/disable", headers=H, json={"password": PW, "code": codes[2]}).status_code == 400  # old set is gone
    assert client.post("/api/account/2fa/disable", headers=H, json={"password": PW, "code": new[0]}).status_code == 200
    assert client.get("/api/state").json()["user"]["twofa"] is False
    assert client.post("/api/login", headers=H, json={"username": "sam", "password": PW}).json() == {"ok": True}


def test_totp_rfc6238_vectors():
    import base64
    s = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    for t, want in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
        assert totp.code_at(s, t // 30) == want
        assert totp.verify(s, want, now=t) == t // 30
        assert totp.verify(s, want, now=t, last_step=t // 30) is None


# ---------------------------------------------------------------- recurring bills (tap to log)

def test_parse_renews_and_due_rules():
    assert core.parse_renews("Dec") == (12, None)
    assert core.parse_renews("aug 31st") == (8, 31)
    assert core.parse_renews("sept") == (9, None)
    assert core.parse_renews("April") == (4, None)
    assert core.parse_renews("the 15th") == (None, 15)
    assert core.parse_renews("") == (None, None)
    b = lambda f, m=None, d=None: {"frequency": f, "due_month": m, "due_day": d}
    assert core.bill_due_in(b("monthly"), "2026-11") is True
    assert core.bill_due_in(b("yearly"), "2026-11") is None                      # month unknown
    assert core.bill_due_in(b("yearly", 11), "2026-11") and not core.bill_due_in(b("yearly", 12), "2026-11")
    assert [m for m in range(1, 13) if core.bill_due_in(b("quarterly", 2), f"2026-{m:02d}")] == [2, 5, 8, 11]
    assert core.bill_due_date(b("monthly", None, 31), "2027-02") == "2027-02-28"
    assert core.bill_due_date(b("monthly"), "2026-11") == "2026-11-01"


def test_log_skip_and_due_list(client):
    login(client)
    ym = client.get("/api/state").json()["summary"]["ym"]
    m = int(ym[5:])
    add = lambda **b: client.post("/api/bills", headers=H, json={"renews": "", "notes": "", "active": True, **b}).json()["id"]
    phone = add(name="Phone", amount_cents=6000, frequency="monthly", category_id=1, due_day=15)
    yearly = add(name="Domain", amount_cents=1200, frequency="yearly", category_id=2, due_month=m)
    other = add(name="Insurance", amount_cents=50000, frequency="yearly", category_id=2, due_month=m % 12 + 1)
    unknown = add(name="Mystery", amount_cents=900, frequency="quarterly", category_id=2)
    nocat = add(name="Gym", amount_cents=3000, frequency="monthly", due_day=3)

    d = client.get(f"/api/bills/due?ym={ym}").json()
    assert [x["name"] for x in d["due"]] == ["Gym", "Phone", "Domain"]          # by due day, unset last
    assert d["needs_month"] == [{"id": unknown, "name": "Mystery", "frequency": "quarterly"}]
    assert all(x["logged"] is None and not x["skipped"] for x in d["due"])
    assert next(x for x in d["due"] if x["name"] == "Phone")["due_date"] == f"{ym}-15"

    # log with an edited amount; it becomes a normal spend entry linked to the bill
    r = client.post(f"/api/bills/{phone}/log", headers=H, json={"ym": ym, "amount_cents": 6150})
    assert r.status_code == 200
    tx = client.get(f"/api/entries/{r.json()['id']}").json()
    assert (tx["description"], tx["amount_cents"], tx["category_id"], tx["date"]) == ("Phone", 6150, 1, f"{ym}-15")
    assert client.post(f"/api/bills/{phone}/log", headers=H, json={"ym": ym, "amount_cents": 6000}).status_code == 400
    # a bill without a category needs one picked
    assert client.post(f"/api/bills/{nocat}/log", headers=H, json={"ym": ym, "amount_cents": 3000}).status_code == 400
    assert client.post(f"/api/bills/{nocat}/log", headers=H, json={"ym": ym, "amount_cents": 3000, "category_id": 2}).status_code == 200
    # skip and un-skip
    client.post(f"/api/bills/{yearly}/skip", headers=H, json={"ym": ym})
    d = {x["name"]: x for x in client.get(f"/api/bills/due?ym={ym}").json()["due"]}
    assert d["Phone"]["logged"]["logged_cents"] == 6150 and d["Domain"]["skipped"] is True
    client.post(f"/api/bills/{yearly}/skip", headers=H, json={"ym": ym, "skip": False})
    assert {x["name"]: x for x in client.get(f"/api/bills/due?ym={ym}").json()["due"]}["Domain"]["skipped"] is False
    # deleting the entry makes the bill due again; deleting the bill keeps the entry
    client.delete(f"/api/entries/{r.json()['id']}", headers=H)
    assert {x["name"]: x for x in client.get(f"/api/bills/due?ym={ym}").json()["due"]}["Phone"]["logged"] is None
    gym_tx = {x["name"]: x for x in client.get(f"/api/bills/due?ym={ym}").json()["due"]}["Gym"]["logged"]["tx_id"]
    client.delete(f"/api/bills/{nocat}", headers=H)
    assert client.get(f"/api/entries/{gym_tx}").json()["bill_id"] is None
    assert other not in [x["id"] for x in client.get(f"/api/bills/due?ym={ym}").json()["due"]]


def test_bill_columns_added_to_older_databases(tmp_path):
    conn = core.connect(tmp_path / "old.db")
    s = core.SCHEMA.read_text(encoding="utf-8")
    s = s.replace("    bill_id      INTEGER REFERENCES bills(id) ON DELETE SET NULL,  -- logged from the bills' due list\n", "")
    s = s.replace("    active       INTEGER NOT NULL DEFAULT 1,\n    due_day      INTEGER CHECK (due_day BETWEEN 1 AND 31),    -- day of the month it's paid (optional)\n"
                  "    due_month    INTEGER CHECK (due_month BETWEEN 1 AND 12)   -- quarterly/yearly: a month it's due in\n",
                  "    active       INTEGER NOT NULL DEFAULT 1\n")
    assert "due_day      INTEGER" not in s and "bill_id      INTEGER" not in s
    conn.executescript(s)
    conn.execute("INSERT INTO bills(name, amount_cents, frequency, renews) VALUES ('Domain', 1200, 'yearly', 'aug 31st')")
    core.init_db(conn)
    assert {"bill_id"} <= {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    assert tuple(conn.execute("SELECT due_month, due_day FROM bills").fetchone()) == (8, 31)
    conn.close()


# ---------------------------------------------------------------- display keys (kitchen dashboard)
def _display_key(hid=1, name="Kitchen"):
    key = "bt_disp_testkey"
    main._acct.execute("INSERT INTO display_keys(token_hash, household_id, name) VALUES (?,?,?)",
                       (main.sha(key), hid, name))
    return key


def test_display_summary_needs_a_valid_key(client):
    client.get("/api/health")                                   # opens the accounts db
    assert client.get("/api/display/summary").status_code == 401
    assert client.get("/api/display/summary", headers={"Authorization": "Bearer nope"}).status_code == 401
    # a login session is not enough: this endpoint only takes display keys
    login(client)
    assert client.get("/api/display/summary").status_code == 401


def test_display_summary_shows_totals_only(client):
    login(client)
    ym = client.get("/api/state").json()["summary"]["ym"]
    client.post("/api/entries", headers=H, json={"ym": ym, "category_id": 1, "amount_cents": 125050,
                                                 "description": "secret store name"})
    key = _display_key()
    client.cookies.clear()
    r = client.get("/api/display/summary", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"ym", "month", "budgeted", "spent", "left", "percent_spent", "days_left", "over_budget"}
    assert d["ym"] == ym and d["spent"] == 1250.50
    assert d["left"] == round(d["budgeted"] - d["spent"], 2)
    assert "Food/Drink" in d["over_budget"]                     # 1,250.50 spent of 1,200 budgeted
    assert "secret store name" not in r.text                    # no transaction details leak
    assert 1 <= d["days_left"] <= 31
    row = main._acct.execute("SELECT last_used_at FROM display_keys").fetchone()
    assert row["last_used_at"] is not None


def test_display_key_bad_tries_are_rate_limited(client):
    client.get("/api/health")
    for _ in range(30):
        client.get("/api/display/summary", headers={"Authorization": "Bearer wrong"})
    assert client.get("/api/display/summary", headers={"Authorization": "Bearer wrong"}).status_code == 429
