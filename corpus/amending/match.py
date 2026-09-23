"""Whether the change between two reprints of a provision is the one an
amending Act instructed, and where in the provision it landed.

The instruction names the piece -- "section 4(1)(f)" -- and gives the
words, so each is checked at that piece first. Found at another piece
instead, it is "elsewhere": the words changed as Parliament said, but
the parser has them under a different paragraph, which is the fault
margin notes cannot reveal because the parser placed them too. Already
true of the older reprint, it is "earlier": in force before the two
being compared. Found nowhere, it is "not found". What changed that no
instruction accounts for is returned as unexplained.

Pieces are paired across the reprints by node_diff, on their own labels
rather than their full paths, because the parser can nest a piece
differently in each: s 4(1)(f) read as (1)(f) in one reprint and (8C)(f)
in the next shares no path, but it is one paragraph.

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


def _already(ins: dict, a: str) -> bool:
    """Whether the older reprint already reads as instructed."""
    old, new = clean(ins.get("old") or ""), clean(ins.get("new") or "")
    if ins["action"] in ("substitute", "insert_after", "insert_before", "replace_provision", "insert_at_end"):
        return bool(new) and new.rstrip(";.") in a and not (old and old in a and old not in new)
    if ins["action"] == "omit":
        return bool(old) and old not in a
    return False


def match(instructions: list[dict], older: list[dict], newer: list[dict]) -> dict:
    """{"instructions": each with "status", "at" (the piece's path, or
    None) and "pair" (that piece in (older, newer), either None where one
    reprint lacks it), "unexplained": [{"path", "pair"}] for each changed
    piece no instruction accounts for}. The pair is what lets a caller
    reviewing either reprint find its own side of the piece."""
    ops = node_diff([(u["align"], u["text"] or "") for u in older], [(u["align"], u["text"] or "") for u in newer])
    pairs = [(older[op["old"]] if op["old"] is not None else None,
              newer[op["new"]] if op["new"] is not None else None) for op in ops]
    a_heading = clean(older[0]["root_heading"]) if older else ""
    b_heading = clean(newer[0]["root_heading"]) if newer else ""
    a_paths = {u.get("path") or "" for u in older}
    pair_of = {}
    for a, b in pairs:
        for unit in (a, b):
            if unit is not None:
                pair_of[id(unit)] = (a, b)
    explained: set = set()
    out = []
    for ins in instructions:
        status, at, landed = "not found", None, None
        target = _key(ins.get("path") or [])
        action = ins["action"]
        if ins.get("heading"):
            if _made(ins, a_heading, b_heading):
                status, at = "matched", "heading"
            elif _already(ins, a_heading):
                status = "earlier"
        elif action == "insert_section":
            if newer and not older:
                status, at = "matched", "whole"
            elif older:
                status = "earlier"
        elif action == "insert_provision":
            parent = target.rsplit("/", 1)[0] if "/" in target else ""
            made = f"{parent}/{ins['number']}".strip("/").lower() if ins.get("number") else None
            added = [b for a, b in pairs if a is None and b is not None]
            if made and made in a_paths:
                status = "earlier"
            elif made and any((b.get("path") or "") == made for b in added):
                landed = next(b for b in added if (b.get("path") or "") == made)
                status, at = "matched", made
            else:
                number = (ins.get("number") or "").lower()
                hit = next((b for b in added if (b["tree_node"]["node"].get("number") or "").lower() == number), None)
                # A Schedule the parser didn't split into items has nowhere
                # for an item to be added but the Schedule's own text.
                words = clean(ins.get("new") or "")[:80].rstrip(";.")
                grew = next((b for a, b in pairs if b is not None and words and words in _text(b)
                             and words not in _text(a)), None)
                if hit is not None or grew is not None:
                    landed = hit if hit is not None else grew
                    status, at = "elsewhere", landed.get("path") or ""
        elif action == "repeal":
            gone = [a for a, b in pairs if a is not None and (b is None or not _text(b).strip("* "))]
            landed = next((a for a in gone if (a.get("path") or "") == target), None)
            if landed is not None:
                status, at = "matched", target
        elif action in ("insert_definition", "replace_definition", "unparsed"):
            status = "unchecked"
        else:
            found = [(a, b) for a, b in pairs if a is not None and b is not None and _made(ins, _text(a), _text(b))]
            here = [b for a, b in found if target in ((a.get("path") or "").lower(), (b.get("path") or "").lower())]
            # Two pieces can take the same words -- s 374(2)(b)(ii)(A) and
            # (iii) both do -- so the one numbered as the target is the one.
            own = target.rsplit("/", 1)[-1]
            numbered = [b for a, b in found if (b["tree_node"]["node"].get("number") or "").lower() == own]
            if here:
                landed = here[0]
                status, at = "matched", landed.get("path") or ""
            elif found:
                landed = (numbered or [found[0][1]])[0]
                status, at = "elsewhere", landed.get("path") or ""
            elif any(_already(ins, _text(a)) for a in older if (a.get("path") or "").lower() == target):
                status = "earlier"
        if at:
            explained.add(at)
        out.append({**ins, "status": status, "at": at,
                    "pair": pair_of.get(id(landed), (None, None)) if landed is not None else (None, None)})

    changed = [{"path": (b or a).get("path") or "", "pair": (a, b)}
               for (a, b), op in zip(pairs, ops) if op["op"] != "equal"]
    if a_heading != b_heading:
        changed.insert(0, {"path": "heading", "pair": (older[0] if older else None, newer[0] if newer else None)})
    return {"instructions": out, "unexplained": [c for c in changed if c["path"] not in explained]}
