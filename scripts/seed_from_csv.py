"""Seed the grants table from the Notion CSV export.

Usage:  python scripts/seed_from_csv.py path/to/grants.csv

Expected columns (Notion export, May 2026):
Grant name, Funder, Scope/topic, Max amount, Duration (months), Deadline, Link, Notes, Grant start
Extra columns are ignored; missing ones are tolerated. Idempotent by grant name.
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import get_db, init_db  # noqa: E402

COLMAP = {
    "Grant name": "name",
    "Funder": "funder",
    "Scope/topic": "scope",
    "Max amount": "max_amount",
    "Duration (months)": "duration_months",
    "Deadline": "deadline",
    "Link": "link",
    "Notes": "notes",
    "Grant start": "grant_start",
}

DATE_FORMATS = ["%B %d, %Y", "%d %B %Y", "%Y-%m-%d", "%d.%m.%Y"]


def parse_date(text: str):
    text = (text or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def main(path: str) -> None:
    init_db()
    inserted = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            fields = {dst: (row.get(src) or "").strip() or None for src, dst in COLMAP.items()}
            if not fields["name"]:
                continue
            fields["deadline_date"] = parse_date(fields.get("deadline"))
            with get_db() as db:
                if db.execute("SELECT id FROM grants WHERE name=?", (fields["name"],)).fetchone():
                    skipped += 1
                    continue
                cols = list(fields.keys())
                db.execute(
                    f"INSERT INTO grants ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    list(fields.values()),
                )
                inserted += 1
    print(f"Seed: {inserted} inseriti, {skipped} saltati (già presenti).")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/seed_from_csv.py path/to/grants.csv")
    main(sys.argv[1])
