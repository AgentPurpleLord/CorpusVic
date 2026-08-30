"""Tests for ai_pipeline/history_notes.py's margin-note parsing."""
from ai_pipeline.history_notes import merge_continuations, parse_note


def test_parse_note_simple_section():
    result = parse_note("S. 3 amended by No. 52/2014 s. 11.")
    assert result["section"] == "3"
    assert result["sub_path"] == []


def test_parse_note_section_with_subpath():
    result = parse_note("S. 3(2)(a) amended by No. 52/2014 s. 11.")
    assert result["section"] == "3"
    assert result["sub_path"] == ["(2)", "(a)"]


def test_parse_note_notes_to_section():
    result = parse_note("Notes to s. 6 inserted by No. 20/2015 s. 22.")
    assert result["section"] == "6"


def test_parse_note_heading_preceding():
    result = parse_note("Heading preceding s. 6A inserted by No. 8870 s. 6(1).")
    assert result["section"] == "6A"


def test_parse_note_part_division_subdivision():
    result = parse_note("Pt 3 Div. 1 Subdiv. (18) (Heading) repealed by No. 43/2012 s. 3(Sch. item 11.1).")
    assert result["part"] == "3"
    assert result["division"] == "1"
    assert result["sub_path"] == ["(18)"]


def test_parse_note_definition_name():
    result = parse_note("S. 2A(1) def. of medicinal cannabis product inserted by No. 20/2016 s. 143.")
    assert result["def_name"] == "medicinal cannabis product"


def test_parse_note_new_prefix_stripped_before_matching():
    result = parse_note("New s. 6B inserted by No. 8870 s. 6(1).")
    assert result["section"] == "6B"


def test_parse_note_unparseable_kept_as_raw_not_dropped():
    """Coverage is ~98%, not 100% -- an unparseable note must still come
    back with its raw text intact for manual linking, never silently
    dropped."""
    result = parse_note("Some unusual phrasing that doesn't match any citation shape.")
    assert result["raw"] == "Some unusual phrasing that doesn't match any citation shape."
    assert result["section"] is None


def test_merge_continuations_joins_wrapped_note_fragments():
    notes = [
        "S. 3 amended by Nos 6731 s. 2(1), 6958",
        "9576 s. 11(1), 10026 s. 2(a)(b).",
        "S. 4 substituted by No. 60/2000 s. 5.",
    ]
    merged = merge_continuations(notes)
    assert len(merged) == 2
    assert merged[0] == "S. 3 amended by Nos 6731 s. 2(1), 6958 9576 s. 11(1), 10026 s. 2(a)(b)."
    assert merged[1] == "S. 4 substituted by No. 60/2000 s. 5."


def test_merge_continuations_drops_blank_entries():
    assert merge_continuations(["S. 1 amended by No. 1/2000 s. 1.", "", "   "]) == ["S. 1 amended by No. 1/2000 s. 1."]
