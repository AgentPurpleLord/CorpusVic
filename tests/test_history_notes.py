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


# ---------------------------------------------------------------------
# Schedules, Chapters, and notes that aren't amendments at all
# ---------------------------------------------------------------------

def test_parse_note_schedule():
    result = parse_note("Sch. 2 repealed by No. 6958 s. 8(4)(d).")
    assert result["schedule"] == "2"
    assert result["section"] is None


def test_parse_note_schedule_clause():
    # A Schedule numbers its own clauses from 1 again, so the clause only
    # means anything alongside the Schedule it belongs to.
    result = parse_note("Sch. 1 cl. 4A(1) amended by No. 47/2016 s. 37(11).")
    assert result["schedule"] == "1"
    assert result["section"] == "4A"
    assert result["sub_path"] == ["(1)"]


def test_parse_note_schedule_range_takes_its_first_endpoint():
    result = parse_note("Schs 8A–11 repealed.")
    assert result["schedule"] == "8A"


def test_parse_note_chapter_prefixed_part_and_division():
    # An Act that groups its Parts under Chapters cites both.
    result = parse_note("Ch. 8 Pt 8.2 Div. 5 (Heading) amended by No. 19/2017 s. 56.")
    assert result["chapter"] == "8"
    assert result["part"] == "8.2"
    assert result["division"] == "5"


def test_parse_note_chapter_alone():
    result = parse_note("Ch. 10 (Heading and s. 439) inserted by No. 68/2009 s. 55.")
    assert result["chapter"] == "10"
    assert result["part"] is None


def test_parse_note_example_to_a_section():
    result = parse_note("Examples to s. 59 amended by No. 68/2009 s. 97(Sch. item 55.10).")
    assert result["section"] == "59"


def test_a_provenance_note_is_marked_as_one():
    """"No. 6103 s. 15." says this provision reproduces section 15 of the
    previous consolidation; "cf. [1819] 60 George III" points at the
    English statute it derives from. Neither names a provision of *this*
    Act, so neither can attach to one -- and reporting them as notes that
    "could not be linked" made 62 correctly-handled notes look like
    parser failures."""
    assert parse_note("No. 6103 s. 15.")["kind"] == "provenance"
    assert parse_note("cf. [1819] 60 George III, c. VIII ss 1, 2.")["kind"] == "provenance"
    assert parse_note("See: Act No. 35/1999. Reprint No. 1 as at 1 July 2008.")["kind"] == "provenance"


def test_an_amendment_note_is_not_marked_as_provenance():
    assert parse_note("S. 3 amended by No. 52/2014 s. 11.")["kind"] == "amendment"
    assert parse_note("Sch. 2 repealed by No. 6958 s. 8(4)(d).")["kind"] == "amendment"


def test_a_provenance_note_starts_its_own_note():
    # It used to be glued onto whichever amendment note preceded it,
    # taking that note's citation with it.
    merged = merge_continuations([
        "S. 405 amended by No. 9576 s. 11(1).",
        "No. 6103 s. 405.",
        "S. 405(1) amended by No. 25/2023 s. 7.",
    ])
    assert merged == [
        "S. 405 amended by No. 9576 s. 11(1).",
        "No. 6103 s. 405.",
        "S. 405(1) amended by No. 25/2023 s. 7.",
    ]
