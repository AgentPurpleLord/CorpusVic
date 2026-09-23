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

    assert result["unexplained"] == ["1/f"]


def test_an_instruction_the_reprint_does_not_reflect_is_not_found():
    ins = _one('In section 4(1)(f) of the Criminal Procedure Act 2009, omit "or".')

    result = match([ins], _units("Definitions", ("f", "x")), _units("Definitions", ("f", "x")))

    assert result["instructions"][0]["status"] == "not found"


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
