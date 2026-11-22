"""Budget logic: months, envelopes, rollover balances, moves, locking.

Everything here works on a sqlite3 connection and integer cents. The web layer
(app/main.py) and the command-line tools both go through these
functions so the rules live in one place.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from fractions import Fraction
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
GROUPS = ["Needs", "Fun", "Investing - Long", "Investing - Short"]
INTENTS = ("want", "need", "necessity", "impulse")
PER_MONTH = {"monthly": Fraction(1), "quarterly": Fraction(1, 3), "yearly": Fraction(1, 12)}


class BudgetError(ValueError):
    """A rule was broken (locked month, bad input). Message is safe to show the user."""


# ---------------------------------------------------------------- connection

def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    # Small forward-only migrations for databases created by older versions.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "year" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN year INTEGER")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
    if "is_investment" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN is_investment INTEGER NOT NULL DEFAULT 0")
        # Starting point for the imported sheet; changeable in Settings -> Categories.
        conn.execute("UPDATE categories SET is_investment=1 WHERE name IN ('Roth IRA', 'ETF Investment')")
    if "goal_cents" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN goal_cents INTEGER")
        conn.execute("ALTER TABLE categories ADD COLUMN goal_ym TEXT")


# Year an entry counts toward in tracker reports: purchase date, else month, else known year.
YEAR_SQL = "COALESCE(substr(t.date,1,4), substr(t.ym,1,4), CAST(t.year AS TEXT))"


class tx:
    """`with tx(conn):` runs the block in one write transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# ---------------------------------------------------------------- months

def ym_of(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def next_ym(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:])
    return f"{y + (m == 12):04d}-{m % 12 + 1:02d}"


def valid_ym(ym: str) -> str:
    try:
        dt.date(int(ym[:4]), int(ym[5:7]), 1)
        assert len(ym) == 7 and ym[4] == "-"
    except Exception:
        raise BudgetError(f"Bad month '{ym}'")
    return ym


def months_between(a: str, b: str) -> int:
    """Whole months from `a` to `b` ('2026-11' -> '2027-02' is 3)."""
    return (int(b[:4]) * 12 + int(b[5:7])) - (int(a[:4]) * 12 + int(a[5:7]))


def add_months(ym: str, n: int) -> str:
    total = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def goal_progress(*, balance: int, budget: int, goal_cents: int, goal_ym: str | None, ym: str) -> dict:
    """How a category's bank balance stands against its savings goal, as of the end of `ym`.

    The balance already includes `ym`'s budget, so the months that can still add money are the
    ones after `ym` up to and including the goal month.
    status: done / on_track / behind / due (goal month reached, still short) / open (no date).
    """
    need = max(goal_cents - balance, 0)
    out = {"goal_cents": goal_cents, "goal_ym": goal_ym, "saved": balance, "need": need,
           "pct": max(0.0, min(1.0, balance / goal_cents)) if goal_cents else 1.0,
           "months_left": None, "per_month": None, "eta_ym": None}
    if need == 0:
        return {**out, "status": "done"}
    if goal_ym:
        left = months_between(ym, goal_ym)
        out["months_left"] = max(left, 0)
        if left <= 0:
            return {**out, "status": "due"}
        per = -(-need // left)  # round up to the cent
        out["per_month"] = per
        return {**out, "status": "on_track" if budget >= per else "behind"}
    if budget > 0:
        out["eta_ym"] = add_months(ym, -(-need // budget))
    return {**out, "status": "open"}


def bill_monthly_cents(conn, category_id: int) -> int:
    rows = conn.execute(
        "SELECT amount_cents, frequency FROM bills WHERE category_id=? AND active=1",
        (category_id,),
    ).fetchall()
    return round(sum(r["amount_cents"] * PER_MONTH[r["frequency"]] for r in rows))


def ensure_months(conn, upto: str | None = None) -> None:
    """Create every month from the last existing one through `upto` (default: this month).

    New months copy each active category's current default budget.
    """
    upto = upto or ym_of(dt.date.today())
    last = conn.execute("SELECT max(ym) FROM months").fetchone()[0]
    if last is None:
        todo = [upto]
    else:
        todo, ym = [], last
        while ym < upto:
            ym = next_ym(ym)
            todo.append(ym)
    if not todo:
        return
    cats = conn.execute(
        "SELECT id, default_budget_cents FROM categories WHERE archived=0"
    ).fetchall()
    for ym in todo:
        conn.execute("INSERT OR IGNORE INTO months(ym) VALUES (?)", (ym,))
        conn.executemany(
            "INSERT OR IGNORE INTO month_budgets(ym, category_id, amount_cents) VALUES (?,?,?)",
            [(ym, c["id"], c["default_budget_cents"]) for c in cats],
        )


def month_status(conn, ym: str) -> str:
    row = conn.execute("SELECT status FROM months WHERE ym=?", (ym,)).fetchone()
    if row is None:
        raise BudgetError(f"Month {ym} doesn't exist")
    return row["status"]


def require_open(conn, ym: str) -> None:
    if month_status(conn, ym) != "open":
        raise BudgetError(f"{ym} is closed. Reopen it to make changes.")


def open_months(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT ym FROM months WHERE status='open' ORDER BY ym")]


def close_month(conn, ym: str, user_id: int | None) -> None:
    require_open(conn, ym)
    earlier = conn.execute(
        "SELECT min(ym) FROM months WHERE status='open' AND ym < ?", (ym,)
    ).fetchone()[0]
    if earlier:
        raise BudgetError(f"Close {earlier} first. Months close in order.")
    current = ym_of(dt.date.today())
    if ym > current:
        raise BudgetError("That month hasn't started yet.")
    conn.execute(
        "UPDATE months SET status='closed', closed_at=datetime('now'), closed_by=? WHERE ym=?",
        (user_id, ym),
    )
    # Closing the current month early (e.g. on the 29th) opens next month right away,
    # so new entries have somewhere to go.
    if ym == current:
        ensure_months(conn, next_ym(ym))


def reopen_month(conn, ym: str) -> None:
    if month_status(conn, ym) != "closed":
        raise BudgetError(f"{ym} is already open")
    later = conn.execute(
        "SELECT max(ym) FROM months WHERE status='closed' AND ym > ?", (ym,)
    ).fetchone()[0]
    if later:
        raise BudgetError(f"Reopen {later} first. Only the latest closed month can be reopened.")
    conn.execute("UPDATE months SET status='open', closed_at=NULL, closed_by=NULL WHERE ym=?", (ym,))


# ---------------------------------------------------------------- budgets

def set_month_budget(conn, ym: str, category_id: int, cents: int) -> None:
    require_open(conn, ym)
    conn.execute(
        "INSERT INTO month_budgets(ym, category_id, amount_cents) VALUES (?,?,?) "
        "ON CONFLICT(ym, category_id) DO UPDATE SET amount_cents=excluded.amount_cents",
        (ym, category_id, cents),
    )


def set_default_budget(conn, category_id: int, cents: int) -> None:
    """Change a category's default and apply it to every open month (like editing
    'Monthly Spending' in the sheet changed the live month)."""
    conn.execute("UPDATE categories SET default_budget_cents=? WHERE id=?", (cents, category_id))
    for ym in open_months(conn):
        set_month_budget(conn, ym, category_id, cents)


def refresh_bill_budgets(conn) -> None:
    """Recompute defaults for categories whose budget comes from their bill list."""
    for c in conn.execute("SELECT id, default_budget_cents FROM categories WHERE budget_from_bills=1").fetchall():
        cents = bill_monthly_cents(conn, c["id"])
        if cents != c["default_budget_cents"]:
            set_default_budget(conn, c["id"], cents)


# ---------------------------------------------------------------- summaries

def month_summary(conn, ym: str) -> dict:
    """Per-category budget / spent / left for `ym`, plus each envelope's running balance
    (the sheet's 'The Bank') as of the end of `ym`."""
    rows = conn.execute(
        """
        WITH per AS (
            SELECT mb.ym, mb.category_id,
                   mb.amount_cents AS budget,
                   COALESCE((SELECT sum(t.amount_cents) FROM transactions t
                             WHERE t.ym = mb.ym AND t.category_id = mb.category_id), 0) AS spent
            FROM month_budgets mb
        )
        SELECT c.id, c.name, c.grp, c.sort, c.archived, c.budget_from_bills, c.goal_cents, c.goal_ym,
               COALESCE(cur.budget, 0) AS budget,
               COALESCE(cur.spent, 0)  AS spent,
               (SELECT COALESCE(sum(p.budget - p.spent), 0) FROM per p
                 WHERE p.category_id = c.id AND p.ym <= :ym) AS balance,
               cur.category_id IS NOT NULL AS in_month
        FROM categories c
        LEFT JOIN per cur ON cur.category_id = c.id AND cur.ym = :ym
        ORDER BY c.archived, c.sort, c.name
        """,
        {"ym": ym},
    ).fetchall()
    cats = []
    for r in rows:
        d = dict(r)
        d["left"] = d["budget"] - d["spent"]
        goal_cents, goal_ym = d.pop("goal_cents"), d.pop("goal_ym")
        d["goal"] = goal_progress(balance=d["balance"], budget=d["budget"], goal_cents=goal_cents,
                                  goal_ym=goal_ym, ym=ym) if goal_cents else None
        # Hide archived categories that have nothing going on.
        if d["archived"] and not d["in_month"] and d["balance"] == 0:
            continue
        cats.append(d)
    income = conn.execute("SELECT COALESCE(sum(amount_cents),0) FROM income WHERE active=1").fetchone()[0]
    total_budget = sum(c["budget"] for c in cats)
    return {
        "ym": ym,
        "status": month_status(conn, ym),
        "categories": cats,
        "income": income,
        "budgeted": total_budget,
        "spent": sum(c["spent"] for c in cats),
        "left": sum(c["left"] for c in cats),
        "bank_total": sum(c["balance"] for c in cats),
        "unassigned": income - total_budget,
    }


# ---------------------------------------------------------------- transactions

def _clean_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise BudgetError(f"Bad date '{value}'")


def _clean_intent(value: str | None) -> str | None:
    if not value:
        return None
    value = value.lower()
    if value not in INTENTS:
        raise BudgetError(f"Unknown type '{value}'")
    return value


def _set_tags(conn, tx_id: int, tag_ids: list[int] | None) -> None:
    if tag_ids is None:
        return
    conn.execute("DELETE FROM transaction_tags WHERE transaction_id=?", (tx_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id) VALUES (?,?)",
        [(tx_id, t) for t in tag_ids],
    )


def _require_category(conn, category_id: int) -> None:
    if conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone() is None:
        raise BudgetError("Pick a category")


def add_entry(conn, *, ym: str, category_id: int, amount_cents: int, kind: str = "spend",
              description: str = "", notes: str = "", date: str | None = None,
              intent: str | None = None, tag_ids: list[int] | None = None,
              reimbursed: bool = False, user_id: int | None = None, source: str = "app") -> int:
    """Add a spend (positive) or an add-money entry (stored negative)."""
    if kind not in ("spend", "add"):
        raise BudgetError("Use add_move for moves")
    if amount_cents <= 0:
        raise BudgetError("Amount must be more than zero")
    require_open(conn, ym)
    _require_category(conn, category_id)
    signed = amount_cents if kind == "spend" else -amount_cents
    cur = conn.execute(
        """INSERT INTO transactions(ym, category_id, amount_cents, kind, description, notes, date,
                                    intent, reimbursed, entered_by, source, sort)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,
                   (SELECT COALESCE(max(sort),0)+1 FROM transactions WHERE ym=?))""",
        (ym, category_id, signed, kind, description.strip(), notes.strip(), _clean_date(date),
         _clean_intent(intent), int(bool(reimbursed)), user_id, source, ym),
    )
    _set_tags(conn, cur.lastrowid, tag_ids)
    return cur.lastrowid


def add_move(conn, *, ym: str, from_id: int, to_id: int, amount_cents: int, notes: str = "",
             date: str | None = None, user_id: int | None = None) -> str:
    """Move money between envelopes: + on the source, - on the destination."""
    if from_id == to_id:
        raise BudgetError("Pick two different categories")
    if amount_cents <= 0:
        raise BudgetError("Amount must be more than zero")
    require_open(conn, ym)
    _require_category(conn, from_id)
    _require_category(conn, to_id)
    move_id = uuid.uuid4().hex
    names = dict(conn.execute("SELECT id, name FROM categories WHERE id IN (?,?)", (from_id, to_id)).fetchall())
    desc = f"Move {names[from_id]} → {names[to_id]}"
    for cat, signed in ((from_id, amount_cents), (to_id, -amount_cents)):
        conn.execute(
            """INSERT INTO transactions(ym, category_id, amount_cents, kind, move_id, description, notes,
                                        date, entered_by, sort)
               VALUES (?,?,?,'move',?,?,?,?,?, (SELECT COALESCE(max(sort),0)+1 FROM transactions WHERE ym=?))""",
            (ym, cat, signed, move_id, desc, notes.strip(), _clean_date(date), user_id, ym),
        )
    return move_id


def add_record(conn, *, amount_cents: int, description: str = "", notes: str = "",
               date: str | None = None, tag_ids: list[int] | None = None,
               reimbursed: bool = False, user_id: int | None = None, source: str = "app",
               year: int | None = None) -> int:
    """A tracking-only record (not part of any envelope), e.g. trip costs paid with points."""
    if amount_cents == 0:
        raise BudgetError("Amount can't be zero")
    date = _clean_date(date)
    if not date and year is None and source == "app":
        year = dt.date.today().year
    cur = conn.execute(
        """INSERT INTO transactions(ym, category_id, amount_cents, kind, description, notes, date,
                                    reimbursed, entered_by, source, year)
           VALUES (NULL, NULL, ?, 'record', ?, ?, ?, ?, ?, ?, ?)""",
        (amount_cents, description.strip(), notes.strip(), date, int(bool(reimbursed)),
         user_id, source, year),
    )
    _set_tags(conn, cur.lastrowid, tag_ids)
    return cur.lastrowid


def get_tx(conn, tx_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        raise BudgetError("That entry no longer exists")
    return row


def update_entry(conn, tx_id: int, **fields) -> None:
    """Edit an entry. Moves edit both halves together (amount, notes, date)."""
    row = get_tx(conn, tx_id)
    if row["ym"]:
        require_open(conn, row["ym"])
    new_ym = fields.get("ym")
    if new_ym and new_ym != row["ym"]:
        if row["kind"] == "record":
            raise BudgetError("Tracking-only records don't belong to a month")
        require_open(conn, new_ym)

    if row["kind"] == "move":
        sets = {}
        if "amount_cents" in fields:
            if fields["amount_cents"] <= 0:
                raise BudgetError("Amount must be more than zero")
            sets["amount_cents"] = fields["amount_cents"]
        for k in ("notes", "date", "ym"):
            if fields.get(k) is not None:
                sets[k] = _clean_date(fields[k]) if k == "date" else fields[k]
        for half in conn.execute("SELECT id, amount_cents FROM transactions WHERE move_id=?", (row["move_id"],)):
            vals = dict(sets)
            if "amount_cents" in vals:
                vals["amount_cents"] = vals["amount_cents"] if half["amount_cents"] > 0 else -vals["amount_cents"]
            _apply(conn, half["id"], vals)
        return

    vals = {}
    if "amount_cents" in fields:
        amt = fields["amount_cents"]
        kind = fields.get("kind", row["kind"])
        if row["kind"] != "record":
            if amt <= 0:
                raise BudgetError("Amount must be more than zero")
            amt = amt if kind == "spend" else -amt
            vals["kind"] = kind
        vals["amount_cents"] = amt
    if fields.get("category_id") is not None and row["kind"] != "record":
        _require_category(conn, fields["category_id"])
        vals["category_id"] = fields["category_id"]
    for k in ("description", "notes"):
        if fields.get(k) is not None:
            vals[k] = fields[k].strip()
    if "date" in fields:
        vals["date"] = _clean_date(fields["date"])
    if "intent" in fields:
        vals["intent"] = _clean_intent(fields["intent"])
    if "reimbursed" in fields and fields["reimbursed"] is not None:
        vals["reimbursed"] = int(bool(fields["reimbursed"]))
    if new_ym:
        vals["ym"] = new_ym
    _apply(conn, tx_id, vals)
    _set_tags(conn, tx_id, fields.get("tag_ids"))


def _apply(conn, tx_id: int, vals: dict) -> None:
    if not vals:
        return
    cols = ", ".join(f"{k}=?" for k in vals)
    conn.execute(f"UPDATE transactions SET {cols}, updated_at=datetime('now') WHERE id=?",
                 (*vals.values(), tx_id))


def delete_entry(conn, tx_id: int) -> None:
    row = get_tx(conn, tx_id)
    if row["ym"]:
        require_open(conn, row["ym"])
    if row["move_id"]:
        conn.execute("DELETE FROM transactions WHERE move_id=?", (row["move_id"],))
    else:
        conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
