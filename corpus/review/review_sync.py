"""
The review work, as text that git can merge.

data/legislation.db is what several processes write to while you review;
this module is how that work travels between machines. They are two
different jobs, and one file was doing both.

The database was committed as a single 1.7 MB binary. git cannot diff it,
cannot merge it, and stores a whole new copy of it on every commit -- 22
commits carrying 10.8 MB of blobs for a 1.7 MB file. Worse, because there
is no merge, `sync.pull` had to be fast-forward only and refused on *any*
divergence: reviewing on the server while a code change landed elsewhere
was enough to jam it, though those two never touch the same file.

So the durable form is text:

    data/review/publication.jsonl           the one table not keyed by act
    data/review/crimes-act/verified.jsonl   one line per provision
    data/review/crimes-act/corrections.jsonl
    ...

**One line per row, one directory per act.** That is what buys the
granularity: git merges lines, so two different provisions reviewed on
two machines merge with no help, and the same provision reviewed twice is
a conflict you can actually read, because the line is JSON. Per-provision
*files* would be some fourteen hundred of them for the Crimes Act alone
and tens of thousands across the corpus -- worse for git, worse for the
filesystem, and no better at merging than a line is.

The limit, measured rather than assumed: two lines apart merges, and
immediately adjacent lines do not -- git treats them as one hunk. So two
reviewers working down the same Act collide only where they are working
on the very next provision to each other, and the rows are sorted by the
table's own key precisely so that "next to each other in the file" means
"next to each other in the Act" rather than "written at the same time".

**The registry is read from the database, not written down here.** A
hand-kept list of tables is a list that goes stale, and a table missing
from it is review work that silently stops being synced -- which is the
exact failure this module exists to remove. So the shape comes from
`pragma table_info` at runtime: a table added to db.py's schema is
exported the first time anyone exports.

**The database stays the working store.** Three processes write to it and
two of them at once -- review.py while you review, run_ai_review.py
scanning in the background, both writing ai_scan_findings -- which is
what sqlite's write-ahead log is for and what two processes appending to
a JSONL file would interleave into nonsense. What changes is that the
database stops being the thing that travels, and becomes rebuildable from
what does.
"""
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REVIEW_DIRNAME = "review"

# Columns sqlite fills in itself. Their values are whichever machine
# happened to write the row first, so they are not identity and must not
# be written down -- see Table.omit.
_ROWID_ALIASES = ("id",)

# When a table has no natural key, rows are ordered by whichever of these
# it has, so an append-only log appends at the end of the file rather
# than inserting in the middle of it. The serialised line breaks any
# remaining tie, so the order is total and reproducible.
_FALLBACK_ORDER = ("ts", "created_at", "orphaned_at", "scanned_at", "requested_at")


@dataclass(frozen=True)
class Table:
    """One table, and how it is laid out on disk."""

    name: str
    columns: tuple
    key: tuple          # the natural key, for ordering and for reading a diff
    omit: tuple         # columns not written: autoincrement ids
    shard: "str | None"  # the column to split files by, almost always "act"

    def sort_key(self, row: dict, line: str) -> tuple:
        return tuple(("", row[k]) if row.get(k) is not None else ("\x00", "")
                     for k in self.key) + (line,)


def review_dir(base_dir) -> Path:
    return Path(base_dir or ".") / "data" / REVIEW_DIRNAME


def describe(conn: sqlite3.Connection) -> list:
    """Every table in the database, and how each one is written out.

    Read from the schema so that adding a table to db.py is enough for it
    to start syncing. Nothing here names a table."""
    tables = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        info = list(conn.execute(f"PRAGMA table_info({name})"))
        columns = tuple(r[1] for r in info)
        pk = tuple(r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5]))
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0] or ""
        # AUTOINCREMENT means sqlite owns the value. Two machines would
        # both hand out 1726 for different rows, and a merge of two files
        # that each claim it looks clean while being wrong.
        autoinc = "AUTOINCREMENT" in sql.upper() and len(pk) == 1 and pk[0] in _ROWID_ALIASES
        omit = pk if autoinc else ()
        key = tuple(c for c in pk if c not in omit)
        if not key:
            key = tuple(c for c in _FALLBACK_ORDER if c in columns)
        tables.append(Table(name=name, columns=columns, key=key, omit=omit,
                            shard="act" if "act" in columns else None))
    return tables


# Columns added after rows were already written, left out while empty: a
# "note": null on every existing line would rewrite every one of them for
# nothing. Reading a line without one leaves it null (see _insert).
_SPARSE = {("history_decisions", "note")}


def _line(table: Table, row: sqlite3.Row) -> str:
    record = {c: row[c] for c in table.columns
              if c not in table.omit and not ((table.name, c) in _SPARSE and row[c] is None)}
    # sort_keys so a field never moves on its own; ensure_ascii off so the
    # legislation's own punctuation stays readable in a diff rather than
    # becoming a row of escapes.
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


def _write_if_changed(path: Path, text: str) -> bool:
    """True when the file was actually rewritten.

    Compared before writing so that exporting on every status poll costs
    a read rather than a write, and leaves mtimes -- which sync's own
    staleness checks read -- alone."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


class Unloaded(RuntimeError):
    """data/review/ holds work the database does not."""


# Where the export records what it last left on disk. Beside the
# database, gitignored, and deliberately NOT a table: every table is
# exported, so a table holding a fingerprint of the exported files would
# change the files it fingerprints.
_STATE_FILE = "review-sync-state.json"


def _state_path(base: Path) -> Path:
    return base / "data" / _STATE_FILE


def fingerprint(base_dir=None) -> str:
    """A stamp of every review file on disk, content and all.

    Cheap enough to take on every export -- 2 MB of text, hashed once --
    and the only thing that can tell "these files are as I left them"
    from "something else has been at them"."""
    import hashlib

    out = review_dir(base_dir)
    digest = hashlib.sha256()
    for path in sorted(out.rglob("*.jsonl")):
        digest.update(str(path.relative_to(out)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _record_state(base: Path) -> None:
    """Remembers that the database and the files agree, because they were
    just made to."""
    try:
        path = _state_path(base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fingerprint": fingerprint(base)}), encoding="utf-8")
    except OSError:
        # A checkout that cannot write this can still export. Losing the
        # guard is worse than nothing, but refusing to work at all is
        # worse than that.
        pass


def files_are_ahead(base_dir=None) -> "str | None":
    """Why an export must not run, or None: the review files have changed
    since the database was last written from them or into them.

    The failure this exists for, and it has happened. A plain `git pull`
    brings new review work as text and leaves the database exactly as it
    was -- so the database is now *older* than the files, and an export
    writes it back over them, removing every row and every file it did
    not itself produce. One pull and one commit an hour apart cost 193
    verified provisions, 207 corrections and 9 link annotations.

    sync.pull imports after merging and is safe. `git pull` from a
    terminal is not, and the pre-commit hook exports on any commit
    touching data/review/, so nothing unusual has to happen.

    unloaded() does not catch this: it refuses only when the database is
    *entirely* empty, which is the fresh clone it was written for. A
    database that is merely out of date looks perfectly healthy.

    With no state recorded -- a checkout from before this guard -- the
    current state is adopted rather than refused, because refusing would
    stop every existing checkout working and the far commoner reading of
    "no record" is "this is the first export since an upgrade"."""
    base = Path(base_dir or ".")
    if not review_dir(base).is_dir():
        return None
    try:
        recorded = json.loads(_state_path(base).read_text(encoding="utf-8")).get("fingerprint")
    except (OSError, ValueError):
        recorded = None
    if not recorded:
        _record_state(base)
        return None
    if recorded == fingerprint(base):
        return None
    return (
        f"The review files in {review_dir(base)} have changed since this database was last "
        "written from them -- a `git pull` or a checkout, most likely. Exporting now would "
        "write this database back over them and delete the work that arrived.\n\n"
        "Load it first:\n\n    python3 -m corpus.review.review_sync import\n\n"
        "If you are certain this database is the newer of the two, take the files as they are "
        "with `python3 -m corpus.review.review_sync adopt`, which records them as seen without "
        "changing either side."
    )


def adopt(base_dir=None) -> str:
    """Records the review files as they stand, without loading them.

    The way past files_are_ahead when the database really is the newer of
    the two -- after resolving a merge conflict by hand, say. Deliberately
    a separate, named thing rather than a flag on export: choosing which
    of two versions of somebody's review work survives is not a thing to
    do in passing."""
    base = Path(base_dir or ".")
    _record_state(base)
    return f"Took {review_dir(base)} as it stands. The next export will write over it."


def unloaded(base_dir=None) -> "str | None":
    """Why an export must not run, or None.

    The one state this arrangement can destroy work in, and it is the
    ordinary state of a fresh clone: the text is there, the database is
    not yet built from it, and `db._connect` will happily create an empty
    one on being asked a question. An export from that database writes
    nothing and then removes every file it did not write -- so the first
    status poll after a clone would delete the entire corpus's review
    work, report success, and leave a clean tree.

    So: files with rows in them, and a database with no rows in any
    table, means the import has not been run. Nothing else counts. A
    database with a single row anywhere is a database somebody is working
    in."""
    base = Path(base_dir or ".")
    out = review_dir(base)
    if not out.is_dir():
        return None
    files = [p for p in out.rglob("*.jsonl") if p.stat().st_size > 0]
    if not files:
        return None

    from corpus.storage import db

    target = db.db_path(base)
    # exists() first, and no connection if it does not -- connecting is
    # what creates the empty database this exists to notice.
    if target.exists():
        conn = db._connect(base)
        for table in describe(conn):
            if conn.execute(f"SELECT 1 FROM {table.name} LIMIT 1").fetchone():
                return None
    return (f"There are {len(files)} file(s) of review work in {out} and nothing in the "
            "database, so it has not been built from them yet. Run "
            "`python3 -m corpus.review.review_sync import`. Until then nothing here is exported, "
            "because an export from an empty database would delete every one of those files.")


def export(base_dir=None, conn: "sqlite3.Connection | None" = None) -> dict:
    """Writes the database out as text. Returns what it did.

    Safe to call often: files whose content has not changed are not
    touched, and files whose rows have all gone are removed -- a stale
    file left behind would be re-imported later and quietly resurrect
    work somebody deleted."""
    from corpus.storage import db

    base = Path(base_dir or ".")
    # Before connecting, for the reason unloaded() gives. Both checks ask
    # the same question -- is this database fit to be written over those
    # files? -- at the two scales it can be wrong at: never loaded, and
    # loaded but since overtaken.
    if conn is None:
        blocked = unloaded(base) or files_are_ahead(base)
        if blocked:
            raise Unloaded(blocked)
    owned = conn is None
    conn = conn or db._connect(base)
    out = review_dir(base)

    wanted: dict = {}
    for table in describe(conn):
        rows = conn.execute(f"SELECT * FROM {table.name}").fetchall()
        buckets: dict = {}
        for row in rows:
            line = _line(table, row)
            shard = row[table.shard] if table.shard else None
            buckets.setdefault(shard, []).append((table.sort_key(dict(row), line), line))
        for shard, entries in buckets.items():
            entries.sort()
            name = f"{table.name}.jsonl"
            path = out / shard / name if shard else out / name
            wanted[path] = "".join(line + "\n" for _key, line in entries)

    written = sum(_write_if_changed(path, text) for path, text in wanted.items())

    # Anything under data/review/ that this export did not produce is a
    # table or an act whose rows are gone.
    removed = 0
    if out.is_dir():
        for path in sorted(out.rglob("*.jsonl")):
            if path not in wanted:
                path.unlink()
                removed += 1
        for directory in sorted(out.rglob("*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

    if owned:
        pass  # db owns its connections; closing here would drop a shared one
        # What is on disk is now exactly what this database says, so say
        # so -- this is the only moment the two are known to agree.
        _record_state(base)
    return {"files": len(wanted), "written": written, "removed": removed,
            "path": str(out)}


class ImportError_(RuntimeError):
    """A review file could not be read. Nothing was changed."""


def read_rows(path: Path, table: Table) -> list:
    """One file's rows, or a refusal naming the line that could not be
    read.

    Half-importing a file is how you end up with a corpus that is neither
    what was on disk nor what was in the database, and no way to tell
    which rows made it."""
    rows = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as e:
            raise ImportError_(f"{path}:{number} is not valid JSON -- {e}") from e
        if not isinstance(record, dict):
            raise ImportError_(f"{path}:{number} is not a JSON object.")
        unknown = set(record) - set(table.columns)
        if unknown:
            raise ImportError_(
                f"{path}:{number} has field(s) the {table.name} table does not "
                f"have: {', '.join(sorted(unknown))}")
        rows.append(record)
    return rows


def _insert(conn: sqlite3.Connection, table: Table, rows: list) -> None:
    """Puts one file's rows into a table.

    Grouped by which columns each row actually carries, rather than
    filling the absent ones with NULL. A column added to the schema after
    a file was written is absent from every line in it, and a NULL there
    would fail against the NOT NULL DEFAULT the column was added with --
    so the file would stop importing for a reason that has nothing to do
    with the review work in it. Letting the default apply is what the
    default is for."""
    groups: dict = {}
    for row in rows:
        columns = tuple(c for c in table.columns if c not in table.omit and c in row)
        groups.setdefault(columns, []).append(row)
    for columns, group in groups.items():
        if not columns:
            continue
        conn.executemany(
            f"INSERT INTO {table.name} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [[row[c] for c in columns] for row in group])


def load_into(conn: sqlite3.Connection, out: Path) -> dict:
    """Reads every file under data/review/ into an already-built schema.

    Shared by the import and by `check`, so that what the round trip
    proves is what the import actually does."""
    tables = {t.name: t for t in describe(conn)}
    counts: dict = {}
    for path in sorted(out.rglob("*.jsonl")):
        table = tables.get(path.stem)
        if table is None:
            raise ImportError_(
                f"{path} names a table this database does not have ({path.stem}).")
        rows = read_rows(path, table)
        if not rows:
            continue
        _insert(conn, table, rows)
        counts[table.name] = counts.get(table.name, 0) + len(rows)
    return counts


def import_(base_dir=None, backup: bool = True) -> dict:
    """Rebuilds the database from data/review/.

    Built into a new file and moved into place, so a file that cannot be
    read leaves the database that is there untouched -- the same reason
    the search index is built into a scratch file and renamed.

    The database being replaced is copied aside first. It is meant to be
    derived, but "meant to be" is not a guarantee, and this is the one
    operation that could discard a review nobody had exported yet."""
    from corpus.storage import db

    base = Path(base_dir or ".")
    out = review_dir(base)
    if not out.is_dir():
        raise ImportError_(f"There is no {out} to import from.")

    target = db.db_path(base)
    scratch = target.with_name(target.name + ".importing")
    scratch.unlink(missing_ok=True)

    fresh = sqlite3.connect(str(scratch))
    fresh.row_factory = sqlite3.Row
    counts: dict = {}
    try:
        fresh.executescript(db._SCHEMA)
        counts = load_into(fresh, out)
        fresh.commit()
    except sqlite3.Error as e:
        fresh.close()
        scratch.unlink(missing_ok=True)
        raise ImportError_(f"Could not rebuild the database: {e}") from e
    except ImportError_:
        fresh.close()
        scratch.unlink(missing_ok=True)
        raise
    finally:
        try:
            fresh.close()
        except sqlite3.Error:
            pass

    kept = None
    if backup and target.exists():
        from .sync import BACKUP_DIR
        import time

        backups = base / BACKUP_DIR
        backups.mkdir(parents=True, exist_ok=True)
        kept = backups / f"legislation-{time.strftime('%Y%m%d-%H%M%S')}.db"
        shutil.copy2(target, kept)

    # sqlite holds the file it opened by descriptor, so a connection left
    # open here would go on reading the replaced one for ever.
    db.close_connections()
    for sidecar in ("-wal", "-shm"):
        target.with_name(target.name + sidecar).unlink(missing_ok=True)
    os.replace(scratch, target)
    # The other moment the two are known to agree.
    _record_state(base)
    return {"rows": counts, "total": sum(counts.values()),
            "backup": str(kept) if kept else None, "path": str(target)}


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("direction", choices=("export", "import", "check", "adopt"),
                    help="export: database -> data/review/. "
                         "import: data/review/ -> database. "
                         "check: export, import into a scratch copy, compare. "
                         "adopt: record the files as seen without loading them.")
    ap.add_argument("--base-dir", default=".")
    args = ap.parse_args()

    try:
        _run(args)
    except (Unloaded, ImportError_) as e:
        # A refusal is the normal way this says no, and a traceback would
        # bury the sentence that says what to do about it.
        print(str(e), file=sys.stderr)
        raise SystemExit(1)


def _run(args):
    if args.direction == "export":
        stats = export(args.base_dir)
        print(f"{stats['files']} file(s), {stats['written']} rewritten, "
              f"{stats['removed']} removed -> {stats['path']}")
    elif args.direction == "import":
        stats = import_(args.base_dir)
        print(f"{stats['total']} row(s) into {stats['path']}")
        for name, n in sorted(stats["rows"].items()):
            print(f"   {name:20} {n:>6}")
        if stats["backup"]:
            print(f"previous database kept at {stats['backup']}")
    elif args.direction == "adopt":
        print(adopt(args.base_dir))
    else:
        print(check(args.base_dir))


def check(base_dir=None) -> str:
    """Exports, imports into a scratch database, and compares every row.

    The one question worth being able to answer on demand: would a round
    trip through text lose anything?"""
    from corpus.storage import db

    base = Path(base_dir or ".")
    export(base)
    live = db._connect(base)
    tables = describe(live)
    before = {t.name: sorted(_line(t, r) for r in live.execute(f"SELECT * FROM {t.name}"))
              for t in tables}

    scratch = Path(str(base / "data" / "legislation.roundtrip.db"))
    scratch.unlink(missing_ok=True)
    fresh = sqlite3.connect(str(scratch))
    fresh.row_factory = sqlite3.Row
    try:
        fresh.executescript(db._SCHEMA)
        load_into(fresh, review_dir(base))
        fresh.commit()
        after = {t.name: sorted(_line(t, r) for r in fresh.execute(f"SELECT * FROM {t.name}"))
                 for t in describe(fresh)}
    finally:
        fresh.close()
        scratch.unlink(missing_ok=True)

    lines = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name, []), after.get(name, [])
        mark = "ok  " if a == b else "LOST"
        lines.append(f"  {mark} {name:20} {len(a):>6} -> {len(b):>6}")
    verdict = "identical" if before == after else "DIFFERENT -- do not rely on this"
    return "\n".join(lines) + f"\n{verdict}"


if __name__ == "__main__":
    main()
