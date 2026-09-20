"""
Folds data/legislation.db's write-ahead log into the main database file,
so the .db file alone is a complete, self-contained snapshot -- run this
before committing it to git.

Why this matters: db.py opens the database in WAL mode (see its own
_connect docstring), which means a recent write can sit in
data/legislation.db-wal, not yet folded into data/legislation.db itself.
The database itself is no longer committed -- what travels is
data/review/**.jsonl, written out from it (see corpus/review_sync.py) --
but this still matters for the same reason it always did, one step
earlier in the chain: an export reads the database file, so a write still
sitting in the -wal would be missing from the text that gets committed,
with nothing to indicate anything was missing. corpus/sync.py checkpoints
before every export for exactly this; run it by hand if you are
exporting or committing by hand.

Usage:
    python checkpoint_db.py

Safe to run any time, including with no pending WAL writes (a no-op) or
while review.py is running against the same file (SQLite's own
WAL-checkpoint locking handles that safely). Consider wiring this up as
a local git pre-commit hook -- see deploy/README.md's "Working from a
remote dev environment" section -- since a hook doesn't travel with the
repo across clones and needs setting up again in each new environment.
"""
import sys

from corpus.storage import db


def main():
    conn = db._connect()
    before = db.db_path().stat().st_size
    # TRUNCATE (rather than the default PASSIVE) both checkpoints and
    # truncates the -wal file to zero bytes afterwards -- the point isn't
    # just folding writes in, it's leaving no ambiguity that the main
    # file is now the complete, current state.
    busy, log_frames, checkpointed_frames = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        print("Checkpoint incomplete -- another connection is still writing. Try again once it's done.")
        return 1
    after = db.db_path().stat().st_size
    print(f"Checkpointed {checkpointed_frames} page(s) from the WAL into {db.db_path()} ({before} -> {after} bytes).")
    print("Safe to commit now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
