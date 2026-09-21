"""Hover previews: which links get a card.

A card over a link a reader is about to click is in the way, so previews
belong on the links a reader follows to *check* something -- a defined
term, a cross-referenced provision -- and nowhere else. The script decides
that with one CSS selector (`a.closest(".prov")`), so what these tests pin
down is which of a real page's links that selector reaches.

Asserted against rendered pages rather than against the selector string,
because the defect the selector is fixing was a markup question: the
outline, the nav and the margin notes all sat inside the element that was
being matched before.
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from corpus.publishing import html_view

PREVIEW_JS = (Path(__file__).resolve().parent.parent
              / "static" / "site" / "preview.js")

# The selector the script itself uses, read from it rather than repeated
# here: these tests are about which links it reaches, so widening the
# selector has to be able to fail them.
PREVIEWABLE = re.search(r'PREVIEWABLE = "\.([\w-]+)"',
                        PREVIEW_JS.read_text(encoding="utf-8")).group(1)


VOID = {"br", "img", "input", "hr", "meta", "link", "source", "wbr"}


class Links(HTMLParser):
    """Every link on a page, with the classes of everything enclosing it.

    Whole pages, never fragments: `closest()` climbs to the root, so a
    fragment cut out of the page would answer "nothing encloses this"
    about ancestors that do.
    """

    def __init__(self):
        super().__init__()
        self.found = []     # (href, ancestor class names)
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag not in VOID:
            self._stack.append(classes)
        if tag == "a" and "href" in attrs:
            self.found.append((attrs["href"], set().union(*self._stack, classes)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag not in VOID and self._stack:
            self._stack.pop()


def links(html):
    parser = Links()
    parser.feed(html)
    return parser.found


def previewable(href_and_ancestors):
    """What `a.closest(PREVIEWABLE)` answers for that link."""
    return PREVIEWABLE in href_and_ancestors[1]


@pytest.fixture
def crimes_act():
    return json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "parsed" / "crimes-act.json")
        .read_text(encoding="utf-8"))


def render_section(parsed, slug):
    return html_view.render_section(
        parsed, "Crimes Act 1958", "/browse/crimes-act", slug,
        crossrefs=[], amendment_index={}, timeline=[], version_urls={},
        superseded=None, version_dates={}, show_review_badge=False,
        timeline_unavailable=False, notice=None)


def render_index(parsed):
    return html_view.render_index(
        parsed, "Crimes Act 1958", "/browse/crimes-act",
        superseded=None, show_review_badge=False)


# --- a provision page ----------------------------------------------------

def test_a_cross_reference_in_the_text_gets_a_card(crimes_act):
    """The case the feature exists for: checking what another provision
    says without losing your place."""
    found = [href for href, _ in filter(previewable, links(render_section(crimes_act, "s3")))
             if "/section/" in href]
    assert found, "no previewable cross-reference found in s 3"


def test_the_nav_and_breadcrumb_do_not(crimes_act):
    """A reader following one of these means to go there."""
    chrome = {"section-nav", "breadcrumb"}
    for link in links(render_section(crimes_act, "s3")):
        if link[1] & chrome:
            assert not previewable(link), link[0]


def test_the_margin_notes_do_not(crimes_act):
    """The amendment notes are cells of the same grid as the provisions,
    but they are apparatus rather than the Act's own words -- and they
    link to the endnotes, which is not a provision to preview."""
    notes = [l for l in links(render_section(crimes_act, "s3")) if "prov-notes" in l[1]]
    assert notes, "no margin-note links to check"
    for link in notes:
        assert not previewable(link), link[0]


# --- the index page ------------------------------------------------------

def test_the_contents_and_outline_do_not(crimes_act):
    """Nothing on the index is the Act's own text, so nothing there
    previews -- including the outline that used to sit on every provision
    page."""
    on_index = links(render_index(crimes_act))
    assert on_index, "no links on the index to check"
    assert not [href for href, _ in filter(previewable, on_index)]


# --- how the card behaves ------------------------------------------------

def test_a_click_anywhere_else_dismisses_it():
    """Escape worked; a click did not, so the way to get rid of a card in
    the way was to wait out the close delay."""
    js = PREVIEW_JS.read_text(encoding="utf-8")
    assert re.search(r'addEventListener\("click".*?closest\("\.linkpeek"\)', js, re.S)


def test_it_waits_before_opening_but_not_long():
    """Instant would flash a card every time the pointer crossed a link
    mid-sentence; half a second read as unresponsive."""
    delay = int(re.search(r"OPEN_DELAY = (\d+)", PREVIEW_JS.read_text(encoding="utf-8")).group(1))
    assert 100 <= delay <= 300
