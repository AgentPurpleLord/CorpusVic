"""Second reading speeches and Statements of Compatibility, from a Bill's
page on legislation.vic.gov.au through Hansard to the Act's public site.

The Bill-page fixture is the content API's real answer for the Criminal
Procedure Bill 2008, trimmed to the readings.
"""
import json
import urllib.parse
from pathlib import Path

import pytest

from corpus.hansard import bill_page, fetch, speech
from corpus.publishing import html_view

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "criminal-procedure-bill-2008.json").read_text())

SPEECH_PAGE = """<html><head><script>track()</script></head><body>
<nav><p>Home</p><p>Search Hansard</p></nav><div class="aside"><p>Related</p></div>
<div id="content"><h2 class="title">CRIMINAL PROCEDURE BILL</h2>
<p class="speaker" onclick="x()">Mr HULLS (Attorney-General) &mdash; I move:</p>
<p><i>That this bill be now read a second time.</i>
<p>This bill is the most significant reform of criminal procedure in decades.</p>
<ul><li>committal</li><li>trial</li></ul></div>
<footer><p>Copyright</p></footer></body></html>"""


def test_each_house_gives_its_statement_and_its_speech():
    found = bill_page.readings(FIXTURE)
    assert [(r["house"], r["kind"]) for r in found] == [
        ("Legislative Assembly", "statement_of_compatibility"), ("Legislative Assembly", "second_reading"),
        ("Legislative Council", "statement_of_compatibility"), ("Legislative Council", "second_reading")]
    assert found[1]["member"] == "Hon R Hulls" and found[1]["date"] == "2008-12-04"
    # The field's own date is the first reading's; the statement is
    # tabled with the second, which is the day Hansard has it.
    assert found[0]["date"] == "2008-12-04"
    assert all("&amp;" not in r["url"] for r in found)


def test_a_link_to_the_retired_hansard_is_found_again_on_the_current_one():
    """tex.parliament.vic.gov.au no longer exists. Its links name the
    record; the current site's search finds it by those names, as the
    recent Bill pages' own links do."""
    second = bill_page.readings(FIXTURE)[1]["url"]
    assert second.startswith("https://hansard.parliament.vic.gov.au/search/?")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(second).query))
    assert q["IW_FIELD_IN_SpeechTitle"] == "Criminal Procedure Bill"
    assert (q["IW_FIELD_IN_HOUSENAME"], q["IW_FIELD_IN_ACTIVITYTYPE"]) == ("ASSEMBLY", "Second reading")
    assert (q["IW_FIELD_IN_SittingDay"], q["IW_FIELD_IN_SittingMonth"], q["IW_FIELD_IN_SittingYear"]) == ("4", "December", "2008")
    assert q["IW_FIELD_ADVANCE_PHRASE"] == "be now read a second time"
    recent = "http://hansard.parliament.vic.gov.au/search/?IW_FIELD_IN_SpeechTitle=Bail+Amendment+Bill+2023"
    assert bill_page.current_link(recent) == recent


def test_a_page_keeps_the_speech_and_drops_the_rest():
    kept = speech.main_text(SPEECH_PAGE)
    assert kept == ("<h3>CRIMINAL PROCEDURE BILL</h3>\n"
                    "<p>Mr HULLS (Attorney-General) — I move:</p>\n"
                    "<p><i>That this bill be now read a second time.</i></p>\n"
                    "<p>This bill is the most significant reform of criminal procedure in decades.</p>\n"
                    "<ul><li>committal</li><li>trial</li></ul>")
    assert speech.paragraph_count(kept) == 5


def test_nothing_unsafe_survives():
    kept = speech.sanitise('<p style="x" onmouseover="y">a <a href="javascript:z">b</a><img src=x onerror=1>'
                           "<script>alert(1)</script>&lt;script&gt;</p><iframe src=x></iframe>")
    assert kept == "<p>a b&lt;script&gt;</p>"


def test_line_breaks_in_place_of_paragraphs_become_paragraphs():
    assert speech.sanitise("One.<br><br>Two.<br>Still two.") == "<p>One.</p>\n<p>Two.<br>Still two.</p>"


@pytest.fixture
def base(tmp_path):
    (tmp_path / "data" / "bill_links").mkdir(parents=True)
    (tmp_path / "data" / "bill_links" / "bills.yaml").write_text(
        "# hand-written\ncriminal-procedure-bill-2008:\n  title: Criminal Procedure Bill 2008\n"
        "  url: https://www.legislation.vic.gov.au/bills/criminal-procedure-bill-2008\n")
    (tmp_path / "data" / "bill_links" / "x.json").write_text(json.dumps(
        {"bill_slug": "criminal-procedure-bill-2008", "act_slug": "criminal-procedure-act-v114", "links": []}))
    return tmp_path


def _api(node):
    def get(url):
        if "/route?" in url:
            return json.dumps({"data": {"attributes": {"endpoint": "https://content.example/node"}}}).encode()
        return json.dumps(node).encode()
    return get


def test_an_act_finds_the_bill_it_was_enacted_from(base):
    assert fetch.bill_for("criminal-procedure-act", base)[0] == "criminal-procedure-bill-2008"
    assert fetch.bill_for("criminal-procedure-bill-2008", base)[0] == "criminal-procedure-bill-2008"
    assert fetch.bill_for("road-safety-act", base) is None


def test_a_bill_found_by_title_is_recorded(base):
    def get(url):
        if "road-safety-bill-1986" in url:
            raise OSError("404")
        return _api(FIXTURE)(url)
    bill_slug, entry = fetch.bill_for("road-safety-act", base, title="Road Safety Act 1986", get=get)
    assert bill_slug == "road-safety-bill-1985", "introduced the year before"
    text = (base / "data" / "bill_links" / "bills.yaml").read_text()
    assert text.startswith("# hand-written") and "road-safety-bill-1985:" in text


def test_fetching_writes_every_record_and_keeps_one_that_failed(base):
    def page(url):
        if "COUNCIL" in url:
            raise OSError("unreachable")
        return SPEECH_PAGE.encode()
    doc = fetch.fetch_bill("criminal-procedure-bill-2008", fetch._bills(base)["criminal-procedure-bill-2008"],
                           base, get_api=_api(FIXTURE), get_page=page, act_work="criminal-procedure-act")
    written = fetch.load("criminal-procedure-bill-2008", base)
    assert written == doc and written["act_work"] == "criminal-procedure-act"
    assembly = [s for s in written["speeches"] if s["house"] == "Legislative Assembly"]
    council = [s for s in written["speeches"] if s["house"] == "Legislative Council"]
    assert all("This bill is the most significant reform" in s["html"] for s in assembly)
    assert all(s["html"] == "" and "unreachable" in s["error"] and s["source_url"] for s in council)


def test_a_saved_page_fills_a_record_and_survives_fetching_again(base):
    """Hansard's firewall refuses requests that aren't a person's
    browser. A page saved from one is read the same way, and a later
    fetch that is refused again does not throw it away."""
    entry = fetch._bills(base)["criminal-procedure-bill-2008"]
    refused = lambda url: (_ for _ in ()).throw(OSError("Request Rejected"))
    fetch.fetch_bill("criminal-procedure-bill-2008", entry, base, get_api=_api(FIXTURE), get_page=refused)
    record = fetch.from_saved_page("criminal-procedure-bill-2008", 1, SPEECH_PAGE, base)
    assert "most significant reform" in record["html"] and "error" not in record

    again = fetch.fetch_bill("criminal-procedure-bill-2008", entry, base, get_api=_api(FIXTURE), get_page=refused)
    assert "most significant reform" in again["speeches"][1]["html"]
    assert again["speeches"][0].get("error"), "the others are still not held"
    with pytest.raises(ValueError):
        fetch.from_saved_page("criminal-procedure-bill-2008", 0, "<html><body>Request Rejected</body></html>", base)


DOC = {"bill_slug": "x-bill", "bill_title": "X Bill 2008", "act_work": "x-act", "speeches": [
    {"kind": "statement_of_compatibility", "house": "Legislative Assembly", "date": "2008-12-04",
     "member": "Hon R Hulls", "source_url": "http://hansard.example/1", "html": "<p>Compatible.</p>"},
    {"kind": "second_reading", "house": "Legislative Assembly", "date": "2008-12-04",
     "member": "Hon R Hulls", "source_url": "http://hansard.example/2", "html": "<p>I move.</p>\n<p>Reform.</p>"},
    {"kind": "second_reading", "house": "Legislative Council", "date": "2009-02-05",
     "member": "Hon. Justin Madden", "source_url": "http://hansard.example/3", "html": "", "error": "x"},
]}


def test_the_speeches_page_sets_each_record_as_hansard_has_it():
    page = html_view.render_speeches(DOC, "X Act 2009", "/browse/x-act")
    assembly = page.split("Legislative Council")[0]
    assert assembly.index("Second reading speech") < assembly.index("Statement of Compatibility"), "the speech first"
    assert "<p>I move.</p>\n<p>Reform.</p>" in page
    assert "Legislative Assembly — 4 December 2008 — Hon R Hulls" in page
    assert "Not held here." in page and 'href="http://hansard.example/3"' in page


def test_the_contents_page_links_to_the_speeches_only_where_held():
    parsed = {"nodes": [{"type": "section", "number": "1", "heading": "Purpose", "text": "x"}],
              "hierarchy": ["section"]}
    with_link = html_view.render_index(parsed, "X Act 2009", "/browse/x-act", speeches_href="/browse/x-act/speeches")
    assert 'href="/browse/x-act/speeches">Second reading speech</a>' in with_link
    assert with_link.index("Second reading speech") < with_link.index("Purpose"), "at the top"
    assert "Second reading speech" not in html_view.render_index(parsed, "X Act 2009", "/browse/x-act")


def test_an_act_is_given_the_speeches_fetched_for_it(tmp_path, monkeypatch):
    from corpus.web import dashboard
    (tmp_path / "data" / "speeches").mkdir(parents=True)
    (tmp_path / "data" / "speeches" / "x-bill.json").write_text(json.dumps(DOC))
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    assert dashboard.speeches_for("x-act-v3")["bill_slug"] == "x-bill", "any reprint of the Act"
    assert dashboard.speeches_for("y-act") is None
    (tmp_path / "data" / "speeches" / "x-bill.json").write_text(json.dumps(
        {**DOC, "speeches": [{**r, "html": ""} for r in DOC["speeches"]]}))
    assert dashboard.speeches_for("x-act") is None, "no link to a page with no speech on it"
