"""Tests for review.py's pure, non-interactive logic: unit grouping, label
computation, reflow/offset mapping, and verification stamping. The
FastAPI endpoints themselves (edit/split/merge/accept, all thin wrappers
around this same logic plus in-memory server state) are deliberately not
covered here -- they were exercised end to end against real parsed Act
data and a real browser session instead (see the module docstring)."""
import json
from pathlib import Path

import pytest

from corpus.storage import db
from corpus.parsing.reparse import parse_fingerprint
from corpus.review.review import (
    _is_elevated_risk,
    _now_iso,
    _resume_point,
    _was_inserted,
    build_current_nodes,
    build_effective_nodes_indexed,
    can_renest_under,
    commit_unit,
    compute_unit_labels,
    compute_unit_tree_info,
    finished_units,
    group_into_units,
    order_and_units,
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
    path = Path("data/parsed") / f"{act}.json"
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


def test_reviewing_one_section_in_the_middle_keeps_the_rest_of_the_act(tmp_path, monkeypatch):
    """The bug this replaces, at the size it actually happened.

    One section of the Crimes Act was reviewed -- s464C, which sits at
    unit 547 of 906. Because the finished units were taken to be
    everything below the highest marker, 4,767 provisions were inferred
    to have been merged away and vanished from the browse view, the
    search index, both exports and the public site. The Act's index began
    at s464C; Murder was gone.

    A reviewer opening one section in the middle is the ordinary case,
    not an odd one, and it must cost nothing."""
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", str(i), f"Section {i}") for i in range(1, 11)]
    _write_parsed("crimes-act", nodes)
    # Unit 7 committed, nothing before it: exactly one finished unit.
    _write_verified("crimes-act", [
        dict(nodes[6], verified_at="2024-01-01T00:00:00+00:00",
             _source_node_index=6, _unit_end_index=6),
    ])

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert len(current) == 10, "no provision may disappear because a later one was reviewed"
    assert [n["heading"] for n in current] == [f"Section {i}" for i in range(1, 11)]


def test_a_merge_inside_a_committed_unit_is_still_honoured(tmp_path, monkeypatch):
    """The inference is narrowed, not removed. Within a unit the reviewer
    did commit, a node with no verified row was still folded into its
    neighbour and must still go."""
    monkeypatch.chdir(tmp_path)
    nodes = [
        make_node("section", "1", "Kept"),
        make_node("section", "2", "Target"),
        make_node("subsection", "1", None, "folded into the target"),
    ]
    _write_parsed("crimes-act", nodes)
    # Unit 1 committed with only its target: index 2 was merged into it.
    _write_verified("crimes-act", [
        dict(nodes[1], verified_at="2024-01-01T00:00:00+00:00",
             _source_node_index=1, _unit_end_index=1),
    ])

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n["heading"] for n in current] == ["Kept", "Target"]
    assert all("folded into the target" not in (n.get("text") or "") for n in current)


def test_an_unreviewed_unit_before_a_reviewed_one_is_left_alone(tmp_path, monkeypatch):
    """The distinction the whole change rests on: unit 0 has no marker,
    so nothing is known about it and nothing may be inferred -- even
    though unit 2 above it is finished."""
    monkeypatch.chdir(tmp_path)
    nodes = [
        make_node("section", "1", "Untouched"),
        make_node("subsection", "1", None, "still here"),
        make_node("section", "2", "Reviewed"),
    ]
    _write_parsed("crimes-act", nodes)
    _write_verified("crimes-act", [
        dict(nodes[2], verified_at="2024-01-01T00:00:00+00:00",
             _source_node_index=2, _unit_end_index=1),
    ])

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n.get("heading") for n in current] == ["Untouched", None, "Reviewed"]


def test_finished_units_is_the_set_of_markers_not_a_range():
    """_resume_point answers "how far did I get"; this answers "what did
    I finish". They were the same function, and that is what deleted the
    Act."""
    units = [[0], [1], [2], [3], [4]]
    verified = [{"_source_node_index": 3, "_unit_end_index": 3}]

    assert set(finished_units(units, verified, markers_are_complete=True)) == {3}
    # The other question, unchanged: resume *after* the furthest marker.
    assert _resume_point(units, verified, markers_are_complete=True) == 4


def test_rows_from_before_markers_existed_keep_their_contiguous_reading():
    """Marker-free rows predate the marker and were always committed
    whole and in order, so there the contiguous range really is the set
    of finished units. Narrowing it would resurrect nodes those reviewers
    genuinely merged away."""
    units = [[0], [1], [2], [3]]
    verified = [{"_source_node_index": 0}, {"_source_node_index": 1}]

    assert list(finished_units(units, verified)) == [0, 1]


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


def test_build_effective_nodes_indexed_keeps_original_positions_for_an_unverified_parse(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed("crimes-act", nodes)

    effective, units, fingerprint = build_effective_nodes_indexed("crimes-act")

    assert [n["heading"] for n in effective] == ["Murder", "Manslaughter"]
    assert units == group_into_units(nodes)
    assert fingerprint == parse_fingerprint(nodes)


def test_build_effective_nodes_indexed_holds_none_at_a_merged_away_position(tmp_path, monkeypatch):
    # Unlike build_current_nodes, which drops a merged-away node and
    # reindexes everything after it, this keeps every position stable --
    # run_ai_review.py needs node_index values that still match
    # diagnostics/ai_scan_findings' own keying (see the function's own
    # docstring).
    monkeypatch.chdir(tmp_path)
    section = make_node("section", "1", "Murder")
    subsection = make_node("subsection", "1", None, "text merged into the section")
    trailing = make_node("section", "2", "Manslaughter")
    _write_parsed("crimes-act", [section, subsection, trailing])
    merged_target = dict(section, text="Murder, including: text merged into the section", verified_at="2024-01-01T00:00:00+00:00", _source_node_index=0, _unit_end_index=0)
    _write_verified("crimes-act", [merged_target])

    effective, _units, _fingerprint = build_effective_nodes_indexed("crimes-act")

    assert effective[0]["text"] == "Murder, including: text merged into the section"
    assert effective[1] is None
    assert effective[2]["heading"] == "Manslaughter"  # untouched, and at its original position


def test_build_effective_nodes_indexed_keeps_every_node_when_the_parse_has_moved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    section = make_node("section", "1", "Murder")
    subsection = make_node("subsection", "1", None, "text merged into the section")
    _write_parsed("crimes-act", [section, subsection], record_fingerprint=False)
    merged_target = dict(section, verified_at="2024-01-01T00:00:00+00:00", _source_node_index=0, _unit_end_index=0)
    _write_verified("crimes-act", [merged_target])

    effective, _units, _fingerprint = build_effective_nodes_indexed("crimes-act")

    assert [n is not None for n in effective] == [True, True]
    assert [n["type"] for n in effective] == ["section", "subsection"]


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


# ---------------------------------------------------------------------
# The blind-review gate
#
# An elevated-risk piece can't be accepted until the reviewer records
# their own independent read of it. That is worth real friction where the
# parser is genuinely unsure -- and worthless everywhere else, since a
# reviewer made to justify every ordinary piece stops reading and starts
# clicking, which is the exact failure it exists to prevent.
# ---------------------------------------------------------------------

def _findings(monkeypatch, *findings):
    """Stands in for what load_diagnostics put on node 7 at startup."""
    from corpus.review import review
    monkeypatch.setattr(review, "_findings_by_node", {7: list(findings)} if findings else {})


def test_a_warning_gates_a_piece_behind_its_own_assessment(monkeypatch):
    _findings(monkeypatch, {"severity": "warning", "message": "duplicate numbering"})

    assert _is_elevated_risk(7) is True


def test_an_error_gates_a_piece_too(monkeypatch):
    _findings(monkeypatch, {"severity": "error", "message": "lines unaccounted for"})

    assert _is_elevated_risk(7) is True


def test_an_info_finding_does_not_gate_a_piece(monkeypatch):
    """Every info-level finding across this repo's own Acts is the same
    one -- "section 45 has no body text", the ordinary shape of a Section
    whose content sits in its subsections. Gating on those put 1360 of
    3060 units behind a written assessment where only 74 carry a real
    warning."""
    _findings(monkeypatch, {"severity": "info", "message": "has no body text"})

    assert _is_elevated_risk(7) is False


def test_a_warning_alongside_an_info_finding_still_gates(monkeypatch):
    _findings(
        monkeypatch,
        {"severity": "info", "message": "has no body text"},
        {"severity": "warning", "message": "duplicate numbering"},
    )

    assert _is_elevated_risk(7) is True


def test_a_piece_with_no_findings_is_not_gated(monkeypatch):
    _findings(monkeypatch)

    assert _is_elevated_risk(7) is False


# ---------------------------------------------------------------------
# _ai_suggestion_precondition -- an AI suggestion (corpus/
# ai_assist.py) only ever becomes available once a reviewer's own
# independent blind-review guess is already recorded, so it can never
# anchor the judgement that step exists to protect. See that module's
# own docstring for why.
# ---------------------------------------------------------------------

def _setup_ai_precondition(monkeypatch, *, findings, node_count=1):
    from corpus.review import review
    monkeypatch.setattr(review, "_nodes", [make_node("section", "1") for _ in range(node_count)])
    monkeypatch.setattr(review, "_merged_away", set())
    monkeypatch.setattr(review, "_act", "test-act")
    monkeypatch.setattr(review, "_findings_by_node", {0: list(findings)} if findings else {})
    return review


def test_ai_precondition_404s_for_an_out_of_range_node(monkeypatch, isolate_corrections):
    from fastapi import HTTPException
    review = _setup_ai_precondition(monkeypatch, findings=[{"severity": "warning", "message": "m"}])

    with pytest.raises(HTTPException) as exc:
        review._ai_suggestion_precondition(99)
    assert exc.value.status_code == 404


def test_ai_precondition_refuses_a_piece_with_no_elevated_risk_finding(monkeypatch, isolate_corrections):
    from fastapi import HTTPException
    review = _setup_ai_precondition(monkeypatch, findings=[])

    with pytest.raises(HTTPException) as exc:
        review._ai_suggestion_precondition(0)
    assert exc.value.status_code == 400
    assert "isn't flagged" in exc.value.detail


def test_ai_precondition_refuses_before_a_blind_review_is_recorded(monkeypatch, isolate_corrections):
    from fastapi import HTTPException
    review = _setup_ai_precondition(monkeypatch, findings=[{"severity": "warning", "message": "duplicate numbering"}])

    with pytest.raises(HTTPException) as exc:
        review._ai_suggestion_precondition(0)
    assert exc.value.status_code == 400
    assert "independent assessment" in exc.value.detail


def test_ai_precondition_returns_the_finding_once_a_blind_review_exists(monkeypatch, isolate_corrections):
    review = _setup_ai_precondition(monkeypatch, findings=[{"severity": "warning", "message": "duplicate numbering"}])
    db.save_blind_review(
        "test-act", 0, guessed_type="section", guessed_number="1", guessed_heading=None,
        reasoning="looks like a section", matched_type=True, matched_number=True,
    )

    finding = review._ai_suggestion_precondition(0)
    assert finding == {"severity": "warning", "message": "duplicate numbering"}


def test_a_piece_reports_which_fields_no_longer_match_the_parse(monkeypatch):
    """What is stored and what the parser says now can differ for two very
    different reasons -- a human corrected it, or it was decided before a
    parser fix and is a stale snapshot of one. Only a reviewer can tell
    which, so the piece says what differs rather than choosing."""
    from corpus.review import review

    parsed = {"type": "note", "number": None, "heading": None,
              "text": "A proceeding may also be commenced under section 83AL."}
    monkeypatch.setattr(review, "_nodes", [parsed])

    assert review._differs_from_parse(0, parsed) == []
    stale = dict(parsed, text=parsed["text"] + " Part 2.2—Charge-sheet")
    assert review._differs_from_parse(0, stale) == ["text"]
    relabelled = dict(parsed, type="subsection", number="3")
    assert review._differs_from_parse(0, relabelled) == ["type", "number"]
    # An absent field and an empty one are the same thing here, so a
    # heading that was never set doesn't read as a change.
    assert review._differs_from_parse(0, {**parsed, "heading": ""}) == []


# ---------------------------------------------------------------------------
# Structural edits (corpus/structure.py) applied on the read path
# ---------------------------------------------------------------------------

def _write_structure(act: str, edits: dict) -> None:
    db.save_structure_edits(act, edits)


def test_order_and_units_answers_in_node_indices_not_list_positions():
    """group_into_units works in positions into the list it is handed,
    which stop being indices the moment anything is inserted or moved.
    The whole job here is translating them back."""
    parse = [make_node("section", "1"), make_node("subsection", "1"), make_node("section", "2")]
    inserted = make_node("subsection", "2", text="a subsection the parser missed")
    edits = {3: {"after": 1, "deleted": False, "node": inserted}}
    at = lambda i: parse[i] if i < len(parse) else edits[i]["node"]

    order, units = order_and_units(len(parse), edits, at)

    assert order == [0, 1, 3, 2]
    assert units == [[0, 1, 3], [2]]


def test_a_deleted_node_disappears_from_the_browse_view(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Running header")]
    _write_parsed("crimes-act", nodes)
    _write_structure("crimes-act", {1: {"after": 0, "deleted": True, "node": None}})

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n["heading"] for n in current] == ["Murder"]


def test_an_inserted_node_shows_up_where_it_was_put(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed("crimes-act", nodes)
    typed_in = make_node("section", "1A", "Attempted murder")
    _write_structure("crimes-act", {2: {"after": 0, "deleted": False, "node": typed_in}})

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n["heading"] for n in current] == ["Murder", "Attempted murder", "Manslaughter"]


def test_a_moved_node_reads_in_its_new_place(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [
        make_node("section", "1", "Murder"),
        make_node("note", None, None, "a note that belongs under s 2"),
        make_node("section", "2", "Manslaughter"),
    ]
    _write_parsed("crimes-act", nodes)
    _write_structure("crimes-act", {1: {"after": 2, "deleted": False, "node": None}})

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n.get("heading") or n["text"] for n in current] == [
        "Murder", "Manslaughter", "a note that belongs under s 2",
    ]


def test_structural_edits_are_ignored_when_the_parse_has_moved_under_them(tmp_path, monkeypatch):
    """The same guard verified rows get. "Delete node 1" against a parse
    node 1 no longer means is not a deletion a reviewer ever asked for."""
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed("crimes-act", nodes, record_fingerprint=False)
    _write_structure("crimes-act", {1: {"after": 0, "deleted": True, "node": None}})

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n["heading"] for n in current] == ["Murder", "Manslaughter"]


def test_an_inserted_node_is_not_inferred_to_have_been_merged_away(tmp_path, monkeypatch):
    """A piece typed into an already-reviewed Section has no verified row
    because nobody has reviewed it yet -- which is exactly the shape the
    merged-away inference looks for. Mistaking one for the other would
    make it vanish the next time the server started."""
    monkeypatch.chdir(tmp_path)
    section = make_node("section", "1", "Murder")
    _write_parsed("crimes-act", [section])
    _write_verified("crimes-act", [
        dict(section, verified_at="2024-01-01T00:00:00+00:00", _source_node_index=0, _unit_end_index=0),
    ])
    typed_in = make_node("subsection", "1", None, "a subsection the extractor dropped")
    _write_structure("crimes-act", {1: {"after": 0, "deleted": False, "node": typed_in}})

    current, _notes, _hierarchy = build_current_nodes("crimes-act")

    assert [n.get("text") for n in current] == ["", "a subsection the extractor dropped"]


def test_was_inserted_tells_a_typed_in_node_from_a_parsed_one():
    edits = {5: {"node": {"type": "note"}}, 2: {"deleted": True, "node": None}}
    assert _was_inserted(edits, 5)
    assert not _was_inserted(edits, 2)
    assert not _was_inserted(edits, 0)


def test_effective_nodes_are_long_enough_to_index_an_inserted_node(tmp_path, monkeypatch):
    """run_ai_review.py keys its findings by node_index straight into
    this list, so an inserted node has to have a slot at its own index --
    which is above the parse, by construction."""
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder")]
    _write_parsed("crimes-act", nodes)
    _write_structure("crimes-act", {
        1: {"after": 0, "deleted": False, "node": make_node("subsection", "1", None, "added")},
    })

    effective, units, _fingerprint = build_effective_nodes_indexed("crimes-act")

    assert len(effective) == 2
    assert effective[1]["text"] == "added"
    assert units == [[0, 1]]


def test_a_deleted_position_holds_none_in_the_indexed_view(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("note", None, None, "a page footer")]
    _write_parsed("crimes-act", nodes)
    _write_structure("crimes-act", {1: {"after": 0, "deleted": True, "node": None}})

    effective, _units, _fingerprint = build_effective_nodes_indexed("crimes-act")

    assert effective[0]["heading"] == "Murder"
    assert effective[1] is None


# ---------------------------------------------------------------------
# Continuations are named after what they continue
# ---------------------------------------------------------------------

def test_a_continuation_is_labelled_with_the_provision_it_continues():
    """It is not a provision of its own: it is the rest of subsection
    (1)'s sentence, resumed after (a) and (b) (Criminal Procedure Act
    s 11(1) is the shape). Labelled "[continuation 1]" it read as a
    separate thing that happened to land there."""
    unit = [
        make_node("section", "11", "Place of hearing", ""),
        dict(make_node("subsection", "1", None, "nearest to-"), path={"section": "11", "subsection": "1"}),
        dict(make_node("paragraph", "a", None, "where the offence was committed; or"),
             path={"section": "11", "subsection": "1", "paragraph": "a"}),
        dict(make_node("continuation", None, None, "except where otherwise provided."),
             path={"section": "11", "subsection": "1"}, depth_rank=7),
    ]
    assert compute_unit_labels(unit) == ["SECTION", "(1)", "(1)(a)", "(1) continuation"]


def test_a_continuation_inside_a_defined_term_is_named_after_the_term():
    unit = [
        make_node("section", "3", "Definitions", "In this Act-"),
        dict(make_node("definition", None, "sentence", "includes-"), path={"definition": "sentence"}),
        dict(make_node("paragraph", "a", None, "the recording of a conviction; and"),
             path={"definition": "sentence", "paragraph": "a"}),
        dict(make_node("continuation", None, None, "but does not include a fine."),
             path={"definition": "sentence"}, depth_rank=7),
    ]
    assert compute_unit_labels(unit)[-1] == "sentence continuation"


def test_a_continuation_that_closes_a_whole_section_says_so():
    """Some sections break into a list directly, with no subsection to
    resume -- there is no chain to name it by, and the thing it continues
    is the section itself."""
    unit = [
        make_node("section", "31", "Transfer", "If the Court considers that-"),
        dict(make_node("paragraph", "a", None, "a fair hearing cannot be had; or"),
             path={"section": "31", "paragraph": "a"}),
        dict(make_node("continuation", None, None, "the Court may transfer the proceeding."),
             path={"section": "31"}),
    ]
    assert compute_unit_labels(unit)[-1] == "SECTION continuation"


def test_annotate_paths_gives_a_continuation_the_path_of_what_it_resumes():
    """Its own type's rank is only a default. Read instead of the
    depth_rank the parser recorded, it cleared levels the continuation
    sits inside -- a wrap-up under s 11(1)(b)'s list lost the (1)."""
    from corpus.parsing.tree import annotate_paths

    nodes = annotate_paths([
        make_node("section", "11", "Place of hearing", ""),
        make_node("subsection", "1", None, "nearest to-"),
        make_node("paragraph", "a", None, "one place; or"),
        dict(make_node("continuation", None, None, "except where provided."), depth_rank=7),
    ])

    assert nodes[-1]["path"]["subsection"] == "1"
    assert nodes[-1]["path"]["paragraph"] is None


def test_a_section_level_continuation_clears_the_subsection_it_is_not_in():
    from corpus.parsing.tree import annotate_paths

    nodes = annotate_paths([
        make_node("section", "31", "Transfer", "If the Court considers-"),
        make_node("paragraph", "a", None, "a fair hearing cannot be had; or"),
        make_node("continuation", None, None, "the Court may transfer."),
    ])

    assert nodes[-1]["path"]["subsection"] is None
    assert nodes[-1]["path"]["section"] == "31"


def test_a_reprints_profile_resolves_to_its_acts_own_profile():
    """Reading a box needs the patterns the parse used, and a versioned
    document records a versioned profile name: the Criminal Procedure
    Act's parse says "criminal-procedure-act-v114" while the profile file
    is "criminal-procedure-act.yaml", one profile serving every reprint.
    Handing the recorded name straight to load_profile raised on the
    largest document in the corpus, so every "read this piece from its
    box" on it failed."""
    from corpus.review.review import load_parse_profile

    assert load_parse_profile("criminal-procedure-act-v114") == "criminal-procedure-act"


def test_a_document_with_no_profile_at_all_is_not_invented_one():
    from corpus.review.review import load_parse_profile

    assert load_parse_profile("no-such-act-anywhere") is None
