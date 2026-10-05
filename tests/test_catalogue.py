"""Searching legislation.vic.gov.au (corpus/history/catalogue.py), with
the site's answers recorded rather than fetched."""
import json
from urllib.parse import parse_qs, urlparse

from corpus.history import catalogue


def _act(title, no, year, path):
    return {"attributes": {"title": title, "field_act_sr_number": no, "field_act_sr_year": year,
                           "path": {"alias": path}}}


def _bill(id_, title, year):
    return {"id": id_, "attributes": {"title": title, "field_legislation_year": year,
                                      "field_legislation_status": "Passed", "path": {"alias": "/bills/x"}}}


def _site(acts, bills, asked=None):
    def get(url):
        parsed = urlparse(url)
        (asked if asked is not None else []).append(parse_qs(parsed.query))
        return json.dumps({"data": acts if parsed.path.endswith("/act_in_force") else bills}).encode()
    return get


def test_titles_beginning_with_the_search_come_first():
    found = catalogue.search("Bail", get=_site(
        [_act("Footscray (Bailey Reserve) Land Act 1972", "8322", "1972", "/f"),
         _act("Bail Act 1977", "9008", "1977", "/in-force/acts/bail-act-1977")],
        [_bill("a" * 36, "Justice Legislation (Sexual Offences and Bail) Bill", None),
         _bill("b" * 36, "Bail Amendment Bill 2015", "2015"), _bill("c" * 36, "Bail Amendment Bill 2023 ", "2023")]))

    assert [a["title"] for a in found["acts"]] == ["Bail Act 1977", "Footscray (Bailey Reserve) Land Act 1972"]
    assert found["acts"][0] == {"title": "Bail Act 1977", "act_no": "9008", "year": 1977,
                                "path": "/in-force/acts/bail-act-1977"}
    # Bills newest first, the title's stray space trimmed.
    assert [b["title"] for b in found["bills"]][:2] == ["Bail Amendment Bill 2023", "Bail Amendment Bill 2015"]


def test_the_search_asks_by_title():
    asked = []
    catalogue.search("Bail", get=_site([], [], asked))
    assert all(q["filter[title][operator]"] == ["CONTAINS"] and q["filter[title][value]"] == ["Bail"] for q in asked)


def test_a_one_letter_search_asks_nothing():
    assert catalogue.search("B", get=lambda url: (_ for _ in ()).throw(AssertionError("asked"))) == \
        {"acts": [], "bills": []}


def test_a_bills_pdfs_are_its_print_and_its_em_never_the_doc_copies():
    def media(id_, type_, file_id):
        return {"id": id_, "type": type_, "attributes": {},
                "relationships": {"field_media_file": {"data": {"id": file_id}}}}

    def file(id_, url):
        return {"id": id_, "type": "file--file", "attributes": {"uri": {"url": url}}}

    def doc(title, *media_ids):
        return {"id": title, "type": "paragraph--bill_document", "attributes": {"field_bill_document_title": title},
                "relationships": {"field_bill_document_documents": {"data": [{"id": m} for m in media_ids]}}}

    included = [doc("Introduction print – Bill", "m1", "m2"), doc("Introduction print – Explanatory Memorandum", "m3"),
                media("m1", "media--pdf", "f1"), media("m2", "media--document", "f2"), media("m3", "media--pdf", "f3"),
                file("f1", "/sites/default/files/bi1.pdf"), file("f2", "/sites/default/files/bi1.doc"),
                file("f3", "/sites/default/files/exi1.pdf")]
    docs = catalogue.bill_documents("f" * 8 + "-" + "a" * 27, get=lambda url: json.dumps({"included": included}).encode())

    assert [(d["kind"], d["pdf_url"].rsplit("/", 1)[-1]) for d in docs] == [("bill", "bi1.pdf"), ("em", "exi1.pdf")]
    assert docs[0]["pdf_url"].startswith("https://content.legislation.vic.gov.au/")
