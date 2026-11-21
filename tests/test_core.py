import datetime as dt

import pytest

from app import core


@pytest.fixture
def conn(tmp_path):
    c = core.connect(tmp_path / "t.db")
    core.init_db(c)
    c.execute("INSERT INTO categories(id, name, grp, sort, default_budget_cents) VALUES (1,'Food','Needs',1,100000)")
    c.execute("INSERT INTO categories(id, name, grp, sort, default_budget_cents) VALUES (2,'Misc','Fun',2,20000)")
    c.execute("INSERT INTO categories(id, name, sort, budget_from_bills) VALUES (3,'Subs',3,1)")
    return c


def bal(conn, ym, name):
    return next(c for c in core.month_summary(conn, ym)["categories"] if c["name"] == name)


def test_rollover_and_spending(conn):
    core.ensure_months(conn, "2026-01")
    core.ensure_months(conn, "2026-02")
    core.add_entry(conn, ym="2026-01", category_id=1, amount_cents=30000, description="costco")
    core.add_entry(conn, ym="2026-02", category_id=1, amount_cents=150000, description="big shop")
    jan, feb = bal(conn, "2026-01", "Food"), bal(conn, "2026-02", "Food")
    assert (jan["left"], jan["balance"]) == (70000, 70000)
    assert (feb["left"], feb["balance"]) == (-50000, 20000)


def test_add_money_and_move(conn):
    core.ensure_months(conn, "2026-01")
    core.add_entry(conn, ym="2026-01", category_id=2, amount_cents=40000, kind="add", description="gift")
    assert bal(conn, "2026-01", "Misc")["balance"] == 60000
    mid = core.add_move(conn, ym="2026-01", from_id=2, to_id=1, amount_cents=10000)
    assert bal(conn, "2026-01", "Misc")["balance"] == 50000
    assert bal(conn, "2026-01", "Food")["balance"] == 110000
    # editing one half edits both
    half = conn.execute("SELECT id FROM transactions WHERE move_id=? AND amount_cents<0", (mid,)).fetchone()[0]
    core.update_entry(conn, half, amount_cents=5000)
    assert bal(conn, "2026-01", "Misc")["balance"] == 55000
    assert bal(conn, "2026-01", "Food")["balance"] == 105000
    core.delete_entry(conn, half)
    assert conn.execute("SELECT count(*) FROM transactions WHERE move_id=?", (mid,)).fetchone()[0] == 0


def test_closed_month_is_locked_and_defaults_dont_rewrite_it(conn):
    core.ensure_months(conn, "2026-01")
    core.ensure_months(conn, "2026-02")
    tid = core.add_entry(conn, ym="2026-01", category_id=1, amount_cents=100)
    core.close_month(conn, "2026-01", None)
    with pytest.raises(core.BudgetError):
        core.add_entry(conn, ym="2026-01", category_id=1, amount_cents=100)
    with pytest.raises(core.BudgetError):
        core.update_entry(conn, tid, amount_cents=999)
    with pytest.raises(core.BudgetError):
        core.delete_entry(conn, tid)
    with pytest.raises(core.BudgetError):  # can't move an entry into a closed month
        t2 = core.add_entry(conn, ym="2026-02", category_id=1, amount_cents=100)
        core.update_entry(conn, t2, ym="2026-01")
    core.set_default_budget(conn, 1, 50000)
    assert bal(conn, "2026-01", "Food")["budget"] == 100000
    assert bal(conn, "2026-02", "Food")["budget"] == 50000


def test_close_order_and_reopen(conn):
    for ym in ("2026-01", "2026-02", "2026-03"):
        core.ensure_months(conn, ym)
    with pytest.raises(core.BudgetError, match="first"):
        core.close_month(conn, "2026-02", None)
    core.close_month(conn, "2026-01", None)
    core.close_month(conn, "2026-02", None)
    with pytest.raises(core.BudgetError, match="first"):
        core.reopen_month(conn, "2026-01")
    core.reopen_month(conn, "2026-02")
    assert core.month_status(conn, "2026-02") == "open"


def test_close_current_month_early_opens_next(conn):
    this = core.ym_of(dt.date.today())
    nxt = core.next_ym(this)
    core.ensure_months(conn, this)
    for ym in core.open_months(conn)[:-1]:
        core.close_month(conn, ym, None)
    core.close_month(conn, this, None)  # e.g. on the 29th
    assert core.month_status(conn, this) == "closed"
    assert core.open_months(conn) == [nxt]
    # next month got its budgets, and can't itself be closed before it starts
    assert conn.execute("SELECT count(*) FROM month_budgets WHERE ym=?", (nxt,)).fetchone()[0] > 0
    with pytest.raises(core.BudgetError, match="hasn't started"):
        core.close_month(conn, nxt, None)
    # reopening works and leaves next month open too
    core.reopen_month(conn, this)
    assert core.open_months(conn) == [this, nxt]


def test_bill_driven_budget(conn):
    core.ensure_months(conn, "2026-01")
    conn.execute("INSERT INTO bills(name, amount_cents, frequency, category_id) VALUES ('Hulu', 1000, 'monthly', 3)")
    conn.execute("INSERT INTO bills(name, amount_cents, frequency, category_id) VALUES ('Prime', 15000, 'yearly', 3)")
    conn.execute("INSERT INTO bills(name, amount_cents, frequency, category_id) VALUES ('Brush', 900, 'quarterly', 3)")
    core.refresh_bill_budgets(conn)
    assert bal(conn, "2026-01", "Subs")["budget"] == 1000 + 1250 + 300


def test_records_stay_out_of_envelopes(conn):
    core.ensure_months(conn, "2026-01")
    conn.execute("INSERT INTO tags(id, name, kind) VALUES (1, 'Trip', 'project')")
    core.add_record(conn, amount_cents=50000, description="flights on points", tag_ids=[1])
    assert core.month_summary(conn, "2026-01")["spent"] == 0


def test_validation(conn):
    core.ensure_months(conn, "2026-01")
    with pytest.raises(core.BudgetError):
        core.add_entry(conn, ym="2026-01", category_id=1, amount_cents=0)
    with pytest.raises(core.BudgetError):
        core.add_entry(conn, ym="2026-01", category_id=1, amount_cents=5, date="13/45/2026")
    with pytest.raises(core.BudgetError):
        core.add_move(conn, ym="2026-01", from_id=1, to_id=1, amount_cents=5)
    with pytest.raises(core.BudgetError):
        core.add_entry(conn, ym="2026-01", category_id=99, amount_cents=5)
