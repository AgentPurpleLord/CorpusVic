"""
SQLite-backed storage for the durable, human-created review data that used
to live in data/verified/<act>.json, data/links/<act>.json, and
data/corrections.jsonl -- one shared file, data/legislation.db, holding
every Act.

Deliberately scoped to *only* that data, not the whole pipeline:
data/extracted/, data/diagnostics/, data/akn/, and data/markdown/ stay
exactly as they are, plain JSON/XML/Markdown files under data/, because
they're pure regenerable pipeline output -- re-running run_pipeline.py /
export_akn.py / export_markdown.py recreates them from the source PDF,
so there's nothing there that "longevity" is actually about. What
genuinely needs it is the other three: hours of a human's own
accept/flag/edit decisions, span-level link annotations, and the
correction log run_pipeline.py reads back in as few-shot examples --
exactly the things a crash mid `path.write_text(json.dumps(whole_file))`
could previously corrupt outright.

data/ai_parsed/<act>.json (the raw parse) is technically regenerable the
same way, but is committed to git *alongside* data/legislation.db as a
deliberate pair -- see .gitignore's own comment on this -- since this
file's own verified rows are keyed by a positional index into that exact
parse, and a mismatched regeneration would silently misalign them (see
migrate_json_to_sqlite.py for bringing any pre-migration JSON review data
into this file, and checkpoint_db.py before committing this one).

Every function here keeps the exact name and dict/list shape its old
JSON-backed counterpart had (load_verified/save_verified in review.py,
load_links/save_links/add_link/delete_link in link_annotations.py,
add_correction/load_examples/stats in examples_store.py) -- callers
elsewhere in the pipeline don't change at all, only where this data
actually lives.

Connections are cached per resolved absolute path, not just opened once
at import time: every consumer resolves "data/legislation.db" relative to
the current working directory exactly the way the old JSON paths did
(Path("data/verified") / f"{act}.json", etc), and tests rely on that --
monkeypatch.chdir(tmp_path) to isolate a test still works unchanged,
because a different CWD resolves to a different absolute db path and
therefore a fresh connection/schema, the same isolation a fresh directory
of JSON files used to give for free.
"""
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LABELS = ["act_citation", "defined_term", "bill_reference", "em_reference", "other"]


class LinkError(ValueError):
    """A link annotation request was invalid -- an out-of-range or empty
    span, or a label outside LABELS. Raised before anything is written,
    so a bad request from the frontend can't corrupt stored link data."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS verified (
    act TEXT NOT NULL,
    source_node_index INTEGER NOT NULL,
    type TEXT NOT NULL,
    number TEXT,
    heading TEXT,
    text TEXT NOT NULL DEFAULT '',
    page_start INTEGER,
    page_end INTEGER,
    char_start INTEGER,
    char_end INTEGER,
    source TEXT,
    path_json TEXT,
    history_json TEXT,
    verified_at TEXT,
    needs_followup INTEGER NOT NULL DEFAULT 0,
    unit_end_index INTEGER,
    PRIMARY KEY (act, source_node_index)
);
CREATE INDEX IF NOT EXISTS idx_verified_act ON verified(act);

CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    act TEXT NOT NULL,
    node_index INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    text TEXT,
    label TEXT NOT NULL,
    target_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_act ON links(act);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    act TEXT NOT NULL,
    ts REAL NOT NULL,
    changed INTEGER NOT NULL,
    ai_output_json TEXT NOT NULL,
    human_output_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_changed ON corrections(changed);

CREATE TABLE IF NOT EXISTS blind_reviews (
    act TEXT NOT NULL,
    node_index INTEGER NOT NULL,
    guessed_type TEXT NOT NULL,
    guessed_number TEXT,
    guessed_heading TEXT,
    reasoning TEXT NOT NULL,
    matched_type INTEGER NOT NULL,
    matched_number INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (act, node_index)
);
CREATE INDEX IF NOT EXISTS idx_blind_reviews_act ON blind_reviews(act);
"""

_connections: dict[str, sqlite3.Connection] = {}


def db_path(base_dir: "str | Path | None" = None) -> Path:
    """base_dir anchors the db file at a caller-chosen directory instead
    of the current working directory -- review.py assumes it's always
    launched from the repo root, same as its own existing data/*.json
    paths always did, so it never needs to pass this. dashboard.py is
    designed to be run from anywhere (it resolves its own data/ paths off
    its own BASE_DIR, not cwd), so it passes that explicitly on every
    call rather than this module trying to cache a base dir globally --
    which would go stale the moment a test (or anything else) points
    BASE_DIR somewhere else after the fact."""
    return Path(base_dir or ".") / "data" / "legislation.db"


def _connect(base_dir: "str | Path | None" = None) -> sqlite3.Connection:
    path = db_path(base_dir)
    key = str(path.resolve())
    conn = _connections.get(key)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _connections[key] = conn
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Verified review state (was data/verified/<act>.json)
# ---------------------------------------------------------------------------

_VERIFIED_COLUMNS = (
    "type", "number", "heading", "text", "page_start", "page_end",
    "char_start", "char_end", "source", "path_json", "history_json",
    "verified_at", "needs_followup", "unit_end_index",
)


def _verified_row_to_dict(row: sqlite3.Row) -> dict:
    node = {
        "type": row["type"], "number": row["number"], "heading": row["heading"], "text": row["text"],
        "page_start": row["page_start"], "page_end": row["page_end"],
        "char_start": row["char_start"], "char_end": row["char_end"], "source": row["source"],
        "_source_node_index": row["source_node_index"],
    }
    if row["path_json"] is not None:
        node["path"] = json.loads(row["path_json"])
    if row["history_json"] is not None:
        node["history"] = json.loads(row["history_json"])
    if row["verified_at"] is not None:
        node["verified_at"] = row["verified_at"]
    if row["needs_followup"]:
        node["needs_followup"] = True
    if row["unit_end_index"] is not None:
        node["_unit_end_index"] = row["unit_end_index"]
    return node


def load_verified(act: str, base_dir: "str | Path | None" = None) -> list[dict]:
    rows = _connect(base_dir).execute(
        "SELECT * FROM verified WHERE act = ? ORDER BY source_node_index", (act,)
    ).fetchall()
    return [_verified_row_to_dict(row) for row in rows]


def save_verified(act: str, verified: list[dict], base_dir: "str | Path | None" = None) -> None:
    """Replaces every stored verified entry for this Act with exactly
    what's in `verified` now -- the same "overwrite the whole file with
    the current in-memory list" semantics review.py's callers already
    rely on (they hold the complete, authoritative in-memory list and
    call this every time any part of it changes), just as one transaction
    instead of one whole-file rewrite."""
    conn = _connect(base_dir)
    with conn:
        conn.execute("DELETE FROM verified WHERE act = ?", (act,))
        conn.executemany(
            f"INSERT INTO verified (act, source_node_index, {', '.join(_VERIFIED_COLUMNS)}) "
            f"VALUES (?, ?, {', '.join('?' for _ in _VERIFIED_COLUMNS)})",
            [
                (
                    act, node.get("_source_node_index"), node["type"], node.get("number"), node.get("heading"),
                    node.get("text") or "", node.get("page_start"), node.get("page_end"),
                    node.get("char_start"), node.get("char_end"), node.get("source"),
                    json.dumps(node["path"]) if node.get("path") is not None else None,
                    json.dumps(node["history"]) if node.get("history") is not None else None,
                    node.get("verified_at"), 1 if node.get("needs_followup") else 0,
                    node.get("_unit_end_index"),
                )
                for node in verified
            ],
        )


# ---------------------------------------------------------------------------
# Link annotations (was data/links/<act>.json)
# ---------------------------------------------------------------------------

def _link_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "node_index": row["node_index"], "start": row["start"], "end": row["end"],
        "text": row["text"], "label": row["label"],
        "target": json.loads(row["target_json"]) if row["target_json"] is not None else None,
        "created_at": row["created_at"],
    }


def load_links(act: str) -> list[dict]:
    rows = _connect().execute("SELECT * FROM links WHERE act = ? ORDER BY created_at", (act,)).fetchall()
    return [_link_row_to_dict(row) for row in rows]


def save_links(act: str, links: list[dict]) -> None:
    """Bulk-replace, same "whole file" semantics as save_verified -- not
    on review.py's own hot path (it uses add_link/delete_link, each one
    a single-row change), but kept for parity with the old JSON API and
    for tests/bulk imports (see migrate_json_to_sqlite.py)."""
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM links WHERE act = ?", (act,))
        conn.executemany(
            "INSERT INTO links (id, act, node_index, start, end, text, label, target_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    link["id"], act, link["node_index"], link["start"], link["end"], link.get("text"),
                    link["label"], json.dumps(link["target"]) if link.get("target") is not None else None,
                    link.get("created_at") or _now_iso(),
                )
                for link in links
            ],
        )


def add_link(act: str, node_index: int, start: int, end: int, label: str, node_text: str, target: dict | None = None) -> dict:
    """Validates and stores one span annotation, returning the saved
    record (with a fresh id and timestamp). `node_text` is the exact
    stored text of the node being annotated, passed in by the caller
    (which already has the parsed nodes loaded) rather than reloaded here
    -- keeps this a pure function callers can unit-test without touching
    any Act's own parsed data at all.

    `target` is an optional pre-resolved destination (see
    ai_pipeline/link_targets.py's resolve_link) -- which Act, which
    definition node, etc. this span points to. Resolution is the caller's
    job, not this module's: this stays a plain storage/validation layer,
    with no opinion on what counts as a valid target beyond "whatever the
    caller decided." None means unresolved (most bill_reference/
    em_reference spans, or any span whose text didn't match anything),
    not an error."""
    if label not in LABELS:
        raise LinkError(f"Unknown label {label!r} -- must be one of {LABELS}")
    if not (0 <= start < end <= len(node_text)):
        raise LinkError(f"Span {start}:{end} is out of range for a {len(node_text)}-character node")
    record = {
        "id": uuid.uuid4().hex,
        "node_index": node_index,
        "start": start,
        "end": end,
        "text": node_text[start:end],
        "label": label,
        "target": target,
        "created_at": _now_iso(),
    }
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO links (id, act, node_index, start, end, text, label, target_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["id"], act, node_index, start, end, record["text"], label,
                json.dumps(target) if target is not None else None, record["created_at"],
            ),
        )
    return record


def delete_link(act: str, link_id: str) -> bool:
    """Removes one annotation by id. Returns False (no-op) if the id
    isn't found for this Act -- callers surface that as a 404 rather than
    silently succeeding on a stale or mistyped id."""
    conn = _connect()
    with conn:
        cur = conn.execute("DELETE FROM links WHERE act = ? AND id = ?", (act, link_id))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Correction log (was data/corrections.jsonl, shared across every Act)
# ---------------------------------------------------------------------------

def add_correction(act: str, ai_output: dict, human_output: dict, changed: bool) -> None:
    fields = ("type", "number", "heading", "text")
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO corrections (act, ts, changed, ai_output_json, human_output_json) VALUES (?, ?, ?, ?, ?)",
            (
                act, time.time(), 1 if changed else 0,
                json.dumps({k: ai_output.get(k) for k in fields}),
                json.dumps({k: human_output.get(k) for k in fields}),
            ),
        )


def _correction_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "ts": row["ts"], "act": row["act"], "changed": bool(row["changed"]),
        "ai_output": json.loads(row["ai_output_json"]), "human_output": json.loads(row["human_output_json"]),
    }


def load_examples(k: int = 6) -> list[dict]:
    """Most recent corrected examples first, plus a few confirmed-correct
    ones -- same selection commit_unit's few-shot prompt-building always
    used, across every Act (not just one), since a correction from any
    Act is still a useful example of what a human actually approved."""
    conn = _connect()
    changed_rows = conn.execute(
        "SELECT * FROM corrections WHERE changed = 1 ORDER BY ts DESC LIMIT ?", (k,)
    ).fetchall()
    unchanged_rows = conn.execute(
        "SELECT * FROM corrections WHERE changed = 0 ORDER BY ts DESC LIMIT ?", (max(0, k // 2),)
    ).fetchall()
    return [_correction_row_to_dict(r) for r in changed_rows] + [_correction_row_to_dict(r) for r in unchanged_rows]


def stats() -> dict:
    row = _connect().execute("SELECT COUNT(*) AS total, COALESCE(SUM(changed), 0) AS changed FROM corrections").fetchone()
    return {"total": row["total"], "changed": row["changed"]}


# ---------------------------------------------------------------------------
# Blind reviews -- a reviewer's own, independent classification of an
# elevated-risk piece (AI-engine-sourced, or carrying a diagnostics
# finding), recorded *before* review.py's UI reveals what the parser
# actually produced. New in this project: there's no prior JSON-file
# equivalent, so it goes straight into the DB rather than following an
# existing shape.
# ---------------------------------------------------------------------------

def _blind_review_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "guessed_type": row["guessed_type"], "guessed_number": row["guessed_number"],
        "guessed_heading": row["guessed_heading"], "reasoning": row["reasoning"],
        "matched_type": bool(row["matched_type"]), "matched_number": bool(row["matched_number"]),
        "reviewed_at": row["reviewed_at"],
    }


def get_blind_review(act: str, node_index: int) -> "dict | None":
    row = _connect().execute(
        "SELECT * FROM blind_reviews WHERE act = ? AND node_index = ?", (act, node_index)
    ).fetchone()
    return _blind_review_row_to_dict(row) if row is not None else None


def save_blind_review(
    act: str, node_index: int, *, guessed_type: str, guessed_number: "str | None", guessed_heading: "str | None",
    reasoning: str, matched_type: bool, matched_number: bool,
) -> dict:
    """One row per (act, node_index): re-submitting overwrites rather than
    accumulating a history, since the point is a single honest first
    read, not a record of every attempt at guessing again."""
    reviewed_at = _now_iso()
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO blind_reviews (act, node_index, guessed_type, guessed_number, guessed_heading, "
            "reasoning, matched_type, matched_number, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(act, node_index) DO UPDATE SET guessed_type=excluded.guessed_type, "
            "guessed_number=excluded.guessed_number, guessed_heading=excluded.guessed_heading, "
            "reasoning=excluded.reasoning, matched_type=excluded.matched_type, "
            "matched_number=excluded.matched_number, reviewed_at=excluded.reviewed_at",
            (
                act, node_index, guessed_type, guessed_number, guessed_heading, reasoning,
                1 if matched_type else 0, 1 if matched_number else 0, reviewed_at,
            ),
        )
    return {
        "guessed_type": guessed_type, "guessed_number": guessed_number, "guessed_heading": guessed_heading,
        "reasoning": reasoning, "matched_type": matched_type, "matched_number": matched_number,
        "reviewed_at": reviewed_at,
    }


def blind_review_stats(act: "str | None" = None) -> dict:
    """Aggregate agreement rate across every blind review recorded so far
    (one Act, or every Act when act is None) -- a genuine accuracy signal
    on the parser itself, not just a review-friction nudge: how often a
    reviewer's own first, independent read actually matched what the
    parser produced, before they ever saw it."""
    conn = _connect()
    query = "SELECT COUNT(*) AS total, COALESCE(SUM(matched_type), 0) AS type_matched, COALESCE(SUM(matched_number), 0) AS number_matched FROM blind_reviews"
    row = conn.execute(f"{query} WHERE act = ?", (act,)).fetchone() if act is not None else conn.execute(query).fetchone()
    return {"total": row["total"], "type_matched": row["type_matched"], "number_matched": row["number_matched"]}
