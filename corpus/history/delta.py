"""A version of an Act kept as only what its margin notes say changed.

Every official change of wording is printed as a margin note beside the
piece it changed, citing the amending Act. So between two reprints, the
pieces that changed are exactly those whose notes cite an Act they did
not cite before -- the OCPC is trusted to have labelled every one, and a
difference the parser reads anywhere else is the parser's, not
Parliament's. On the CPA, 394 of 430 differences between reprints had no
note behind them.

So one version of a work -- the one reviewed in full -- is held whole,
and every other keeps only its changed pieces, its notes and the pages
they print on: its text elsewhere is its neighbour's toward that base
(see assemble).
"""
import re
from pathlib import Path

from corpus.domain import amendments

# The verb a citation in a note is the object of: "amended by Nos 5/2018,
# 7/2026, repealed by No. 9/2027" -- the nearest before it.
_VERB_RE = re.compile(r"\b(inserted|substituted|amended|repealed|renumbered|re-numbered|expired)\b", re.I)
_KIND = {"inserted": "inserted", "repealed": "repealed", "expired": "repealed"}


def _name(node: dict) -> str:
    return node.get("_node_id") or node.get("id") or ""


def _section_label(schedule, number) -> str:
    return f"{schedule or ''}:{number}"


def notes_index(nodes: list[dict], unattached: "list[dict] | None" = None) -> dict:
    """Every margin note in one version, by the name of the piece it is
    printed against, plus the notes that attach to nothing (a repealed
    section's, whose node is gone) by the section they name. Small enough
    to keep for every version, which is what lets a step between two
    versions be worked out without either one's full parse."""
    by_name: dict = {}
    sections: dict = {}
    for node in nodes:
        if node.get("type") in ("section", "clause", "item") and node.get("number"):
            sections[_section_label((node.get("path") or {}).get("schedule"), node["number"])] = _name(node)
        raws = [h.get("raw") if isinstance(h, dict) else h for h in node.get("history") or []]
        if raws and _name(node):
            by_name.setdefault(_name(node), []).extend(r for r in raws if r)
    loose: dict = {}
    for note in unattached or []:
        if note.get("raw") and note.get("section"):
            loose.setdefault(_section_label(note.get("schedule"), note["section"]), []).append(note["raw"])
    return {"by_name": by_name, "loose": loose, "sections": sections}


def _cites(notes) -> dict:
    """{Act cited: the kind of change the note says it made}."""
    out = {}
    for note in notes or []:
        for c in amendments.citations_in(note):
            act = f"{c['act_no']}/{c['year']}" if c.get("year") else str(c["act_no"])
            verbs = _VERB_RE.findall(note[:c["start"]])
            out[act] = _KIND.get(verbs[-1].lower(), "changed") if verbs else "changed"
    return out


def _target(note: str) -> str:
    """What a note says it is about, as printed: "s. 4(8c)(f)" out of
    "S. 4(8C)(f) amended by No. 7/2026 s. 3." -- the words before its
    first verb."""
    m = _VERB_RE.search(note)
    return re.sub(r"\s+", " ", note[:m.start() if m else len(note)]).strip(" .,;").lower()


def _cited_by_target(index: dict) -> dict:
    """{printed target: every Act the notes about it cite}."""
    out: dict = {}
    for notes in (*index["by_name"].values(), *index["loose"].values()):
        for note in notes:
            out.setdefault(_target(note), set()).update(_cites([note]))
    return out


def changed_pieces(older: dict, newer: dict) -> dict:
    """{piece name: "changed" | "inserted" | "repealed"} for one step
    between two versions' notes indexes: each piece carrying a note that
    cites an Act the earlier version's notes about the same printed target
    did not.

    By the note's own target ("S. 4(8C)(f)"), not by where the parser
    attached it: attachment varies from one parse to the next, and a note
    that only moved is not an amendment. And by target rather than by
    provision: one Act commencing in stages amends another piece of a
    section it amended before. The piece named is the one the note is on
    in the later version; a loose note (its provision gone) names the
    section in whichever version still has it."""
    before = _cited_by_target(older)
    out = {}

    def note_changes(notes):
        new = {}
        for note in notes:
            had = before.get(_target(note), set())
            new.update({act: kind for act, kind in _cites([note]).items() if act not in had})
        return new

    for name, notes in newer["by_name"].items():
        new = note_changes(notes)
        if new:
            out[name] = _strongest(new.values())
    for label, notes in newer["loose"].items():
        name = older["sections"].get(label) or newer["sections"].get(label)
        new = note_changes(notes)
        if name and new:
            out[name] = _strongest([*new.values(), out.get(name, "changed")])
    return out


def _strongest(kinds) -> str:
    kinds = set(kinds)
    return "repealed" if "repealed" in kinds else "inserted" if "inserted" in kinds else "changed"


# -- A slim version, and a whole one made from it ----------------------------

def _subtree(nodes: list[dict], name: str) -> list[int]:
    """The indices of the piece `name` and everything under it."""
    return [i for i, n in enumerate(nodes) if _name(n) == name or _name(n).startswith(name + "/")]


def _outermost(names) -> list[str]:
    """The names not inside another of them: a changed section takes its
    changed paragraphs with it."""
    names = sorted(set(names))
    return [n for n in names if not any(n != m and n.startswith(m + "/") for m in names)]


_KEEP_FIELDS = ("act", "source", "engine", "profile", "document_type", "version", "hierarchy", "endnotes",
                "unattached_notes", "parser_version")


def slim(parse: dict, toward: str, text: "list[str]", pages_only: "list[str]" = ()) -> dict:
    """A version's parse cut down to the pieces named: `text` are the ones
    whose words this version supplies over its neighbour `toward` (the
    changes in the step toward the base), `pages_only` the ones kept only
    for where they print (the changes in the step away from it, so both
    printed sides of every change stay on hand). `parse` is the full
    parse, its nodes named."""
    nodes = parse["nodes"]
    pieces = []
    for name in _outermost([*text, *pages_only]):
        span = _subtree(nodes, name)
        if not span:
            continue   # not in this version: see "removed"
        before = [_name(nodes[i]) for i in range(span[0] - 1, -1, -1)]
        # Where it goes if the neighbour hasn't got it: after whatever
        # precedes it here that isn't under it.
        after = next((b for b in before if not b.startswith(name + "/")), None)
        pieces.append({"name": name, "words": name in text or any(name.startswith(t + "/") for t in text),
                       "after": after, "nodes": [nodes[i] for i in span]})
    kept_pages = sorted({r["page"] for p in pieces for n in p["nodes"] for r in n.get("rects") or []}
                        | {n.get("page_start") for p in pieces for n in p["nodes"] if n.get("page_start")}
                        | {note.get("page") for note in parse.get("unattached_notes") or [] if note.get("page")})
    return {**{k: parse[k] for k in _KEEP_FIELDS if k in parse},
            "slim": {"toward": toward, "pieces": pieces,
                     # Named in the text list but absent here: taken away.
                     "removed": [t for t in text if not _subtree(nodes, t)],
                     "notes": notes_index(nodes, parse.get("unattached_notes")),
                     "kept_pages": kept_pages},
            "fingerprint": parse.get("fingerprint")}


def assemble(neighbour: list[dict], slim_part: dict) -> list[dict]:
    """A slim version's whole text: its neighbour's, with each of its own
    pieces put in by name. What comes from the neighbour loses its boxes
    and pages -- they are where another reprint printed it."""
    nodes = [{**n, "rects": [], "page_start": None, "page_end": None, "_borrowed": True} for n in neighbour]
    for name in slim_part.get("removed") or []:
        span = set(_subtree(nodes, name))
        nodes = [n for i, n in enumerate(nodes) if i not in span]
    for piece in slim_part["pieces"]:
        span = _subtree(nodes, piece["name"])
        if not piece["words"]:
            # Kept for where it prints: the neighbour's words, this
            # version's boxes.
            own = {_name(n): n for n in piece["nodes"]}
            for i in span:
                mine = own.get(_name(nodes[i]))
                if mine is not None:
                    nodes[i] = {**nodes[i], "rects": mine.get("rects") or [], "page_start": mine.get("page_start"),
                                "page_end": mine.get("page_end"), "history": mine.get("history") or []}
                    nodes[i].pop("_borrowed", None)
            continue
        if span:
            nodes[span[0]:span[-1] + 1] = [dict(n) for n in piece["nodes"]]
            continue
        after = _subtree(nodes, piece["after"]) if piece.get("after") else []
        at = (after[-1] + 1) if after else len(nodes)
        nodes[at:at] = [dict(n) for n in piece["nodes"]]
    return nodes


def blank_pages(pdf_path: Path, keep_pages: "list[int]", out_path: Path) -> None:
    """The PDF with every page but `keep_pages` (1-based) left blank at its
    own size: the page numbers everything was recorded against stay true,
    and a discarded page costs a few bytes."""
    import pymupdf

    src = pymupdf.open(str(pdf_path))
    out = pymupdf.open()
    keep = set(keep_pages)
    for i, page in enumerate(src):
        if i + 1 in keep:
            out.insert_pdf(src, from_page=i, to_page=i)
        else:
            out.new_page(width=page.rect.width, height=page.rect.height)
    out.save(str(out_path), garbage=4, deflate=True)
    out.close()
    src.close()
