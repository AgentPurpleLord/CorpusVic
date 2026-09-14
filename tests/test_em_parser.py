"""Tests for corpus/em_parser.py -- the flat Explanatory Memorandum
parser (Clause N entries + organisational Chapter/Part headers), a much
simpler shape than rule_parser.py's nested Act/Bill parser."""
from corpus.em_parser import parse_em

from conftest import HEAD_X0, PARA_X0, SUBPARA_X0, line, page


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
    assert entry["text"] == "provides that the Bill does not change the nature of a committal proceeding."


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


# ---------------------------------------------------------------------
# Bulleted lists
#
# An EM sets a list with the marker alone on its own extracted line and
# the item's text beside it at a deeper indent -- so read in order the
# lines are "•", the item, "•", the item, and the marker never arrives
# attached to what it introduces. Read as flowing text they collapse into
# one run-on paragraph; these check they become provisions of their own,
# the same way an Act's paragraph list does. Indents follow conftest's
# measured columns: HEAD_X0 is the body, PARA_X0 a first-level item,
# SUBPARA_X0 one nested inside it.
# ---------------------------------------------------------------------

def _bulleted_entry():
    return [
        line("Clause 1"),
        line("sets out the purposes of the Bill which are—"),
        line("•", x0=HEAD_X0),
        line("to clarify the law; and", x0=PARA_X0),
        line("•", x0=HEAD_X0),
        line("to simplify procedure.", x0=PARA_X0),
    ]


def test_each_bullet_becomes_its_own_provision():
    result = _parse(_bulleted_entry())

    assert [(n["type"], n["text"]) for n in result.nodes] == [
        ("clause", "sets out the purposes of the Bill which are—"),
        ("paragraph", "to clarify the law; and"),
        ("paragraph", "to simplify procedure."),
    ]


def test_a_bullet_item_that_wraps_stays_one_provision():
    lines = [
        line("Clause 1"),
        line("provides that a charge must—"),
        line("•", x0=HEAD_X0),
        line("state the offence that the accused is alleged", x0=PARA_X0),
        line("to have committed;", x0=PARA_X0),
    ]
    result = _parse(lines)

    assert result.nodes[1]["text"] == "state the offence that the accused is alleged to have committed;"


def test_a_nested_bullet_list_nests():
    lines = [
        line("Clause 28"),
        line("lists the offences that may be heard summarily—"),
        line("•", x0=HEAD_X0),
        line("an offence referred to in Schedule 2;", x0=PARA_X0),
        line("•", x0=HEAD_X0),
        line("an indictable offence described as being—", x0=PARA_X0),
        line("•", x0=PARA_X0),
        line("a level 5 or 6 offence; or", x0=SUBPARA_X0),
        line("•", x0=PARA_X0),
        line("punishable by a term of imprisonment.", x0=SUBPARA_X0),
    ]
    result = _parse(lines)

    assert [n["type"] for n in result.nodes] == [
        "clause", "paragraph", "paragraph", "subparagraph", "subparagraph",
    ]


def test_prose_resuming_after_a_list_keeps_its_place():
    # It belongs to the entry, but it comes *after* the items -- and the
    # clause's own text prints above them. Folding it back into that text
    # would make the entry read out of order.
    lines = [
        line("Clause 28"),
        line("lists the offences that may be heard summarily—"),
        line("•", x0=HEAD_X0),
        line("an offence referred to in Schedule 2;", x0=PARA_X0),
        line("A level 5 offence is punishable by 10 years imprisonment."),
    ]
    result = _parse(lines)

    assert [(n["type"], n["text"]) for n in result.nodes] == [
        ("clause", "lists the offences that may be heard summarily—"),
        ("paragraph", "an offence referred to in Schedule 2;"),
        ("note", "A level 5 offence is punishable by 10 years imprisonment."),
    ]


def test_a_line_back_at_an_outer_item_continues_that_item():
    # "... described as being—", its own sub-list, then the rest of the
    # same first-level item.
    lines = [
        line("Clause 28"),
        line("lists the offences—"),
        line("•", x0=HEAD_X0),
        line("an offence described as being—", x0=PARA_X0),
        line("•", x0=PARA_X0),
        line("a level 5 offence; or", x0=SUBPARA_X0),
        line("in either case, an indictable offence.", x0=PARA_X0),
    ]
    result = _parse(lines)

    assert result.nodes[1]["text"] == "an offence described as being— in either case, an indictable offence."
    assert result.nodes[2]["text"] == "a level 5 offence; or"


def test_a_list_does_not_leak_into_the_next_entry():
    lines = _bulleted_entry() + [
        line("Clause 2"),
        line("provides for the commencement of the Bill."),
    ]
    result = _parse(lines)

    assert find(result.nodes, "clause", "2")["text"] == "provides for the commencement of the Bill."
    assert [n["type"] for n in result.nodes] == ["clause", "paragraph", "paragraph", "clause"]


def test_every_bulleted_line_is_still_accounted_for():
    result = _parse(_bulleted_entry())

    assert result.lines_consumed == result.lines_total
    assert not result.warnings


# ---------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------

def test_an_entry_under_a_schedule_records_which_schedule():
    # A Schedule restarts clause numbering from 1, so "Clause 11" under
    # Schedule 1 and the body's own "Clause 11" are different provisions
    # sharing a number -- and a reader shown two identical "clause 11"
    # cross-references can't tell which is which.
    lines = [
        line("Clause 11"),
        line("deals with the body of the Bill."),
        line("SCHEDULE 1—CHARGES ON A CHARGE-SHEET", bold=True),
        line("Clause 11"),
        line("provides for stating an intent to deceive."),
    ]
    result = _parse(lines)

    body, schedule = [n for n in result.nodes if n["type"] == "clause"]
    assert "schedule" not in body
    assert schedule["schedule"] == "1"
    assert find(result.nodes, "heading_group", None)["schedule"] == "1"


def test_a_chapter_heading_after_a_schedule_closes_it():
    lines = [
        line("SCHEDULE 1—CHARGES", bold=True),
        line("Clause 1"),
        line("is in the Schedule."),
        line("Chapter 2—COMMENCING A PROCEEDING", bold=True),
        line("Clause 2"),
        line("is back in the body."),
    ]
    result = _parse(lines)

    assert find(result.nodes, "clause", "1")["schedule"] == "1"
    assert "schedule" not in find(result.nodes, "clause", "2")


def test_paragraph_breaks_survive_but_wrapped_lines_do_not():
    """An Explanatory Memorandum is prose: a clause note runs to several
    paragraphs, and where one ends is carried only by the space the
    typesetter left above the next. Keeping every printed line break left
    a note looking like verse; keeping none left it one undifferentiated
    block."""
    lines = [
        line("Clause 2", bold=True, y0=88, x1=260),
        line("provides for the commencement of the Bill. Chapter 1 comes", y0=100, x1=440),
        # Stops short of the margin, and a clear step up from the 12pt
        # pitch above: the end of a paragraph.
        line("into operation on the day after Royal Assent.", y0=112, x1=390),
        line("The other provisions commence on a day to be", y0=130, x1=440),
        line("proclaimed.", y0=142, x1=280),
    ]
    result = parse_em([page(lines)])
    entry = find(result.nodes, "clause", "2")

    assert entry["text"].split("\n") == [
        "provides for the commencement of the Bill. Chapter 1 comes into operation "
        "on the day after Royal Assent.",
        "The other provisions commence on a day to be proclaimed.",
    ]


def test_a_wrapped_line_is_not_a_paragraph_just_because_it_is_short():
    """The vertical gap has to agree. Without it, every line that happened
    to end early was called a paragraph break -- 91 of them in the
    Criminal Procedure Bill's own EM, splitting sentences mid-clause."""
    lines = [
        line("Clause 2", bold=True, y0=88, x1=260),
        # Short, but with no extra space above the line that follows it.
        line("provides for the commencement", y0=100, x1=330),
        line("of the Bill on Royal Assent.", y0=112, x1=440),
    ]
    entry = find(parse_em([page(lines)]).nodes, "clause", "2")

    assert "\n" not in entry["text"]
