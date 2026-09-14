"""
Works out what changed in a provision between two Authorised Versions of
an Act.

An Act is reprinted every few weeks, and each reprint restates the whole
thing, so "what actually changed" isn't recorded anywhere -- it has to
be worked out by comparing two 500-page documents. Between consecutive
versions of the Criminal Procedure Act, that's a few dozen provisions
out of around seven hundred.

Three decisions shape the rest of this module.

**A provision is a Section and everything under it.** A Section's own
`text` is often just a lead-in sentence, or nothing at all, with the
real content sitting in its subsections and paragraphs as separate
nodes. Comparing only the Section's own text would report almost every
real amendment as no change. This is the same unit review.py works
through and the browse view gives a page to.

**Provisions are matched by identity, never by position.** A version
that inserts section 26A shifts every node after it, so comparing node
400 of one version with node 400 of the next would report the entire
rest of the Act as rewritten. Instead, provisions are matched on
(Schedule, number) -- commentary.provision_key, the same key that keeps
a Schedule's clause 11 apart from the body's section 11.

**Comparison is on the words, not the characters.** A reprint
repaginates and re-extracts the text, and neither is stable at the
character level: the same sentence can wrap at a different point (those
wrap points are stored as literal newlines in the text -- see
extract.reflow), and PyMuPDF sometimes puts two spaces where the last
reprint put one. Comparing raw text reported 23 of 48 provisions in the
Criminal Procedure Act's timeline as amended when the only difference
was `"may  issue"` versus `"may issue"`.

**The same words in a different order isn't an amendment.** A table's
columns, a Note block, and a run of bullet points can each come out of
extraction in a different sequence between reprints -- sections 7A,
389E and 387I of the Criminal Procedure Act each reported a delete and
an insert of identical text for exactly this reason. An amendment
changes words; it doesn't just move them around. These are set aside as
`reordered` rather than dropped entirely, because this judgement is a
heuristic, and hiding it completely would make a genuine case of
reordered text invisible.
"""
import difflib
import re
from collections import Counter

from .commentary import provision_key
from .extract import reflow
from .hierarchy import UNIT_BOUNDARY_TYPES, UNIT_ROOT_TYPES, schedule_numbers

# Words, and the punctuation stuck to them. Splitting on whitespace
# alone keeps "255(5)(ab)" and "1958," whole, which is what a reader
# expects to see highlighted -- splitting punctuation off separately
# would turn one changed cross-reference into a scatter of tiny diffs.
_WORD_RE = re.compile(r"\S+")

# Any run of whitespace, however it got there.
_SPACES = re.compile(r"\s+")

# Leftover Private Use Area characters from older parses. A PDF can embed
# a font with a symbol encoding and no proper Unicode mapping, and
# PyMuPDF then hands back the raw code point: "Part 3.10 of the Evidence
# Act 2008" used to come out of Criminal Procedure Act v110 as "Part
# \uf033\uf02e\uf031\uf030", and out of v111, from the same words on the
# same page, as "Part 3.10". extract.py now fixes the ones this project's
# Acts actually use at the source (see its own _SYMBOL_FONT_PUA), but
# this stays as a safety net for any parse generated before that fix, or
# for a stray code it doesn't happen to cover -- subtracting 0xF000 gives
# back the plain ASCII character, applied here only for comparison, so an
# older reprint's font quirks are never reported as an amendment.
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
    "equal", "insert" or "delete" -- what a reader needs in order to see
    which words moved.

    A replacement comes back as a delete followed by an insert, rather
    than as an operation of its own: legislative amendments are almost
    always "omit X, insert Y", and showing that as a struck-out phrase
    next to its replacement matches how the Act's own amending words
    describe it.
    """
    old_words, new_words = _tokenise(old), _tokenise(new)
    segments: list[dict] = []

    def add(op: str, words: list[str]) -> None:
        if not words:
            return
        # Runs of the same operation are joined together, so a rendered
        # diff is a handful of spans rather than one per word.
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
    under it, up to the next boundary. Reflowed, so wherever the printer
    happened to break a line isn't part of the comparison."""
    parts = [nodes[root].get("text") or ""]
    i = root + 1
    while i < len(nodes) and nodes[i]["type"] not in UNIT_BOUNDARY_TYPES:
        parts.append(nodes[i].get("text") or "")
        i += 1
    # reflow removes the line wraps; _SPACES removes the rest of the
    # printer's whitespace quirks, which are no more part of the
    # provision's actual wording than the wraps are.
    return _SPACES.sub(" ", _unmap_pua(reflow(" ".join(p for p in parts if p)))).strip()


def _unit_history(nodes: list[dict], root: int) -> list[str]:
    """The amendment notes printed in the margin against a provision and
    everything under it, worded exactly as the Act itself prints them.

    These are what name the Act behind a change. The diff itself can
    tell that section 366 was amended between two versions, but not by
    what -- the Act records that separately, and a note present in the
    new version but absent from the old one ("S. 366 (Heading) amended
    by No. 1/2026 s. 74(1).") is the amendment this diff just found.
    Across the five Criminal Procedure Act versions held here, every one
    of the 13 detected changes was backed up by exactly such a note.
    """
    notes = []
    i = root
    while i < len(nodes) and (i == root or nodes[i]["type"] not in UNIT_BOUNDARY_TYPES):
        for note in nodes[i].get("history") or []:
            raw = note.get("raw")
            if raw:
                notes.append(raw)
        i += 1
    return notes


# What a diff treats as a provision in its own right. Sections and
# clauses are the obvious ones. The containers are included too, because
# amending one is a real amendment that the Act's own margin notes
# record ("Ch. 8 Pt 8.2 Div. 5 (Heading) amended by No. 19/2017 s. 56"),
# and because a Schedule whose items are unnumbered prose keeps them on
# its own node rather than in separate clauses below it -- Schedule 3 of
# the Criminal Procedure Act gained "and Food Innovation" at version
# 114, which a diff that only looked at Sections would have missed
# entirely.
_CONTAINER_TYPES = ("schedule", "chapter", "part", "division", "subdivision")


def provision_identity(node_type: str, schedule: "str | None", number: "str | None") -> tuple:
    """A provision's identity within one version, and the key everything
    in a timeline is stored under. commentary.provision_key keeps a
    Schedule's clause 11 apart from the body's section 11; the kind is
    included too, so a Part 8.2 and a section 8.2 aren't treated as the
    same thing either. Sections and clauses share one kind, since a Bill
    calls a provision a clause and the Act it becomes calls it a
    section."""
    kind = "provision" if node_type in UNIT_ROOT_TYPES else node_type
    return (kind, *provision_key(schedule, number))


def provisions(nodes: list[dict]) -> dict[tuple, dict]:
    """{key -> {"key", "kind", "number", "schedule", "heading", "text",
    "node_index"}} for everything in one version a reader would call a
    provision -- see _CONTAINER_TYPES.

    A provision must have a number of its own. That rules out bare
    topical headings and the synthetic node an Act's front matter lands
    on, which carries the version number and the as-at date, and so
    would look different between every pair of versions even when
    nothing in the Act actually changed.

    Where a version repeats a number within the same Schedule -- which
    happens when the parser mis-splits a heading, not something the Act
    itself does -- the first one wins, and the second is dropped rather
    than silently overwriting it. Reporting a provision as rewritten
    just because a duplicate of it appeared later in the document would
    be worse than not reporting it at all.
    """
    schedules = schedule_numbers(nodes)
    found: dict[tuple, dict] = {}
    for index, node in enumerate(nodes):
        node_type = node["type"]
        if node_type not in UNIT_ROOT_TYPES and node_type not in _CONTAINER_TYPES:
            continue
        if not node.get("number"):
            continue
        # A Schedule isn't "inside itself" -- keyed on the body, or on
        # whichever Schedule it sits in, so Schedule 3 doesn't get read
        # as clause 3 of Schedule 3.
        schedule = None if node_type == "schedule" else schedules[index]
        key = provision_identity(node_type, schedule, node["number"])
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
            "history": _unit_history(nodes, index),
            "node_index": index,
        }
    return found


def label(provision: dict) -> str:
    """How a provision is named for a reader: "section 11", or "Schedule
    1 clause 11" where a Schedule's own numbering would otherwise
    collide with the body's, and a container by its own kind."""
    if provision.get("kind") != "provision":
        return f"{provision['type'].capitalize()} {provision['number']}"
    if provision.get("schedule"):
        return f"Schedule {provision['schedule']} clause {provision['number']}"
    return f"section {provision['number']}"


def is_reordering(diff: list[dict]) -> bool:
    """Whether a word diff just moves words around without changing any
    of them -- the same words deleted and re-inserted, the same number
    of times.

    This is what it looks like when a table gets read column-first in
    one reprint and row-first in the next, and it isn't an amendment. It
    would also be what a genuine transposition looks like, which is why
    diff_versions reports these separately instead of just calling them
    unchanged: in every case seen so far the cause was how the text was
    extracted, but that's an observation, not a guarantee it always will
    be.
    """
    removed = Counter(w for s in diff if s["op"] == "delete" for w in s["text"].split())
    added = Counter(w for s in diff if s["op"] == "insert" for w in s["text"].split())
    return bool(removed) and removed == added


def diff_versions(old_nodes: list[dict], new_nodes: list[dict]) -> dict:
    """What changed between two versions of the same Act.

    Returns {"changed", "inserted", "repealed", "reordered", "unchanged"}
    -- the first four are lists of provisions, the last is just a count.
    A "changed" entry carries both texts and the word-level diff between
    them, and so does a "reordered" one, for a caller that wants to look
    at what got set aside (see is_reordering).

    "repealed" and "inserted" are the words the Act's own language uses
    for a provision that's gone or newly added. Using different words
    here would make the timeline read as if it were describing something
    other than the actual amendment.
    """
    old, new = provisions(old_nodes), provisions(new_nodes)
    changed, inserted, repealed, reordered = [], [], [], []
    unchanged = 0
    for key, after in new.items():
        before = old.get(key)
        if before is None:
            inserted.append({**after, "new_history": after["history"]})
            continue
        diff = word_diff(before["text"], after["text"])
        words_changed = any(segment["op"] != "equal" for segment in diff)
        if not words_changed and before["heading"] == after["heading"]:
            unchanged += 1
            continue
        was = set(before["history"])
        entry = {
            **after,
            "old_text": before["text"],
            "old_heading": before["heading"],
            "diff": diff,
            # The notes this version prints that the last one didn't --
            # the Act's own account of the change just found above.
            "new_history": [note for note in after["history"] if note not in was],
        }
        # A changed heading counts as an amendment no matter what the
        # body says: the Act's own margin notes record "(Heading)
        # amended by ..." as a change in its own right.
        if before["heading"] == after["heading"] and is_reordering(diff):
            reordered.append(entry)
        else:
            changed.append(entry)
    for key, before in old.items():
        if key not in new:
            repealed.append(before)
    # Ordered by where each provision actually sits in the document, not
    # by parsing its number. The Crimes Act prints s 464, then 464AA,
    # then 464AAB, then 464A -- no rule based on the number alone would
    # reproduce that order, and guessing one would show provisions to a
    # reader in an order the Act itself doesn't use. The document
    # already has the real order, so there's no need to guess.
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

    `versions` is [{"version", "as_at", "as_at_printed", "nodes"}, ...]
    in any order; they're compared in version order. Each entry in a
    provision's list is one version where it changed, as
    {"version", "as_at", "as_at_printed", "change", ...} where change is
    "inserted", "changed" or "repealed" -- and, for a change, the word
    diff that produced it and the `new_history` notes naming the Act
    that made it.

    A provision that's never changed has no entry at all. That's what
    lets the browse view show a timeline only where there's actually one
    to show, rather than a control on every single provision that
    mostly just says "nothing happened".
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
