"""
SQLite-backed storage for the durable, human-created review data that
used to live in data/verified/<act>.json, data/links/<act>.json, and
data/corrections.jsonl -- now one shared file, data/legislation.db,
holding every Act.

This is deliberately scoped to *only* that data, not the whole
pipeline: data/extracted/, data/diagnostics/, data/akn/, and
data/markdown/ stay exactly as they are, plain JSON/XML/Markdown files
under data/, because they're pipeline output that can always be
regenerated -- re-running run_pipeline.py, export_akn.py or
export_markdown.py recreates them from the source PDF, so there's
nothing there that "durability" really needs to protect. What actually
needs it is the other three: hours of a human's own accept/flag/edit
decisions, span-level link labels, and the correction log recording
what a human actually changed -- exactly the things a crash partway
through writing a whole JSON file back out used to be able to corrupt.

data/ai_parsed/<act>.json (the raw parse) could technically be
regenerated the same way, but it's committed to git *alongside*
data/legislation.db as a deliberate pair -- see .gitignore's own
comment on this -- because this file's verified rows are keyed by a
position in that exact parse, and regenerating a mismatched one would
silently misalign them (see migrate_json_to_sqlite.py for bringing any
pre-migration JSON review data into this file, and checkpoint_db.py
before committing this one).

Every function here keeps the exact name and shape its old JSON-backed
counterpart had (load_verified/save_verified in review.py,
load_links/save_links/add_link/delete_link in link_annotations.py,
add_correction/stats in examples_store.py) -- callers elsewhere in the
pipeline don't change at all, only where this data actually lives.

Connections are cached per resolved absolute path, not just opened once
when the module loads: every caller resolves "data/legislation.db"
relative to the current working directory, exactly the way the old JSON
paths did (Path("data/verified") / f"{act}.json", etc), and tests rely
on that -- using monkeypatch.chdir(tmp_path) to isolate a test still
works unchanged, because a different working directory resolves to a
different absolute database path, and therefore a fresh connection and
schema -- the same isolation a fresh directory of JSON files used to
give for free.
"""
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LABELS = ["act_citation", "defined_term", "bill_reference", "em_reference", "other"]


class LinkError(ValueError):
    """A link-labelling request was invalid -- an out-of-range or empty
    span, or a label not in LABELS. Raised before anything is written,
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
    -- What the parser produced, before the human changed it. The column
    -- keeps its original name: it predates the removal of the
    -- model-backed parser this project used to also offer, and renaming
    -- it would mean migrating every committed database for no gain.
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

-- A local model's answer to one bounded question about one elevated-risk
-- node (see ai_pipeline/ai_assist.py) -- offered only after a reviewer's
-- own blind_reviews row already exists for that node, never before, so
-- it can't anchor the independent judgement that step is there to get.
-- Keyed the same way blind_reviews is, and moved/blocked by rename_act
-- the same way, since it's cached against a specific parse position too
-- -- see _HUMAN_WORK_TABLES below.
CREATE TABLE IF NOT EXISTS ai_suggestions (
    act TEXT NOT NULL,
    node_index INTEGER NOT NULL,
    answer TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    confidence TEXT NOT NULL,
    model TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    PRIMARY KEY (act, node_index)
);
CREATE INDEX IF NOT EXISTS idx_ai_suggestions_act ON ai_suggestions(act);

-- Extra node types one Act's reviewer defined for themselves, on top of
-- schema.NODE_TYPES and whatever levels that Act's profile declares.
-- Kept per-Act rather than shared across all of them on purpose: a
-- label that makes sense in one Act ("penalty", say) would just be
-- clutter in the relabel dropdown of every other. These are labels,
-- not hierarchy levels -- an Act's nesting order comes from its profile
-- (see ai_pipeline/hierarchy.py), so a type added here doesn't nest and
-- can't have anything nested under it.
-- Which parse the rows in `verified` were reviewed against.
-- Those rows are keyed by a *position* in data/ai_parsed/<act>.json, so
-- they only make sense against the exact parse that produced them.
-- Storing that parse's fingerprint (ai_pipeline/reparse.parse_fingerprint)
-- lets a mismatch be detected instead of passing silently -- review.py
-- refuses to infer anything from positions it can't vouch for, and
-- run_pipeline.py knows when a re-parse needs its rows reattached.
CREATE TABLE IF NOT EXISTS parse_state (
    act TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Reviewed pieces a re-parse couldn't find anywhere in the new node
-- list any more. They have no position left to be keyed by, so they
-- can't stay in `verified` -- but they're a human's work, and deleting
-- them just because the parser changed its mind is exactly the failure
-- that reattaching review work exists to prevent. Kept here, reported,
-- and never silently dropped.
CREATE TABLE IF NOT EXISTS orphaned_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    act TEXT NOT NULL,
    node_json TEXT NOT NULL,
    orphaned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orphaned_reviews_act ON orphaned_reviews(act);

CREATE TABLE IF NOT EXISTS custom_types (
    act TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (act, name)
);
"""

_connections: dict[str, sqlite3.Connection] = {}


def db_path(base_dir: "str | Path | None" = None) -> Path:
    """base_dir puts the database file at a caller-chosen directory
    instead of the current working directory. review.py assumes it's
    always launched from the repo root, the same way its old data/*.json
    paths always did, so it never needs to pass this. dashboard.py is
    designed to run from anywhere (it resolves its own data/ paths off
    its own BASE_DIR, not the working directory), so it passes that
    explicitly on every call, rather than this module trying to cache a
    base directory globally -- which would go stale the moment a test
    (or anything else) points BASE_DIR somewhere else afterwards."""
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
    what's in `verified` now. This matches the "overwrite the whole
    thing with the current in-memory list" behaviour review.py's callers
    already rely on (they hold the complete, up-to-date list in memory
    and call this every time any part of it changes) -- just as one
    database transaction instead of rewriting a whole file."""
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
# Which parse the verified rows above belong to
# ---------------------------------------------------------------------------

def load_parse_fingerprint(act: str, base_dir: "str | Path | None" = None) -> "str | None":
    """The fingerprint of the parse this Act's verified rows were
    reviewed against, or None for rows stored before fingerprints
    existed. None is not the same as "matches" -- a caller that needs to
    trust a stored `_source_node_index` must treat None as "can't vouch
    for this"."""
    row = _connect(base_dir).execute(
        "SELECT fingerprint FROM parse_state WHERE act = ?", (act,)
    ).fetchone()
    return row["fingerprint"] if row else None


def save_parse_fingerprint(act: str, fingerprint: str, base_dir: "str | Path | None" = None) -> None:
    conn = _connect(base_dir)
    with conn:
        conn.execute(
            "INSERT INTO parse_state (act, fingerprint, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(act) DO UPDATE SET fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
            (act, fingerprint, _now_iso()),
        )


def load_orphaned_reviews(act: str, base_dir: "str | Path | None" = None) -> list[dict]:
    rows = _connect(base_dir).execute(
        "SELECT id, node_json, orphaned_at FROM orphaned_reviews WHERE act = ? ORDER BY id", (act,)
    ).fetchall()
    return [{**json.loads(r["node_json"]), "_orphan_id": r["id"], "_orphaned_at": r["orphaned_at"]} for r in rows]


def add_orphaned_reviews(act: str, nodes: list[dict], base_dir: "str | Path | None" = None) -> None:
    """Adds to the table, never replaces it -- two re-parses in a row can
    each strand a different piece, and the second one running must not
    erase what the first one recorded."""
    if not nodes:
        return
    conn = _connect(base_dir)
    at = _now_iso()
    with conn:
        conn.executemany(
            "INSERT INTO orphaned_reviews (act, node_json, orphaned_at) VALUES (?, ?, ?)",
            [(act, json.dumps(node), at) for node in nodes],
        )


# A human's own work, keyed by document slug. custom_types is here too:
# a reviewer's own label belongs to them like everything else, and a
# rename that left it behind would strand it under a slug nothing
# addresses any more. ai_suggestions isn't a human's own work, but it's
# cached against a specific node position the same way blind_reviews
# is, so it needs the same move-or-block treatment or a rename would
# leave it silently pointing at whatever node now sits in that old
# position.
_HUMAN_WORK_TABLES = (
    "verified", "links", "corrections", "blind_reviews", "orphaned_reviews", "custom_types", "ai_suggestions",
)

# Bookkeeping the pipeline writes about a parse, not anything a person
# did. Moved along with the rest, but overwritten at the destination
# rather than blocking the move.
_DERIVED_TABLES = ("parse_state",)


def rename_act(old: str, new: str, base_dir: "str | Path | None" = None) -> dict[str, int]:
    """Moves every stored row from one document slug to another,
    returning {table: rows moved}.

    This is needed because a document's slug is its identity here -- the
    parse's filename, the review database's key, the browse URL -- so
    filing a document under a new name (an Act starting version
    tracking becomes "criminal-procedure-act-v114") would otherwise
    strand hours of review work under a slug nothing looks up any more.

    Refuses to run rather than merging if `new` already holds a
    person's work: two documents' review interleaved by node position
    would be worse than either alone, with no way to tell afterwards
    which decision belonged to which. The parse fingerprint doesn't
    count as that kind of work -- it's just what the pipeline recorded
    about the destination's own parse, and it has to be overwritten, or
    the moved rows would be left claiming to belong to a parse they
    were never actually reviewed against.
    """
    conn = _connect(base_dir)
    existing = sum(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE act = ?", (new,)).fetchone()[0]
        for table in _HUMAN_WORK_TABLES
    )
    if existing:
        raise ValueError(
            f"{new!r} already has {existing} stored row(s) of review work -- refusing to merge "
            "two documents into one set of node positions."
        )
    moved = {}
    with conn:
        for table in _DERIVED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE act = ?", (new,))
        for table in _HUMAN_WORK_TABLES + _DERIVED_TABLES:
            cur = conn.execute(f"UPDATE {table} SET act = ? WHERE act = ?", (new, old))
            if cur.rowcount:
                moved[table] = cur.rowcount
    return moved


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
    """Replaces the whole set at once, same as save_verified -- not
    something review.py itself uses often (it calls add_link/delete_link
    instead, one row at a time), but kept to match the old JSON API and
    for tests and bulk imports (see migrate_json_to_sqlite.py)."""
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
    """Checks and stores one span label, returning the saved record
    (with a fresh id and timestamp). `node_text` is the exact stored
    text of the node being labelled, passed in by the caller (which
    already has the parsed nodes loaded) rather than reloaded here --
    that keeps this a plain function callers can test without touching
    any Act's parsed data at all.

    `target` is an optional destination worked out ahead of time (see
    ai_pipeline/link_targets.py's resolve_link) -- which Act, which
    definition node, etc. this span points to. Working that out is the
    caller's job, not this module's: this stays a plain storage and
    validation layer, with no opinion on what counts as a valid target
    beyond "whatever the caller decided." None means unresolved (most
    bill_reference/em_reference spans, or any span whose text didn't
    match anything) -- that's not an error."""
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
    """Removes one label by id. Returns False (does nothing) if the id
    isn't found for this Act -- callers turn that into a 404 rather than
    silently succeeding on a stale or mistyped id."""
    conn = _connect()
    with conn:
        cur = conn.execute("DELETE FROM links WHERE act = ? AND id = ?", (act, link_id))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Correction log (was data/corrections.jsonl, shared across every Act)
# ---------------------------------------------------------------------------

def add_correction(act: str, parser_output: dict, human_output: dict, changed: bool) -> None:
    fields = ("type", "number", "heading", "text")
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO corrections (act, ts, changed, ai_output_json, human_output_json) VALUES (?, ?, ?, ?, ?)",
            (
                act, time.time(), 1 if changed else 0,
                json.dumps({k: parser_output.get(k) for k in fields}),
                json.dumps({k: human_output.get(k) for k in fields}),
            ),
        )


def stats() -> dict:
    row = _connect().execute("SELECT COUNT(*) AS total, COALESCE(SUM(changed), 0) AS changed FROM corrections").fetchone()
    return {"total": row["total"], "changed": row["changed"]}


# ---------------------------------------------------------------------------
# Blind reviews -- a reviewer's own independent classification of a
# higher-risk piece (one carrying a diagnostics finding), recorded
# *before* review.py's UI shows what the parser actually produced. This
# is new to the project, with no earlier JSON-file version, so it goes
# straight into the database rather than following an existing shape.
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
    """One row per (act, node_index): resubmitting overwrites rather
    than building up a history, since the point is a single honest first
    read, not a record of every time someone guessed again."""
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
    """Overall agreement rate across every blind review recorded so far
    (one Act, or every Act when act is None) -- a genuine measure of the
    parser's own accuracy, not just a nudge to review more carefully:
    how often a reviewer's own first, independent read actually matched
    what the parser produced, before they'd seen it."""
    conn = _connect()
    query = "SELECT COUNT(*) AS total, COALESCE(SUM(matched_type), 0) AS type_matched, COALESCE(SUM(matched_number), 0) AS number_matched FROM blind_reviews"
    row = conn.execute(f"{query} WHERE act = ?", (act,)).fetchone() if act is not None else conn.execute(query).fetchone()
    return {"total": row["total"], "type_matched": row["type_matched"], "number_matched": row["number_matched"]}


# ---------------------------------------------------------------------
# AI suggestions -- a local model's cached answer to one bounded question
# about one elevated-risk node (see ai_pipeline/ai_assist.py)
# ---------------------------------------------------------------------

def _ai_suggestion_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "answer": row["answer"], "reasoning": row["reasoning"], "confidence": row["confidence"],
        "model": row["model"], "requested_at": row["requested_at"],
    }


def get_ai_suggestion(act: str, node_index: int) -> "dict | None":
    row = _connect().execute(
        "SELECT * FROM ai_suggestions WHERE act = ? AND node_index = ?", (act, node_index)
    ).fetchone()
    return _ai_suggestion_row_to_dict(row) if row is not None else None


def save_ai_suggestion(act: str, node_index: int, *, answer: str, reasoning: str, confidence: str, model: str) -> dict:
    """One row per (act, node_index): re-asking overwrites rather than
    accumulating a history, the same as save_blind_review -- a reviewer
    wants this node's current answer, not a log of every time they
    clicked the button."""
    requested_at = _now_iso()
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT INTO ai_suggestions (act, node_index, answer, reasoning, confidence, model, requested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(act, node_index) DO UPDATE SET answer=excluded.answer, reasoning=excluded.reasoning, "
            "confidence=excluded.confidence, model=excluded.model, requested_at=excluded.requested_at",
            (act, node_index, answer, reasoning, confidence, model, requested_at),
        )
    return {"answer": answer, "reasoning": reasoning, "confidence": confidence, "model": model, "requested_at": requested_at}


# ---------------------------------------------------------------------
# Per-Act custom node types
# ---------------------------------------------------------------------
def load_custom_types(act: str) -> list[str]:
    """This Act's own extra node types, oldest first -- the order they
    were added is the order they appear in the relabel dropdown, below
    the built-in ones, so adding a new type never reshuffles the list a
    reviewer has already gotten used to."""
    rows = _connect().execute(
        "SELECT name FROM custom_types WHERE act = ? ORDER BY created_at, name", (act,)
    ).fetchall()
    return [row["name"] for row in rows]


def add_custom_type(act: str, name: str) -> None:
    conn = _connect()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO custom_types (act, name, created_at) VALUES (?, ?, ?)",
            (act, name, _now_iso()),
        )


def rename_custom_type(act: str, old_name: str, new_name: str) -> None:
    """Implemented as an insert plus a delete, rather than updating the
    primary key directly, so the renamed type keeps its original
    created_at -- and therefore its place in load_custom_types' order."""
    conn = _connect()
    with conn:
        row = conn.execute(
            "SELECT created_at FROM custom_types WHERE act = ? AND name = ?", (act, old_name)
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "INSERT OR IGNORE INTO custom_types (act, name, created_at) VALUES (?, ?, ?)",
            (act, new_name, row["created_at"]),
        )
        conn.execute("DELETE FROM custom_types WHERE act = ? AND name = ?", (act, old_name))


def delete_custom_type(act: str, name: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM custom_types WHERE act = ? AND name = ?", (act, name))
