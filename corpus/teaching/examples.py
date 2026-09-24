"""A reviewer's decisions, harvested as examples the parser is held to.

Examples are keyed by the printed line (page, height, opening words), not
by a node's name or position, because both of those are the parser's own
and change when it does. An example lives in data/teaching/<act>.jsonl,
committed with the review data it came from.

    python -m corpus.teaching.examples criminal-procedure-act-v114
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from corpus import PROJECT_ROOT


def examples_path(act: str, base_dir=None) -> Path:
    return Path(base_dir or PROJECT_ROOT) / "data" / "teaching" / f"{act}.jsonl"


def example_id(act: str, seen: dict) -> str:
    key = f"{act}|{seen['page']}|{round(seen['y0'])}|{seen['text'][:60]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def said(node: "dict | None") -> "dict | None":
    """What a node says a line is: its type and number, and for a defined
    term the term, since a definition carries no number."""
    if node is None:
        return None
    out = {"type": node.get("type"), "number": (node.get("number") or None)}
    if node.get("type") == "definition":
        out["heading"] = " ".join((node.get("heading") or "").split()) or None
    return out


def agree(a: "dict | None", b: "dict | None") -> bool:
    if a is None or b is None:
        return a is b
    return (a["type"], (a.get("number") or "").lower(), (a.get("heading") or "").lower()) == \
           (b["type"], (b.get("number") or "").lower(), (b.get("heading") or "").lower())


def harvest(act: str) -> list[dict]:
    """The examples this Act's review data gives, against its current
    parse. Only decided pieces count: accepted (as they were, or
    corrected), merged away in a finished unit, or deleted. A parse made
    before nodes recorded what the parser saw gives none."""
    from corpus.review.review import (_node_at, _was_inserted, finished_units, load_parsed, load_structure_edits,
                                      load_verified, names_by_index, order_and_units, positions_are_trustworthy,
                                      verified_by_index)

    nodes, _unattached, _hierarchy, fingerprint = load_parsed(act)
    if not any("seen" in n for n in nodes):
        return []
    edits = load_structure_edits(act, fingerprint, nodes=nodes)
    order, units = order_and_units(len(nodes), edits, _node_at(nodes, edits))
    verified = load_verified(act)
    by_index, _unplaced = verified_by_index(verified, names_by_index(nodes, edits))
    removed = set(range(len(nodes))) - set(order)
    if positions_are_trustworthy(act, fingerprint):
        for u in finished_units(units, list(verified), markers_are_complete=True):
            removed.update(i for i in units[u] if i not in by_index and not _was_inserted(edits, i))

    from corpus.storage import db

    drawn = db.load_node_rects(act)
    out = []
    for i, node in enumerate(nodes):
        seen = node.get("seen")
        if not seen or (i not in by_index and i not in removed):
            continue
        expected = None if i in removed else said(by_index[i])
        parser = said(node)
        kind = "removed" if expected is None else ("confirmed" if agree(parser, expected) else "corrected")
        # Where the piece prints -- your box if you drew one -- so a later
        # parse opening something in the middle of it is caught: a false
        # split opens a line no example is about.
        rects = drawn.get(node.get("id")) or node.get("rects") or []
        out.append({"id": example_id(act, seen), "act": act, "kind": kind, "node_id": node.get("id"),
                    "parser": parser, "expected": expected, "seen": seen,
                    "rects": [{k: r[k] for k in ("page", "x0", "y0", "x1", "y1")} for r in rects]})
    return out


def load(act: str, base_dir=None) -> list[dict]:
    path = examples_path(act, base_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def update(act: str, base_dir=None) -> dict:
    """Harvest and merge into the stored examples. A stored example whose
    line the current parse opens no node at is kept -- the parse can't
    speak for it, and it is often exactly a false split a reviewer
    removed. One whose line the parse does open is replaced by what the
    review now says, or dropped if the piece is no longer decided."""
    from corpus.review.review import load_parsed

    fresh = harvest(act)
    nodes = load_parsed(act)[0]
    covered = {example_id(act, n["seen"]) for n in nodes if n.get("seen")}
    kept = [e for e in load(act, base_dir) if e["id"] not in covered]
    merged = sorted(kept + fresh, key=lambda e: (e["seen"]["page"], e["seen"]["y0"], e["seen"]["x0"]))
    path = examples_path(act, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One example a line, so a changed decision shows as one line in git.
    path.write_text("".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in merged),
                    encoding="utf-8")
    counts = {}
    for e in merged:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {"act": act, "examples": len(merged), "harvested": len(fresh), "kept": len(kept), "kinds": counts}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    print(json.dumps(update(ap.parse_args().act), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
