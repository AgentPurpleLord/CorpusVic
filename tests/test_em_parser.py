"""Tests for ai_pipeline/em_parser.py -- the flat Explanatory Memorandum
parser (Clause N entries + organisational Chapter/Part headers), a much
simpler shape than rule_parser.py's nested Act/Bill parser."""
from ai_pipeline.em_parser import parse_em

from conftest import line, page


def _parse(lines):
    return parse_em([page(lines)])


def find(nodes, type_, number):
    for n in nodes:
        if n["type"] == type_ and n.get("number") == number:
            return n
    raise AssertionError(f"no {type_} {number!r} in {[(n['type'], n.get('number')) for n in nodes]}")


def test_basic_entries_and_completeness():
    lines = [
        line("Clause 1"),
        line("sets out the purposes of the Bill."),
        line("Clause 2"),
        line("provides for the commencement of the Bill."),
    ]
    result = _parse(lines)
    assert result.lines_consumed == result.lines_total
    assert not result.warnings
    e1 = find(result.nodes, "clause", "1")
    assert e1["text"] == "sets out the purposes of the Bill."
    e2 = find(result.nodes, "clause", "2")
    assert e2["text"] == "provides for the commencement of the Bill."


def test_inline_clause_number_on_same_line_as_explanation():
    """"Clause N provides ..." (no separate header line) is just as common
    as a standalone "Clause N" -- both must produce the same shape."""
    lines = [line("Clause 155 provides that the Bill does not change the"), line("nature of a committal proceeding.")]
    result = _parse(lines)
    entry = find(result.nodes, "clause", "155")
    assert entry["text"] == "provides that the Bill does not change the\nnature of a committal proceeding."


def test_pinpoint_clause_reference_gets_its_own_entry():
    lines = [
        line("Clause 6"),
        line("sets out how a criminal proceeding is commenced."),
        line("Clause 6(4) provides that, in the case of an indictable"),
        line("offence, additional matters apply."),
    ]
    result = _parse(lines)
    base = find(result.nodes, "clause", "6")
    assert base["text"] == "sets out how a criminal proceeding is commenced."
    pinpoint = find(result.nodes, "clause", "6(4)")
    assert "additional matters apply." in pinpoint["text"]


def test_a_clause_mentioned_mid_sentence_is_not_a_new_entry():
    """A line that merely *mentions* "Clause N" partway through, as part of
    a continuing sentence, must not be mistaken for a new entry -- only a
    line that *starts* with "Clause" does."""
    lines = [
        line("Clause 3"),
        line("defines various terms used in the Bill."),
        line("The accused. Clause 3 defines direct indictment."),
    ]
    result = _parse(lines)
    entry = find(result.nodes, "clause", "3")
    assert "The accused. Clause 3 defines direct indictment." in entry["text"]
    assert not any(n.get("number") == "3" and n is not entry for n in result.nodes)


def test_chapter_and_part_headers_are_organisational_only():
    lines = [
        line("CHAPTER 2—COMMENCING A CRIMINAL PROCEEDING", bold=True),
        line("PART 2.1—WAYS IN WHICH A CRIMINAL PROCEEDING IS COMMENCED", bold=True),
        line("Clause 5"),
        line("sets out how a proceeding is commenced."),
    ]
    result = _parse(lines)
    headings = [n for n in result.nodes if n["type"] == "heading_group"]
    assert len(headings) == 2
    assert headings[0]["heading"] == "CHAPTER 2—COMMENCING A CRIMINAL PROCEEDING"
    assert headings[1]["heading"] == "PART 2.1—WAYS IN WHICH A CRIMINAL PROCEEDING IS COMMENCED"
    entry = find(result.nodes, "clause", "5")
    assert entry["text"] == "sets out how a proceeding is commenced."


def test_chapter_heading_wrapped_across_two_bold_lines_merges_not_orphans():
    """Regression: a Chapter/Part title that wraps onto a second bold line
    used to fall through to the "text before any entry" fallback and spawn
    a spurious orphaned entry instead of extending the heading in
    progress."""
    lines = [
        line("CHAPTER 2—COMMENCING A CRIMINAL", bold=True),
        line("PROCEEDING", bold=True),
        line("Clause 5"),
        line("sets out how a proceeding is commenced."),
    ]
    result = _parse(lines)
    headings = [n for n in result.nodes if n["type"] == "heading_group"]
    assert len(headings) == 1
    assert headings[0]["heading"] == "CHAPTER 2—COMMENCING A CRIMINAL PROCEEDING"
    assert not result.warnings


def test_front_matter_before_first_clause_is_skipped_not_orphaned():
    lines = [
        line("Criminal Procedure Bill 2008"),
        line("Introduction Print"),
        line("EXPLANATORY MEMORANDUM"),
        line("Clause 1"),
        line("sets out the purposes of the Bill."),
    ]
    result = _parse(lines)
    assert result.lines_consumed == result.lines_total
    assert any("skipped 3 front-matter line" in w for w in result.warnings), result.warnings
    assert len(result.nodes) == 1
    assert result.nodes[0]["number"] == "1"


def test_a_clause_cross_reference_far_ahead_is_not_mistaken_for_a_new_entry():
    """Regression, from the real Criminal Procedure Bill 2008 EM: clause
    2's own commencement note discusses when a much later clause takes
    effect ("Clause 384 comes into operation on 1 July 2010") as an
    ordinary sentence within its own explanation. That line has the exact
    shape of a genuine new entry, but jumping from clause 2 to clause 384
    with no Chapter/Part/Schedule heading in between is implausible --
    the OCPC's own guide requires a note for every clause, so real
    entries proceed close to 1-by-1. Without this check, clause 2's note
    would be cut short and a bogus, premature "384" entry created,
    conflicting with the real clause 384 entry that appears far later."""
    lines = [
        line("Clause 2"),
        line("provides for the commencement of the Bill.  Chapter 1 comes"),
        line("into operation on the day after Royal Assent."),
        line("Clause 384 comes into operation on 1 July 2010.  This clause"),
        line("provides for the repeal of sentence indication procedures."),
        line("Clause 3"),
        line("defines various words and expressions used in the Bill."),
    ]
    result = _parse(lines)
    entry_2 = find(result.nodes, "clause", "2")
    assert "Clause 384 comes into operation on 1 July 2010." in entry_2["text"]
    assert not any(n.get("number") == "384" for n in result.nodes)
    entry_3 = find(result.nodes, "clause", "3")
    assert entry_3["text"] == "defines various words and expressions used in the Bill."


def test_a_clause_cross_reference_is_still_accepted_right_after_a_heading():
    """The plausibility guard resets at every Chapter/Part/Schedule
    heading, since numbering can legitimately jump or restart there (a
    Schedule's own items commonly restart from 1) -- the very first entry
    after one is always accepted, however large the jump."""
    lines = [
        line("Clause 5"),
        line("sets out an earlier matter."),
        line("SCHEDULE 1—CHARGES ON A CHARGE-SHEET", bold=True),
        line("Clause 384"),
        line("is the first item explained in this Schedule."),
    ]
    result = _parse(lines)
    entry_384 = find(result.nodes, "clause", "384")
    assert entry_384["text"] == "is the first item explained in this Schedule."


def test_bullet_points_stay_inline_as_continuation_text():
    lines = [
        line("Clause 1"),
        line("sets out the purposes of the Bill which are—"),
        line("•"),
        line("to clarify the law; and"),
        line("•"),
        line("to simplify procedure."),
    ]
    result = _parse(lines)
    entry = find(result.nodes, "clause", "1")
    assert "•\nto clarify the law; and\n•\nto simplify procedure." in entry["text"]
