"""Whether the change between two reprints of a provision is the one an
amending Act instructed, and where in the provision it landed.

The instruction names the piece -- "section 4(1)(f)" -- and gives the
words, so each is checked at that piece first. Found at another piece
instead, it is "elsewhere": the words changed as Parliament said, but
the parser has them under a different paragraph, which is the fault
margin notes cannot reveal because the parser placed them too. Found
nowhere, it is "not found" -- usually not yet commenced in the newer
reprint. What changed that no instruction accounts for is returned as
unexplained.

`older` and `newer` are html_view._wording_units of the provision in
each reprint.
"""
from corpus.amending.instructions import clean
from corpus.domain.diffing import node_diff, normalise


def _text(unit: "dict | None") -> str:
    return clean(normalise(unit["text"] or "")) if unit else ""


def _key(path) -> str:
    return "/".join(path).lower() if isinstance(path, list) else (path or "").lower()


def _made(ins: dict, a: str, b: str) -> bool:
    """Whether the words of one piece went from `a` to `b` as instructed."""
    old, new = clean(ins.get("old") or ""), clean(ins.get("new") or "")
    action = ins["action"]
    if a == b:
        return False
    if action == "substitute":
        return old in a and new in b and (b.count(old) < a.count(old) or old in new)
    if action == "insert_after":
        return any(f"{old}{gap}{new}" in b for gap in (" ", "")) and new not in a
    if action == "insert_before":
        return any(f"{new}{gap}{old}" in b for gap in (" ", "")) and new not in a
    if action == "omit":
        return b.count(old) < a.count(old)
    if action == "insert_at_end":
        return b.endswith(new) and not a.endswith(new)
    if action == "replace_provision":
        return new.rstrip(";.") in b and new.rstrip(";.") not in a
    return False


def match(instructions: list[dict], older: list[dict], newer: list[dict]) -> dict:
    """{"instructions": each with "status" and "at" (the piece's path, or
    None), "unexplained": paths of changed pieces no instruction matched}."""
    a_pieces = {u.get("path") or "": u for u in older}
    b_pieces = {u.get("path") or "": u for u in newer}
    a_heading = clean(older[0]["root_heading"]) if older else ""
    b_heading = clean(newer[0]["root_heading"]) if newer else ""
    explained: set = set()
    out = []
    for ins in instructions:
        status, at = "not found", None
        target = _key(ins.get("path") or [])
        action = ins["action"]
        if ins.get("heading"):
            if _made(ins, a_heading, b_heading):
                status, at = "matched", "heading"
        elif action == "insert_provision":
            parent = target.rsplit("/", 1)[0] if "/" in target else ""
            made = f"{parent}/{ins['number']}".strip("/").lower() if ins.get("number") else None
            if made and made in b_pieces and made not in a_pieces:
                status, at = "matched", made
        elif action == "repeal":
            if target in a_pieces and (target not in b_pieces or not _text(b_pieces[target]).strip("* ")):
                status, at = "matched", target
        elif action in ("insert_definition", "unparsed"):
            status = "unchecked"
        else:
            if target in a_pieces and target in b_pieces and _made(ins, _text(a_pieces[target]), _text(b_pieces[target])):
                status, at = "matched", target
            else:
                for path in a_pieces.keys() & b_pieces.keys():
                    if _made(ins, _text(a_pieces[path]), _text(b_pieces[path])):
                        status, at = "elsewhere", path
                        break
        if at:
            explained.add(at)
        out.append({**ins, "status": status, "at": at})

    ops = node_diff([(u["align"], u["text"] or "") for u in older], [(u["align"], u["text"] or "") for u in newer])
    changed = []
    for op in ops:
        if op["op"] == "equal":
            continue
        unit = newer[op["new"]] if op["new"] is not None else older[op["old"]]
        changed.append(unit.get("path") or "")
    if a_heading != b_heading:
        changed.insert(0, "heading")
    return {"instructions": out, "unexplained": [p for p in changed if p not in explained]}
