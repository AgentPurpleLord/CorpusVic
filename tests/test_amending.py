"""Reading what an amending Act instructs, and checking the change
between two reprints against it (corpus/amending)."""
from corpus.amending.instructions import read_act, read_provision
from corpus.amending.match import match
from corpus.parsing.identity import annotate_ids
from corpus.publishing.html_view import HIERARCHY_ORDER, _wording_units

CPA = "Criminal Procedure Act 2009"


def _one(text, principal=CPA):
    [ins] = read_provision(text, "s. 82", principal)
    return ins


# --- reading --------------------------------------------------------------

def test_a_substitution_names_its_piece_and_its_words():
    ins = _one("In section 4(1)(f) of the Criminal Procedure Act 2009, for “charge-sheet” substitute “charge”.")

    assert (ins["section"], ins["path"], ins["action"]) == ("4", ["1", "f"], "substitute")
    assert (ins["old"], ins["new"], ins["target_act"]) == ("charge-sheet", "charge", CPA)


def test_a_list_of_instructions_is_one_each_cited_by_its_item():
    out = read_provision('In section 353(1) of the Principal Act— (a) in paragraph (b), for "x" substitute "y"; '
                         '(b) after "offence" insert "or a stalking offence"; (c) omit "or".', "s. 72", CPA)

    assert [(i["provision"], i["path"], i["action"]) for i in out] == [
        ("s. 72(a)", ["1", "b"], "substitute"),
        ("s. 72(b)", ["1"], "insert_after"),
        ("s. 72(c)", ["1"], "omit"),
    ]
    assert out[0]["target_act"] == CPA, "the Principal Act, as this Part defines it"


def test_a_provision_given_whole_carries_its_own_number():
    ins = _one("After section 359(1)(b) of the Criminal Procedure Act 2009 insert— “(ba) a stalking offence;”.")

    assert (ins["action"], ins["path"], ins["number"], ins["new"]) == (
        "insert_provision", ["1", "b"], "ba", "a stalking offence;")
    assert _one("For section 353(1)(b) of the Criminal Procedure Act 2009 substitute— “(b) any matter;”.")[
        "action"] == "replace_provision"


def test_repeals_headings_and_definitions():
    assert _one("Section 12(3) of the Criminal Procedure Act 2009 is repealed.")["action"] == "repeal"
    heading = _one('In the heading to section 366 of the Criminal Procedure Act 2009, after "offences" insert "and stalking".')
    assert heading["heading"] and heading["action"] == "insert_after"
    definition = _one("In section 3 of the Criminal Procedure Act 2009 insert the following definition in "
                      "alphabetical order— “stalking offence means an offence against section 21A;”.")
    assert definition["action"] == "insert_definition"


def test_words_no_form_fits_are_kept_not_dropped():
    ins = _one("Section 10 of the Criminal Procedure Act 2009 is amended as the Governor may direct.")

    assert ins["action"] == "unparsed" and "Governor" in ins["raw"]


def test_an_act_is_read_with_what_its_principal_act_means_in_each_part():
    nodes = [
        {"type": "part", "number": "3", "heading": "Amendment of Criminal Procedure Act 2009", "text": ""},
        {"type": "section", "number": "71", "heading": "Committal", "text": ""},
        {"type": "subsection", "number": "1", "heading": None, "text": "In section 181(2) of the Principal Act—"},
        {"type": "paragraph", "number": "a", "heading": None, "text": 'omit "or";'},
        {"type": "paragraph", "number": "b", "heading": None, "text": 'after "trial" insert "or hearing".'},
    ]

    out = read_act(nodes)

    assert [(i["provision"], i["target_act"], i["action"]) for i in out] == [
        ("s. 71(1)(a)", CPA, "omit"), ("s. 71(1)(b)", CPA, "insert_after")]


# --- matching ---------------------------------------------------------------

def _units(heading, *pieces):
    nodes = [{"type": "section", "number": "4", "heading": heading, "text": ""},
             {"type": "subsection", "number": "1", "heading": None, "text": "In this Act—"}]
    for number, text in pieces:
        nodes.append({"type": "paragraph", "number": number, "heading": None, "text": text})
    annotate_ids(nodes, HIERARCHY_ORDER)
    return _wording_units(nodes, HIERARCHY_ORDER)


def test_a_change_where_the_act_says_is_matched():
    ins = _one('In section 4(1)(f) of the Criminal Procedure Act 2009, for "a charge" substitute "the charge".')

    result = match([ins], _units("Definitions", ("f", "a charge is filed")),
                   _units("Definitions", ("f", "the charge is filed")))

    assert (result["instructions"][0]["status"], result["instructions"][0]["at"]) == ("matched", "1/f")
    assert result["unexplained"] == []


def test_the_right_words_under_the_wrong_paragraph_are_found_elsewhere():
    """The change the parser put under (g), when the Act made it to (f):
    what a misplaced margin note would never show."""
    ins = _one('In section 4(1)(f) of the Criminal Procedure Act 2009, for "a charge" substitute "the charge".')

    result = match([ins], _units("Definitions", ("f", "x"), ("g", "a charge is filed")),
                   _units("Definitions", ("f", "x"), ("g", "the charge is filed")))

    assert (result["instructions"][0]["status"], result["instructions"][0]["at"]) == ("elsewhere", "1/g")


def test_a_change_no_instruction_made_is_unexplained():
    result = match([], _units("Definitions", ("f", "a charge- sheet")), _units("Definitions", ("f", "a charge-sheet")))

    assert [c["path"] for c in result["unexplained"]] == ["1/f"]


def test_an_instruction_the_reprint_does_not_reflect_is_not_found():
    ins = _one('In section 4(1)(f) of the Criminal Procedure Act 2009, omit "or".')

    result = match([ins], _units("Definitions", ("f", "x or y")), _units("Definitions", ("f", "x or y")))

    assert result["instructions"][0]["status"] == "not found"


def test_an_instruction_the_older_reprint_already_reflects_is_earlier():
    """In force before the two reprints compared: 1/2026's ss 79-81
    commenced before the oldest CPA reprint held."""
    ins = _one('In section 4(1)(f) of the Criminal Procedure Act 2009, for "taken" substitute "take".')

    result = match([ins], _units("Definitions", ("f", "must take")), _units("Definitions", ("f", "must take")))

    assert result["instructions"][0]["status"] == "earlier"


def test_a_piece_nested_differently_in_each_reprint_is_still_paired():
    """v111 read s 4(1)(f) as (1)(f) and v112 as (8C)(f): no path in
    common, but one paragraph."""
    ins = _one('In section 4(1)(f) of the Criminal Procedure Act 2009, for "a charge" substitute "the charge".')
    older = _units("Definitions", ("f", "a charge is filed"))
    newer = _units("Definitions", ("f", "the charge is filed"))
    newer[-1]["path"] = "8c/f"

    result = match([ins], older, newer)

    assert result["instructions"][0]["status"] == "matched", "matched on the older reprint's path"


def test_an_inserted_paragraph_and_a_changed_heading_are_matched():
    inserted = _one("After section 4(1)(f) of the Criminal Procedure Act 2009 insert— “(fa) a stalking offence;”.")
    heading = _one('In the heading to section 4 of the Criminal Procedure Act 2009, after "Definitions" insert "and stalking".')

    result = match([inserted, heading], _units("Definitions", ("f", "x")),
                   _units("Definitions and stalking", ("f", "x"), ("fa", "a stalking offence;")))

    assert [(i["status"], i["at"]) for i in result["instructions"]] == [("matched", "1/fa"), ("matched", "heading")]
    assert result["unexplained"] == []


# --- which Acts are needed --------------------------------------------------

def test_only_acts_amending_between_held_reprints_are_needed(tmp_path):
    import json

    from corpus.amending.scope import acts_between

    def reprint(version, note, acts):
        nodes = [{"type": "section", "number": "4", "heading": "Definitions", "text": "",
                  "history": [{"raw": note}] if note else []},
                 {"type": "subsection", "number": "1", "heading": None, "text": "words"}]
        annotate_ids(nodes, HIERARCHY_ORDER)
        table = [{"citation": c, "title": f"Act {c}", "act_no": c.split("/")[0], "year": c.split("/")[1]} for c in acts]
        path = tmp_path / "data" / "parsed" / f"act-v{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"nodes": nodes, "endnotes": {"amending_acts": table}}))

    reprint(1, "S. 4 amended by No. 3/2020 s. 5.", ["3/2020"])
    reprint(2, "S. 4 amended by Nos 3/2020 s. 5, 7/2026 s. 82.", ["3/2020", "7/2026"])

    needed = acts_between("act-v2", tmp_path)

    assert [(a["citation"], a["versions"], a["title"]) for a in needed] == [("7/2026", [2], "Act 7/2026")], \
        "3/2020 amended it before the oldest reprint held, so there is nothing to check it against"


# --- fetching and reading the PDF -------------------------------------------

def test_an_act_is_found_through_the_content_api_by_its_title():
    """The route and record legislation.vic.gov.au's API gave for 1/2026,
    saved; nothing here touches the network."""
    import json

    from corpus import PROJECT_ROOT
    from corpus.amending.fetch import locate, slug

    fixtures = PROJECT_ROOT / "tests" / "fixtures" / "amending"
    asked = []

    def get(url):
        asked.append(url)
        name = "route-1-2026.json" if "/route?" in url else "node-1-2026.json"
        return (fixtures / name).read_bytes()

    act = {"citation": "1/2026", "title": "Justice Legislation Amendment (Family Violence, Stalking and Other Matters) Act 2026",
           "record": {"act_no": "1", "year": "2026"}}

    found = locate(act, get)

    assert slug(act["title"]) == "justice-legislation-amendment-family-violence-stalking-and-other-matters-act-2026"
    assert found["pdf_url"].endswith("/2026-02/26-001aa-authorised.pdf")
    assert "site=6" in asked[0] and "include=field_as_made_authorized_version" in asked[1]


def test_a_title_that_leads_to_another_act_is_refused():
    import pytest

    from corpus import PROJECT_ROOT
    from corpus.amending.fetch import NotFound, locate

    fixtures = PROJECT_ROOT / "tests" / "fixtures" / "amending"
    act = {"citation": "2/2026", "title": "Justice Legislation Amendment (Family Violence, Stalking and Other Matters) Act 2026",
           "record": {"act_no": "2", "year": "2026"}}

    with pytest.raises(NotFound, match="not 2/2026"):
        locate(act, lambda url: (fixtures / ("route-1-2026.json" if "/route?" in url else "node-1-2026.json")).read_bytes())


def test_the_cpa_amendments_in_1_2026_are_read_from_its_pdf():
    import pytest

    from corpus.amending.fetch import pdf_path
    from corpus.amending.pdf import read_pdf

    pdf = pdf_path("1/2026")
    if not pdf.exists():
        pytest.skip("amending Act PDFs are not in git; python -m corpus.amending.fetch fetches them")
    out = [i for i in read_act(read_pdf(pdf)) if i["target_act"] == CPA]
    by = {(i["provision"], "/".join(i["path"])): i for i in out}

    assert len(out) == 25 and not [i for i in out if i["action"] == "unparsed"]
    assert by[("s. 82", "1/f")]["new"] == "paragraphs (a) to (e)", "the running header between the lines is gone"
    assert by[("s. 72(b)", "1/b")]["number"] == "c", "an item that inserts a paragraph"
    assert ("s. 77", "2/b/iii") in by, "(ii)(A) and (iii): the second target by its own numbering"
    assert by[("s. 83", "")]["action"] == "insert_section" and by[("s. 83", "")]["section"] == "464"


def test_a_schedule_item_is_cited_through_the_section_that_enacts_it():
    import pytest

    from corpus.amending.fetch import pdf_path
    from corpus.amending.pdf import read_pdf

    pdf = pdf_path("13/2025")
    if not pdf.exists():
        pytest.skip("amending Act PDFs are not in git")
    [first, _] = [i for i in read_act(read_pdf(pdf)) if i["target_act"] == CPA]

    assert (first["provision"], first["schedule"], first["path"]) == ("s. 96(Sch. 3 item 2.1)", "3", ["22A"])
