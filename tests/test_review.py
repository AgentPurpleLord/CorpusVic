"""Tests for review.py's pure, non-interactive logic: unit grouping, label
computation, reflow/offset mapping, and verification stamping. The
FastAPI endpoints themselves (edit/split/merge/accept, all thin wrappers
around this same logic plus in-memory server state) are deliberately not
covered here -- they were exercised end to end against real parsed Act
data and a real browser session instead (see the module docstring)."""
import json
from pathlib import Path

import pytest

from ai_pipeline import db
from ai_pipeline.reparse import parse_fingerprint
from review import (
    _now_iso,
    _resume_point,
    build_current_nodes,
    can_renest_under,
    commit_unit,
    compute_unit_labels,
    compute_unit_tree_info,
    group_into_units,
    reflow_with_map,
    save_verified,
    validate_custom_type_name,
)

from conftest import make_node


def _write_parsed(act: str, nodes: list[dict], *, record_fingerprint: bool = True) -> None:
    """The pipeline's own output: a node list plus the fingerprint that
    identifies it. `record_fingerprint` also tells the database that this
    Act's review rows belong to this parse, which is what run_pipeline.py
    does after re-anchoring them -- pass False to reproduce review rows
    left pointing at a parse that has since been replaced."""
    path = Path("data/ai_parsed") / f"{act}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = parse_fingerprint(nodes)
    path.write_text(
        json.dumps({"nodes": nodes, "unattached_notes": [], "hierarchy": [], "fingerprint": fingerprint}),
        encoding="utf-8",
    )
    if record_fingerprint:
        db.save_parse_fingerprint(act, fingerprint)


def _write_verified(act: str, verified: list[dict]) -> None:
    save_verified(act, verified)


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


_DEFAULT_HIERARCHY = ["chapter", "part", "division", "subdivision", "section", "subsection", "paragraph", "subparagraph"]


def test_compute_unit_tree_info_nests_by_hierarchy_depth():
    # part(0) > division(1) > section(2), and a second section(3) as a
    # sibling of the first under that same division -- two sections in a
    # row are never one nested inside the other, only both children of
    # whatever division/part is currently open.
    root_types = ["part", "division", "section", "section"]
    info = compute_unit_tree_info(root_types, _DEFAULT_HIERARCHY)
    assert [i["depth"] for i in info] == [0, 1, 2, 2]
    assert [i["parent_unit_no"] for i in info] == [None, 0, 1, 1]


def test_compute_unit_tree_info_pops_back_out_to_a_shallower_sibling():
    # part(0) > division(1) > section(2), then a second part(0) as a sibling of the first
    root_types = ["part", "division", "section", "part"]
    info = compute_unit_tree_info(root_types, _DEFAULT_HIERARCHY)
    assert [i["depth"] for i in info] == [0, 1, 2, 0]
    assert info[3]["parent_unit_no"] is None


def test_compute_unit_tree_info_heading_group_nests_at_the_current_depth_without_opening_one():
    # part(0), heading_group sitting at the same depth as a section would, then a section as its sibling
    root_types = ["part", "heading_group", "section"]
    info = compute_unit_tree_info(root_types, _DEFAULT_HIERARCHY)
    assert info[1] == {"depth": 1, "parent_unit_no": 0}
    assert info[2] == {"depth": 1, "parent_unit_no": 0}  # the heading_group didn't push a new level


def test_compute_unit_tree_info_top_level_heading_group_has_no_parent():
    root_types = ["heading_group", "heading_group", "part"]
    info = compute_unit_tree_info(root_types, _DEFAULT_HIERARCHY)
    assert info[0] == {"depth": 0, "parent_unit_no": None}
    assert info[1] == {"depth": 0, "parent_unit_no": None}
    assert info[2] == {"depth": 0, "parent_unit_no": None}


def test_can_renest_under_allows_nesting_directly_under_the_dragged_onto_piece():
    # section(0), subsection(1) "(1)", paragraph(2) "(a)" -- renesting a
    # later stray piece(3) under the subsection is fine, nothing of
    # subsection-or-shallower rank sits between them.
    unit_types = ["section", "subsection", "paragraph", "subparagraph"]
    assert can_renest_under(unit_types, target_pos=1, node_pos=3, hierarchy_order=_DEFAULT_HIERARCHY) is True


def test_can_renest_under_rejects_when_a_same_rank_piece_intervenes():
    # subsection(1) "(1)" ... subsection(2) "(2)" ... piece(3) -- nesting
    # piece(3) under subsection(1) would be a lie once paths are
    # recomputed: subsection(2) is what actually precedes it.
    unit_types = ["section", "subsection", "subsection", "subparagraph"]
    assert can_renest_under(unit_types, target_pos=1, node_pos=3, hierarchy_order=_DEFAULT_HIERARCHY) is False


def test_can_renest_under_rejects_when_a_shallower_piece_intervenes():
    # A section boundary appearing between the target and the dragged
    # piece would mean they're not even in the same review unit any more
    # in spirit -- rejected the same way a same-rank one is.
    unit_types = ["section", "subsection", "section", "paragraph"]
    assert can_renest_under(unit_types, target_pos=1, node_pos=3, hierarchy_order=_DEFAULT_HIERARCHY) is False


def test_can_renest_under_allows_immediately_adjacent_pieces():
    unit_types = ["section", "subsection", "paragraph"]
    assert can_renest_under(unit_types, target_pos=1, node_pos=2, hierarchy_order=_DEFAULT_HIERARCHY) is True


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


def test_resume_point_with_complete_markers_never_counts_nodes_instead():
    """After re-anchoring, the markers are rebuilt from the new unit
    layout, so a row count that doesn't line up with whole units is
    normal -- and the count-based fallback would both invent a resume
    point and delete every row past it."""
    units = [[0], [1], [2, 3, 4], [5, 6]]
    verified = [{}, {}, {}]

    assert _resume_point(units, list(verified), markers_are_complete=True) == 0
    assert _resume_point(units, verified, markers_are_complete=True) == 0
    assert len(verified) == 3  # nothing trimmed


def test_now_iso_is_utc_and_sorts_chronologically():
    a = _now_iso()
    b = _now_iso()
    assert a.endswith("+00:00")
    assert a <= b  # lexicographic order matches chronological order


def test_reflow_with_map_collapses_a_single_wrap_to_one_space():
    reflowed, offsets = reflow_with_map("accused\nmeans a person")
    assert reflowed == "accused means a person"
    assert len(offsets) == len(reflowed) + 1


def test_reflow_with_map_strips_leading_and_trailing_whitespace():
    reflowed, offsets = reflow_with_map("\n  hello world  \n")
    assert reflowed == "hello world"
    assert len(offsets) == len(reflowed) + 1


def test_reflow_with_map_handles_empty_text():
    reflowed, offsets = reflow_with_map("")
    assert reflowed == ""
    assert offsets == [0]
    reflowed, offsets = reflow_with_map(None)
    assert reflowed == ""
    assert offsets == [0]


def test_reflow_with_map_offsets_round_trip_a_real_content_span():
    """A reflowed-text selection, sliced back out of the *raw* text using
    the offset map, must recover exactly the same characters -- this is
    the whole point of the map: a browser selection is always made
    against the displayed (reflowed) string, but a link/split action
    needs to index into the stored (raw) one."""
    raw = "Appeal Costs\nAct 1998 applies to this matter."
    reflowed, offsets = reflow_with_map(raw)
    assert reflowed == "Appeal Costs Act 1998 applies to this matter."
    start_r, end_r = reflowed.index("Appeal Costs Act 1998"), reflowed.index("Appeal Costs Act 1998") + len("Appeal Costs Act 1998")
    start_raw, end_raw = offsets[start_r], offsets[end_r]
    assert raw[start_raw:end_raw] == "Appeal Costs\nAct 1998"


def test_reflow_with_map_preserves_multiple_internal_spaces():
    """Only whitespace runs that contain a newline collapse to one space
    -- an ordinary multi-space run mid-line (not a PDF wrap point) is left
    exactly as it is."""
    reflowed, _ = reflow_with_map("some  text")
    assert reflowed == "some  text"


def test_build_current_nodes_falls_back_to_the_original_parse_when_nothing_is_verified_yet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed("crimes-act", nodes)

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n["heading"] for n in current] == ["Murder", "Manslaughter"]
    assert all("verified_at" not in n for n in current)


def test_build_current_nodes_uses_the_edited_version_of_a_committed_unit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed("crimes-act", nodes)
    edited = dict(nodes[0], heading="Murder (as edited)", verified_at="2024-01-01T00:00:00+00:00", _source_node_index=0, _unit_end_index=0)
    _write_verified("crimes-act", [edited])

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert current[0]["heading"] == "Murder (as edited)"
    assert current[1]["heading"] == "Manslaughter"  # not yet reached -- shown as originally parsed


def test_build_current_nodes_drops_a_node_that_was_merged_away(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    section = make_node("section", "1", "Murder")
    subsection = make_node("subsection", "1", None, "text merged into the section")
    _write_parsed("crimes-act", [section, subsection])
    # Simulate review.py's merge endpoint: only the target (index 0) made
    # it into `verified`, tagged as the end of unit 0 -- index 1 (the
    # subsection folded into it) never gets its own verified entry.
    merged_target = dict(section, text="Murder, including: text merged into the section", verified_at="2024-01-01T00:00:00+00:00", _source_node_index=0, _unit_end_index=0)
    _write_verified("crimes-act", [merged_target])

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert len(current) == 1
    assert current[0]["text"] == "Murder, including: text merged into the section"


def test_build_current_nodes_keeps_every_node_when_the_parse_has_moved(tmp_path, monkeypatch):
    # "Merged away" is inferred from a node having no verified row, which
    # only means anything while the rows and the parse agree on what an
    # index is. Against a parse they don't belong to, that inference
    # deleted real provisions from the browse view and both exports --
    # so it doesn't run at all, and nothing disappears.
    monkeypatch.chdir(tmp_path)
    section = make_node("section", "1", "Murder")
    subsection = make_node("subsection", "1", None, "text merged into the section")
    _write_parsed("crimes-act", [section, subsection], record_fingerprint=False)
    merged_target = dict(section, verified_at="2024-01-01T00:00:00+00:00", _source_node_index=0, _unit_end_index=0)
    _write_verified("crimes-act", [merged_target])

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n["type"] for n in current] == ["section", "subsection"]


# ---------------------------------------------------------------------
# Custom node-type names
# ---------------------------------------------------------------------

def test_validate_custom_type_name_normalises_case_spaces_and_hyphens():
    assert validate_custom_type_name("Penalty Note", []) == "penalty_note"
    assert validate_custom_type_name("  transitional-provision ", []) == "transitional_provision"


def test_validate_custom_type_name_rejects_an_empty_name():
    with pytest.raises(ValueError, match="required"):
        validate_custom_type_name("   ", [])


def test_validate_custom_type_name_rejects_a_name_that_cannot_be_a_node_type():
    # Has to survive being written into a node's "type" field and ranked
    # by hierarchy.py, so it takes the same shape the built-in types have.
    for bad in ("9lives", "penalty!", "_leading", "x" * 41):
        with pytest.raises(ValueError, match="must start with a letter"):
            validate_custom_type_name(bad, [])


def test_validate_custom_type_name_rejects_a_duplicate_after_normalising():
    with pytest.raises(ValueError, match="already exists"):
        validate_custom_type_name("Penalty Note", ["section", "penalty_note"])
