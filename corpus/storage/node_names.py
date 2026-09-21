"""Filling in each review row's node_id, from the parse it was keyed by.

Every table holding a person's review work has been keyed by a position
in data/parsed/<act>.json. A position means nothing once the parse
changes, so each row also carries the provision's own name now -- see
corpus/parsing/identity.py. The rows written before that column existed
get their name here, by reading the parse their position still points
into.

Nothing is guessed. A row whose position has no node in the current
parse -- an inserted node sitting above every parse position, or a row
left over from a parse that has since been re-run -- is reported and
left alone.

    python -m corpus.storage.node_names            what it would name
    python -m corpus.storage.node_names --write    name them
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from corpus.parsing.identity import disambiguate, inserted_id, node_ids
from corpus.storage import db

# Which column in each table holds the position.
POSITION_COLUMN = {
    "verified": "source_node_index",
    "links": "node_index",
    "blind_reviews": "node_index",
    "ai_suggestions": "node_index",
    "ai_scan_findings": "node_index",
    "structure_edits": "node_index",
    "node_rects": "node_index",
}


def _names_for(act: str, base_dir: "str | Path | None" = None) -> "list[str] | None":
    path = Path(base_dir or ".") / "data" / "parsed" / f"{act}.json"
    if not path.exists():
        return None
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return node_ids(parsed["nodes"], parsed.get("hierarchy") or None)


def _inserted_names(conn, act: str, names: list) -> dict:
    """Names for the provisions a reviewer added to this Act.

    An insert sits above every parse position and has no path of its own,
    so it is named against the provision it was put after -- which may
    itself be an insert, so the earlier ones are resolved first (they are
    given lower indices as they are made, so index order is the order
    they can be resolved in).
    """
    inserts = list(conn.execute(
        "SELECT node_index, after_index, node_json FROM structure_edits "
        "WHERE act = ? AND node_json IS NOT NULL AND node_index >= ? "
        "ORDER BY node_index", (act, len(names))))

    by_index, taken = {}, set(names)
    for row in inserts:
        node = json.loads(row["node_json"])
        anchor_index = row["after_index"]
        if anchor_index is not None and 0 <= anchor_index < len(names):
            anchor = names[anchor_index]
        elif anchor_index in by_index:
            anchor = by_index[anchor_index]
        else:
            # The front of the document, or an anchor that no longer
            # exists. Either way there is nothing to hang the name off.
            anchor = "inserted"
        name = disambiguate(inserted_id(anchor, node), taken, node)
        taken.add(name)
        by_index[row["node_index"]] = name
    return by_index


def name_rows(base_dir: "str | Path | None" = None, write: bool = False) -> dict:
    """Give every unnamed row the name of the provision it points at.

    Re-runnable: a row that already has a name is left as it is, so this
    can be run again after a parse without disturbing what it named
    before.
    """
    conn = db._connect(base_dir)
    named, inserted, unnamed = Counter(), Counter(), Counter()
    missing_parse = set()
    cache: dict = {}

    for table, position in POSITION_COLUMN.items():
        rows = list(conn.execute(
            f"SELECT rowid, act, {position} AS position FROM {table} WHERE node_id IS NULL"))
        for row in rows:
            act = row["act"]
            if act not in cache:
                names = _names_for(act, base_dir)
                if names is None:
                    missing_parse.add(act)
                cache[act] = (names, _inserted_names(conn, act, names) if names else {})
            names, inserts = cache[act]
            position_value = row["position"]

            if names is not None and position_value is not None and 0 <= position_value < len(names):
                name = names[position_value]
                named[table] += 1
            elif position_value in inserts:
                name = inserts[position_value]
                inserted[table] += 1
            else:
                unnamed[table] += 1
                continue
            if write:
                conn.execute(f"UPDATE {table} SET node_id = ? WHERE rowid = ?",
                             (name, row["rowid"]))
    if write:
        conn.commit()
    return {"named": dict(named), "inserted": dict(inserted), "unnamed": dict(unnamed),
            "missing_parse": sorted(missing_parse)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="name the rows (without this, only reports)")
    args = ap.parse_args()

    report = name_rows(write=args.write)
    verb = "named" if args.write else "would name"
    total = sum(report["named"].values()) + sum(report["inserted"].values())
    print(f"{verb} {total} row(s)")
    for table, count in sorted(report["named"].items()):
        print(f"  {count:6d}  {table}")
    if report["inserted"]:
        print(f"\nof those, added by a reviewer and named against the provision they follow:")
        for table, count in sorted(report["inserted"].items()):
            print(f"  {count:6d}  {table}")
    if report["unnamed"]:
        print("\nleft alone -- their position points at no node in the current parse:")
        for table, count in sorted(report["unnamed"].items()):
            print(f"  {count:6d}  {table}")
    if report["missing_parse"]:
        print(f"\nno parse on disk for: {', '.join(report['missing_parse'])}")


if __name__ == "__main__":
    main()
