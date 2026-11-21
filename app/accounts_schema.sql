-- Accounts database: who can log in and which household they belong to.
-- Each household's budget lives in its own database file (data/households/<id>.db),
-- so one household's data can never show up in another's queries.

PRAGMA foreign_keys = ON;

-- AUTOINCREMENT: ids are never reused, so a new household or person can't inherit an old one's id.
CREATE TABLE IF NOT EXISTS households (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    setup_done  INTEGER NOT NULL DEFAULT 0,   -- 0 = show the first-run setup screen
    currency    TEXT NOT NULL DEFAULT 'USD',  -- ISO 4217 code, display only
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- sign-ups use their email address
    email             TEXT UNIQUE COLLATE NOCASE,            -- NULL for accounts made with manage.py
    email_verified_at TEXT,
    verify_required   INTEGER NOT NULL DEFAULT 0,             -- 1 = must confirm email before logging in
    display_name      TEXT NOT NULL,
    password_hash     TEXT NOT NULL,
    household_id      INTEGER NOT NULL REFERENCES households(id),
    role              TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
    theme             TEXT NOT NULL DEFAULT 'system',         -- system / light / dark / noir / paper
    accent            TEXT NOT NULL DEFAULT 'teal',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_users_household ON users(household_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);

-- One-time links: email verification, password reset, household invites. Stored hashed.
CREATE TABLE IF NOT EXISTS tokens (
    token_hash    TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('verify', 'reset', 'invite')),
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    household_id  INTEGER REFERENCES households(id) ON DELETE CASCADE,
    email         TEXT,                      -- invites: who it was sent to (optional)
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT NOT NULL,
    used_at       TEXT
);
