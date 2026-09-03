"""Tests for ai_pipeline/reparse.py -- keeping a human's review work
attached to its provisions when an Act is parsed again.

Review rows are keyed by a *position* into data/ai_parsed/<act>.json, so
a parser change that adds or re-splits one node shifts every row after
it onto the wrong provision. These cover the two halves of the fix: a
fingerprint that makes such a shift detectable, and a re-anchoring that
moves each row onto the node holding the provision it describes."""
from ai_pipeline.db import load_orphaned_reviews, load_parse_fingerprint, load_verified, save_verified
from ai_pipeline.hierarchy import group_into_units
from ai_pipeline.reparse import (
    apply_remap,
    describe_remap,
    node_identity,
    parse_fingerprint,
    remap_verified,
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
