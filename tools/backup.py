"""Make a consistent copy of every database (safe while the app is running).

    python tools/backup.py [dest_dir] [--keep 30]

Writes one folder per run, backup-YYYYMMDD-HHMMSS/, holding accounts.db and
households/<id>.db, and deletes the oldest runs beyond --keep. The app also does this by
itself once a day (BUDGET_BACKUP_KEEP, default 14; 0 turns it off).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.accounts import backup_all  # noqa: E402
from app.main import DATA_DIR  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dest", nargs="?", type=Path, default=DATA_DIR / "backups")
    ap.add_argument("--keep", type=int, default=30)
    a = ap.parse_args()
    out, n, removed = backup_all(DATA_DIR, a.dest, a.keep)
    size = sum(p.stat().st_size for p in out.rglob("*.db")) // 1024
    print(f"Backed up {n} database(s) to {out} ({size} KB); removed {removed} old backups")


if __name__ == "__main__":
    main()
