"""Reading an amending Act's PDF, on lines set here in the shapes the real
Acts are set in -- so what broke on the Criminal Procedure Act's amending
Acts from 2020 to 2024 stays fixed without their PDFs in git.

The lines are handed to read_pdf as its own line reader would give them,
rather than drawn into a PDF: a font that can draw "Schedule 1—" in bold
is not one every machine has."""
import pytest

from corpus.amending import pdf as amending_pdf
from corpus.amending.instructions import read_act
from corpus.amending.pdf import read_pdf

CPA = "Criminal Procedure Act 2009"


@pytest.fixture
def set_lines(monkeypatch):
    """lines: (x, size, bold, text), set down the page from the body's top."""
    def make(lines):
        monkeypatch.setattr(amending_pdf.fitz, "open", lambda path: None)
        monkeypatch.setattr(amending_pdf, "_lines", lambda doc: [
            {"text": text, "x": x, "y": 160 + 18 * n, "size": size, "bold": bold, "starts_bold": bold}
            for n, (x, size, bold, text) in enumerate(lines)])
        return "act.pdf"
    return make


ENACTS = (150, 12, False, "The Parliament of Victoria enacts:")


def _cpa(path):
    return [i for i in read_act(read_pdf(path)) if i["target_act"] == CPA]


def test_a_division_heading_wrapping_onto_schedule_is_not_a_schedule(set_lines):
    """Bail Amendment Act 2023: "Division 1—Certain offences no longer to be
    / Schedule 2 offences". Read as a Schedule, s. 67 below became a
    Schedule's Act heading and its amendments to the CPA were lost."""
    path = set_lines([
        ENACTS,
        (150, 14, True, "Division 1—Certain offences no longer to be"),
        (150, 14, True, "Schedule 2 offences"),
        (150, 12, True, "67 Criminal Procedure Act 2009"),
        (180, 12, False, "(1) In section 121(3) of the Criminal Procedure"),
        (200, 12, False, 'Act 2009, for "surety" substitute "bail guarantor".'),
    ])

    [ins] = _cpa(path)
    assert ins["provision"] == "s. 67(1)" and ins["section"] == "121" and ins["action"] == "substitute"


def test_a_quote_closed_in_small_type_is_closed(set_lines):
    """No. 30/2021: inserted text ending on a Note, set small. The close
    went unseen and the next Part, with s. 86 amending the CPA, was read
    as part of the inserted text."""
    path = set_lines([
        ENACTS,
        (150, 12, True, "85 New section 45A inserted"),
        (200, 12, False, "After section 45 of the Victorian Fisheries Authority Act 2016 insert—"),
        (200, 12, False, '"45A Assaulting authorised officers'),
        (220, 10, False, 'see section 45A of the Victorian Fisheries Authority Act 2016.".'),
        (150, 16, True, "Part 8—Amendments to other Acts"),
        (150, 12, True, "86 Schedule 2 amended"),
        (200, 12, False, 'After item 3A of Schedule 2 to the Criminal Procedure Act 2009 insert— "3AB Item".'),
    ])

    [ins] = _cpa(path)
    assert ins["provision"] == "s. 86"


def test_an_act_with_a_preamble_is_read(set_lines):
    """No. 39/2022 enacts "therefore", after its preamble; read from its
    first page instead, it stopped at "Endnotes" in its contents."""
    path = set_lines([
        (150, 12, False, "Endnotes"),
        (150, 12, False, "The Parliament of Victoria therefore enacts:"),
        (150, 12, True, "820 Criminal Procedure Act 2009 amended"),
        (200, 12, False, 'In section 3 of the Criminal Procedure Act 2009 omit "the".'),
    ])

    assert _cpa(path)


def test_a_schedule_act_heading_that_wraps_keeps_the_schedule_going(set_lines):
    """No. 9/2020: "14 Charter of Human Rights and Responsibilities" then
    "Act 2006". Read as a section it ended the Schedule, and every item
    after it -- item 20.1 amending the CPA among them -- went unread. Its
    Schedule prints no enacting section, so the one saying the Schedule
    amends is taken instead."""
    path = set_lines([
        ENACTS,
        (150, 12, True, "390 Consequential amendments"),
        (200, 12, False, "An Act specified in the heading to an item in Schedule 1 is amended as set out in that item."),
        (150, 16, True, "Schedule 1—Consequential amendments"),
        (170, 12, True, "14 Charter of Human Rights and Responsibilities"),
        (184, 12, True, "Act 2006"),
        (189, 12, False, 'In section 4(1), for "Local Government Act 1989" substitute "Local Government Act 2020".'),
        (170, 12, True, "20 Criminal Procedure Act 2009"),
        (189, 12, False, '20.1 In section 3, for "Local Government Act 1989" substitute "Local Government Act 2020".'),
    ])

    [ins] = _cpa(path)
    assert ins["provision"] == "s. 390(Sch. 1 item 20.1)" and ins["section"] == "3"


def test_an_act_is_found_at_its_address_without_the_short_words():
    """"(Trial by Judge Alone ...)" is at .../trial-judge-alone-...; older
    Acts keep every word, so the full address is still tried first."""
    import json

    from corpus.amending.fetch import locate

    asked = []

    def get(url):
        asked.append(url)
        if "/route?" in url:
            if "trial-by-judge" in url:
                raise OSError("HTTP Error 404: Not Found")
            return json.dumps({"data": {"attributes": {"endpoint": "https://x/node"}}}).encode()
        return json.dumps({"data": {"attributes": {"field_act_sr_number": "11", "field_act_sr_year": "2022"}},
                           "included": [{"type": "file--file", "attributes": {"url": "https://x/a.pdf"}}]}).encode()

    act = {"citation": "11/2022", "record": {"act_no": "11", "year": "2022"},
           "title": "Justice Legislation Amendment (Trial by Judge Alone and Other Matters) Act 2022"}

    found = locate(act, get)

    assert found["page"].endswith("/justice-legislation-amendment-trial-judge-alone-and-other-matters-act-2022")
    assert "trial-by-judge" in asked[0]
