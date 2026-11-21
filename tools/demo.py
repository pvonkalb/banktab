"""Create a demo household full of made-up data, to try the app or take screenshots.

    python tools/demo.py [--data DIR] [--months 8] [--seed 7]

Writes "Alex & Jordan" (about eight months of spending, bills, trackers and closed months)
into a separate data folder (default ./demo-data) and prints a login. Run the app on it with:

    BUDGET_DATA_DIR=demo-data python -m uvicorn app.main:app --port 8000

Nothing here touches your real data folder. Every name and amount is invented.
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import accounts, core  # noqa: E402

# (name, group, monthly budget $, is_investment, budget comes from bills)
CATEGORIES = [
    ("Mortgage", "Needs", 1850, False, False),
    ("Utilities", "Needs", 0, False, True),
    ("Groceries", "Needs", 760, False, False),
    ("Gas", "Needs", 220, False, False),
    ("Insurance", "Needs", 265, False, False),
    ("Phone & internet", "Needs", 145, False, False),
    ("Subscriptions", "Needs", 0, False, True),
    ("Dining out", "Fun", 320, False, False),
    ("Fun money", "Fun", 200, False, False),
    ("Travel", "Fun", 300, False, False),
    ("Gifts", "Fun", 90, False, False),
    ("Car maintenance", "Investing - Short", 150, False, False),
    ("Home repairs", "Investing - Short", 175, False, False),
    ("Medical", "Investing - Short", 110, False, False),
    ("Emergency fund", "Investing - Long", 300, False, False),
    ("Roth IRA", "Investing - Long", 500, True, False),
]
BILLS = [  # (name, $, frequency, category, renews)
    ("Electric", 112.00, "monthly", "Utilities", ""), ("Natural gas", 58.00, "monthly", "Utilities", ""),
    ("Water & sewer", 46.00, "monthly", "Utilities", ""), ("Trash", 24.00, "monthly", "Utilities", ""),
    ("Streaming", 15.49, "monthly", "Subscriptions", ""), ("Music", 11.99, "monthly", "Subscriptions", ""),
    ("Cloud storage", 2.99, "monthly", "Subscriptions", ""), ("Warehouse club", 65.00, "yearly", "Subscriptions", "Mar"),
    ("Password manager", 36.00, "yearly", "Subscriptions", "Sep"),
]
GROCERY = ["Trader Joe's", "King Soopers", "Costco", "Sprouts", "Natural Grocers", "Safeway"]
DINING = ["Taco Star", "Pho 99", "Blue Spruce Brewing", "Corner Bakery", "Sushi Den", "Pizza Lab", "Burrito Bros", "Thai Basil"]
FUN = ["Movie night", "Bookstore", "Climbing gym", "Concert tickets", "Board game cafe", "Mini golf"]
GAS = ["Shell", "Conoco", "King Soopers Fuel", "Costco Gas"]


def money(rng: random.Random, lo: float, hi: float) -> int:
    return round(rng.uniform(lo, hi) * 100)


def build(data_dir: Path, months: int, seed: int) -> tuple[str, str]:
    rng = random.Random(seed)
    if accounts.accounts_path(data_dir).exists():
        raise SystemExit(f"{data_dir} already has data. Use an empty folder (or delete it) for the demo.")
    acct = accounts.open_accounts(data_dir)
    hid = accounts.create_household(acct, data_dir, "Alex & Jordan", setup_done=True)
    password = "demo-" + secrets.token_hex(4)
    users = []
    for username, name in (("alex", "Alex"), ("jordan", "Jordan")):
        uid = acct.execute("INSERT INTO users(username, display_name, password_hash, household_id, role) VALUES (?,?,?,?,?)",
                           (username, name, accounts.hash_password(password), hid, "owner" if username == "alex" else "member")).lastrowid
        users.append({"id": uid, "username": username, "display_name": name})
    acct.close()

    conn = accounts.open_household(data_dir, hid)
    for u in users:
        accounts.sync_member(conn, u)
    cat = {}
    for i, (name, grp, budget, invest, from_bills) in enumerate(CATEGORIES, 1):
        cat[name] = conn.execute(
            "INSERT INTO categories(name, grp, sort, default_budget_cents, is_investment, budget_from_bills) VALUES (?,?,?,?,?,?)",
            (name, grp, i, budget * 100, int(invest), int(from_bills))).lastrowid
    for name, amount, freq, category, renews in BILLS:
        conn.execute("INSERT INTO bills(name, amount_cents, frequency, renews, category_id) VALUES (?,?,?,?,?)",
                     (name, round(amount * 100), freq, renews, cat[category]))
    conn.execute("INSERT INTO income(name, amount_cents) VALUES ('Alex paycheck', 455000), ('Jordan paycheck', 382000)")
    core.refresh_bill_budgets(conn)
    tags = {}
    for name, kind in (("Home repairs 2026", "tax"), ("HSA / medical 2026", "tax"), ("Lake trip", "project")):
        tags[name] = conn.execute("INSERT INTO tags(name, kind) VALUES (?,?)", (name, kind)).lastrowid

    today = dt.date.today()
    current = core.ym_of(today)
    first = current
    for _ in range(months - 1):
        y, m = int(first[:4]), int(first[5:])
        first = f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"
    core.ensure_months(conn, first)
    core.ensure_months(conn)
    yms = [r[0] for r in conn.execute("SELECT ym FROM months ORDER BY ym")]

    def add(ym, day, category, cents, desc, kind="spend", intent=None, tag=None, who=None, notes=""):
        last = 28 if ym != current else max(1, min(28, today.day))
        d = f"{ym}-{min(day, last):02d}"
        core.add_entry(conn, ym=ym, category_id=cat[category], amount_cents=cents, kind=kind, description=desc,
                       notes=notes, date=d, intent=intent, tag_ids=[tags[tag]] if tag else None,
                       user_id=(who or rng.choice(users))["id"])

    for idx, ym in enumerate(yms):
        last_day = 28 if ym != current else max(1, today.day)
        days = lambda n: sorted(rng.randint(1, last_day) for _ in range(n))  # noqa: E731
        add(ym, 1, "Mortgage", 185000, "Mortgage payment", who=users[0])
        for name, amount, freq, category, _ in BILLS:
            if freq == "monthly":
                wobble = rng.uniform(.8, 1.12) if category == "Utilities" else 1
                add(ym, rng.randint(3, 20), category, round(amount * wobble * 100), name)
        if ym[5:] == "03":
            add(ym, 12, "Subscriptions", 6500, "Warehouse club")
        if ym[5:] == "09":
            add(ym, 9, "Subscriptions", 3600, "Password manager")
        add(ym, 5, "Insurance", 26200, "Auto + home insurance")
        add(ym, 8, "Phone & internet", money(rng, 138, 146), "Phone & internet")
        for d in days(rng.randint(7, 10)):
            add(ym, d, "Groceries", money(rng, 18, 145), rng.choice(GROCERY), intent=rng.choice([None, None, "need"]))
        for d in days(rng.randint(3, 5)):
            add(ym, d, "Gas", money(rng, 38, 62), rng.choice(GAS))
        for d in days(rng.randint(4, 7)):
            add(ym, d, "Dining out", money(rng, 14, 78), rng.choice(DINING), intent=rng.choice([None, "want", "impulse"]))
        for d in days(rng.randint(2, 4)):
            add(ym, d, "Fun money", money(rng, 12, 85), rng.choice(FUN), intent=rng.choice([None, "want"]))
        add(ym, 15, "Roth IRA", 50000, "Roth IRA contribution", who=users[0])
        if rng.random() < .5:
            add(ym, rng.randint(2, 26), "Gifts", money(rng, 25, 90), rng.choice(["Birthday gift", "Wedding gift", "Baby shower"]))
        if rng.random() < .35:
            add(ym, rng.randint(2, 26), "Car maintenance", money(rng, 45, 260), rng.choice(["Oil change", "Brake pads", "Car wash pass"]))
        if rng.random() < .4:
            add(ym, rng.randint(2, 26), "Home repairs", money(rng, 40, 380), rng.choice(["Hardware store", "Plumber", "Paint", "Furnace filter"]),
                tag="Home repairs 2026")
        if rng.random() < .4:
            add(ym, rng.randint(2, 26), "Medical", money(rng, 25, 210), rng.choice(["Urgent care", "Pharmacy", "Dentist copay"]),
                tag="HSA / medical 2026")
        # A summer trip paid from the travel envelope that's been saving up.
        if idx == len(yms) - 4:
            add(ym, 6, "Travel", 64000, "Cabin rental", tag="Lake trip", intent="want")
            add(ym, 7, "Travel", 18800, "Kayak rental", tag="Lake trip")
            add(ym, 8, "Dining out", 9600, "Lakeside grill", tag="Lake trip")
        # Money in: a birthday gift into Fun money; a move to cover dining overspending.
        if idx == 2:
            add(ym, 18, "Fun money", 15000, "Birthday money from grandma", kind="add")
        if idx % 3 == 1:
            core.add_move(conn, ym=ym, from_id=cat["Fun money"], to_id=cat["Dining out"], amount_cents=5000,
                          notes="cover dining", user_id=users[1]["id"])
    for ym in yms[:-1]:  # everything but the current month is closed, like a real household would
        core.close_month(conn, ym, users[0]["id"])
    conn.close()
    return "alex", password


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("demo-data"))
    ap.add_argument("--months", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    user, pw = build(a.data, a.months, a.seed)
    print(f"Demo household ready in {a.data}\n  login: {user} (or jordan)\n  password: {pw}\n"
          f"Run:  BUDGET_DATA_DIR={a.data} python -m uvicorn app.main:app --port 8000")


if __name__ == "__main__":
    main()
