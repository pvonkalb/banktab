-- Budget app schema. All money is stored as integer cents.
-- Sign convention for transactions: positive = money leaving an envelope (spending),
-- negative = money entering an envelope (gift, refund, or the receiving side of a move).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
    grp                  TEXT,              -- Needs / Fun / Investing - Long / Investing - Short
    sort                 INTEGER NOT NULL DEFAULT 0,
    default_budget_cents INTEGER NOT NULL DEFAULT 0,
    budget_from_bills    INTEGER NOT NULL DEFAULT 0,  -- 1 = default budget is the sum of linked bills
    archived             INTEGER NOT NULL DEFAULT 0,
    is_investment        INTEGER NOT NULL DEFAULT 0   -- reports count it as invested, not spent
);

CREATE TABLE IF NOT EXISTS months (
    ym         TEXT PRIMARY KEY,           -- 'YYYY-MM'
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_at  TEXT,
    closed_by  INTEGER REFERENCES users(id)
);

-- The budget each category had in each month. Copied from the category default when
-- the month is created, so later changes to defaults never rewrite closed months.
CREATE TABLE IF NOT EXISTS month_budgets (
    ym           TEXT NOT NULL REFERENCES months(ym),
    category_id  INTEGER NOT NULL REFERENCES categories(id),
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (ym, category_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY,
    ym           TEXT REFERENCES months(ym),          -- NULL for tracking-only records
    category_id  INTEGER REFERENCES categories(id),   -- NULL for tracking-only records
    amount_cents INTEGER NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'spend'
                 CHECK (kind IN ('spend', 'add', 'move', 'record')),
    move_id      TEXT,                                -- shared by both halves of a move
    description  TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    date         TEXT,                                -- purchase date 'YYYY-MM-DD' (optional)
    posted_date  TEXT,                                -- date the bank posted it (from CSV/link)
    year         INTEGER,                             -- for undated records whose year is known
    intent       TEXT CHECK (intent IN ('want', 'need', 'necessity', 'impulse')),
    reimbursed   INTEGER NOT NULL DEFAULT 0,          -- e.g. HSA reimbursement claimed
    entered_by   INTEGER REFERENCES users(id),
    source       TEXT NOT NULL DEFAULT 'app',         -- app / sheet / csv
    sort         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((ym IS NULL) = (category_id IS NULL)),
    CHECK ((kind = 'record') = (category_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_tx_month ON transactions(ym, category_id);
CREATE INDEX IF NOT EXISTS ix_tx_move ON transactions(move_id);

CREATE TABLE IF NOT EXISTS tags (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind     TEXT NOT NULL DEFAULT 'project' CHECK (kind IN ('tax', 'project')),
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id         INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (transaction_id, tag_id)
);

CREATE TABLE IF NOT EXISTS bills (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    frequency    TEXT NOT NULL CHECK (frequency IN ('monthly', 'quarterly', 'yearly')),
    renews       TEXT NOT NULL DEFAULT '',     -- free text, e.g. 'Dec' or 'aug 31st'
    category_id  INTEGER REFERENCES categories(id),
    notes        TEXT NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS income (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);
