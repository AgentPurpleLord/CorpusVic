"""Issue #57 -- a list of provisions that belongs to another Act.

Victorian drafting names the Act once, in the line that introduces the
list, and never again:

    (b) an offence under any of the following provisions of the
        Crimes Act 1958-
        (i) Division 2 of Part I (other than sections 75, 75A, ...);

The linkifier reads one provision at a time, so it resolved (i)'s
references against the Act being rendered and linked them into the
Criminal Procedure Act. A link that looks right and goes to the wrong law
is the worst error a legislation site can make, and it is the one this
codebase already writes down the rule against: "leave unlinked, not
linked wrong".
"""
import json
import re
from pathlib import Path

import pytest

from corpus.domain.act_scope import governing_act, scope_by_unit
from corpus.publishing.html_view import render_section

from conftest import make_node

PARSED = Path(__file__).resolve().parent.parent / "data" / "parsed"


def _parsed(nodes):
    return {"nodes": nodes, "hierarchy": None}


def _render(nodes, slug="s10"):
    return render_section(_parsed(nodes), "Test Act", "/browse/a", slug,
                          show_review_badge=False)


def _links(html):
    """(label, href) in page order. A list, not a dict: the same label
    appears twice on these pages on purpose -- "section 38" inside the
    list is the other Act's, and "section 38" after it is this one's."""
    body = html[html.index('class="provisions"'):]
    return [(label, href) for href, label
            in re.findall(r'<a [^>]*href="([^"]+)"[^>]*>([^<]*)</a>', body)]


def _first(links, label):
    return next(href for text, href in links if text == label)


# --- reading the lead-in -------------------------------------------------

def test_a_lead_in_names_the_act_its_list_belongs_to():
    assert governing_act(
        "an offence against any of the following provisions of the Crimes Act 1958—"
    ) == "Crimes Act 1958"


def test_a_colon_introduces_a_list_just_as_a_dash_does():
    assert governing_act("the following provisions of the Sentencing Act 1991:") == "Sentencing Act 1991"


def test_an_act_mentioned_in_passing_governs_nothing():
    """The asymmetry that sets how eager this is: missing a lead-in
    leaves a reference unlinked, while matching a passing mention hands a
    whole subtree to an Act that was only named on the way past."""
    assert governing_act("has the same meaning as in the Crimes Act 1958") is None
    assert governing_act("A person who contravenes the Crimes Act 1958 is guilty.") is None


def test_an_unrecognised_title_governs_nothing():
    """Resolved or nothing -- never a guess about where the children of
    an Act nobody can name should point."""
    assert governing_act("under the Entirely Invented Act 1999—") is None


# --- how far it reaches --------------------------------------------------

def _units(*depths_and_texts):
    return [{"depth": d, "text": t} for d, t in depths_and_texts]


def test_a_lead_in_governs_what_is_nested_under_it():
    units = _units(
        (0, "This section applies to—"),
        (1, "an offence under any of the following provisions of the Crimes Act 1958—"),
        (2, "section 38;"),
        (2, "section 39;"),
    )
    assert scope_by_unit(units) == [None, None, "Crimes Act 1958", "Crimes Act 1958"]


def test_the_next_sibling_is_back_on_this_act():
    """The bug in its other direction: a scope that leaked past the list
    would start mislinking this Act's own references."""
    units = _units(
        (1, "an offence under any of the following provisions of the Crimes Act 1958—"),
        (2, "section 38;"),
        (1, "an offence against section 12 of this Act."),
    )
    assert scope_by_unit(units) == [None, "Crimes Act 1958", None]


def test_a_lead_in_is_not_governed_by_itself():
    """Its own text names the Act outright, so it links the ordinary way."""
    units = _units((0, "the following provisions of the Crimes Act 1958—"), (1, "section 38;"))
    assert scope_by_unit(units)[0] is None


# --- what gets rendered --------------------------------------------------

def _act_with_a_foreign_list(title="Crimes Act 1958"):
    return [
        make_node("section", "10", "Application"),
        make_node("subsection", "1", None, "This section applies to a charge for—"),
        make_node("paragraph", "a", None,
                  f"an offence against any of the following provisions of the {title}—"),
        make_node("subparagraph", "i", None, "section 38 (rape);"),
        make_node("paragraph", "b", None, "an offence against section 38 of this Act."),
        # A section 38 of its own, so "the same number in two Acts" is
        # actually at stake -- which is what made the original bug
        # invisible on the page.
        make_node("section", "38", "Something else entirely"),
    ]


def test_a_reference_under_the_lead_in_points_at_the_other_act():
    links = _links(_render(_act_with_a_foreign_list()))

    assert _first(links, "section 38") == "/browse/crimes-act/section/s38"


def test_and_the_paragraph_after_the_list_still_points_here():
    """Same number in both Acts, one page apart. (i) is the Crimes Act's
    section 38; (b), back at the outer level, is this Act's own."""
    body = _render(_act_with_a_foreign_list())
    body = body[body.index('class="provisions"'):]
    in_list = body[body.index("rape"):]
    after = body[body.index("of this Act") - 400:]

    assert "/browse/crimes-act/section/s38" in body
    assert "/browse/a/section/s38" in after, "the paragraph after the list lost its own link"


def test_an_act_this_pipeline_has_not_parsed_goes_to_the_resolver():
    """Detected and named, just not held here: the standing resolver says
    so, which beats both a wrong link and silence.

    (A title with a parenthesised subtitle -- "... (Amendment) Act 1983"
    -- is deliberately not matchable by the citation pattern, so this
    uses one without.)"""
    links = _links(_render(_act_with_a_foreign_list(
        "Abattoir and Meat Inspection Act 1973")))

    assert _first(links, "section 38").startswith("/legislation/"), \
        "a reference handed to an unparsed Act should reach the resolver"


def test_a_section_the_other_act_does_not_have_is_left_as_text():
    nodes = _act_with_a_foreign_list()
    nodes[3] = make_node("subparagraph", "i", None, "section 99999;")

    assert "section 99999" not in [label for label, _h in _links(_render(nodes))]


def test_an_act_naming_itself_governs_nothing():
    """Its own references belong to it, so nothing changes."""
    nodes = [
        make_node("section", "10", "Application"),
        make_node("paragraph", "a", None,
                  "an offence against the following provisions of the Test Act 1999—"),
        make_node("subparagraph", "i", None, "section 10;"),
    ]
    html = render_section(_parsed(nodes), "Test Act 1999", "/browse/a", "s10",
                          show_review_badge=False)
    assert "/browse/a/section/s10" in html


# --- the document the issue was raised about -----------------------------

@pytest.mark.skipif(not (PARSED / "criminal-procedure-act-v114.json").exists(),
                    reason="the Criminal Procedure Act parse is not in this checkout")
def test_the_clause_from_the_issue():
    """Criminal Procedure Act, Schedule 1 clause 4A. Every one of these
    linked into the Criminal Procedure Act before."""
    parsed = json.loads((PARSED / "criminal-procedure-act-v114.json").read_text(encoding="utf-8"))
    html = render_section(parsed, "Criminal Procedure Act 2009",
                          "/browse/criminal-procedure-act", "s4a", show_review_badge=False)
    body = html[html.index('class="provisions"'):]
    listed = body[body.index("following provisions"):][:2200]
    hrefs = re.findall(r'href="([^"]+)"', listed)

    assert any(h.startswith("/browse/crimes-act/section/s75") for h in hrefs), \
        "'sections 75' still does not point at the Crimes Act"
    assert not any(h.startswith("/browse/criminal-procedure-act/#division") for h in hrefs), \
        "a Division in the list still points back into this Act"
