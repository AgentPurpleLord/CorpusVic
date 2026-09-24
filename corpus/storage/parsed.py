"""A parse file, read whole.

A version of an Act can be kept slim (corpus/history/delta.py): only the
pieces its margin notes say changed, the rest being its neighbour's
toward the version reviewed in full. Everything that wants a version's
nodes reads it through here, so none of it has to know.
"""
import hashlib
import json
from pathlib import Path


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def chain(path: Path) -> list[Path]:
    """This parse file and every one it is built from, nearest first."""
    out, seen = [Path(path)], {str(Path(path))}
    while True:
        slim = _read(out[-1]).get("slim")
        if not slim:
            return out
        nxt = out[-1].parent / f"{slim['toward']}.json"
        if str(nxt) in seen or not nxt.exists():
            return out
        seen.add(str(nxt))
        out.append(nxt)


def stamp(path: Path) -> tuple:
    """What a cache of this parse must be keyed on: its own file and the
    files it is built from -- a re-parse of the base reaches every version
    made from it."""
    out = []
    for p in chain(path):
        st = p.stat()
        out.append((st.st_mtime_ns, st.st_size))
    return tuple(out)


def load(path: Path) -> dict:
    """The parse, with a slim one's nodes put back together from its
    neighbours. Its fingerprint is its own and its neighbour's together,
    so positions recorded against it go stale when either changes."""
    from corpus.history import delta
    from corpus.parsing.identity import annotate_ids

    data = _read(path)
    slim = data.get("slim")
    if not slim:
        return data
    neighbour_path = Path(path).parent / f"{slim['toward']}.json"
    neighbour = load(neighbour_path)
    nodes = [dict(n) for n in neighbour["nodes"]]
    annotate_ids(nodes, neighbour.get("hierarchy") or None)
    fingerprint = hashlib.sha1(f"{data.get('fingerprint')}|{neighbour.get('fingerprint')}".encode()).hexdigest()[:16]
    return {**data, "nodes": delta.assemble(nodes, slim), "fingerprint": fingerprint,
            "hierarchy": data.get("hierarchy") or neighbour.get("hierarchy")}
