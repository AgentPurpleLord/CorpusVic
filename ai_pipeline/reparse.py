"""
Keeps a human's review work attached to its provisions when an Act is
parsed again.

review.py stores each reviewed piece keyed by `_source_node_index` -- a
*position* in data/ai_parsed/<act>.json. That is fine while the parse
never changes, and wrong the moment it does: a parser improvement that
adds, removes or re-splits a single node shifts every index after it, and
the stored rows silently start describing different provisions.

Two things here guard against that.

`parse_fingerprint` stamps a parse with a digest of its own structure.
run_pipeline.py writes it into the parse and review.py writes it onto every
row it saves, so a mismatch is *detectable* rather than silent. That
matters most for review.py's merged-away inference: it treats any node in
an already-finished unit that has no verified row as "the reviewer merged
this away", which against a shifted parse quietly deletes real provisions
from the browse view and from both exports (203 of the Criminal Procedure
Act's 4680 nodes, in the case that prompted this module).

`remap_verified` is the repair: it matches each stored row to the node in
the new parse that holds the same provision -- by what the provision *is*
(its type, number, heading and opening words) rather than where it sat --
and rewrites the index. A row whose provision is genuinely gone is kept
and marked, never dropped: it is a human's work, and losing it silently is
the failure this whole module exists to prevent.
"""
import hashlib
import json
import re
from pathlib import Path

from .extract import reflow
from .hierarchy import schedule_numbers

# How much of a provision's own text takes part in its identity. Enough to
# tell two same-numbered paragraphs apart, short enough that a reviewer's
# own edit to the tail of a long provision doesn't stop it matching.
_IDENTITY_TEXT_CHARS = 120


def _norm(value: "str | None") -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def node_identity(node: dict, schedule: "str | None" = None) -> tuple:
    """What makes this provision itself, independent of where it sits in
    the node list. Deliberately not the full text: a reviewer may have
    edited it, and an edited provision is still the same provision.

    `schedule` is which Schedule the node sits in (see
    hierarchy.schedule_numbers), and is left out of the identity entirely
    when not given -- which is every call remap_verified makes, since both
    sides of that comparison are positions in the *same* new node list and
    a caller there already has the option of passing it. It matters where
    two documents are being compared instead (see carry_forward_review):
    without it a Schedule's clause 11 is indistinguishable from the body's
    section 11 the moment they are looked up by number alone."""
    base = (
        node.get("type") or "",
        _norm(node.get("number")),
        _norm(node.get("heading")),
        _norm(reflow(node.get("text")))[:_IDENTITY_TEXT_CHARS],
    )
    return base if schedule is None else (_norm(schedule), *base)


def parse_fingerprint(nodes: list[dict]) -> str:
    """A digest of a parse's own structure. Changes whenever the node list
    does -- a different count, a re-typed node, a renumbered one -- which
    is exactly when a stored `_source_node_index` stops being trustworthy.
    Text is deliberately excluded past the identity prefix, so re-running
    the same parser over the same PDF reproduces the same fingerprint."""
    digest = hashlib.sha256()
    digest.update(str(len(nodes)).encode())
    for node in nodes:
        digest.update("\x1f".join(node_identity(node)).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def structural_identity(node: dict, schedule: "str | None" = None) -> tuple:
    """The same provision minus its wording -- what a piece *is*, before
    any question of what it currently says. Type, number and heading; plus
    the Schedule it sits in, wherever that's known (see node_identity)."""
    identity = node_identity(node, schedule)
    return identity[:4] if schedule is not None else identity[:3]


def _label(row: dict) -> str:
    return " ".join(str(row.get(k)) for k in ("type", "number", "heading") if row.get(k))


def _restamp_unit_markers(rows: list[dict], units: list[list[int]]) -> int:
    """Rebuilds the `_unit_end_index` markers _resume_point reads, over the
    new unit layout. Only a *contiguous* run of finished units from the
    start is marked, because _resume_point trusts the highest marker it
    finds and treats every unit below it as done: marking a late unit that
    happens to be complete would declare the unfinished ones before it
    finished too, and every node in them without a row would then read as
    merged away. Returns how many units were marked."""
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
    """Which new-list index each old row belongs at, and how sure -- the
    matching tiers remap_verified's own docstring describes. `build_key`
    is (tier_name, {identity -> [candidate indices]}, key_fn_for_a_row) for
    each tier in turn, tried in order, so an exact match is claimed before
    a merely structural one is allowed to take a node that might belong to
    a row that matches it exactly.

    Split out of remap_verified so carry_forward_review can run the same
    two-tier matching against a *different* document's nodes without
    duplicating it -- the only thing that differs between the two callers
    is what goes into the tables and the row keys, both supplied by
    `build_key`."""
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


def _apply_decisions(rows: list[dict], decided: dict[int, tuple[int, str]], new_nodes: list[dict],
                     units: "list[list[int]] | None") -> tuple[list[dict], dict]:
    """Rewrites `rows` onto the positions `_decide` chose, and reports what
    happened -- the tail end remap_verified and carry_forward_review both
    need once matching is done."""
    report = {"matched": 0, "moved": 0, "text_changed": 0, "orphaned": 0, "orphans": [], "changed": []}
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
        if tier == "changed":
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
    rows: list[dict], new_nodes: list[dict], units: "list[list[int]] | None" = None
) -> tuple[list[dict], dict]:
    """Re-points stored review rows at their provisions in a new parse.

    Returns (rows, report). Matching runs in two tiers, and the difference
    between them is the point of this function:

      * A row whose provision is unchanged -- same type, number, heading
        and opening words -- is carried across intact, acceptance and all.
      * A row whose provision still exists but whose *wording* has changed
        is carried across too, but its acceptance is withdrawn and it is
        flagged for follow-up. Re-parsing usually changes text because the
        parser now gets something right that it previously got wrong, and
        quietly re-applying a human's "I accept this" to words they never
        read would be worse than asking them to look again.
      * A row whose provision has gone is kept with `_orphaned` set and
        counted. It is a human's work; it is not deleted.

    Where a parse holds several identical provisions (a Schedule
    reprinting an Act's own numbering), the candidate nearest the row's
    old position wins. Exact matches are allocated before changed ones, so
    a provision whose wording drifted can never claim the node belonging
    to one that didn't.

    `_unit_end_index` is re-derived rather than carried: unit numbering
    shifts with the node list, and a stale marker is what makes review.py
    resume in the wrong place and then infer that every unreviewed node
    before it was "merged away". Pass `units` (review.group_into_units's
    output for the new parse) to have them rebuilt; without it they are
    dropped, and review.py re-stamps them as units complete.
    """
    exact: dict[tuple, list[int]] = {}
    structural: dict[tuple, list[int]] = {}
    for index, node in enumerate(new_nodes):
        exact.setdefault(node_identity(node), []).append(index)
        structural.setdefault(structural_identity(node), []).append(index)

    decided = _decide(rows, (("exact", exact, node_identity), ("changed", structural, structural_identity)))
    return _apply_decisions(rows, decided, new_nodes, units)


def _cross_version_identity(node: dict, schedule: "str | None") -> tuple:
    """The same provision across two different documents, once even its
    heading may have moved -- deliberately weaker than structural_identity.

    Within one document's own reparse, a repeated number is a parser
    mistake and the heading is what tells the two apart (see
    structural_identity's own docstring). Between two Authorised Versions
    it is nothing of the sort: an amending Act freely rewrites a
    provision's heading in the same stroke as its body -- section 366 of
    the Criminal Procedure Act gained both a new heading and new
    paragraphs from the same amending Act at once -- and requiring the old
    heading to still match would read that ordinary amendment as the
    provision disappearing and an unrelated new one appearing in its
    place. Type, Schedule and number is everything that has to survive an
    amendment for it to still be recognisably the same provision.
    """
    return (node.get("type") or "", _norm(schedule), _norm(node.get("number")))


def carry_forward_review(
    old_nodes: list[dict], old_rows: list[dict], new_nodes: list[dict],
    units: "list[list[int]] | None" = None,
) -> tuple[list[dict], dict]:
    """Seeds a new Authorised Version's review work from an earlier one --
    remap_verified's own matching, extended for the one thing a single
    document's own reparse never has to tell apart: `old_nodes` and
    `new_nodes` are two different documents, each with its own Schedule
    structure, so a bare number is not a safe key between them. Schedule 3
    of one version and Schedule 3 of the next both start their clauses at
    1 again, and so does the body -- looking a row up by number alone
    would as happily hand a Schedule 1 clause 11's review to the body's
    section 11 in the new version as to its own counterpart.

    Otherwise this is exactly remap_verified's own three-way outcome, and
    for the same reason: a provision whose wording is identical between
    versions is carried across intact; one whose wording moved is carried
    across with its acceptance withdrawn and flagged, because "the last
    person to read this read different words" is true here just as it is
    after a parser change, and for the same reason deserves a second look
    rather than a silently inherited tick; one no longer present in the
    new version -- most often because it was repealed -- is kept, marked
    orphaned, never dropped.
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
    if report["orphaned"]:
        bits.append(f"{report['orphaned']} no longer in the parse (kept, marked orphaned)")
    if "units_marked" in report:
        bits.append(f"resume point now unit {report['units_marked']}")
    return "; ".join(bits)


def apply_remap(
    act: str,
    new_nodes: list[dict],
    units: "list[list[int]] | None" = None,
    base_dir: "str | Path | None" = None,
) -> "dict | None":
    """Re-anchors an Act's stored review work onto a parse that has just
    been rewritten, and records which parse it now belongs to.

    Returns the remap report, or None when there was no review work to
    move (the ordinary case for a first parse) or when the new parse is
    the one the rows already belong to -- re-anchoring an unchanged parse
    is not free, since `_unit_end_index` is re-derived and a unit whose
    pieces a reviewer deliberately merged away can no longer look
    complete, which would walk the resume point backwards for nothing.

    Orphans are moved out of `verified` and into `orphaned_reviews`: the
    verified table is keyed by node position and an orphan has none left,
    so this is the only way to keep them at all.
    """
    from . import db

    fingerprint = parse_fingerprint(new_nodes)
    rows = db.load_verified(act, base_dir)
    if not rows:
        db.save_parse_fingerprint(act, fingerprint, base_dir)
        return None
    if db.load_parse_fingerprint(act, base_dir) == fingerprint:
        return None

    remapped, report = remap_verified(rows, new_nodes, units)
    db.add_orphaned_reviews(act, [r for r in remapped if r.get("_orphaned")], base_dir)
    db.save_verified(act, [r for r in remapped if not r.get("_orphaned")], base_dir)
    db.save_parse_fingerprint(act, fingerprint, base_dir)
    return report


def apply_carry_forward(
    work: str, new_slug: str, new_nodes: list[dict],
    units: "list[list[int]] | None" = None, base_dir: "str | Path | None" = None,
) -> "dict | None":
    """Seeds a newly-parsed Authorised Version's review work from the
    nearest earlier version of the same work that has some, so a reviewer
    opens a new reprint already carrying forward everything that didn't
    change instead of starting from nothing on every one of its ~700
    provisions.

    Never overwrites: a no-op the moment `new_slug` already holds any
    verified rows of its own, whether that is a reviewer's own work or an
    earlier run of this same carry-forward. Also a no-op where no earlier
    version of `work` has review rows to carry forward at all -- the
    ordinary case for a work's first version, or one nobody has reviewed
    yet.

    Walks earlier versions from the nearest outward rather than always
    reading from the very first one, so a version several reprints old
    that was never reviewed doesn't stand in front of a more recent one
    that was.
    """
    from . import db
    from .versions import split_document_slug

    if db.load_verified(new_slug, base_dir):
        return None
    _work, target_version = split_document_slug(new_slug)
    if target_version is None:
        return None

    ai_parsed_dir = Path(base_dir or ".") / "data" / "ai_parsed"
    earlier = []
    for path in ai_parsed_dir.glob(f"{work}-v*.json"):
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
