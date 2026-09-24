"""
Keeps a human's review work attached to the right provisions when an Act
gets parsed again.

review.py stores each reviewed piece keyed by `_source_node_index` -- a
*position* in data/parsed/<act>.json. That's fine as long as the
parse never changes, but wrong the moment it does: a parser improvement
that adds, removes or re-splits even one node shifts every index after
it, and the stored review rows silently start describing the wrong
provisions.

Two things here guard against that.

`parse_fingerprint` stamps a parse with a digest of its own structure.
run_pipeline.py writes it into the parse, and review.py writes it onto
every row it saves, so a mismatch can be *detected* instead of passing
silently. That matters most for review.py's "merged away" logic: it
treats any node in an already-finished unit with no matching row as "the
reviewer merged this away" -- which, against a shifted parse, silently
deletes real provisions from the browse view and both exports (this
happened to 203 of the Criminal Procedure Act's 4680 nodes, which is
what led to this module being written).

`remap_verified` is the fix: it matches each stored row to the node in
the new parse holding the same provision -- based on what the provision
*is* (its type, number, heading and opening words), not where it used to
sit -- and updates the index. A row whose provision is genuinely gone is
kept and marked, never dropped -- it's a human's work, and losing it
silently is exactly what this module exists to prevent.
"""
import hashlib
import inspect
import json
import re
from pathlib import Path

from corpus.domain import hierarchy
from corpus.parsing import em_parser, rule_parser
from corpus.parsing.extract import reflow
from corpus.domain.hierarchy import schedule_numbers

# How much of a provision's text counts towards its identity. Enough to
# tell two paragraphs with the same number apart, but short enough that
# a reviewer's edit to the end of a long provision doesn't stop it
# matching.
_IDENTITY_TEXT_CHARS = 120


def _norm(value: "str | None") -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def node_identity(node: dict, schedule: "str | None" = None) -> tuple:
    """What makes this provision itself, regardless of where it sits in
    the node list. This deliberately isn't the full text: a reviewer may
    have edited it, and an edited provision is still the same provision.

    `schedule` is which Schedule the node sits in (see
    hierarchy.schedule_numbers). It's left out of the identity entirely
    when not given, which is what remap_verified always does, since both
    sides of that comparison are positions in the *same* new node list.
    It matters when two different documents are being compared instead
    (see carry_forward_review): without it, a Schedule's clause 11 would
    be indistinguishable from the body's section 11 the moment they're
    looked up by number alone."""
    base = (
        node.get("type") or "",
        _norm(node.get("number")),
        _norm(node.get("heading")),
        _norm(reflow(node.get("text")))[:_IDENTITY_TEXT_CHARS],
    )
    return base if schedule is None else (_norm(schedule), *base)


def parser_version() -> str:
    """A digest of the parsing code itself, recorded into every parse.

    Two versions of one Act can only be compared to find what Parliament
    amended if both were read by the same parser. Read by different ones,
    the parsers' own disagreements -- a definition split out of a block of
    text in one and not the other, a paragraph reclassified, a false split
    removed -- arrive at the browse view as provisions inserted and
    repealed between versions, which is a claim about the law that isn't
    true. Since a re-parse is done one document at a time, a work sits in
    exactly that state for as long as it takes to get through its
    versions.

    Derived from the source rather than declared, so it cannot be
    forgotten: any change to how these modules read a PDF changes it."""
    digest = hashlib.sha256()
    for module in (rule_parser, em_parser, hierarchy):
        digest.update(inspect.getsource(module).encode("utf-8"))
    # Rules approved from review change how a PDF is read as surely as the
    # code does (corpus/teaching).
    from corpus.teaching.rules import rules_path
    if rules_path().exists():
        digest.update(rules_path().read_bytes())
    return digest.hexdigest()[:16]


def parse_fingerprint(nodes: list[dict]) -> str:
    """A digest of a parse's own structure. Changes whenever the node
    list does -- a different count, a node with a new type, a
    renumbered one -- which is exactly when a stored
    `_source_node_index` stops being trustworthy. Text beyond the short
    identity prefix is deliberately left out, so running the same parser
    over the same PDF again produces the same fingerprint."""
    digest = hashlib.sha256()
    digest.update(str(len(nodes)).encode())
    for node in nodes:
        digest.update("\x1f".join(node_identity(node)).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def structural_identity(node: dict, schedule: "str | None" = None) -> tuple:
    """The same provision minus its wording -- what a piece *is*, before
    asking what it currently says. Type, number and heading, plus the
    Schedule it's in wherever that's known (see node_identity)."""
    identity = node_identity(node, schedule)
    return identity[:4] if schedule is not None else identity[:3]


def _label(row: dict) -> str:
    return " ".join(str(row.get(k)) for k in ("type", "number", "heading") if row.get(k))


def _restamp_unit_markers(rows: list[dict], units: list[list[int]]) -> int:
    """Rebuilds the `_unit_end_index` markers _resume_point reads, based
    on the new unit layout. Only a *continuous* run of finished units
    from the very start is marked, because _resume_point trusts the
    highest marker it finds and treats every unit below it as done:
    marking a later unit that happens to be complete would wrongly mark
    the unfinished ones before it as done too, and every node in them
    without a row would then look merged away. Returns how many units
    were marked."""
    have = {row["_source_node_index"] for row in rows if "_source_node_index" in row}
    by_index = {row["_source_node_index"]: row for row in rows if "_source_node_index" in row}
    marked = 0
    for unit_no, indices in enumerate(units):
        if not indices or not all(i in have for i in indices):
            break
        by_index[indices[-1]]["_unit_end_index"] = unit_no
        marked = unit_no + 1
    return marked


def _decide(rows: list[dict], build_key) -> dict[int, tuple[int, str]]:
    """Which new-list index each old row belongs at, and how confident
    that match is -- the matching tiers remap_verified's own docstring
    describes. `build_key` is a list of (tier_name, {identity ->
    [candidate indices]}, key_fn_for_a_row) tuples, one per tier, tried
    in order, so an exact match is claimed first, before a merely
    structural one is allowed to take a node that an exact match might
    still need.

    Split out of remap_verified so carry_forward_review can run the same
    two-tier matching against a *different* document's nodes without
    copying the logic -- the only thing that differs between the two
    callers is what goes into the tables and the row keys, both supplied
    through `build_key`."""
    used: set[int] = set()
    decided: dict[int, tuple[int, str]] = {}

    def nearest(pool: list[int], old_index) -> "int | None":
        free = [i for i in pool if i not in used]
        if not free:
            return None
        return min(free, key=lambda i: abs(i - old_index) if isinstance(old_index, int) else i)

    for tier, table, key in build_key:
        for position, row in enumerate(rows):
            if position in decided:
                continue
            best = nearest(table.get(key(row), []), row.get("_source_node_index"))
            if best is not None:
                used.add(best)
                decided[position] = (best, tier)
    return decided


# What a row holds about the provision itself, as opposed to what a
# reviewer decided about it. Refreshing an unapproved row replaces
# exactly these and leaves `needs_followup` alone.
_NODE_CONTENT_KEYS = ("type", "number", "heading", "text", "page_start", "page_end",
                      "char_start", "char_end", "source", "path", "history")


def _refresh_from_node(row: dict, node: dict) -> None:
    """Puts the new parse's reading of a provision into a row nobody
    approved.

    A stored row overrides the parser output wherever it sits (see
    review.build_current_nodes), and that is true whether or not anyone
    ever accepted it -- so a provision flagged as wrongly parsed went on
    showing its wrong text for ever, and the improved parse never
    surfaced for the very provisions marked as needing it. The flag stays
    on, so it is still in the queue; only the words change."""
    for key in _NODE_CONTENT_KEYS:
        if key in node:
            row[key] = node[key]
        else:
            row.pop(key, None)
    row.pop("verified_at", None)
    row.pop("_text_changed", None)


def _apply_decisions(rows: list[dict], decided: dict[int, tuple[int, str]], new_nodes: list[dict],
                     units: "list[list[int]] | None",
                     keep_accepted: bool = False) -> tuple[list[dict], dict]:
    """Rewrites `rows` onto the positions `_decide` chose, and reports
    what happened -- the last step both remap_verified and
    carry_forward_review need once matching is done.

    `keep_accepted` re-parses only what nobody approved: see
    remap_verified for why that is worth having and what it costs."""
    report = {"matched": 0, "moved": 0, "text_changed": 0, "orphaned": 0, "orphans": [], "changed": [],
              "kept": 0, "refreshed": 0}
    remapped: list[dict] = []
    for position, original in enumerate(rows):
        row = {k: v for k, v in original.items() if k != "_unit_end_index"}
        found = decided.get(position)
        if found is None:
            row.pop("_source_node_index", None)
            row["_orphaned"] = True
            report["orphaned"] += 1
            report["orphans"].append(_label(row))
            remapped.append(row)
            continue
        index, tier = found
        row.pop("_orphaned", None)
        if index != original.get("_source_node_index"):
            report["moved"] += 1
        row["_source_node_index"] = index
        report["matched"] += 1
        if keep_accepted and not original.get("verified_at"):
            # Nobody approved this one, so it is exactly what the re-parse
            # is for: take the new parser's reading of it.
            _refresh_from_node(row, new_nodes[index])
            report["refreshed"] += 1
        elif tier == "changed":
            if keep_accepted:
                # Approved, and the parser now reads it differently. The
                # reviewer's words stand: that is the whole point of the
                # mode, and their row is what every reader already shows.
                report["kept"] += 1
            else:
                row.pop("verified_at", None)
                row["needs_followup"] = True
                row["_text_changed"] = True
                report["text_changed"] += 1
                report["changed"].append(_label(row))
        remapped.append(row)

    remapped.sort(key=lambda r: r.get("_source_node_index", len(new_nodes)))
    if units is not None:
        report["units_marked"] = _restamp_unit_markers(remapped, units)
    return remapped, report


def remap_verified(
    rows: list[dict], new_nodes: list[dict], units: "list[list[int]] | None" = None,
    keep_accepted: bool = False,
) -> tuple[list[dict], dict]:
    """Re-points stored review rows at their provisions in a new parse.

    Returns (rows, report). Matching runs in two tiers, and the
    difference between them is the whole point of this function:

      * A row whose provision is unchanged -- same type, number, heading
        and opening words -- carries across intact, acceptance and all.
      * A row whose provision still exists but whose *wording* changed
        also carries across, but its acceptance is withdrawn and it's
        flagged for another look. Re-parsing usually changes text
        because the parser now gets something right it used to get
        wrong, and silently re-applying a human's "I accept this" to
        words they never actually read would be worse than just asking
        them to look again.
      * A row whose provision is gone is kept, with `_orphaned` set, and
        counted. It's a human's work, and it doesn't get deleted.

    `keep_accepted` changes the middle case, for the reviewer who wants a
    better parser over the rest of a document without paying for it in
    finished work: an approved row keeps its acceptance, and an
    unapproved row is re-read from the new parse instead (see
    _refresh_from_node).

    Where a parse holds several identical provisions (say, a Schedule
    that reprints an Act's own numbering), whichever candidate is
    nearest the row's old position wins. Exact matches are given out
    before changed ones, so a provision whose wording drifted can never
    take the node that belongs to one that matches exactly.

    `_unit_end_index` is worked out fresh rather than carried over: unit
    numbers shift along with the node list, and a stale marker is what
    makes review.py resume in the wrong place and then wrongly assume
    every unreviewed node before it was "merged away". Pass `units`
    (review.group_into_units's output for the new parse) to have these
    rebuilt; without it they're dropped, and review.py marks those
    units as complete again from scratch.
    """
    exact: dict[tuple, list[int]] = {}
    structural: dict[tuple, list[int]] = {}
    for index, node in enumerate(new_nodes):
        exact.setdefault(node_identity(node), []).append(index)
        structural.setdefault(structural_identity(node), []).append(index)

    decided = _decide(rows, (("exact", exact, node_identity), ("changed", structural, structural_identity)))
    return _apply_decisions(rows, decided, new_nodes, units, keep_accepted)


def _cross_version_identity(node: dict, schedule: "str | None") -> tuple:
    """The same provision across two different documents, allowing for
    its heading to have changed too -- deliberately a looser match than
    structural_identity.

    Within one document's own reparse, a repeated number is a parser
    mistake, and the heading is what tells the two apart (see
    structural_identity's own docstring). Between two Authorised
    Versions, that's not true at all: an amending Act can rewrite a
    provision's heading in the same stroke as its body -- section 366 of
    the Criminal Procedure Act got both a new heading and new paragraphs
    from the same amending Act at once. Requiring the old heading to
    still match would read that ordinary amendment as the provision
    disappearing and an unrelated new one taking its place. Type,
    Schedule and number are all that has to survive an amendment for a
    provision to still be recognisably the same one.
    """
    return (node.get("type") or "", _norm(schedule), _norm(node.get("number")))


def carry_forward_review(
    old_nodes: list[dict], old_rows: list[dict], new_nodes: list[dict],
    units: "list[list[int]] | None" = None,
) -> tuple[list[dict], dict]:
    """Seeds a new Authorised Version's review work from an earlier
    version -- the same matching remap_verified does, extended to
    handle one thing a single document's own reparse never has to deal
    with: `old_nodes` and `new_nodes` are two different documents, each
    with its own Schedule structure, so a bare number isn't a safe key
    between them. Schedule 3 of one version and Schedule 3 of the next
    both start numbering their clauses at 1 again, and so does the
    body -- looking a row up by number alone could just as easily hand a
    Schedule 1 clause 11's review to the body's section 11 in the new
    version instead of its own counterpart.

    Otherwise this gives exactly the same three outcomes as
    remap_verified, for the same reasons: a provision whose wording is
    identical between versions carries across intact; one whose wording
    changed carries across too, but with its acceptance withdrawn and
    flagged, because "the last person to read this read different
    words" is just as true here as after a parser change, and deserves
    the same second look rather than a silently inherited tick; one no
    longer present in the new version -- usually because it was
    repealed -- is kept, marked orphaned, never dropped.
    """
    old_schedule = schedule_numbers(old_nodes)
    new_schedule = schedule_numbers(new_nodes)

    def row_schedule(row: dict) -> "str | None":
        index = row.get("_source_node_index")
        return old_schedule[index] if isinstance(index, int) and 0 <= index < len(old_schedule) else None

    exact: dict[tuple, list[int]] = {}
    same_provision: dict[tuple, list[int]] = {}
    for index, node in enumerate(new_nodes):
        sched = new_schedule[index]
        exact.setdefault(node_identity(node, sched), []).append(index)
        same_provision.setdefault(_cross_version_identity(node, sched), []).append(index)

    decided = _decide(old_rows, (
        ("exact", exact, lambda row: node_identity(row, row_schedule(row))),
        ("changed", same_provision, lambda row: _cross_version_identity(row, row_schedule(row))),
    ))
    return _apply_decisions(old_rows, decided, new_nodes, units)


def describe_remap(report: dict) -> str:
    bits = [f"{report['matched']} reviewed piece(s) re-anchored"]
    if report["moved"]:
        bits.append(f"{report['moved']} moved position")
    if report["text_changed"]:
        bits.append(f"{report['text_changed']} whose wording changed -- acceptance withdrawn, flagged for another look")
    if report.get("kept"):
        bits.append(f"{report['kept']} approved piece(s) whose wording the parser now reads differently -- "
                    "acceptance kept, as asked")
    if report.get("refreshed"):
        bits.append(f"{report['refreshed']} unapproved piece(s) re-read from this parse")
    if report["orphaned"]:
        bits.append(f"{report['orphaned']} no longer in the parse (kept, marked orphaned)")
    if "units_marked" in report:
        bits.append(f"resume point now unit {report['units_marked']}")
    if report.get("structure_edits_dropped"):
        bits.append(
            f"{report['structure_edits_dropped']} structural edit(s) discarded -- they name node "
            "positions this parse has replaced (any inserted text was kept as an orphaned review)"
        )
    return "; ".join(bits)


def apply_remap(
    act: str,
    new_nodes: list[dict],
    units: "list[list[int]] | None" = None,
    base_dir: "str | Path | None" = None,
    keep_accepted: bool = False,
) -> "dict | None":
    """Re-attaches an Act's stored review work to a parse that's just
    been redone, and records which parse it now belongs to.

    `keep_accepted` re-parses only what nobody approved: an approved
    provision keeps its text and its tick even where the parser now reads
    it differently, and an unapproved one -- flagged or never looked at --
    is re-read from this parse. Off by default, because withdrawing
    acceptance from words a reviewer never saw is the safer thing to do
    when nobody has said otherwise.

    Returns the remap report, or None when there was no review work to
    move (the normal case for a first parse) or when the new parse is
    already the one the rows belong to -- re-attaching an unchanged
    parse isn't free, since `_unit_end_index` gets rebuilt, and a unit
    whose pieces a reviewer deliberately merged away would no longer
    look complete, walking the resume point backwards for no reason.

    Orphaned rows move out of `verified` and into `orphaned_reviews`:
    the verified table is keyed by node position, and an orphan doesn't
    have one any more, so this is the only way to keep them at all.

    Structural edits (corpus/structure.py) do not survive a
    re-parse, and are discarded here -- see _discard_structure_edits.
    """
    from corpus.storage import db

    fingerprint = parse_fingerprint(new_nodes)
    unchanged = db.load_parse_fingerprint(act, base_dir) == fingerprint
    dropped = 0 if unchanged else _discard_structure_edits(act, base_dir)
    rows = db.load_verified(act, base_dir)
    if unchanged:
        return None
    if not rows:
        db.save_parse_fingerprint(act, fingerprint, base_dir)
        return {"matched": 0, "moved": 0, "text_changed": 0, "orphaned": 0,
                "structure_edits_dropped": dropped} if dropped else None

    remapped, report = remap_verified(rows, new_nodes, units, keep_accepted)
    db.add_orphaned_reviews(act, [r for r in remapped if r.get("_orphaned")], base_dir)
    db.save_verified(act, [r for r in remapped if not r.get("_orphaned")], base_dir)
    db.save_parse_fingerprint(act, fingerprint, base_dir)
    if dropped:
        report["structure_edits_dropped"] = dropped
    return report


def _discard_structure_edits(act: str, base_dir: "str | Path | None" = None) -> int:
    """Drops this document's structural edits, keeping the text of
    anything a reviewer had typed in. Returns how many were dropped.

    A structural edit says "insert after node 412", "delete node 87",
    "put node 5 after node 260" -- all of them positions into the parse
    that has just been replaced. A verified row can be re-anchored
    because it carries its provision's own type, number, heading and
    opening words, so it can be recognised wherever it ended up; an edit
    carries no such thing about the node it *points at*, and applying
    "delete node 87" to whatever node 87 now happens to be would delete a
    provision nobody asked to lose. Redoing a handful of moves is a small
    cost; a deletion landing on the wrong section, silently, is not.

    What can't be redone from the PDF is the text of an inserted piece --
    a person typed it, precisely because the parse didn't have it -- so
    those go to orphaned_reviews, the same place a reviewed row goes when
    its provision is gone from the new parse. Nothing a human wrote is
    deleted here.
    """
    from corpus.storage import db

    edits = db.load_structure_edits(act, base_dir)
    if not edits:
        return 0
    inserted = [edit["node"] for _index, edit in sorted(edits.items()) if edit.get("node")]
    if inserted:
        db.add_orphaned_reviews(act, inserted, base_dir)
    db.save_structure_edits(act, {}, base_dir)
    return len(edits)


def apply_carry_forward(
    work: str, new_slug: str, new_nodes: list[dict],
    units: "list[list[int]] | None" = None, base_dir: "str | Path | None" = None,
) -> "dict | None":
    """Seeds a newly-parsed Authorised Version's review work from the
    nearest earlier version of the same work that has some, so a
    reviewer opens a new reprint already carrying over everything that
    didn't change, instead of starting from nothing across all of its
    roughly 700 provisions.

    Never overwrites: this does nothing the moment `new_slug` already
    has any verified rows of its own, whether that's a reviewer's own
    work or an earlier run of this same carry-forward. It also does
    nothing if no earlier version of `work` has any review rows to carry
    forward -- the normal case for a work's first version, or one nobody
    has reviewed yet.

    Checks earlier versions starting from the nearest one and working
    outward, rather than always starting from the very first version, so
    an old reprint that was never reviewed doesn't get checked before a
    more recent one that was.
    """
    from corpus.storage import db
    from .versions import split_document_slug

    if db.load_verified(new_slug, base_dir):
        return None
    _work, target_version = split_document_slug(new_slug)
    if target_version is None:
        return None

    parsed_dir = Path(base_dir or ".") / "data" / "parsed"
    earlier = []
    for path in parsed_dir.glob(f"{work}-v*.json"):
        _w, version = split_document_slug(path.stem)
        if version is not None and version < target_version:
            earlier.append((version, path))
    earlier.sort(reverse=True)  # nearest version first

    for _version, source_path in earlier:
        source_slug = source_path.stem
        source_rows = db.load_verified(source_slug, base_dir)
        if not source_rows:
            continue
        try:
            source_nodes = json.loads(source_path.read_text(encoding="utf-8"))["nodes"]
        except (OSError, ValueError, KeyError):
            continue
        remapped, report = carry_forward_review(source_nodes, source_rows, new_nodes, units)
        db.add_orphaned_reviews(new_slug, [r for r in remapped if r.get("_orphaned")], base_dir)
        db.save_verified(new_slug, [r for r in remapped if not r.get("_orphaned")], base_dir)
        db.save_parse_fingerprint(new_slug, parse_fingerprint(new_nodes), base_dir)
        report["source"] = source_slug
        return report
    return None
