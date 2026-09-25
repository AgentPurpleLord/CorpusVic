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


# {path: ((mtime, size), the parse it is built from or None)}. Every cache
# of a version is keyed on its chain, and a work's versions are each
# stamped for every one of them: reading a whole parse file each time
# just for this one field made a work of twenty versions read thousands.
_toward: dict = {}


def _toward_of(path: Path) -> "str | None":
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _toward.get(str(path))
    if hit is None or hit[0] != key:
        slim = _read(path).get("slim")
        hit = _toward[str(path)] = (key, slim["toward"] if slim else None)
    return hit[1]


def chain(path: Path) -> list[Path]:
    """This parse file and every one it is built from, nearest first."""
    out, seen = [Path(path)], {str(Path(path))}
    while True:
        toward = _toward_of(out[-1])
        if not toward:
            return out
        nxt = out[-1].parent / f"{toward}.json"
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


# The last few parses put back together, by file and stamp. A slim
# version is built from its neighbour, that from the next, up to the base:
# with nothing kept, loading every version of a work of a hundred rebuilt
# each whole chain again, and History review took minutes. Loaded in
# build_order, each one's neighbour is always here. Few, since a whole
# Act read into memory is tens of MB.
_loaded: dict = {}
_KEEP = 4


def build_order(paths) -> list[Path]:
    """The parse files each after the one it is built from."""
    return sorted((Path(p) for p in paths), key=lambda p: len(chain(p)))


def load(path: Path) -> dict:
    """The parse, with a slim one's nodes put back together from its
    neighbours, and its nodes named. Its fingerprint is its own and its
    neighbour's together, so positions recorded against it go stale when
    either changes."""
    key = (str(path), stamp(path))
    data = _loaded.pop(str(path), None)
    if data is None or data[0] != key:
        data = (key, _build(Path(path)))
    _loaded[str(path)] = data            # most recent last
    while len(_loaded) > _KEEP:
        del _loaded[next(iter(_loaded))]
    # Copies: callers edit the nodes they are given.
    if "nodes" not in data[1]:
        return dict(data[1])
    return {**data[1], "nodes": [dict(n) for n in data[1]["nodes"]]}


def _build(path: Path) -> dict:
    from corpus.history import delta
    from corpus.parsing.identity import annotate_ids

    data = _read(path)
    slim = data.get("slim")
    if not slim:
        if "nodes" in data:
            annotate_ids(data["nodes"], data.get("hierarchy") or None)
        return data
    neighbour = load(path.parent / f"{slim['toward']}.json")   # named already
    fingerprint = hashlib.sha1(f"{data.get('fingerprint')}|{neighbour.get('fingerprint')}".encode()).hexdigest()[:16]
    return {**data, "nodes": delta.assemble(neighbour["nodes"], slim), "fingerprint": fingerprint,
            # The neighbour's: its words are, and a slim version's own
            # hierarchy may be an older parse's.
            "hierarchy": neighbour.get("hierarchy") or data.get("hierarchy")}
