"""
What changed in a provision between two Authorised Versions of an Act.

An Act is reprinted every few weeks and each reprint restates the whole
thing, so "what actually changed" is not recorded anywhere -- it has to be
worked out by comparing two 500-page documents. Between consecutive
versions of the Criminal Procedure Act that is a few dozen provisions out
of some seven hundred.

Three decisions shape the rest of this module.

**A provision is a Section and everything under it.** A Section's own
`text` is frequently a lead-in sentence or nothing at all, with the
substance sitting in its subsections and paragraphs as separate nodes, so
comparing only the Section's own text would report almost every real
amendment as no change. The unit here is the same one review.py works
through and the browse view gives a page to.

**Provisions are matched by identity, never by position.** A version that
inserts section 26A shifts every node after it, so comparing node 400 of
one version with node 400 of the next would report the entire remainder of
the Act as rewritten. They are matched on (Schedule, number) --
commentary.provision_key, the same key that keeps a Schedule's clause 11
apart from the body's section 11.

**Comparison is on the words, not on the characters.** A reprint
repaginates and re-extracts, and neither is stable at the character level:
the same sentence wraps at different points (those wrap points are in the
stored text as literal newlines -- see extract.reflow), and PyMuPDF will
put two spaces where the last reprint put one. Comparing raw text reported
23 of 48 provisions in the Criminal Procedure Act's timeline as amended
when the only difference was `"may  issue"` against `"may issue"`.

**The same words in a different order is not an amendment.** A table's
columns, a Note block, and a run of bullet markers each come out of
extraction in a different sequence between reprints -- sections 7A, 389E
and 387I of the Criminal Procedure Act each reported a delete and an
insert of identical text. An amendment changes words; it does not merely
move them. These are set aside as `reordered` rather than dropped, because
the judgement is a heuristic and hiding it entirely would make a genuine
transposition invisible.
"""
import difflib
import re
from collections import Counter

from .commentary import provision_key
from .extract import reflow
from .hierarchy import UNIT_BOUNDARY_TYPES, UNIT_ROOT_TYPES, schedule_numbers

# Words, and the punctuation attached to them. Splitting on whitespace
# alone keeps "255(5)(ab)" and "1958," whole, which is what a reader
# expects to see highlighted -- tokenising punctuation separately turns a
# single changed cross-reference into a scatter of tiny diffs.
_WORD_RE = re.compile(r"\S+")

# Any run of whitespace, however it got there.
_SPACES = re.compile(r"\s+")

# Characters the extractor left in the Private Use Area. A PDF may embed a
# font with a symbol encoding and no Unicode mapping, and PyMuPDF then
# hands back the raw code point: "Part 3.10 of the Evidence Act 2008"
# comes out of Criminal Procedure Act v110 as "Part \uf033\uf02e\uf031\uf030"
# and out of v111, from the same words on the same page, as "Part 3.10".
# The mapping is the standard symbol-font one -- the code point less
# 0xF000 is the ASCII character -- and is applied only for comparison, so
# whether a reprint happened to embed its fonts one way or the other is
# not reported as an amendment.
_PUA = re.compile(r"[\uf000-\uf0ff]")


def _unmap_pua(text: str) -> str:
    def ascii_for(match: "re.Match") -> str:
        code = ord(match.group()) - 0xF000
        return chr(code) if 0x20 <= code < 0x7F else match.group()

    return _PUA.sub(ascii_for, text)


def _tokenise(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def word_diff(old: str, new: str) -> list[dict]:
    """The two texts as a run of {"op", "text"} segments, where op is
    "equal", "insert" or "delete" -- what a reader needs to see which
    words moved.

    A replacement comes back as its delete followed by its insert rather
    than as an op of its own: legislative amendment is overwhelmingly
    "omit X, insert Y", and showing that as a struck-out phrase beside its
    replacement is how the Act's own amending words describe it.
    """
    old_words, new_words = _tokenise(old), _tokenise(new)
    segments: list[dict] = []

    def add(op: str, words: list[str]) -> None:
        if not words:
            return
        # Runs of the same op are joined so a rendered diff is a handful of
        # spans rather than one per word.
        if segments and segments[-1]["op"] == op:
            segments[-1]["text"] += " " + " ".join(words)
        else:
            segments.append({"op": op, "text": " ".join(words)})

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_words, new_words).get_opcodes():
        if tag == "equal":
            add("equal", old_words[i1:i2])
        else:
            add("delete", old_words[i1:i2])
            add("insert", new_words[j1:j2])
    return segments


def _unit_text(nodes: list[dict], root: int) -> str:
    """A provision's full text: its own lead-in plus every node nested
    under it, up to the next boundary. Reflowed, so where the printer
    happened to break a line isn't part of the comparison."""
    parts = [nodes[root].get("text") or ""]
    i = root + 1
    while i < len(nodes) and nodes[i]["type"] not in UNIT_BOUNDARY_TYPES:
        parts.append(nodes[i].get("text") or "")
        i += 1
    # reflow takes out the line wraps; _SPACES takes out the rest of the
    # printer's whitespace, which is no more part of the provision than
    # the wraps are.
    return _SPACES.sub(" ", _unmap_pua(reflow(" ".join(p for p in parts if p)))).strip()


# What a diff treats as a provision in its own right. Sections and clauses
# are the obvious ones. The containers are here because amending one is a
# real amendment the Act's own margin notes record ("Ch. 8 Pt 8.2 Div. 5
# (Heading) amended by No. 19/2017 s. 56"), and because a Schedule whose
# items are unnumbered prose holds them on its own node rather than in
# clauses below it -- Schedule 3 of the Criminal Procedure Act gained
# "and Food Innovation" at version 114, which a Section-only diff missed
# entirely.
_CONTAINER_TYPES = ("schedule", "chapter", "part", "division", "subdivision")


def _key(node_type: str, schedule: "str | None", number: "str | None") -> tuple:
    """A provision's identity within one version. commentary.provision_key
    keeps a Schedule's clause 11 apart from the body's section 11; the kind
    is carried too, so Part 8.2 and section 8.2 are not the same thing
    either. Sections and clauses share one kind: a Bill calls a provision a
    clause and the Act it becomes calls it a section."""
    kind = "provision" if node_type in UNIT_ROOT_TYPES else node_type
    return (kind, *provision_key(schedule, number))


def provisions(nodes: list[dict]) -> dict[tuple, dict]:
    """{key -> {"key", "kind", "number", "schedule", "heading", "text",
    "node_index"}} for everything in one version a reader would call a
    provision -- see _CONTAINER_TYPES.

    A provision must have a number of its own. That excludes bare topical
    headings and the synthetic node an Act's front matter lands in, which
    carry the version number and the as-at date and so differ between
    every pair of versions without anything in the Act having changed.

    Where a version repeats a number within the same Schedule -- which
    happens where the parser mis-splits a heading, not in the Act itself --
    the first wins, and the second is dropped rather than silently
    overwriting it. Reporting a provision as rewritten because a duplicate
    of it appeared later in the document would be worse than not reporting
    it at all.
    """
    schedules = schedule_numbers(nodes)
    found: dict[tuple, dict] = {}
    for index, node in enumerate(nodes):
        node_type = node["type"]
        if node_type not in UNIT_ROOT_TYPES and node_type not in _CONTAINER_TYPES:
            continue
        if not node.get("number"):
            continue
        # A Schedule is not "inside itself": keyed on the body, or on the
        # Schedule it sits in, so Schedule 3 doesn't read as clause 3 of
        # Schedule 3.
        schedule = None if node_type == "schedule" else schedules[index]
        key = _key(node_type, schedule, node["number"])
        if key in found:
            continue
        found[key] = {
            "key": key,
            "kind": key[0],
            "type": node_type,
            "number": node["number"],
            "schedule": schedule,
            "heading": node.get("heading"),
            "text": _unit_text(nodes, index),
            "node_index": index,
        }
    return found


def label(provision: dict) -> str:
    """How a provision is named to a reader: "section 11", "Schedule 1
    clause 11" where a Schedule's own numbering would otherwise collide
    with the body's, and a container by its own kind."""
    if provision.get("kind") != "provision":
        return f"{provision['type'].capitalize()} {provision['number']}"
    if provision.get("schedule"):
        return f"Schedule {provision['schedule']} clause {provision['number']}"
    return f"section {provision['number']}"


def is_reordering(diff: list[dict]) -> bool:
    """Whether a word diff moves words about without changing any of them
    -- the same words deleted and re-inserted, the same number of times.

    This is what a table read column-first in one reprint and row-first in
    the next looks like, and it is not an amendment. It would also be what
    a genuine transposition looks like, which is why diff_versions reports
    these separately instead of calling them unchanged: in every case seen
    so far the cause was extraction, but that is a measurement, not a
    guarantee.
    """
    removed = Counter(w for s in diff if s["op"] == "delete" for w in s["text"].split())
    added = Counter(w for s in diff if s["op"] == "insert" for w in s["text"].split())
    return bool(removed) and removed == added


def diff_versions(old_nodes: list[dict], new_nodes: list[dict]) -> dict:
    """What changed between two versions of the same Act.

    Returns {"changed", "inserted", "repealed", "reordered", "unchanged"}
    -- the first four lists of provisions, the last a count. A "changed"
    entry carries both texts and the word-level diff between them, and a
    "reordered" one the same, for a caller that wants to look at what was
    set aside (see is_reordering).

    "repealed" is what the Act's own language calls a provision that has
    gone; "inserted" likewise. They are the words the amending Act uses,
    and using anything else here would make the timeline read as though it
    were describing something other than the amendment it is describing.
    """
    old, new = provisions(old_nodes), provisions(new_nodes)
    changed, inserted, repealed, reordered = [], [], [], []
    unchanged = 0
    for key, after in new.items():
        before = old.get(key)
        if before is None:
            inserted.append(after)
            continue
        diff = word_diff(before["text"], after["text"])
        words_changed = any(segment["op"] != "equal" for segment in diff)
        if not words_changed and before["heading"] == after["heading"]:
            unchanged += 1
            continue
        entry = {
            **after,
            "old_text": before["text"],
            "old_heading": before["heading"],
            "diff": diff,
        }
        # A heading that changed is an amendment however the body reads:
        # the Act's own margin notes record "(Heading) amended by ..." as a
        # change in its own right.
        if before["heading"] == after["heading"] and is_reordering(diff):
            reordered.append(entry)
        else:
            changed.append(entry)
    for key, before in old.items():
        if key not in new:
            repealed.append(before)
    # Ordered by where each provision sits in the document, not by parsing
    # its number. The Crimes Act prints s 464, then 464AA, then 464AAB,
    # then 464A -- an order no rule read off the number reproduces, and
    # guessing at one would put provisions in front of a reader in an
    # order the Act itself does not use. The document already knows.
    return {
        "changed": sorted(changed, key=lambda p: p["node_index"]),
        "inserted": sorted(inserted, key=lambda p: p["node_index"]),
        "repealed": sorted(repealed, key=lambda p: p["node_index"]),
        "reordered": sorted(reordered, key=lambda p: p["node_index"]),
        "unchanged": unchanged,
    }


def build_timeline(versions: list[dict]) -> dict[tuple, list[dict]]:
    """{provision_key -> the changes to it, oldest first} across every
    version of a work.

    `versions` is [{"version", "as_at", "as_at_printed", "nodes"}, ...] in
    any order; they are compared in version order. Each entry in a
    provision's list is one version at which it changed, as
    {"version", "as_at", "as_at_printed", "change", ...} where change is
    "inserted", "changed" or "repealed" -- and, for a change, the word
    diff that produced it.

    A provision that has never changed has no entry at all. That is what
    lets the browse view show a timeline only where there is one to show,
    rather than a control on every provision that mostly says "nothing
    happened".
    """
    ordered = sorted(versions, key=lambda v: v["version"])
    timeline: dict[tuple, list[dict]] = {}
    for previous, current in zip(ordered, ordered[1:]):
        stamp = {k: current.get(k) for k in ("version", "as_at", "as_at_printed")}
        result = diff_versions(previous["nodes"], current["nodes"])
        for kind, entries in (("changed", result["changed"]), ("inserted", result["inserted"]),
                              ("repealed", result["repealed"])):
            for entry in entries:
                timeline.setdefault(entry["key"], []).append({**stamp, "change": kind, **entry})
    return timeline
