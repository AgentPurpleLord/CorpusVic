"""
Folds data/legislation.db's write-ahead log into the main database file,
so the .db file alone is a complete, self-contained snapshot -- run this
before committing it to git.

Why this matters: db.py opens the database in WAL mode (see its own
_connect docstring), which means a recent write can sit in
data/legislation.db-wal, not yet folded into data/legislation.db itself.
Both files are gitignored except the main one (see .gitignore's own
comment on this) -- data/legislation.db is committed so a fresh clone
(e.g. a temporary cloud dev environment) already has your review
progress, while the -wal/-shm side files are pure runtime state SQLite
recreates on its own and never need to travel with the repo. If the main
file were committed while a WAL still held uncheckpointed writes, a fresh
clone would silently see stale data -- missing whatever hadn't been
folded in yet, with nothing to indicate anything was missing.

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

from corpus import db


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
