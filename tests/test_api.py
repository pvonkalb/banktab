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
