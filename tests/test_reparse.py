"""Tests for corpus/reparse.py -- keeping a human's review work
attached to its provisions when an Act is parsed again.

Review rows are keyed by a *position* into data/parsed/<act>.json, so
a parser change that adds or re-splits one node shifts every row after
it onto the wrong provision. These cover the two halves of the fix: a
fingerprint that makes such a shift detectable, and a re-anchoring that
moves each row onto the node holding the provision it describes."""
import json

from corpus.db import (
    load_orphaned_reviews,
    load_parse_fingerprint,
    load_structure_edits,
    load_verified,
    save_parse_fingerprint,
    save_structure_edits,
    save_verified,
)
from corpus.hierarchy import group_into_units
from corpus.reparse import (
    apply_carry_forward,
    apply_remap,
    carry_forward_review,
    describe_remap,
    node_identity,
    parse_fingerprint,
    remap_verified,
    structural_identity,
)

from conftest import make_node


def _act() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("section", "2", "Commencement", "This Act comes into operation on 1 January."),
        make_node("section", "3", "Definitions", "In this Act—"),
    ]


def _reviewed(node: dict, index: int, **extra) -> dict:
    return dict(node, verified_at="2024-01-01T00:00:00+00:00", _source_node_index=index, **extra)


# ---------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------

def test_the_same_parse_fingerprints_the_same_twice():
    # Re-running an unchanged parser over an unchanged PDF must not look
    # like a change, or every re-parse would needlessly re-anchor.
    assert parse_fingerprint(_act()) == parse_fingerprint(_act())


def test_inserting_a_node_changes_the_fingerprint():
    nodes = _act()
    shifted = nodes[:2] + [make_node("subsection", "1", None, "an inserted piece")] + nodes[2:]

    assert parse_fingerprint(shifted) != parse_fingerprint(nodes)


def test_retyping_a_node_changes_the_fingerprint():
    nodes = _act()
    nodes[0] = dict(nodes[0], type="chapter")

    assert parse_fingerprint(nodes) != parse_fingerprint(_act())


def test_an_edit_past_the_identity_prefix_does_not_change_the_fingerprint():
    # A reviewer's own correction to the tail of a long provision is not a
    # different parse, and must not read as one.
    long_text = "x" * 400
    nodes = [make_node("section", "1", "Long", long_text)]
    edited = [make_node("section", "1", "Long", long_text[:-1] + "y")]

    assert parse_fingerprint(nodes) == parse_fingerprint(edited)


def test_node_identity_ignores_where_the_pdf_wrapped_a_line():
    # Stored text keeps the source PDF's own line-wraps; they are not part
    # of what a provision *is*.
    wrapped = make_node("section", "1", "Purposes", "The purposes of\nthis Act are—")
    flowed = make_node("section", "1", "Purposes", "The purposes of this Act are—")

    assert node_identity(wrapped) == node_identity(flowed)


# ---------------------------------------------------------------------
# Re-anchoring
# ---------------------------------------------------------------------

def test_a_row_follows_its_provision_when_an_earlier_node_is_inserted():
    nodes = _act()
    rows = [_reviewed(nodes[2], 2)]
    shifted = nodes[:2] + [make_node("subsection", "1", None, "an inserted piece")] + nodes[2:]

    remapped, report = remap_verified(rows, shifted)

    assert remapped[0]["_source_node_index"] == 3
    assert remapped[0]["heading"] == "Commencement"
    assert report["matched"] == 1 and report["moved"] == 1


def test_an_unchanged_parse_leaves_every_row_where_it_was():
    nodes = _act()
    rows = [_reviewed(nodes[1], 1), _reviewed(nodes[3], 3)]

    remapped, report = remap_verified(rows, nodes)

    assert [r["_source_node_index"] for r in remapped] == [1, 3]
    assert report["moved"] == 0 and report["orphaned"] == 0


def test_a_reviewers_own_edits_are_carried_across_not_the_parsers_text():
    # The whole point: what is preserved is the human's version.
    nodes = _act()
    rows = [_reviewed(nodes[1], 1, heading="Purposes (as corrected)")]

    remapped, _ = remap_verified(rows, [make_node("part", "1", "Preliminary")] + nodes[1:])

    assert remapped[0]["heading"] == "Purposes (as corrected)"


def test_a_provision_whose_wording_changed_keeps_the_row_but_loses_its_acceptance():
    # The parser now reads this provision differently. Silently re-applying
    # a human's "I accept this" to words they never saw would be worse than
    # asking them to look again.
    nodes = _act()
    rows = [_reviewed(nodes[2], 2)]
    reparsed = list(nodes)
    reparsed[2] = make_node("section", "2", "Commencement", "This Act comes into operation on 1 July.")

    remapped, report = remap_verified(rows, reparsed)

    assert remapped[0]["_source_node_index"] == 2
    assert "verified_at" not in remapped[0]
    assert remapped[0]["needs_followup"] is True
    assert report["text_changed"] == 1


def test_an_exact_match_wins_the_node_over_a_merely_structural_one():
    # Two same-numbered provisions, one unchanged and one reworded: the
    # unchanged row must not be displaced by the reworded one.
    nodes = [
        make_node("section", "1", "A", "text one"),
        make_node("section", "1", "A", "text two"),
    ]
    rows = [_reviewed(make_node("section", "1", "A", "text two (old)"), 0), _reviewed(nodes[1], 1)]

    remapped, report = remap_verified(rows, nodes)

    by_index = {r["_source_node_index"]: r for r in remapped}
    assert by_index[1]["text"] == "text two"
    assert "verified_at" in by_index[1]  # the unchanged one keeps its acceptance
    assert "verified_at" not in by_index[0]
    assert report["text_changed"] == 1


def test_a_provision_the_new_parse_no_longer_has_is_kept_and_marked():
    nodes = _act()
    rows = [_reviewed(make_node("section", "9", "Repealed elsewhere", "gone"), 4)]

    remapped, report = remap_verified(rows, nodes)

    assert report["orphaned"] == 1
    assert remapped[0]["_orphaned"] is True
    assert "_source_node_index" not in remapped[0]
    assert "Repealed elsewhere" in report["orphans"][0]


def test_unit_markers_are_rebuilt_over_the_new_layout():
    # _resume_point trusts the highest `_unit_end_index` it finds, so a
    # marker carried over from the old numbering is what makes review.py
    # resume in the wrong place -- and then infer that every unreviewed
    # node before it was merged away.
    nodes = _act()
    units = group_into_units(nodes)  # [[0], [1], [2], [3]]
    rows = [_reviewed(nodes[0], 0, _unit_end_index=7), _reviewed(nodes[1], 1)]

    remapped, report = remap_verified(rows, nodes, units)

    assert [r.get("_unit_end_index") for r in remapped] == [0, 1]
    assert report["units_marked"] == 2


def test_only_a_contiguous_run_of_finished_units_is_marked():
    # Unit 1 is unreviewed, so unit 2 being complete must not declare it
    # finished too -- that is exactly how a whole unit's provisions come
    # to read as "merged away".
    nodes = _act()
    rows = [_reviewed(nodes[0], 0), _reviewed(nodes[2], 2)]

    _remapped, report = remap_verified(rows, nodes, group_into_units(nodes))

    assert report["units_marked"] == 1


def test_describe_remap_says_only_what_happened():
    nodes = _act()
    rows = [_reviewed(nodes[0], 0), _reviewed(nodes[1], 1)]

    _remapped, report = remap_verified(rows, nodes, group_into_units(nodes))

    assert describe_remap(report) == "2 reviewed piece(s) re-anchored; resume point now unit 2"


# ---------------------------------------------------------------------
# apply_remap: the whole round trip through the database
# ---------------------------------------------------------------------

def test_apply_remap_moves_the_stored_rows_and_records_the_new_parse(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = _act()
    save_verified("crimes-act", [_reviewed(nodes[2], 2)])
    reparsed = nodes[:2] + [make_node("subsection", "1", None, "an inserted piece")] + nodes[2:]

    report = apply_remap("crimes-act", reparsed, group_into_units(reparsed))

    assert report["matched"] == 1
    assert load_verified("crimes-act")[0]["_source_node_index"] == 3
    assert load_parse_fingerprint("crimes-act") == parse_fingerprint(reparsed)


def test_apply_remap_does_nothing_the_second_time_over_the_same_parse(tmp_path, monkeypatch):
    # Re-anchoring is not free: it re-derives the unit markers, and a unit
    # whose pieces the reviewer merged away can no longer look complete.
    # Doing that against the parse the rows already belong to would walk
    # the resume point backwards for no reason.
    monkeypatch.chdir(tmp_path)
    nodes = _act()
    save_verified("crimes-act", [_reviewed(nodes[1], 1)])
    apply_remap("crimes-act", nodes, group_into_units(nodes))

    assert apply_remap("crimes-act", nodes, group_into_units(nodes)) is None


def test_apply_remap_files_an_orphan_out_of_the_verified_table(tmp_path, monkeypatch):
    # `verified` is keyed by node position and an orphan has none left, so
    # it cannot stay there -- but it is a human's work and is not deleted.
    monkeypatch.chdir(tmp_path)
    stranded = _reviewed(make_node("section", "9", "Repealed elsewhere", "gone"), 4)
    save_verified("crimes-act", [stranded])

    report = apply_remap("crimes-act", _act(), group_into_units(_act()))

    assert report["orphaned"] == 1
    assert load_verified("crimes-act") == []
    kept = load_orphaned_reviews("crimes-act")
    assert len(kept) == 1 and kept[0]["heading"] == "Repealed elsewhere"


def test_apply_remap_on_a_first_parse_just_records_which_parse_it_is(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = _act()

    assert apply_remap("crimes-act", nodes, group_into_units(nodes)) is None
    assert load_parse_fingerprint("crimes-act") == parse_fingerprint(nodes)


# ---------------------------------------------------------------------------
# Carrying review work across an Act's own versions
#
# remap_verified re-anchors a row within *one* document's own reparse.
# carry_forward_review does the same matching between two different
# documents -- consecutive Authorised Versions -- so it needs the one
# thing a single document's reparse never has to tell apart: a Schedule's
# own numbering starting from 1 again, same as the body's.
# ---------------------------------------------------------------------------


def _act_with_schedule() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("section", "11", "Powers of entry", "An officer may enter premises."),
        make_node("schedule", "1", "Forms"),
        make_node("clause", "11", None, "Form of warrant."),
    ]


def test_node_identity_tells_a_schedule_clause_from_a_body_section_of_the_same_number():
    body = make_node("section", "11", "Powers of entry", "text")
    clause = make_node("clause", "11", None, "text")

    assert node_identity(body, schedule=None) != node_identity(clause, schedule="1")


def test_structural_identity_is_schedule_aware_the_same_way():
    body = make_node("section", "11", "Heading", "old text")
    clause = make_node("clause", "11", "Heading", "old text")

    assert structural_identity(body, schedule=None) != structural_identity(clause, schedule="1")


def test_carry_forward_matches_an_unchanged_provision_to_its_own_counterpart():
    old_nodes = _act_with_schedule()
    old_rows = [_reviewed(old_nodes[2], 2), _reviewed(old_nodes[4], 4)]
    new_nodes = _act_with_schedule()  # identical -- nothing changed at this version

    remapped, report = carry_forward_review(old_nodes, old_rows, new_nodes)

    assert report["matched"] == 2 and report["text_changed"] == 0
    assert all("verified_at" in r for r in remapped)


def test_carry_forward_does_not_confuse_a_schedule_clause_with_a_same_numbered_section():
    old_nodes = _act_with_schedule()
    # Only the Schedule 1 clause 11 has been reviewed -- not section 11.
    old_rows = [_reviewed(old_nodes[4], 4)]
    new_nodes = _act_with_schedule()

    remapped, report = carry_forward_review(old_nodes, old_rows, new_nodes)

    assert report["matched"] == 1
    matched_index = remapped[0]["_source_node_index"]
    assert new_nodes[matched_index]["type"] == "clause"  # the Schedule clause, not section 11


def test_carry_forward_withdraws_acceptance_where_the_wording_moved():
    old_nodes = _act_with_schedule()
    old_rows = [_reviewed(old_nodes[2], 2)]
    new_nodes = list(_act_with_schedule())
    new_nodes[2] = make_node("section", "11", "Powers of entry", "An officer may enter and search premises.")

    remapped, report = carry_forward_review(old_nodes, old_rows, new_nodes)

    assert report["text_changed"] == 1
    assert "verified_at" not in remapped[0]
    assert remapped[0]["needs_followup"] is True


def test_carry_forward_keeps_a_repealed_provisions_review_marked_orphaned():
    old_nodes = _act_with_schedule()
    old_rows = [_reviewed(old_nodes[2], 2)]
    new_nodes = [n for n in _act_with_schedule() if n.get("number") != "11" or n["type"] != "section"]

    remapped, report = carry_forward_review(old_nodes, old_rows, new_nodes)

    assert report["orphaned"] == 1
    assert remapped[0]["_orphaned"] is True


def _write_parse(path, nodes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")


def test_apply_carry_forward_seeds_a_new_versions_review_from_the_last_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old_nodes = _act()
    save_verified("crimes-act-v110", [_reviewed(old_nodes[1], 1)])
    _write_parse(tmp_path / "data" / "parsed" / "crimes-act-v110.json", old_nodes)
    new_nodes = _act()

    report = apply_carry_forward("crimes-act", "crimes-act-v111", new_nodes, group_into_units(new_nodes))

    assert report is not None and report["source"] == "crimes-act-v110"
    assert load_verified("crimes-act-v111")[0]["_source_node_index"] == 1
    assert load_parse_fingerprint("crimes-act-v111") == parse_fingerprint(new_nodes)


def test_apply_carry_forward_never_overwrites_a_versions_own_review(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old_nodes = _act()
    save_verified("crimes-act-v110", [_reviewed(old_nodes[1], 1)])
    _write_parse(tmp_path / "data" / "parsed" / "crimes-act-v110.json", old_nodes)
    new_nodes = _act()
    own_review = [_reviewed(new_nodes[3], 3)]
    save_verified("crimes-act-v111", own_review)

    assert apply_carry_forward("crimes-act", "crimes-act-v111", new_nodes) is None
    kept = load_verified("crimes-act-v111")
    assert [r["_source_node_index"] for r in kept] == [3]
    assert kept[0]["verified_at"] == "2024-01-01T00:00:00+00:00"


def test_apply_carry_forward_reads_from_the_nearest_reviewed_version(tmp_path, monkeypatch):
    # v110 has no review work; v109 does. The carry-forward must not stop
    # at the empty version in between and report nothing to carry.
    monkeypatch.chdir(tmp_path)
    nodes = _act()
    save_verified("crimes-act-v109", [_reviewed(nodes[1], 1)])
    _write_parse(tmp_path / "data" / "parsed" / "crimes-act-v109.json", nodes)
    _write_parse(tmp_path / "data" / "parsed" / "crimes-act-v110.json", nodes)

    report = apply_carry_forward("crimes-act", "crimes-act-v111", nodes)

    assert report is not None and report["source"] == "crimes-act-v109"


def test_apply_carry_forward_does_nothing_for_a_works_first_version(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = _act()

    assert apply_carry_forward("crimes-act", "crimes-act-v110", nodes) is None


# ---------------------------------------------------------------------
# Structural edits do not survive a re-parse
# ---------------------------------------------------------------------

def test_a_reparse_discards_structural_edits(tmp_path, monkeypatch):
    """"Delete node 87" is a position into the parse that has just been
    replaced. A verified row can be re-anchored because it carries the
    provision it describes; an edit carries nothing about the node it
    points at, so applying it to whatever now sits there would delete a
    provision nobody asked to lose."""
    monkeypatch.chdir(tmp_path)
    old_nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    save_parse_fingerprint("crimes-act", parse_fingerprint(old_nodes))
    save_structure_edits("crimes-act", {1: {"after": 0, "deleted": True, "node": None}})
    new_nodes = [make_node("section", "1", "Murder"), make_node("note", None, None, "a note"),
                 make_node("section", "2", "Manslaughter")]

    report = apply_remap("crimes-act", new_nodes, group_into_units(new_nodes))

    assert load_structure_edits("crimes-act") == {}
    assert report["structure_edits_dropped"] == 1
    assert "structural edit" in describe_remap(report)


def test_a_reparse_keeps_the_text_of_an_inserted_piece(tmp_path, monkeypatch):
    """The one thing a re-parse can't reproduce: a person typed it in
    precisely because the parse didn't have it."""
    monkeypatch.chdir(tmp_path)
    old_nodes = [make_node("section", "1", "Murder")]
    save_parse_fingerprint("crimes-act", parse_fingerprint(old_nodes))
    typed_in = make_node("subsection", "1", None, "a subsection the extractor dropped")
    save_structure_edits("crimes-act", {1: {"after": 0, "deleted": False, "node": typed_in}})
    new_nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]

    apply_remap("crimes-act", new_nodes, group_into_units(new_nodes))

    orphans = load_orphaned_reviews("crimes-act")
    assert [o["text"] for o in orphans] == ["a subsection the extractor dropped"]


def test_structural_edits_are_reported_even_with_no_review_rows_to_remap(tmp_path, monkeypatch):
    """A document can have been restructured without a single piece
    having been accepted yet -- apply_remap's "nothing to move" early
    exit still has to say what it threw away."""
    monkeypatch.chdir(tmp_path)
    old_nodes = [make_node("section", "1", "Murder")]
    save_parse_fingerprint("crimes-act", parse_fingerprint(old_nodes))
    save_structure_edits("crimes-act", {0: {"after": 5, "deleted": False, "node": None}})
    new_nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]

    report = apply_remap("crimes-act", new_nodes, group_into_units(new_nodes))

    assert report["structure_edits_dropped"] == 1
    assert load_structure_edits("crimes-act") == {}


def test_reparsing_the_very_same_parse_leaves_structural_edits_alone(tmp_path, monkeypatch):
    """Re-running the pipeline over an unchanged PDF is not a reason to
    undo a reviewer's restructuring -- the positions still mean what they
    meant."""
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    save_parse_fingerprint("crimes-act", parse_fingerprint(nodes))
    save_structure_edits("crimes-act", {1: {"after": 0, "deleted": True, "node": None}})

    assert apply_remap("crimes-act", nodes, group_into_units(nodes)) is None
    assert set(load_structure_edits("crimes-act")) == {1}
