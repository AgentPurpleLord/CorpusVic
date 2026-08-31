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
    assert definition["text"] == "means the period permitted by or\nunder this Act for commencing an appeal;"


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
    assert result.hierarchy[0] == "chapter"  # available, just unused


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
    assert note["text"] == "See the definitions of originating court and original\njurisdiction in section 3."
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


def test_skip_front_matter_discards_bills_table_of_provisions():
    """A Bill's introduction print opens with a title page and a multi-
    page Table of Provisions whose rows repeat real Part/clause headings
    closely enough to fool the heading classifiers -- skip_front_matter
    discards everything up to the fixed enacting words every Bill's real
    text opens with, rather than trying to parse the TOC as structure."""
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
    result = parse_act([page(lines)], top_level_type="clause", skip_front_matter=True)
    assert result.lines_total == result.lines_consumed
    assert any("skipped 8 front-matter line" in w for w in result.warnings)
    clause = find(result.nodes, "clause", "1")
    assert clause["heading"] == "Purposes"
    # None of the TOC's own row content ("PART 2.1—...", "How a criminal
    # proceeding is commenced") should have leaked into any real node.
    assert not any("2.1" in (n.get("heading") or "") for n in result.nodes)
    assert not any("How a criminal proceeding" in (n.get("text") or "") for n in result.nodes)


def test_skip_front_matter_leaves_act_parsing_unaffected():
    """skip_front_matter defaults to False -- an enacted Act's own PDF has
    no equivalent front matter to skip, and must parse exactly as before."""
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
