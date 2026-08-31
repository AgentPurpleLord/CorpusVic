"""Tests for review.py's pure, non-interactive logic: unit grouping, label
computation, and verification stamping. The interactive prompt-driven
flows (edit_piece, split_piece, merge_piece, _prompt_piece, _prompt_target,
run_section_review) are deliberately not covered here -- they're thin
wrappers around these functions plus Prompt.ask calls, and were verified
this project by scripted stdin sessions during development rather than
mocked-Prompt unit tests."""
from review import (
    _now_iso,
    _resume_point,
    commit_unit,
    compute_unit_labels,
    group_into_units,
)

from conftest import make_node


def test_group_into_units_covers_every_node_exactly_once():
    nodes = [
        make_node("heading_group", heading="Authorised Version"),
        make_node("part", "I", "Offences"),
        make_node("division", "1", "Offences against the person"),
        make_node("section", "1", "Murder"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
        make_node("note", "1", None, "a footnote"),
        make_node("section", "2", "Manslaughter"),
        make_node("subsection", "1", None, "text"),
    ]
    units = group_into_units(nodes)
    covered = sorted(i for u in units for i in u)
    assert covered == list(range(len(nodes)))
    # Each standalone structural node is its own unit; each Section absorbs
    # everything under it up to the next boundary.
    assert units[0] == [0]  # heading_group
    assert units[1] == [1]  # part
    assert units[2] == [2]  # division
    assert units[3] == [3, 4, 5, 6]  # section 1 + its subsection/paragraph/note
    assert units[4] == [7, 8]  # section 2 + its subsection


def test_compute_unit_labels_are_unique_even_with_repeated_note_markers():
    """Regression: several repealed-text "* * * *" Note markers in a row
    all inherit the same path context as whatever numbered piece preceded
    them, which used to produce identical labels for all of them *and*
    for the numbered piece itself -- making that piece impossible to
    select for editing ("Ambiguous -- matches: (1), (1), (1), ...")."""
    section = make_node("section", "2A", "Definitions")
    subsection = make_node("subsection", "1", None, "In this Act—")
    subsection["path"] = {"subsection": "1", "paragraph": None, "subparagraph": None}
    notes = []
    for _ in range(3):
        note = make_node("note", None, None, "* * * * *")
        note["path"] = {"subsection": "1", "paragraph": None, "subparagraph": None}
        notes.append(note)
    unit_nodes = [section, subsection, *notes]

    labels = compute_unit_labels(unit_nodes)
    assert len(labels) == len(set(labels)), f"labels collided: {labels}"
    assert labels[0] == "SECTION"
    assert labels[1] == "(1)"  # the real Subsection keeps its own citation label
    assert labels[2:] == ["[note 1]", "[note 2]", "[note 3]"]


def test_commit_unit_stamps_verified_at_unless_flagged(isolate_corrections):
    section = make_node("section", "1", "Murder")
    subsection = make_node("subsection", "1", None, "text")
    verified: list[dict] = []

    commit_unit([dict(section), dict(subsection)], [section, subsection], "test-act", verified)
    assert all("verified_at" in n for n in verified)
    assert all(n.get("needs_followup") is None for n in verified)

    verified.clear()
    commit_unit([dict(section), dict(subsection)], [section, subsection], "test-act", verified, flagged=True)
    assert all("verified_at" not in n for n in verified)
    assert all(n["needs_followup"] is True for n in verified)


def test_commit_unit_gives_each_node_its_own_timestamp(isolate_corrections):
    nodes = [make_node("section", "1"), make_node("subsection", "1"), make_node("subsection", "2")]
    verified: list[dict] = []
    commit_unit([dict(n) for n in nodes], nodes, "test-act", verified)
    timestamps = [n["verified_at"] for n in verified]
    assert all(timestamps)
    assert timestamps == sorted(timestamps)  # stamped in commit order


def test_commit_unit_tags_the_last_node_with_its_unit_index(isolate_corrections):
    nodes = [make_node("section", "1"), make_node("subsection", "1")]
    verified: list[dict] = []
    commit_unit([dict(n) for n in nodes], nodes, "test-act", verified, unit_index=3)
    assert "_unit_end_index" not in verified[0]
    assert verified[1]["_unit_end_index"] == 3


def test_resume_point_trusts_a_unit_end_marker_over_raw_length():
    """Regression: merge_piece can make commit_unit append *fewer* nodes
    than a unit's original size (the merged-away piece is discarded, never
    reaching `verified`) -- once that happens, the unit's real size no
    longer matches units[u]'s original length, so the raw cumulative-
    length arithmetic below can't tell "this unit committed short because
    of a merge" apart from "review quit partway through this unit", and
    would wrongly trim back a fully-completed unit and force redoing it.
    commit_unit's `_unit_end_index` tag sidesteps that entirely -- when
    present, it's trusted directly instead of counting nodes."""
    units = [[0, 1, 2], [3, 4], [5, 6, 7]]
    # Unit 0 (originally 3 nodes) committed only 2 -- one piece was merged
    # away -- so raw cumulative length no longer lines up with units[0]'s
    # size of 3, exactly the shape a naive length check would misread.
    verified = [{}, {"_unit_end_index": 0}, {}, {"_unit_end_index": 1}]
    assert _resume_point(units, verified) == 2
    assert len(verified) == 4  # untouched -- no legacy trim-back triggered


def test_resume_point_on_unit_boundary():
    units = [[0], [1], [2, 3, 4], [5, 6]]
    verified = [{} for _ in range(2)]  # exactly covers the first two units
    assert _resume_point(units, verified) == 2
    assert len(verified) == 2  # untouched, already aligned


def test_resume_point_trims_back_from_a_mid_unit_position():
    """A prior --flat run can leave `verified` stopped partway through
    what would be one grouped-mode unit; resuming in grouped mode must
    not re-emit those nodes as part of a *new* unit acceptance (which
    would duplicate them) -- it trims back to the last full unit instead."""
    units = [[0], [1], [2, 3, 4], [5, 6]]
    verified = [{}, {}, {}]  # unit 2 (indices 2,3,4) only partially done
    resume_at = _resume_point(units, verified)
    assert resume_at == 2
    assert len(verified) == 2  # trimmed back to the end of unit 1


def test_now_iso_is_utc_and_sorts_chronologically():
    a = _now_iso()
    b = _now_iso()
    assert a.endswith("+00:00")
    assert a <= b  # lexicographic order matches chronological order
