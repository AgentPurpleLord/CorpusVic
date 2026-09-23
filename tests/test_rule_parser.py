"""
Tests for the deterministic rules engine (corpus/rule_parser.py).

Each regression test here corresponds to a real bug found and fixed against
actual Crimes Act / Interpretation of Legislation Act text during this
project -- the synthetic snippets are trimmed-down, minimal reproductions
of the real page content that exposed each bug, not made-up shapes.
"""
from corpus.parsing.rule_parser import parse_act

from conftest import HEAD_X0, PARA_WRAP_X0, PARA_X0, SUBPARA_X0, WRAP_X0, line, page


def _parse(lines):
    result = parse_act([page(lines)])
    assert result.lines_total == result.lines_consumed, (
        "completeness invariant violated -- every input line must land in exactly one node"
    )
    return result


def find(nodes, type_, number=None):
    for n in nodes:
        if n["type"] == type_ and (number is None or n.get("number") == number):
            return n
    raise AssertionError(f"no {type_} number={number!r} found among {[(n['type'], n.get('number')) for n in nodes]}")


def test_basic_hierarchy_and_completeness():
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("1 Murder", bold=True),
        line("(1) A person who commits murder is guilty of an offence.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    section = find(result.nodes, "section", "1")
    assert section["heading"] == "Murder"
    subsection = find(result.nodes, "subsection", "1")
    assert "guilty of an offence" in subsection["text"]


def test_subdivision_recognised_before_first_section():
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("(1) Homicide", bold=True),
        line("1 Murder", bold=True),
        line("Text.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    subdivision = find(result.nodes, "subdivision", "1")
    assert subdivision["heading"] == "Homicide"


def test_subdivision_recognised_after_multiple_sections_already_open():
    """Regression: a Division carrying several numbered Subdivisions
    (common in the real Crimes Act -- Homicide, ..., Abrogation of
    obsolete rules of law, Child stealing, ...) used to only get the
    first one recognised. A previous fix for bold Act-name citations
    being misread as Subdivisions gated Subdivision detection to "before
    any Section has opened", which broke every Subdivision after the
    first one in a Division like this. The real fix is digit-lead
    detection, which doesn't care about position."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("(1) Homicide", bold=True),
        line("1 Murder", bold=True),
        line("Text.", x0=HEAD_X0),
        line("(8G) Abrogation of obsolete rules of law", bold=True),
        line("62 Abrogation of obsolete rules of law", bold=True),
        line("Text.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    first = find(result.nodes, "subdivision", "1")
    assert first["heading"] == "Homicide"
    second = find(result.nodes, "subdivision", "8G")
    assert second["heading"] == "Abrogation of obsolete rules of law"


def test_roman_bracket_inside_list_is_not_a_subdivision():
    """Regression: a bold Act-name citation inside an enumerated list
    ("(i) the ... Act 1987; or") must stay a Subparagraph -- it must
    never be misread as a Subdivision just because it's bold and
    bracket-shaped. A Subdivision's own number is always digit-led in
    this drafting convention; letters/roman numerals never are."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("1 Murder", bold=True),
        line("(1) A defence applies if the person acted under—", x0=HEAD_X0),
        line("(a) an order made under", x0=PARA_X0),
        line("(i) the Conservation, Forests and Lands Act 1987; or", x0=SUBPARA_X0, bold=True),
        line("(ii) the Livestock Disease Control Act 1994; or", x0=SUBPARA_X0, bold=True),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "subdivision" for n in result.nodes)
    sub_i = find(result.nodes, "subparagraph", "i")
    assert "Conservation" in sub_i["text"]


def test_bare_subdivision_number_wraps_heading_from_next_bold_line():
    """"(4A)" alone, with the title on the next bold line, is the same
    wrap pattern as a Section heading spanning two lines."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("1 Murder", bold=True),
        line("Text.", x0=HEAD_X0),
        line("(4A)", bold=True),
        line("Non-fatal strangulation", bold=True),
        line("34AB Definitions", bold=True),
        line("Text.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    subdivision = find(result.nodes, "subdivision", "4A")
    assert subdivision["heading"] == "Non-fatal strangulation"


def test_bare_section_number_wraps_heading_from_next_bold_line():
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("1 Murder", bold=True),
        line("Text.", x0=HEAD_X0),
        line("465AAAA", bold=True),
        line("Police may use assistants and equipment", bold=True),
        line("Text.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    section = find(result.nodes, "section", "465AAAA")
    assert section["heading"] == "Police may use assistants and equipment"


def test_section_number_with_long_letter_suffix():
    """Regression: the section-number pattern used to cap the trailing
    letter suffix at 3 characters, silently failing to match heavily-
    amended section numbers like "464ZFAAA" (5 letters)."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("464ZFAAA Forensic procedure following finding of not guilty", bold=True),
        line("Text.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    section = find(result.nodes, "section", "464ZFAAA")
    assert section["heading"] == "Forensic procedure following finding of not guilty"


def test_pinpoint_citation_mid_sentence_not_promoted_to_subdivision():
    """Regression: a bold mid-sentence pinpoint citation that happens to
    wrap onto its own line ("... section 9A(1A) or\\n(1B) of the
    Corrections Act 1986 ...") must not be misread as a fresh
    Subdivision -- it isn't preceded by a clean sentence break, unlike a
    genuine Subdivision heading."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("31D Intimidation", bold=True),
        line("(f) authorised under section 9A(1A) or", x0=PARA_X0),
        line("(1B) of the Corrections Act 1986 to", x0=PARA_WRAP_X0, bold=True),
        line("exercise a function or power referred to", x0=PARA_WRAP_X0),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "subdivision" and n.get("number") == "1B" for n in result.nodes)


def test_bold_subsection_with_embedded_definition_not_promoted_to_subdivision():
    """Regression: some PDFs print a Subsection's *entire* first line
    bold when a defined term sits early in it (an extraction quirk) --
    this must stay an ordinary Subsection, not a Subdivision, even
    though it's bold and digit-bracket shaped, because its text reads
    as the start of a sentence ("In this section, ...") rather than a
    noun-phrase title."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("31 Emergency workers", bold=True),
        line("(3) For the purposes of subsection (2), etc.", x0=HEAD_X0),
        line("(4) In this section, emergency service vehicle means a", x0=HEAD_X0, bold=True),
        line("motor vehicle that, at a particular time—", x0=WRAP_X0),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "subdivision" for n in result.nodes)
    subsection4 = find(result.nodes, "subsection", "4")
    assert "emergency service vehicle means" in subsection4["text"]


def test_unnumbered_topic_heading_recognised_as_heading_group():
    """"Theft, robbery, burglary, &c." -- a bare topical heading grouping
    a run of Sections, set at ordinary body size (no numbering, no
    larger font) -- must become its own heading_group node, not get
    silently merged into whichever Subsection happened to be open."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 2—Theft and similar offences", bold=True),
        line("72 Basic definition of theft", bold=True),
        line("(1) A person steals if he dishonestly appropriates property.", x0=HEAD_X0),
        line("Theft, robbery, burglary, &c.", bold=True),
        line("74 Theft", bold=True),
        line("(1) A person guilty of theft is guilty of an indictable offence.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    heading_groups = [n for n in result.nodes if n["type"] == "heading_group"]
    assert any(h["heading"] == "Theft, robbery, burglary, &c." for h in heading_groups)
    # The historical bug filed it under whatever was still open (section
    # 72's own subsection (1)), not under the following section.
    subsection1 = find(result.nodes, "subsection", "1")
    assert "Theft, robbery, burglary" not in (subsection1.get("text") or "")


def test_defined_term_opener_not_misread_as_topic_heading():
    """A bold "term means—" opener (the standard defined-term
    convention) must not be mistaken for a bare topical heading just
    because it's short, bold, and at body size."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("2A Definitions", bold=True),
        line("In this Act—", x0=HEAD_X0),
        line("medical practitioner means—", bold=True, x0=HEAD_X0),
        line("a person registered under the Health Practitioner Regulation.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "heading_group" for n in result.nodes)


def test_bold_italic_leading_run_opens_its_own_definition_node():
    """Inside a Definitions section, a line whose own leading run is set
    bold+italic (see extract.py's _leading_bold_italic -- the reliable
    typesetting signal for where a defined term is introduced) opens its
    own "definition" node instead of piling onto whatever came before,
    so a reviewer can see and work through each term individually rather
    than one unbroken block of dozens of definitions concatenated
    together (the real-world shape: a Criminal Procedure Act-style
    Definitions section runs to 50+ terms in one Section)."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("3 Definitions", bold=True),
        line("In this Act—", x0=HEAD_X0),
        line("accused means a person who—", x0=HEAD_X0, leading_bold_italic="accused"),
        line("(a) is charged with an offence; or", x0=PARA_X0),
        line("appeal includes application for leave to appeal;", x0=HEAD_X0, leading_bold_italic="appeal"),
    ]
    result = _parse(lines)
    accused = find(result.nodes, "definition", None)
    assert accused["heading"] == "accused"
    assert accused["text"] == "means a person who—"
    paragraph_a = find(result.nodes, "paragraph", "a")
    assert paragraph_a["text"] == "is charged with an offence; or"
    appeal = [n for n in result.nodes if n["type"] == "definition" and n["heading"] == "appeal"][0]
    assert appeal["text"] == "includes application for leave to appeal;"
    # Two separate definitions, not one node holding both.
    assert sum(1 for n in result.nodes if n["type"] == "definition") == 2


def test_definitions_nested_paragraph_list_closes_when_the_next_definition_opens():
    """A definition's own (a)/(b) list must attach *to that definition*,
    not leak into the next one -- mirroring how a numbered subsection's
    own list closes when a fresh subsection opens."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("3 Definitions", bold=True),
        line("In this Act—", x0=HEAD_X0),
        line("appropriate registrar means—", x0=HEAD_X0, leading_bold_italic="appropriate registrar"),
        line("(a) the registrar at the venue; or", x0=PARA_X0),
        line("(b) if an order is made, the other registrar;", x0=PARA_X0),
        line("arraignment has the meaning given in section 215;", x0=HEAD_X0, leading_bold_italic="arraignment"),
    ]
    result = _parse(lines)
    definitions = [n for n in result.nodes if n["type"] == "definition"]
    assert [d["heading"] for d in definitions] == ["appropriate registrar", "arraignment"]
    paragraphs = [n for n in result.nodes if n["type"] == "paragraph"]
    assert [p["number"] for p in paragraphs] == ["a", "b"]
    arraignment = definitions[1]
    assert "meaning given in section 215" in arraignment["text"]
    assert "registrar" not in arraignment["text"]


def test_definition_continuation_line_stays_attached_without_its_own_lead():
    """A definition's own text commonly wraps onto a second physical
    line with no bold+italic lead of its own -- that line is this same
    definition's continuation, not a fresh one."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("3 Definitions", bold=True),
        line("In this Act—", x0=HEAD_X0),
        line("appeal period means the period permitted by or", x0=HEAD_X0, leading_bold_italic="appeal period"),
        line("under this Act for commencing an appeal;", x0=WRAP_X0),
    ]
    result = _parse(lines)
    assert sum(1 for n in result.nodes if n["type"] == "definition") == 1
    definition = find(result.nodes, "definition", None)
    assert definition["text"] == "means the period permitted by or under this Act for commencing an appeal;"


def test_a_long_defined_terms_own_wrap_extends_the_heading_not_a_new_definition():
    """Regression (Criminal Procedure Act): a long defined term can wrap
    across two physical lines ("indictable offence that may be heard
    and" / "determined summarily means an offence to..."), both entirely
    or partly bold+italic -- the second line must extend the first
    definition's own heading, not be mistaken for its own separate
    definition with an empty, truncated first entry and a second entry
    misnamed after only the wrapped tail of the real term."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("3 Definitions", bold=True),
        line("In this Act—", x0=HEAD_X0),
        line("indictable offence that may be heard and", x0=HEAD_X0, leading_bold_italic="indictable offence that may be heard and"),
        line(
            "determined summarily means an offence to which section 28(1) applies;",
            x0=HEAD_X0, leading_bold_italic="determined summarily",
        ),
        line("informant means a person who commences a proceeding;", x0=HEAD_X0, leading_bold_italic="informant"),
    ]
    result = _parse(lines)
    definitions = [n for n in result.nodes if n["type"] == "definition"]
    assert [d["heading"] for d in definitions] == [
        "indictable offence that may be heard and determined summarily",
        "informant",
    ]
    assert definitions[0]["text"] == "means an offence to which section 28(1) applies;"


def test_bold_italic_leading_run_outside_a_definitions_section_is_not_promoted():
    """The bold+italic signal is only trusted inside a section that
    actually looks like it's introducing defined terms (by heading) --
    gated the same way the rest of definitions.py's own heuristics are,
    so it can't misfire on some other section that happens to carry the
    same styling for an unrelated reason."""
    lines = [
        line("Part I—Offences", bold=True),
        line("5 Murder", bold=True),
        line("A person who commits murder is guilty of an offence.", x0=HEAD_X0, leading_bold_italic="murder"),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "definition" for n in result.nodes)


def test_hanging_list_reattaches_trailing_clause_to_lead_in():
    """The headline case this project was built to fix: "(1) A person
    who -- (a) does X; or (b) does Y -- is guilty of an offence." The
    trailing independent clause grammatically resumes subsection (1)'s
    own lead-in sentence, not paragraph (b)'s -- and the PDF's own
    hanging indent (the trailing clause outdents back past the
    Paragraph's own wrap indent) is what tells them apart.

    It belongs to the subsection but comes *after* its list, so it is a
    node of its own nested inside it rather than more of its text: the
    list items are already in the node list by then, and adding to the
    subsection would print the wrap-up before the list it follows."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("6B Survivor of suicide pact", bold=True),
        line("(2) Any person who—", x0=HEAD_X0),
        line("(a) incites any other person to commit suicide; or", x0=PARA_X0),
        line("(b) aids or abets any other person in the", x0=PARA_X0),
        line("commission of suicide or in an attempt to", x0=PARA_WRAP_X0),
        line("commit suicide—", x0=PARA_WRAP_X0),
        line("shall be guilty of an indictable offence and liable", x0=WRAP_X0),
        line("to level 6 imprisonment (5 years maximum).", x0=WRAP_X0),
    ]
    result = _parse(lines)
    subsection2 = find(result.nodes, "subsection", "2")
    paragraph_b = find(result.nodes, "paragraph", "b")
    wrap_up = find(result.nodes, "continuation")
    assert "shall be guilty" in wrap_up["text"]
    assert "shall be guilty" not in paragraph_b["text"]
    assert "shall be guilty" not in subsection2["text"]
    assert paragraph_b["text"].rstrip().endswith("suicide—")
    # Reading order: the lead-in, then the list, then the wrap-up.
    order = [n["type"] for n in result.nodes]
    assert order.index("subsection") < order.index("paragraph") < order.index("continuation")


def test_chapter_heading_recognised_and_nests_a_part_under_it():
    """The Criminal Procedure Act / Evidence Act group their Parts under
    numbered Chapters. "Chapter N—Title" matches the built-in chapter
    pattern (no profile needed), and a following Part nests inside it."""
    lines = [
        line("Chapter 2—Commencing a criminal proceeding", bold=True),
        line("Part 1—How a criminal proceeding is commenced", bold=True),
        line("1 Commencement", bold=True),
        line("A criminal proceeding is commenced by filing a charge-sheet.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    chapter = find(result.nodes, "chapter", "2")
    assert chapter["heading"] == "Commencing a criminal proceeding"
    # order: chapter, then part, then section -- nesting is reconstructed
    # downstream from this flat order (see akn_export.build_hierarchy_tree).
    types = [n["type"] for n in result.nodes]
    assert types.index("chapter") < types.index("part") < types.index("section")


def test_chapter_title_wrapping_onto_a_second_bold_line_extends_the_heading():
    lines = [
        line("Chapter 2—Commencing a", bold=True),
        line("criminal proceeding", bold=True),
        line("Part 1—How it starts", bold=True),
        line("Text.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    chapter = find(result.nodes, "chapter", "2")
    assert chapter["heading"] == "Commencing a criminal proceeding"


def test_act_with_no_chapter_lines_produces_no_chapter_nodes():
    """Regression guard: "chapter" is in the default hierarchy, but an Act
    that never prints a "Chapter N—..." line must not sprout one."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("1 Murder", bold=True),
        line("(1) A person who commits murder is guilty of an offence.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "chapter" for n in result.nodes)
    assert "chapter" in result.hierarchy  # available, just unused


def test_hanging_list_does_not_fire_on_genuine_nested_wrap():
    """The same mechanism must leave an ordinary multi-line Paragraph
    (no closing clause, just a wrapped sentence within the Paragraph
    itself) alone -- it shouldn't get split away from its own Paragraph
    just because it's the deepest open node."""
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("6 Infanticide", bold=True),
        line("(1) Where a woman causes the death of her child—", x0=HEAD_X0),
        line("(b) a disorder consequent on her giving birth to", x0=PARA_X0),
        line("that child within the preceding 2 years—", x0=PARA_WRAP_X0),
    ]
    result = _parse(lines)
    paragraph_b = find(result.nodes, "paragraph", "b")
    assert "that child within the preceding 2 years" in paragraph_b["text"]


def test_a_section_opens_even_where_the_provision_above_it_ran_on():
    """Regression (Crimes Act ss 320A, 464Y, 464ZGFC; Criminal Procedure
    Act s 7B; 24 clauses of its Bill): a section heading was only
    recognised where the previous line reached a clean sentence break.
    That guard is there for a citation that wrapped -- but a provision
    ending mid-sentence (a wrapped list item, a note) is exactly where the
    next section sits, so 40 real provisions were absorbed into the one
    above them instead of opening."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("(a) something that wraps onto the next line without", x0=PARA_X0),
        line("reaching a full stop", x0=PARA_WRAP_X0),
        line("320A Maximum term of imprisonment for common", bold=True),
        line("assault in certain circumstances", x0=HEAD_X0, bold=True),
        line("Despite section 320, the maximum term is 10 years.", x0=HEAD_X0),
    ]
    result = _parse(lines)

    section = find(result.nodes, "section", "320A")
    assert section["heading"] == "Maximum term of imprisonment for common assault in certain circumstances"
    assert "Despite section 320" in section["text"]


def test_a_wrapped_list_of_section_numbers_is_not_read_as_a_heading():
    """The other half of the same judgement: "sections 84F\nand 84G of the
    Domestic Animals Act 1994" puts a real section number at the start of
    a bold line. A continuation runs on in lower case where a heading's
    title is capitalised."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("(a) to amend sections 84E, 84F", x0=PARA_X0, bold=True),
        line("and 84G of the Domestic Animals Act 1994", x0=PARA_WRAP_X0, bold=True),
    ]
    result = _parse(lines)

    assert not any(n["type"] == "section" and n.get("number") == "84G" for n in result.nodes)
    assert "and 84G of the Domestic Animals Act 1994" in find(result.nodes, "paragraph", "a")["text"]


def test_a_wrapped_act_year_is_still_not_read_as_a_heading():
    # "... Act\n1997 insert--": four digits and nothing else is a year,
    # never a section number, whatever case the words after it are in.
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("(a) in the Crimes (Mental Impairment) Act", x0=PARA_X0, bold=True),
        line("1997 Insert the following section", x0=PARA_WRAP_X0, bold=True),
    ]
    result = _parse(lines)

    assert not any(n["type"] == "section" and n.get("number") == "1997" for n in result.nodes)


def test_multiline_bold_act_citation_is_not_split_into_heading_groups():
    """Regression: a Paragraph listing several Act names being amended can
    wrap across many *consecutive* bold lines ("... the Crimes\n(Mental
    Impairment and Unfitness to be\nTried) Act 1997, the Magistrates'
    Court\nAct 1989, ...") -- an earlier fix let a bold previous line
    count as a "fresh start" for heading detection (to recognise a
    Subdivision opening right after its Division's own bold heading
    line), but that wrongly treated every wrapped line of a multi-line
    bold citation as its own fresh start too, splitting the citation into
    a string of spurious heading_group nodes instead of keeping it as one
    Paragraph's continuing text."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("The purposes of this Act are—", x0=HEAD_X0),
        line("(k) to amend the Crimes Act 1958, the Crimes", x0=PARA_X0),
        line("(Mental Impairment and Unfitness to be", x0=PARA_WRAP_X0, bold=True),
        line("Tried) Act 1997, the Magistrates' Court", x0=PARA_WRAP_X0, bold=True),
        line("Act 1989, the Children, Youth and", x0=PARA_WRAP_X0, bold=True),
        line("Families Act 2005 and the Appeal Costs", x0=PARA_WRAP_X0, bold=True),
        line("Act 1998;", x0=PARA_WRAP_X0, bold=True),
        line("(l) to repeal the Crimes (Criminal Trials)", x0=PARA_X0, bold=True),
        line("Act 1999;", x0=PARA_WRAP_X0, bold=True),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "heading_group" for n in result.nodes)
    paragraph_k = find(result.nodes, "paragraph", "k")
    assert "Families Act 2005 and the Appeal Costs" in paragraph_k["text"]
    assert "Act 1998;" in paragraph_k["text"]
    paragraph_l = find(result.nodes, "paragraph", "l")
    assert "Act 1999;" in paragraph_l["text"]


def test_bold_year_wrap_mid_citation_not_promoted_to_a_new_section():
    """Regression (Criminal Procedure Bill 2008): a Schedule-style
    consequential amendment's own lead-in wraps an Act name's year onto
    its own bold line ("... Act\\n1997 insert-", the citation's year --
    "Crimes (Mental Impairment and Unfitness to be Tried) Act 1997" --
    landing alone on a line together with the next word). "1997 insert-"
    has exactly the same shape as a genuine section heading ("(\\d+)\\s+
    (.+)"), and is bold like one, but it doesn't open right after a clean
    sentence break -- a real section/clause heading always does."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("370 New section 14A inserted", bold=True),
        line("After section 14 of the Crimes (Mental", x0=HEAD_X0),
        line("Impairment and Unfitness to be Tried) Act", x0=HEAD_X0, bold=True),
        line("1997 insert—", x0=HEAD_X0, bold=True),
        line('"14A Appeal in relation to fitness to plead', x0=HEAD_X0, bold=True),
    ]
    result = _parse(lines)
    assert not any(n.get("number") == "1997" for n in result.nodes)
    section_370 = find(result.nodes, "section", "370")
    assert "1997 insert—" in section_370["text"]
    assert "14A Appeal in relation to fitness to plead" in section_370["text"]


def test_section_heading_right_after_a_heading_group_counts_as_fresh_start():
    """Regression (Criminal Procedure Bill 2008): a bare topical
    heading_group (e.g. a caption like "Fraud and blackmail" grouping a
    run of Sections, with no numbering of its own) isn't pushed onto the
    parser's stack the way a Part/Division/Section is, so a section/
    clause heading immediately following one used to be rejected as "not
    a fresh start" whenever the heading_group's own text didn't end in
    terminal punctuation (a bare caption never does) -- silently dropping
    the section/clause number and folding its heading text into whatever
    section preceded the heading_group instead. (A numbered "CHAPTER
    N-..." caption doesn't exercise this path any more -- it's recognised
    as its own "chapter" heading level, pushed onto the stack just like a
    Part/Division, so it doesn't need this heading_group-specific fix.)"""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("The purposes of this Act are to consolidate the law.", x0=HEAD_X0),
        line("Reference to court of appeal", bold=True, size=14.0),
        line("327 Reference by Attorney-General", bold=True),
        line("The Attorney-General may refer a case.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    heading_group = find(result.nodes, "heading_group")
    assert heading_group["heading"] == "Reference to court of appeal"
    section_327 = find(result.nodes, "section", "327")
    assert section_327["heading"] == "Reference by Attorney-General"
    assert "refer a case" in section_327["text"]


def test_bold_section_heading_ends_a_notes_block_instead_of_becoming_a_note_item():
    """Regression (Criminal Procedure Bill 2008): an amendment-history
    Notes block's own numbered entries ("1 If the Magistrates' Court...",
    "2 See section 86...") are always plain body text, never bold -- but
    share the exact "digit(s) then text" shape a genuine section/clause
    heading has. A bold line with that same shape immediately following
    a Notes block is the next section, not one more note, and should end
    notes_mode instead of being swallowed as note "38"."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("37 Contents of preliminary brief", bold=True),
        line("A preliminary brief must include the following.", x0=HEAD_X0),
        line("Notes", bold=True),
        line("1 See section 84 as to service on the accused.", x0=HEAD_X0),
        line("2 See section 86 as to proof of criminal record.", x0=HEAD_X0),
        line("38 Requirements for informant's statement", bold=True),
        line("A statement by the informant must be signed.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    assert not any(n["type"] == "note" and n.get("number") == "38" for n in result.nodes)
    section_38 = find(result.nodes, "section", "38")
    assert section_38["heading"] == "Requirements for informant's statement"
    note_1 = find(result.nodes, "note", "1")
    assert "service on the accused" in note_1["text"]


def test_singular_unnumbered_note_becomes_its_own_note_node():
    """Regression (Criminal Procedure Act): a singular "Note" (as opposed
    to "Notes" with its own numbered "1 ...", "2 ..." items) is the
    standard drafting convention for one explanatory remark under a
    single provision -- its own first line has no leading number to open
    a numbered note with, so it used to fall straight through to ending
    notes_mode immediately, silently gluing the whole note onto whatever
    text was already open (here, section 278's own lead-in) instead of
    ever becoming its own "note" node."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("278 Right of appeal against sentence", bold=True),
        line("A person sentenced for an offence may appeal.", x0=HEAD_X0),
        line("Note", bold=True),
        line("See the definitions of originating court and original", x0=HEAD_X0),
        line("jurisdiction in section 3.", x0=HEAD_X0),
        line("279 How appeal is commenced", bold=True),
        line("An application is commenced by filing a notice.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    section_278 = find(result.nodes, "section", "278")
    assert section_278["text"] == "A person sentenced for an offence may appeal."
    note = find(result.nodes, "note", None)
    assert note["text"] == "See the definitions of originating court and original jurisdiction in section 3."
    section_279 = find(result.nodes, "section", "279")
    assert "notice" in section_279["text"]
    assert "definitions of originating court" not in section_279["text"]


def test_singular_unnumbered_note_ends_at_a_fresh_definition_start():
    """The same unnumbered-Note gap, but ending at a boundary
    _looks_like_boundary can't see on its own (no pattern shape at all,
    only typesetting) -- a fresh defined term opening right after the
    Note, inside a Definitions section."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("3 Definitions", bold=True),
        line("In this Act—", x0=HEAD_X0),
        line("sentence includes—", x0=HEAD_X0, leading_bold_italic="sentence"),
        line("(a) the recording of a conviction; and", x0=PARA_X0),
        line("Note", bold=True),
        line("Section 586 of another Act also applies.", x0=HEAD_X0),
        line("sexual offence has the meaning given by section 4;", x0=HEAD_X0, leading_bold_italic="sexual offence"),
    ]
    result = _parse(lines)
    note = find(result.nodes, "note", None)
    assert note["text"] == "Section 586 of another Act also applies."
    sexual_offence = [n for n in result.nodes if n["type"] == "definition" and n["heading"] == "sexual offence"][0]
    assert sexual_offence["text"] == "has the meaning given by section 4;"
    assert "Section 586" not in sexual_offence["text"]


def test_repealed_marker_between_subsections_gets_its_own_type():
    """Regression (Criminal Procedure Act, e.g. s. 2): 3+ asterisks on
    their own lines is Victoria's standard drafting convention for "a
    subsection used to be here and was repealed" -- distinct from an
    actual footnote/margin note (the parser has no idea *why* the text is
    missing, only that it's not there), so it gets its own "repealed"
    type rather than the "note" every other unclassified aside gets."""
    lines = [
        line("5 Some section", bold=True),
        line("(1) The first subsection.", x0=HEAD_X0),
        line("*", x0=HEAD_X0),
        line("*", x0=HEAD_X0),
        line("*", x0=HEAD_X0),
        line("(3) The third subsection.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    repealed = find(result.nodes, "repealed", None)
    assert repealed["text"] == "* * *"
    assert find(result.nodes, "subsection", "1")["text"] == "The first subsection."
    assert find(result.nodes, "subsection", "3")["text"] == "The third subsection."


def test_top_level_type_clause_parses_a_bill_the_same_way_as_an_act():
    """A Bill's own top-level numbered provision is called a "clause", not
    a "section" -- same drafting shape, same nesting rank (subsection/
    paragraph/subparagraph nest under either identically), just the pre-
    enactment name (see hierarchy.py's HIERARCHY_RANK entry for it)."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("The purposes of this Act are—", x0=HEAD_X0),
        line("(a) to clarify the law.", x0=PARA_X0),
    ]
    result = parse_act([page(lines)], top_level_type="clause")
    assert result.lines_total == result.lines_consumed
    clause = find(result.nodes, "clause", "1")
    assert clause["heading"] == "Purposes"
    paragraph = find(result.nodes, "paragraph", "a")
    assert paragraph["text"] == "to clarify the law."
    assert not any(n["type"] == "section" for n in result.nodes)


def test_front_matter_is_skipped_past_a_bills_table_of_provisions():
    """A Bill's introduction print opens with a title page and a multi-
    page Table of Provisions whose rows repeat real Part/clause headings
    closely enough to fool the heading classifiers -- parse_act discards
    everything up to the fixed enacting words every Bill's real text
    opens with, rather than trying to parse the TOC as structure."""
    lines = [
        line("TABLE OF PROVISIONS", bold=True, size=14.0),
        line("PART 2.1—WAYS IN WHICH A CRIMINAL PROCEEDING IS", bold=True),
        line("COMMENCED", bold=True),
        line("12", x0=HEAD_X0),
        line("How a criminal proceeding is commenced", x0=HEAD_X0),
        line("13", x0=HEAD_X0),
        line("A Bill for an Act to provide for procedures.", x0=HEAD_X0),
        line("The Parliament of Victoria enacts:", bold=True, size=12.0),
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("The purposes of this Act are—", x0=HEAD_X0),
        line("(a) to clarify the law.", x0=PARA_X0),
    ]
    result = parse_act([page(lines)], top_level_type="clause")
    assert result.lines_total == result.lines_consumed
    assert any("skipped 8 front-matter line" in w for w in result.warnings)
    clause = find(result.nodes, "clause", "1")
    assert clause["heading"] == "Purposes"
    # None of the TOC's own row content ("PART 2.1—...", "How a criminal
    # proceeding is commenced") should have leaked into any real node.
    assert not any("2.1" in (n.get("heading") or "") for n in result.nodes)
    assert not any("How a criminal proceeding" in (n.get("text") or "") for n in result.nodes)


def test_front_matter_is_skipped_past_an_acts_own_reprinted_identity_block():
    # Every Authorised Version reprints its own identity -- version
    # number, title, "incorporating amendments as at", the date -- right
    # at the top of its real operative text. Left in, this became two
    # spurious heading_group nodes reading as though this pipeline's own
    # output *were* the Authorised Version, rather than this pipeline's
    # own reading of one.
    lines = [
        line("Authorised Version No. 114", bold=True, size=14.0),
        line("Criminal Procedure Act 2009", bold=True, size=16.0),
        line("No. 7 of 2009", bold=True),
        line("Authorised Version incorporating amendments as at"),
        line("1 July 2026"),
        line("The Parliament of Victoria enacts:", bold=True),
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("The purposes of this Act are—", x0=HEAD_X0),
    ]
    result = parse_act([page(lines)])
    assert result.lines_total == result.lines_consumed
    assert any("skipped 6 front-matter line" in w for w in result.warnings)
    assert not any(n["type"] == "heading_group" for n in result.nodes)
    section = find(result.nodes, "section", "1")
    assert section["heading"] == "Purposes"


def test_front_matter_is_skipped_past_an_older_acts_enacting_words():
    # An Act drafted before "The Parliament of Victoria enacts:" came into
    # use closes its own, longer-form enacting words with "... (that is
    # to say):" instead (Crimes Act 1958, e.g.), wrapped across several
    # lines -- only the fixed tail is matched.
    lines = [
        line("Authorised Version No. 321", bold=True),
        line("Crimes Act 1958", bold=True),
        line("No. 6231 of 1958", bold=True),
        line("An Act to consolidate the Law Relating to Crimes."),
        line("BE IT ENACTED by the Queen's Most Excellent Majesty by and"),
        line("with the advice and consent of the Legislative Council and"),
        line("the Legislative Assembly of Victoria in this present"),
        line("Parliament assembled and by the authority of the same as"),
        line("follows (that is to say):"),
        line("Part I—Preliminary", bold=True),
        line("1 Short title", bold=True),
        line("This Act may be cited as the Crimes Act 1958.", x0=HEAD_X0),
    ]
    result = parse_act([page(lines)])
    assert result.lines_total == result.lines_consumed
    assert not any(n["type"] == "heading_group" for n in result.nodes)
    section = find(result.nodes, "section", "1")
    assert section["heading"] == "Short title"


def test_front_matter_skip_is_a_no_op_without_any_enacting_words():
    # A layout this doesn't recognise at all (or a test fixture with no
    # front matter) is used unchanged, rather than discarding the whole
    # document looking for a formula that was never going to appear.
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("The purposes of this Act are—", x0=HEAD_X0),
    ]
    result = parse_act([page(lines)])
    assert result.lines_total == result.lines_consumed
    assert not result.warnings
    section = find(result.nodes, "section", "1")
    assert section["heading"] == "Purposes"


def test_sub_subparagraph_nests_under_subparagraph():
    """Regression (Criminal Procedure Act, e.g. s. 41): bracketed capital
    letters -- "(A)", "(B)", "(C)" -- one level deeper than a
    subparagraph's lowercase roman numerals, per basic-structure.yaml's
    own note on this rare-but-real level. Case alone disambiguates it
    from paragraph (lowercase letters) and subparagraph (lowercase roman
    numerals), so there's no sequence-continuity ambiguity to resolve the
    way _bracket_level needs for those two."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("41 Contents of full brief", bold=True),
        line("(1) A full brief must contain—", x0=HEAD_X0),
        line("(a) a copy of—", x0=PARA_X0),
        line("(i) records of any medical examination; and", x0=SUBPARA_X0),
        line("(ii) a copy of—", x0=SUBPARA_X0),
        line("(A) records of any forensic procedure; and", x0=SUBPARA_X0 + 20),
        line("(B) the results of any tests.", x0=SUBPARA_X0 + 20),
    ]
    result = _parse(lines)
    sub_a = find(result.nodes, "sub_subparagraph", "A")
    assert "forensic procedure" in sub_a["text"]
    sub_b = find(result.nodes, "sub_subparagraph", "B")
    assert "results of any tests" in sub_b["text"]
    # Document order -- A and B nest right after subparagraph (ii), the
    # actual parent/child reconstruction (tree.py's annotate_paths) is
    # covered by tests/test_tree.py, not here.
    types_in_order = [n["type"] for n in result.nodes]
    subpara_ii_idx = next(i for i, n in enumerate(result.nodes) if n["type"] == "subparagraph" and n["number"] == "ii")
    assert types_in_order[subpara_ii_idx + 1 : subpara_ii_idx + 3] == ["sub_subparagraph", "sub_subparagraph"]


def test_bracket_paragraph_and_subparagraph_unaffected_by_sub_subparagraph_addition():
    """Regression guard: adding the sub_subparagraph pattern must not
    change how an ordinary lowercase paragraph/subparagraph pair (with no
    sub_subparagraph anywhere nearby) is classified."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Murder", bold=True),
        line("(1) A person who—", x0=HEAD_X0),
        line("(a) does X; or", x0=PARA_X0),
        line("(b) does Y—", x0=PARA_X0),
        line("(i) knowingly; or", x0=SUBPARA_X0),
        line("(ii) recklessly,", x0=SUBPARA_X0),
        line("is guilty of an offence.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    assert find(result.nodes, "paragraph", "a")
    assert find(result.nodes, "paragraph", "b")
    assert find(result.nodes, "subparagraph", "i")
    assert find(result.nodes, "subparagraph", "ii")
    assert not any(n["type"] == "sub_subparagraph" for n in result.nodes)


def test_example_marker_becomes_its_own_example_node():
    """Regression (Criminal Procedure Act, e.g. s. 41): a standalone bold
    "Example" line followed by prose is set exactly like a singular
    unnumbered "Note" -- same shape, different marker word (see
    basic-structure.yaml) -- and shares _handle_marked_block's own state
    machine rather than getting a separate implementation."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("41 Contents of full brief", bold=True),
        line("A full brief must contain a notice.", x0=HEAD_X0),
        line("Example", bold=True),
        line("The informant may agree with the accused's legal", x0=HEAD_X0),
        line("practitioner on a time and place for inspection.", x0=HEAD_X0),
        line("42 Contents of preliminary brief", bold=True),
        line("A preliminary brief must include the following.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    section_41 = find(result.nodes, "section", "41")
    assert section_41["text"] == "A full brief must contain a notice."
    example = find(result.nodes, "example", None)
    assert example["text"] == (
        "The informant may agree with the accused's legal practitioner on a time and place for inspection."
    )
    section_42 = find(result.nodes, "section", "42")
    assert "informant may agree" not in section_42["text"]


def test_example_ends_at_a_fresh_definition_start():
    """The same unnumbered-block gap as
    test_singular_unnumbered_note_ends_at_a_fresh_definition_start, but
    for an Example block instead of a Note."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("3 Definitions", bold=True),
        line("In this Act—", x0=HEAD_X0),
        line("sentence includes—", x0=HEAD_X0, leading_bold_italic="sentence"),
        line("(a) the recording of a conviction; and", x0=PARA_X0),
        line("Example", bold=True),
        line("A suspended sentence is still a sentence.", x0=HEAD_X0),
        line("sexual offence has the meaning given by section 4;", x0=HEAD_X0, leading_bold_italic="sexual offence"),
    ]
    result = _parse(lines)
    example = find(result.nodes, "example", None)
    assert example["text"] == "A suspended sentence is still a sentence."
    sexual_offence = [n for n in result.nodes if n["type"] == "definition" and n["heading"] == "sexual offence"][0]
    assert sexual_offence["text"] == "has the meaning given by section 4;"


def test_schedule_heading_opens_a_schedule_and_nests_its_own_sections():
    """Regression (Criminal Procedure Act Schedule 1): a Schedule heading
    uses the same bold "Word N—Title" shape as Part/Division, wraps
    across bold lines the same way, and its own numbered items reuse the
    ordinary "section" type (real Schedules number their own clauses "in
    the same way as sections", per basic-structure.yaml) rather than
    getting a schedule-specific type -- so the existing section/
    subsection/paragraph patterns already give a Schedule's substantive
    content full structural fidelity with no extra code."""
    lines = [
        line("Schedule 1––Charges on a charge-sheet", bold=True, size=16.0),
        line("or indictment", bold=True, size=16.0),
        line("Sections 6(3), 159(3)", x0=HEAD_X0, size=10.0),
        line("1 Statement of offence", bold=True),
        line("(1) A charge must contain a statement of the offence.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    schedule = find(result.nodes, "schedule", "1")
    assert schedule["heading"] == "Charges on a charge-sheet or indictment (Sections 6(3), 159(3))"
    assert schedule["text"] == ""
    section = find(result.nodes, "section", "1")
    assert section["heading"] == "Statement of offence"
    subsection = find(result.nodes, "subsection", "1")
    assert "statement of the offence" in subsection["text"]


def test_schedule_with_no_hangs_off_line_still_opens_its_first_section():
    """Not every Schedule names which section(s) it hangs off right under
    its own heading -- the fresh-start check for the first numbered item
    must still work with nothing but the heading itself in between."""
    lines = [
        line("Schedule 5—Transitional provisions", bold=True, size=16.0),
        line("1 Definitions", bold=True),
        line("In this Schedule—", x0=HEAD_X0),
    ]
    result = _parse(lines)
    find(result.nodes, "schedule", "5")
    section = find(result.nodes, "section", "1")
    assert section["heading"] == "Definitions"


def test_a_bare_schedule_number_takes_its_title_from_the_line_below():
    """Regression (Criminal Procedure Bill 2008): a Bill's introduction
    print sets "SCHEDULE 1" alone, then the sections it hangs off, then
    the title -- where an Act writes "Schedule 1--Title" on one line. Left
    undetected the Bill had no Schedules at all, and its Schedule clauses
    (which restart at 1) read as a second clause 1, 2, 3 in the body."""
    lines = [
        line("SCHEDULES", bold=True),
        line("SCHEDULE 1", bold=True, size=11.0),
        line("Sections 6(3), 159(3)", x0=HEAD_X0, size=10.0),
        line("CHARGES ON A CHARGE-SHEET OR INDICTMENT", bold=True, size=11.0),
        line("1 Statement of offence", bold=True),
        line("A charge must state the offence.", x0=HEAD_X0),
    ]
    result = _parse(lines)

    schedule = find(result.nodes, "schedule", "1")
    assert schedule["heading"] == "CHARGES ON A CHARGE-SHEET OR INDICTMENT (Sections 6(3), 159(3))"
    assert find(result.nodes, "section", "1")["heading"] == "Statement of offence"


def test_a_bare_schedule_title_that_wraps_keeps_the_hangs_off_note_last():
    # The note is printed between the number and the title, so it has to
    # be held until the title -- which can wrap over two bold lines -- has
    # finished arriving, rather than appended as it comes.
    lines = [
        line("SCHEDULE 2", bold=True, size=11.0),
        line("Section 28(1)", x0=HEAD_X0, size=10.0),
        line("INDICTABLE OFFENCES THAT MAY BE HEARD AND", bold=True, size=11.0),
        line("DETERMINED SUMMARILY", bold=True, size=11.0),
        line("1 Common law", bold=True),
        line("Offences at common law of conspiracy to cheat.", x0=HEAD_X0),
    ]
    result = _parse(lines)

    assert find(result.nodes, "schedule", "2")["heading"] == (
        "INDICTABLE OFFENCES THAT MAY BE HEARD AND DETERMINED SUMMARILY (Section 28(1))"
    )


def test_a_bare_schedule_heading_ends_an_open_notes_block():
    # The Schedules follow the last clause of the Bill's body, which can
    # end in a Note -- and a note block swallows everything that isn't a
    # recognised boundary, which is how the very first Schedule went
    # missing while the other two were found.
    lines = [
        line("385 Repeal of Chapter", bold=True),
        line("Note", bold=True),
        line("The repeal does not affect the continuing operation of the amendments.", x0=HEAD_X0),
        line("SCHEDULE 1", bold=True, size=11.0),
        line("CHARGES ON A CHARGE-SHEET", bold=True, size=11.0),
        line("1 Statement of offence", bold=True),
        line("A charge must state the offence.", x0=HEAD_X0),
    ]
    result = _parse(lines)

    schedule = find(result.nodes, "schedule", "1")
    assert schedule["heading"] == "CHARGES ON A CHARGE-SHEET"
    assert "SCHEDULE 1" not in find(result.nodes, "note")["text"]


def test_schedule_own_items_do_not_collide_with_earlier_act_sections():
    """A Schedule's own "1", "2", ... numbering restarts independently of
    the Act's own section numbers -- both must coexist as distinct nodes
    rather than one overwriting or merging into the other."""
    lines = [
        line("Part I—Preliminary", bold=True),
        line("1 Purposes", bold=True),
        line("The purposes of this Act are stated here.", x0=HEAD_X0),
        line("Schedule 1—Forms", bold=True, size=16.0),
        line("1 Form of charge-sheet", bold=True),
        line("A charge-sheet must be in this form.", x0=HEAD_X0),
    ]
    result = _parse(lines)
    act_section = find(result.nodes, "section", "1")
    assert "purposes of this Act" in act_section["text"]
    schedule_items = [n for n in result.nodes if n["type"] == "section" and n["number"] == "1"]
    assert len(schedule_items) == 2
    assert any("Form of charge-sheet" == n["heading"] for n in schedule_items)


# ---------------------------------------------------------------------
# A printed line break is not part of the legislation
# ---------------------------------------------------------------------
def test_a_wrapped_line_becomes_running_prose():
    """The break is where the PDF's column ran out, not something the Act
    says. Keeping it left every consumer to undo it, and made the stored
    text disagree with the same words quoted anywhere else."""
    lines = [
        line("Part I—Offences", bold=True),
        line("1 Murder", bold=True),
        line("A person who commits murder is guilty of", x0=HEAD_X0),
        line("an indictable offence.", x0=WRAP_X0),
    ]
    section = find(_parse(lines).nodes, "section", "1")

    assert section["text"] == "A person who commits murder is guilty of an indictable offence."


def test_a_line_broken_at_a_hyphen_closes_up():
    """Every hyphen-ending line across this project's corpus breaks a
    compound the words already contained -- "charge-sheet",
    "cross-examine" -- never a word split for fit."""
    lines = [
        line("Part I—Offences", bold=True),
        line("1 Commencement", bold=True),
        line("The informant must file the charge-", x0=HEAD_X0),
        line("sheet within 12 months.", x0=WRAP_X0),
    ]
    section = find(_parse(lines).nodes, "section", "1")

    assert section["text"] == "The informant must file the charge-sheet within 12 months."


# ---------------------------------------------------------------------
# A bracket after a comma continues a sentence
# ---------------------------------------------------------------------
def test_a_wrapped_list_of_references_is_not_a_new_provision():
    """"a provision of Subdivision (8A), (8B)," / "(8C), (8D) ..." used to
    open a subsection numbered 8C in the middle of a sentence, taking the
    rest of that sentence with it -- which is what stopped the Criminal
    Procedure Act's section 4(1)(a)(i), (ii) and (iii) separating."""
    lines = [
        line("Part I—Offences", bold=True),
        line("4 Meaning of sexual offence", bold=True),
        line("(1) In this Act, sexual offence means—", x0=HEAD_X0),
        line("(a) an offence against—", x0=PARA_X0),
        line("(i) a provision of Subdivision (8A), (8B),", x0=SUBPARA_X0),
        line("(8C), (8D) or (8E) of the Crimes Act 1958; or", x0=SUBPARA_X0),
        line("(ii) section 327(2) of that Act.", x0=SUBPARA_X0),
    ]
    nodes = _parse(lines).nodes

    assert not any(n["type"] == "subsection" and n.get("number") == "8C" for n in nodes)
    first = find(nodes, "subparagraph", "i")
    assert first["text"] == (
        "a provision of Subdivision (8A), (8B), (8C), (8D) or (8E) of the Crimes Act 1958; or"
    )
    assert find(nodes, "subparagraph", "ii")["text"] == "section 327(2) of that Act."


# ---------------------------------------------------------------------
# An inserted paragraph is still a sibling
# ---------------------------------------------------------------------
def test_a_paragraph_inserted_by_an_amendment_keeps_its_siblings_in_line():
    """An amending Act inserts between (a) and (b) by suffixing: (a),
    (ab), (b). Reading only (ac) as (ab)'s sibling made (b) a
    subparagraph of it -- and then (b)'s own (i), (ii) paragraphs."""
    lines = [
        line("Part I—Offences", bold=True),
        line("4 Meaning of sexual offence", bold=True),
        line("(1) In this Act, sexual offence means—", x0=HEAD_X0),
        line("(a) an offence against the person; or", x0=PARA_X0),
        line("(ab) an intimate image offence; or", x0=PARA_X0),
        line("(b) an offence an element of which involves—", x0=PARA_X0),
        line("(i) any person engaging in sexual activity; or", x0=SUBPARA_X0),
        line("(ii) any person taking part in a sexual act.", x0=SUBPARA_X0),
    ]
    nodes = _parse(lines).nodes
    kinds = {(n["type"], n.get("number")) for n in nodes}

    assert ("paragraph", "ab") in kinds
    assert ("paragraph", "b") in kinds, "the original next paragraph, after an insertion"
    assert ("subparagraph", "i") in kinds and ("subparagraph", "ii") in kinds


# ---------------------------------------------------------------------
# Definitions announced part-way through a section
# ---------------------------------------------------------------------
def _definitions_in_a_subsection():
    return [
        line("Part I—Offences", bold=True),
        line("4 Meaning of sexual offence", bold=True),
        line("(1) An offence is a sexual offence if it is listed.", x0=HEAD_X0),
        line("(6) In this section—", x0=HEAD_X0),
        line("commercial sexual services has the meaning given by", x0=PARA_X0,
             leading_bold_italic="commercial sexual services"),
        line("section 35(1) of the Crimes Act 1958;", x0=PARA_WRAP_X0),
        line("sexual performance has the meaning given by section", x0=PARA_X0,
             leading_bold_italic="sexual performance"),
        line("49Q(3) of that Act.", x0=PARA_WRAP_X0),
    ]


def test_a_lead_in_inside_a_subsection_opens_its_definitions():
    """The Criminal Procedure Act's section 4 is headed "Meaning of sexual
    offence" and puts four defined terms in its subsection (6). Nothing in
    the heading announces them, so the lead-in is the only announcement
    there is -- without it they arrive as one unbroken block of text."""
    nodes = _parse(_definitions_in_a_subsection()).nodes
    terms = [n["heading"] for n in nodes if n["type"] == "definition"]

    assert terms == ["commercial sexual services", "sexual performance"]
    assert find(nodes, "definition")["text"] == (
        "has the meaning given by section 35(1) of the Crimes Act 1958;"
    )


def test_those_definitions_nest_under_the_subsection_that_introduced_them():
    """They belong to subsection (6), not beside it: a definition's depth
    depends on what introduced it, which is why the parser records it on
    the node rather than leaving it to be inferred from the type."""
    from corpus.exporters.akn_export import build_hierarchy_tree
    from corpus.domain.hierarchy import HIERARCHY_ORDER

    nodes = _parse(_definitions_in_a_subsection()).nodes
    roots, _collisions = build_hierarchy_tree(nodes, HIERARCHY_ORDER)

    def walk(tree_node):
        if tree_node["node"] and tree_node["node"].get("number") == "6":
            return tree_node
        for child in tree_node["children"]:
            found = walk(child)
            if found:
                return found

    subsection_6 = next(filter(None, (walk(r) for r in roots)))
    assert [c["node"]["heading"] for c in subsection_6["children"]] == [
        "commercial sexual services", "sexual performance",
    ]


def test_a_topical_heading_is_not_swallowed_by_an_open_definitions_run():
    """A run of definitions stays open until the next section, so one of
    these headings can arrive first. It would otherwise be taken for a
    defined term and take the rest of the Act's structure with it."""
    lines = _definitions_in_a_subsection() + [
        line("Fingerprinting", bold=True, leading_bold_italic="Fingerprinting"),
        line("5 Taking of fingerprints", bold=True),
        line("A member of the force may take fingerprints.", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes

    assert any(n["type"] == "heading_group" and n["heading"] == "Fingerprinting" for n in nodes)
    assert "Fingerprinting" not in [n["heading"] for n in nodes if n["type"] == "definition"]


# ---------------------------------------------------------------------
# Printed lists and wrap-up text
# ---------------------------------------------------------------------
def test_a_bulleted_line_starts_its_own_item():
    """Joining these into the prose gave "as follows—• evidence relevant
    to ... ; • a summary of ..." -- the bullets stranded mid-paragraph,
    one glued tight to the dash before it and the next taking a space.
    A break before a bullet is the one kind the text keeps."""
    lines = [
        line("Part I—Offences", bold=True),
        line("1 Directions", bold=True),
        line("Counsel must inform the judge of each element in", x0=HEAD_X0),
        line("issue, including—", x0=WRAP_X0),
        line("• whether the act was a dangerous act; and", x0=WRAP_X0),
        line("• whether the act caused death.", x0=WRAP_X0),
    ]
    section = find(_parse(lines).nodes, "section", "1")

    assert section["text"].split("\n") == [
        "Counsel must inform the judge of each element in issue, including—",
        "• whether the act was a dangerous act; and",
        "• whether the act caused death.",
    ]


def test_a_bullet_printed_alone_on_its_line_keeps_its_words():
    """The Crimes Act sets some of these with the marker on one line and
    the item on the next."""
    lines = [
        line("Part I—Offences", bold=True),
        line("1 Directions", bold=True),
        line("The matters in issue include—", x0=HEAD_X0),
        line("•", x0=WRAP_X0),
        line("whether the act was a dangerous act; and", x0=WRAP_X0),
    ]
    section = find(_parse(lines).nodes, "section", "1")

    assert section["text"].split("\n") == [
        "The matters in issue include—",
        "• whether the act was a dangerous act; and",
    ]


def test_an_em_dash_at_the_end_of_a_line_does_not_close_up():
    """Unlike a hyphen, it opens a list rather than breaking a word: "an
    offence described as being—" followed by its own list, then the rest
    of the sentence, must not come back as "being—in either case"."""
    lines = [
        line("Part I—Offences", bold=True),
        line("1 Directions", bold=True),
        line("An offence described as being—", x0=HEAD_X0),
        line("in either case, an indictable offence.", x0=HEAD_X0),
    ]
    section = find(_parse(lines).nodes, "section", "1")

    assert section["text"] == "An offence described as being— in either case, an indictable offence."


# ---------------------------------------------------------------------
# Penalties
#
# Victorian drafting sets the penalty for an offence on its own line
# under the provision creating it. Read as more of that provision's text,
# what the offence forbids and what happens to you if you do it run
# together into one block, and the second becomes unfindable.
# ---------------------------------------------------------------------

def test_a_penalty_becomes_its_own_node():
    lines = [
        line("Part I—Offences", bold=True),
        line("Division 1—Offences against the person", bold=True),
        line("3 Punishment for murder", bold=True),
        line("A person who commits murder is guilty of an indictable offence.", x0=HEAD_X0),
        line("Penalty: Level 2 imprisonment (25 years maximum).", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes

    penalty = find(nodes, "penalty")
    assert penalty["text"] == "Penalty: Level 2 imprisonment (25 years maximum)."
    section = find(nodes, "section", "3")
    assert "Penalty" not in section["text"]


def test_a_penalty_keeps_its_wrapped_lines():
    lines = [
        line("Part I—Offences", bold=True),
        line("3 Punishment for murder", bold=True),
        line("A person who commits murder is guilty of an offence.", x0=HEAD_X0),
        line("Penalty: Level 2 imprisonment (25 years", x0=HEAD_X0),
        line("maximum).", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes

    assert find(nodes, "penalty")["text"] == "Penalty: Level 2 imprisonment (25 years maximum)."


def test_a_penalty_line_starting_with_a_number_does_not_end_the_penalty():
    """The regression this was written for: a penalty's own wording
    starts lines with numbers constantly ("1200 penalty units maximum)"),
    and the section pattern is "a number, then some words" -- so half of
    them were read as a new section and the penalty was cut off
    mid-sentence. In the ordinary flow that pattern only opens a section
    on a *bold* line."""
    lines = [
        line("Part I—Offences", bold=True),
        line("3 Punishment", bold=True),
        line("A person who does this is guilty of an offence.", x0=HEAD_X0),
        line("Penalty: In the case of an individual, a level 5 fine", x0=HEAD_X0),
        line("1200 penalty units maximum) or both.", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes

    assert find(nodes, "penalty")["text"].endswith("1200 penalty units maximum) or both.")
    assert not [n for n in nodes if n["type"] == "section" and n.get("number") == "1200"]


def test_a_penalty_ends_at_the_next_bold_heading():
    """A bare topical caption matches no structural pattern at all, so
    only its boldness gives it away -- without that, a penalty at the
    foot of a group's last provision swallowed the caption introducing
    the next one."""
    lines = [
        line("Part I—Offences", bold=True),
        line("3 Punishment", bold=True),
        line("A person who does this is guilty of an offence.", x0=HEAD_X0),
        line("Penalty: 25 penalty units.", x0=HEAD_X0),
        line("Offences relating to Horse-drawn Vehicles, Public Vehicles, Animals, &c.", bold=True),
        line("4 Another offence", bold=True),
        line("Text.", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes

    assert find(nodes, "penalty")["text"] == "Penalty: 25 penalty units."
    assert find(nodes, "section", "4")["heading"] == "Another offence"


def test_a_penalty_ends_at_the_next_bracketed_subsection():
    lines = [
        line("Part I—Offences", bold=True),
        line("3 Punishment", bold=True),
        line("(1) A person who does this is guilty of an offence.", x0=HEAD_X0),
        line("Penalty: 25 penalty units.", x0=HEAD_X0),
        line("(2) In this section, this means that.", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes

    assert find(nodes, "penalty")["text"] == "Penalty: 25 penalty units."
    assert find(nodes, "subsection", "2")["text"] == "In this section, this means that."


def test_the_word_penalty_mid_sentence_is_not_a_penalty():
    """"penalty" is everywhere in ordinary legislative prose. Every real
    penalty line starts one and is capitalised and followed by a colon;
    none of the prose uses is."""
    lines = [
        line("Part I—Offences", bold=True),
        line("3 Recovery", bold=True),
        line("Where a penalty is prescribed by law, the person shall pay it, and the", x0=HEAD_X0),
        line("penalty must be recovered only before the Magistrates' Court.", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes

    assert not [n for n in nodes if n["type"] == "penalty"]
    assert "penalty must be recovered" in find(nodes, "section", "3")["text"]


def test_a_penalty_belongs_to_its_sections_review_unit():
    """Like a note: it is a fact about the provision above it, not a
    container and not a boundary, so it is reviewed alongside the
    offence it attaches to."""
    from corpus.domain.hierarchy import group_into_units

    lines = [
        line("Part I—Offences", bold=True),
        line("3 Punishment", bold=True),
        line("A person who does this is guilty of an offence.", x0=HEAD_X0),
        line("Penalty: 25 penalty units.", x0=HEAD_X0),
    ]
    nodes = _parse(lines).nodes
    units = group_into_units(nodes)

    section_unit = next(u for u in units if nodes[u[0]]["type"] == "section")
    assert [nodes[i]["type"] for i in section_unit] == ["section", "penalty"]


# ---------------------------------------------------------------------
# Where a provision is printed
#
# A node is built from lines that each know exactly where they are, and
# all of it used to be thrown away the moment their text was joined -- a
# node remembered which pages it spanned and nothing else. There was
# nothing to draw, so the parse could only be reviewed as text beside a
# picture of the page, never on it.
# ---------------------------------------------------------------------

def test_every_node_knows_where_it_is_printed():
    lines = [
        line("Part I—Offences", bold=True, y0=100),
        line("3 Punishment for murder", bold=True, y0=130),
        line("(1) A person who commits murder", x0=HEAD_X0, y0=142),
        line("is guilty of an offence.", x0=HEAD_X0, y0=154),
    ]
    nodes = _parse(lines).nodes

    assert all(n.get("rects") for n in nodes), [n["type"] for n in nodes if not n.get("rects")]
    assert find(nodes, "part", "I")["rects"] == [
        {"page": 1, "x0": HEAD_X0, "y0": 100.0, "x1": HEAD_X0 + 200.0, "y1": 110.0},
    ]


def test_a_provisions_lines_become_one_box():
    """One box per run, not one per line: what a reader wants to see is a
    box around the provision."""
    lines = [
        line("Part I—Offences", bold=True, y0=100),
        line("3 Punishment", bold=True, y0=130),
        line("(1) A person who does this", x0=HEAD_X0, y0=142, x1=HEAD_X0 + 180),
        line("is guilty of an offence.", x0=HEAD_X0 + 10, y0=154, x1=HEAD_X0 + 240),
    ]
    rects = find(_parse(lines).nodes, "subsection", "1")["rects"]

    assert rects == [{"page": 1, "x0": HEAD_X0, "y0": 142.0, "x1": HEAD_X0 + 240.0, "y1": 164.0}]


def test_lines_far_apart_are_two_boxes():
    """A provision whose own text resumes below something that
    interrupted it is two runs, and two boxes -- which is also the shape
    a reviewer needs for a continuation that resumes in more than one
    place."""
    lines = [
        line("Part I—Offences", bold=True, y0=100),
        line("3 Punishment", bold=True, y0=130),
        line("Text at the top.", x0=HEAD_X0, y0=142),
        line("Text much further down.", x0=HEAD_X0, y0=400),
    ]
    rects = find(_parse(lines).nodes, "section", "3")["rects"]

    assert len(rects) == 2
    assert [r["y0"] for r in rects] == [130.0, 400.0]


def test_a_box_never_spans_two_pages():
    lines = [
        line("Part I—Offences", bold=True, y0=100),
        line("3 Punishment", bold=True, y0=700),
        line("carried over to the next page.", x0=HEAD_X0, y0=120, page_no=2),
    ]
    rects = find(_parse(lines).nodes, "section", "3")["rects"]

    assert [r["page"] for r in rects] == [1, 2]


def test_a_repealed_marker_is_boxed_across_its_asterisks():
    lines = [
        line("Part I—Offences", bold=True, y0=100),
        line("3 Punishment", bold=True, y0=130),
        line("*", x0=200, y0=142, x1=206),
        line("*", x0=260, y0=142, x1=266),
        line("*", x0=320, y0=142, x1=326),
    ]
    rects = find(_parse(lines).nodes, "repealed")["rects"]

    assert rects == [{"page": 1, "x0": 200.0, "y0": 142.0, "x1": 326.0, "y1": 152.0}]


def test_a_bold_act_name_inside_a_note_does_not_end_the_note():
    """Criminal Procedure Act s 6. This drafting sets an Act's name bold
    wherever it is cited, and a Note is set two points smaller than body
    text -- so where the citation is most of the line, the line's
    dominant weight is bold, at note size.

    Read as a heading, that line ended the Notes block: it fell through
    to paragraph (c) and was glued onto the end of it, note 2 was never
    recognised as a note at all, and its own "2" leaked into the text of
    whatever was left holding it. All three from one bold line."""
    lines = [
        line("Part I—Offences", bold=True, y0=100),
        line("6 Commencement", bold=True, y0=130),
        line("(1) A criminal proceeding is commenced—", x0=HEAD_X0, y0=142),
        line("(a) by filing a charge-sheet containing a charge", x0=PARA_X0, y0=154),
        line("in the Magistrates' Court; or", x0=PARA_WRAP_X0, y0=166),
        line("(b) if the accused is arrested without a warrant", x0=PARA_X0, y0=178),
        line("and is released on bail, by filing a charge-sheet", x0=PARA_WRAP_X0, y0=190),
        line("with a bail justice; or", x0=PARA_WRAP_X0, y0=202),
        line("(c) if a summons is issued under section 14, at", x0=PARA_X0, y0=214),
        line("the time the charge-sheet is signed.", x0=PARA_WRAP_X0, y0=226),
        line("Notes", bold=True, size=10.0, y0=246),
        line("1", size=10.0, y0=258),
        line("A criminal proceeding against a child is commenced in", x0=PARA_X0, size=10.0, y0=258),
        line("the same manner in the Children's Court: section 528 of", x0=PARA_X0, size=10.0, y0=270),
        line("the Children, Youth and Families Act 2005.", x0=PARA_X0, size=10.0, y0=282, bold=True),
        line("2", size=10.0, y0=294),
        line("In the case of a criminal proceeding for an alleged", x0=PARA_X0, size=10.0, y0=294),
        line("offence by a child, a record of reasons must be filed.", x0=PARA_X0, size=10.0, y0=306),
    ]
    nodes = _parse(lines).nodes

    assert find(nodes, "paragraph", "c")["text"] == (
        "if a summons is issued under section 14, at the time the charge-sheet is signed."
    )
    assert find(nodes, "note", "1")["text"].endswith("the Children, Youth and Families Act 2005.")
    assert find(nodes, "note", "2")["text"].startswith("In the case of")
    assert not [n for n in nodes if n["type"] == "continuation"]


def test_a_bold_caption_at_body_size_still_ends_a_penalty():
    """The other half of the same rule, and what it was added for: a
    bare topical caption matches no structural pattern at all, so only
    its weight gives it away -- and it is set at body size, which a
    note's own emphasis never is."""
    lines = [
        line("Part I—Offences", bold=True, y0=100),
        line("7 Punishment", bold=True, y0=130),
        line("A person who does this is guilty of an offence.", x0=HEAD_X0, y0=142),
        line("Penalty: 25 penalty units.", x0=HEAD_X0, y0=160),
        line("Offences relating to Horse-drawn Vehicles, &c.", bold=True, y0=180),
        line("8 Another offence", bold=True, y0=200),
        line("Text.", x0=HEAD_X0, y0=212),
    ]
    nodes = _parse(lines).nodes

    assert find(nodes, "penalty")["text"] == "Penalty: 25 penalty units."
    assert find(nodes, "section", "8")["heading"] == "Another offence"


# ---------------------------------------------------------------------
# Ragged-right: a line that breaks early starts something new; a full
# one that stopped mid-sentence is wrapping (Criminal Procedure Act v22)
# ---------------------------------------------------------------------

MARGIN = 456.0


def _section(*body):
    return [line("Part I—Offences", bold=True, x1=300), line("83 Admissibility", bold=True, x1=300), *body]


def test_a_reference_wrapped_onto_a_new_line_is_not_a_new_provision():
    """"...subject to subsections (2) and" / "(3), admissible as if..." --
    the line above ran to the margin, so "(3)" did not fit on it."""
    nodes = _parse(_section(
        line("(1) The following are, subject to subsections (2) and", x0=HEAD_X0, x1=MARGIN - 3),
        line("(3), admissible as if their contents were evidence.", x0=WRAP_X0, x1=440),
        line("(2) A statement must be signed.", x0=HEAD_X0, x1=380),
        line("(3) A copy must be served.", x0=HEAD_X0, x1=350),
    )).nodes

    assert [n["number"] for n in nodes if n["type"] == "subsection"] == ["1", "2", "3"]
    assert find(nodes, "subsection", "1")["text"].endswith("subsections (2) and (3), admissible as if their contents were evidence.")


def test_a_pinpoint_reference_ending_a_line_does_not_open_its_subsection_again():
    nodes = _parse(_section(
        line("(1) The Court may strike out an appeal.", x0=HEAD_X0, x1=400),
        line("(2) If an appeal is struck out under subsection", x0=HEAD_X0, x1=MARGIN - 1),
        line("(1)(a)—", x0=WRAP_X0, x1=250),
        line("the appellant may apply to reinstate it.", x0=WRAP_X0, x1=420),
    )).nodes

    assert [n["number"] for n in nodes if n["type"] == "subsection"] == ["1", "2"]


def test_an_item_after_an_or_on_its_own_line_still_opens():
    """The line above stopped with room to spare: the break was the
    drafter's."""
    nodes = _parse(_section(
        line("(1) A person is in custody if the person is—", x0=HEAD_X0, x1=MARGIN),
        line("(a) in a prison in the legal custody of the Secretary;", x0=PARA_X0, x1=MARGIN - 2),
        line("or", x0=PARA_WRAP_X0, x1=274),
        line("(b) in custody in a police gaol in the legal custody", x0=PARA_X0, x1=MARGIN - 4),
        line("of the Chief Commissioner.", x0=PARA_WRAP_X0, x1=380),
    )).nodes

    assert [n["number"] for n in nodes if n["type"] == "paragraph"] == ["a", "b"]


def test_a_full_line_that_ends_its_sentence_is_followed_by_a_new_provision():
    nodes = _parse(_section(
        line("(1) The additional evidence is inadmissible unless the court is satisfied—", x0=HEAD_X0, x1=MARGIN),
        line("(a) that it is relevant; and", x0=PARA_X0, x1=MARGIN - 1),
        line("(b) that it is reliable.", x0=PARA_X0, x1=360),
    )).nodes

    assert [n["number"] for n in nodes if n["type"] == "paragraph"] == ["a", "b"]


def test_a_provision_after_omitted_text_still_opens():
    nodes = _parse(_section(
        line("(1) This section applies to a charge.", x0=HEAD_X0, x1=400),
        line("*", x0=434, x1=MARGIN - 13),
        line("(3) Subject to subsection (4), the remaining provisions apply.", x0=HEAD_X0, x1=MARGIN),
    )).nodes

    assert [n["number"] for n in nodes if n["type"] == "subsection"] == ["1", "3"]


def test_an_act_year_wrapped_in_a_note_is_not_a_new_note():
    nodes = _parse(_section(
        line("(1) Evidence may be recorded.", x0=HEAD_X0, x1=380),
        line("Note", bold=True, size=10.0, x1=230),
        # Bold: the line is mostly the Act's name, which a Note prints bold.
        line("Part VI of the Evidence (Miscellaneous Provisions) Act", x0=PARA_X0, size=10.0, x1=MARGIN - 2, bold=True),
        line("1958 provides for the recording of evidence.", x0=PARA_X0, size=10.0, x1=420),
        line("(2) A recording is admissible.", x0=HEAD_X0, x1=360),
    )).nodes

    [note] = [n for n in nodes if n["type"] == "note"]
    assert note["number"] is None and note["text"].endswith("Act 1958 provides for the recording of evidence.")
    assert [n["number"] for n in nodes if n["type"] == "subsection"] == ["1", "2"]


def test_a_reference_wrapped_in_a_note_stays_in_the_note():
    nodes = _parse(_section(
        line("(1) Evidence may be recorded.", x0=HEAD_X0, x1=380),
        line("Note", bold=True, size=10.0, x1=230),
        line("An order may also be made under section 5 and subsection", x0=PARA_X0, size=10.0, x1=MARGIN - 1),
        line("(2) of that section applies to it.", x0=PARA_X0, size=10.0, x1=360),
        line("(2) A recording is admissible.", x0=HEAD_X0, x1=360),
    )).nodes

    [note] = [n for n in nodes if n["type"] == "note"]
    assert note["text"].endswith("subsection (2) of that section applies to it.")
    assert [n["number"] for n in nodes if n["type"] == "subsection"] == ["1", "2"]


# ---------------------------------------------------------------------
# Repealed rows (Criminal Procedure Act v114 s 124(4), s 3)
# ---------------------------------------------------------------------

def _stars(y0):
    return [line("*", x0=x, x1=x + 9, y0=y0) for x in (207, 263, 320, 377, 433)]


def test_each_row_of_stars_is_one_repealed_provision():
    nodes = _parse(_section(
        line("(1) The court may make an order.", x0=HEAD_X0, x1=400, y0=100),
        *_stars(120), *_stars(140),
        line("(4) The order may be varied.", x0=HEAD_X0, x1=380, y0=160),
    )).nodes

    assert [n["text"] for n in nodes if n["type"] == "repealed"] == ["* * * * *", "* * * * *"]


def test_a_list_carries_on_past_a_repealed_item():
    """(b), a repealed (c), then (d): "d" is a roman numeral too, and read
    as one it opened a subparagraph list under (b)."""
    nodes = _parse(_section(
        line("(4) In determining whether—", x0=HEAD_X0, x1=300, y0=100),
        line("(a) the case is disclosed; and", x0=PARA_X0, x1=400, y0=120),
        line("(b) the issues are defined; and", x0=PARA_X0, x1=400, y0=140),
        *_stars(160),
        line("(d) a fair trial will take place; and", x0=PARA_X0, x1=420, y0=180),
        line("(e) a plea is clarified.", x0=PARA_X0, x1=360, y0=200),
    )).nodes

    assert [(n["type"], n["number"]) for n in nodes if n["type"] in ("paragraph", "subparagraph")] == [
        ("paragraph", "a"), ("paragraph", "b"), ("paragraph", "d"), ("paragraph", "e")]


def test_notes_numbered_in_the_margin_keep_their_own_lines():
    """s 124's notes, in the order extraction now gives them: each number
    before its note's first line."""
    nodes = _parse(_section(
        line("(4) The court must have regard to the evidence.", x0=HEAD_X0, x1=420),
        line("Notes", bold=True, size=10.0, x0=210, x1=236),
        line("1", size=10.0, x0=210, x1=217),
        line("Section 102 of the Evidence Act 2008 provides that", x0=230, size=10.0, x1=442),
        line("credibility evidence is not admissible.", x0=230, size=10.0, x1=400),
        line("2", size=10.0, x0=210, x1=217),
        line("Section 103(1) of the Evidence Act 2008 provides that", x0=230, size=10.0, x1=453),
        line("the credibility rule does not apply.", x0=230, size=10.0, x1=380),
    )).nodes

    notes = [(n["number"], n["text"][:14]) for n in nodes if n["type"] == "note"]
    assert notes == [("1", "Section 102 of"), ("2", "Section 103(1)")]


def test_a_preamble_is_its_own_provision_and_the_identity_block_goes():
    """Family Violence Protection Act 2008: its Preamble comes before
    "The Parliament of Victoria therefore enacts:", which the front-matter
    skip did not know -- so the Authorised Version block and the Preamble
    both landed in a made-up "Preliminary" Part."""
    nodes = _parse([
        line("Authorised Version No. 068", bold=True, x1=300),
        line("Family Violence Protection Act 2008", bold=True, x1=300),
        line("Preamble", bold=True, x0=280, x1=330),
        line("In enacting this Act, the Parliament recognises the following principles—", x0=HEAD_X0, x1=MARGIN),
        line("(a) that non-violence is a fundamental social value;", x0=PARA_X0, x1=420),
        line("(b) that family violence is unacceptable in any form.", x0=PARA_X0, x1=430),
        line("The Parliament of Victoria therefore enacts:", x0=HEAD_X0, x1=380),
        line("Part 1—Preliminary", bold=True, size=16.0, x1=300),
        line("1 Purpose", bold=True, x1=250),
        line("The purpose of this Act is to maximise safety.", x0=WRAP_X0, x1=420),
    ]).nodes

    assert [(n["type"], n.get("number"), n.get("heading")) for n in nodes][:4] == [
        ("preamble", None, "Preamble"), ("paragraph", "a", None), ("paragraph", "b", None), ("part", "1", "Preliminary")]
    assert nodes[0]["text"].startswith("In enacting this Act")
    assert not any("Authorised Version" in (n.get("heading") or "") + (n.get("text") or "") for n in nodes)


def test_a_preamble_reads_as_its_own_page():
    from corpus.parsing.identity import annotate_ids
    from corpus.publishing import html_view

    result = _parse([
        line("Preamble", bold=True, x0=280, x1=330),
        line("In enacting this Act, the Parliament recognises—", x0=HEAD_X0, x1=MARGIN),
        line("(a) that non-violence is a fundamental social value.", x0=PARA_X0, x1=420),
        line("The Parliament of Victoria therefore enacts:", x0=HEAD_X0, x1=380),
        line("Part 1—Preliminary", bold=True, size=16.0, x1=300),
        line("1 Purpose", bold=True, x1=250),
        line("The purpose of this Act is to maximise safety.", x0=WRAP_X0, x1=420),
    ])
    annotate_ids(result.nodes, result.hierarchy)
    parsed = {"nodes": result.nodes, "hierarchy": result.hierarchy, "fingerprint": "fp"}

    assert '<a href="/section/preamble">Preamble</a>' in html_view.render_index(parsed, "An Act 2008", "")
    assert "non-violence" in html_view.render_section(parsed, "An Act 2008", "", "preamble")


def test_dot_point_examples_are_one_example_shown_as_a_list():
    """Family Violence Protection Act s 6: "Examples—", then dot points.
    The dash kept the heading from being recognised, so the dot points
    ran on as part of paragraph (b)."""
    from corpus.parsing.identity import annotate_ids
    from corpus.publishing import html_view

    result = _parse([
        line("Part 1—Preliminary", bold=True, size=16.0, x1=300),
        line("6 Meaning of economic abuse", bold=True, x1=330),
        line("Economic abuse is behaviour that is coercive—", x0=WRAP_X0, x1=MARGIN),
        line("(a) in a way that denies autonomy; or", x0=PARA_X0, x1=400),
        line("(b) by withholding financial support.", x0=PARA_X0, x1=380),
        line("Examples—", bold=True, size=10.0, x0=184, x1=236),
        line("• coercing a person to relinquish control over assets and", size=10.0, x0=197, x1=440),
        line("income;", size=10.0, x0=210, x1=250),
        line("• removing a family member's property without permission.", size=10.0, x0=197, x1=450),
    ])
    [example] = [n for n in result.nodes if n["type"] == "example"]
    assert find(result.nodes, "paragraph", "b")["text"] == "by withholding financial support."

    annotate_ids(result.nodes, result.hierarchy)
    page = html_view.render_section({"nodes": result.nodes, "hierarchy": result.hierarchy, "fingerprint": "fp"},
                                    "An Act 2008", "", "s6")
    assert '<span class="prov-text">Examples</span>' in page
    assert ('<ul class="prov-bullets"><li>coercing a person to relinquish control over assets and income;</li>'
            "<li>removing a family member&#x27;s property without permission.</li></ul>") in page
