"""
One-time import of the old JSON-file review data (data/verified/<act>.json,
data/links/<act>.json, data/corrections.jsonl) into data/legislation.db --
see corpus/db.py's module docstring for why this data moved and what
didn't (data/parsed/<act>.json and the other regenerable pipeline
output stay exactly where they are).

Usage:
    python migrate_json_to_sqlite.py

Safe to run more than once: each Act's verified/links rows are replaced
wholesale from that Act's own JSON file (the same semantics review.py's
own save_verified/save_links already have), and corrections are only
appended for lines not already present (matched by exact timestamp +
Act, which the old jsonl format always wrote as a fresh time.time() per
record, real duplicates run to run are effectively impossible) -- so
re-running this after adding more JSON data, or after already having
some real data in data/legislation.db from actually using the tool
post-migration, won't duplicate anything.

Does not delete the old JSON files -- once you've confirmed
data/legislation.db has what you expect (open it with any SQLite browser,
or just run the app and check your review progress and links are still
there), removing data/verified/, data/links/, and data/corrections.jsonl
is up to you.
"""
import json
import sys
from pathlib import Path

from corpus.storage import db


def migrate_verified() -> int:
    verified_dir = Path("data/verified")
    if not verified_dir.exists():
        return 0
    count = 0
    for path in sorted(verified_dir.glob("*.json")):
        act = path.stem
        verified = json.loads(path.read_text(encoding="utf-8"))
        if not verified:
            continue
        db.save_verified(act, verified)
        print(f"  {act}: {len(verified)} verified node(s)")
        count += len(verified)
    return count


def migrate_links() -> int:
    links_dir = Path("data/links")
    if not links_dir.exists():
        return 0
    count = 0
    for path in sorted(links_dir.glob("*.json")):
        act = path.stem
        links = json.loads(path.read_text(encoding="utf-8"))
        if not links:
            continue
        db.save_links(act, links)
        print(f"  {act}: {len(links)} link(s)")
        count += len(links)
    return count


def migrate_corrections() -> int:
    path = Path("data/corrections.jsonl")
    if not path.exists():
        return 0
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        return 0

    conn = db._connect()
    existing = {(row["act"], row["ts"]) for row in conn.execute("SELECT act, ts FROM corrections")}
    new_records = [r for r in records if (r["act"], r["ts"]) not in existing]
    with conn:
        conn.executemany(
            "INSERT INTO corrections (act, ts, changed, ai_output_json, human_output_json) VALUES (?, ?, ?, ?, ?)",
            [
                (r["act"], r["ts"], 1 if r["changed"] else 0, json.dumps(r["ai_output"]), json.dumps(r["human_output"]))
                for r in new_records
            ],
        )
    skipped = len(records) - len(new_records)
    if skipped:
        print(f"  {len(new_records)} correction(s) imported, {skipped} already present (skipped)")
    else:
        print(f"  {len(new_records)} correction(s) imported")
    return len(new_records)


def main():
    print("Verified review state (data/verified/<act>.json):")
    verified_count = migrate_verified()
    if not verified_count:
        print("  nothing to import")

    print("Link annotations (data/links/<act>.json):")
    links_count = migrate_links()
    if not links_count:
        print("  nothing to import")

    print("Correction log (data/corrections.jsonl):")
    corrections_count = migrate_corrections()

    print(f"\nDone -- data/legislation.db now has {verified_count} verified node(s), "
          f"{links_count} link(s), and {corrections_count} newly-imported correction(s) from this run.")
    print("The original JSON files were left untouched.")


if __name__ == "__main__":
    sys.exit(main())
