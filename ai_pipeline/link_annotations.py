"""
Span-level "this should become a link" annotations -- references to other
Acts, defined terms, Bills, Explanatory Memoranda -- layered on top of an
Act's already-parsed nodes (data/ai_parsed/<act>.json) without touching
them. link_review.py is the interactive tool that creates these; this
module is deliberately the only part of that tool that isn't tied to
FastAPI, so the data model and validation stay unit-testable on their own.

Each annotation is a span within one node's stored `text` -- character
offsets, not the reflowed/display text review.py shows for reading -- and
records a *label* (which kind of link it'll become) but not yet a
*target* (which specific Act/definition/Bill it resolves to). Resolving
targets and actually generating hyperlinks is a separate, later pass;
this only captures where a human said "this text should eventually link
somewhere."

Stored at data/links/<act>.json as a flat list, independent of
data/verified/<act>.json -- annotating spans doesn't require (or imply)
that a node has passed structural review, and structural review doesn't
need to know these exist.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

LABELS = ["act_citation", "defined_term", "bill_reference", "em_reference", "other"]


class LinkError(ValueError):
    """A link annotation request was invalid -- an out-of-range or empty
    span, or a label outside LABELS. Raised before anything is written to
    disk, so a bad request from the frontend can't corrupt
    data/links/<act>.json."""


def links_path(act: str) -> Path:
    return Path("data/links") / f"{act}.json"


def load_links(act: str) -> list[dict]:
    path = links_path(act)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_links(act: str, links: list[dict]) -> None:
    path = links_path(act)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(links, indent=2), encoding="utf-8")


def add_link(act: str, node_index: int, start: int, end: int, label: str, node_text: str) -> dict:
    """Validates and appends one span annotation, returning the saved
    record (with a fresh id and timestamp). `node_text` is the exact
    stored text of the node being annotated, passed in by the caller
    (which already has the parsed nodes loaded) rather than reloaded here
    -- keeps this a pure function callers can unit-test without touching
    data/ai_parsed/*.json at all."""
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
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    links = load_links(act)
    links.append(record)
    save_links(act, links)
    return record


def delete_link(act: str, link_id: str) -> bool:
    """Removes one annotation by id. Returns False (no-op, nothing
    written) if the id isn't found -- callers surface that as a 404
    rather than silently succeeding on a stale or mistyped id."""
    links = load_links(act)
    remaining = [link for link in links if link["id"] != link_id]
    if len(remaining) == len(links):
        return False
    save_links(act, remaining)
    return True
