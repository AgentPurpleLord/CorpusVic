"""
Tests for the deterministic rules engine (ai_pipeline/rule_parser.py).

Each regression test here corresponds to a real bug found and fixed against
actual Crimes Act / Interpretation of Legislation Act text during this
project -- the synthetic snippets are trimmed-down, minimal reproductions
of the real page content that exposed each bug, not made-up shapes.
"""
from ai_pipeline.rule_parser import parse_act

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


def test_hanging_list_reattaches_trailing_clause_to_lead_in():
    """The headline case this project was built to fix: "(1) A person
    who -- (a) does X; or (b) does Y -- is guilty of an offence." The
    trailing independent clause grammatically resumes subsection (1)'s
    own lead-in sentence, not paragraph (b)'s -- and the PDF's own
    hanging indent (the trailing clause outdents back past the
    Paragraph's own wrap indent) is what tells them apart."""
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
    assert "shall be guilty" in subsection2["text"]
    assert "shall be guilty" not in paragraph_b["text"]
    assert paragraph_b["text"].rstrip().endswith("suicide—")


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
