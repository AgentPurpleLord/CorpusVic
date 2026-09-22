"""Reading on: the next provision arriving under this one as you scroll.

The thing this must not break is why the site is worth indexing at all:
every provision is its own URL serving its own complete page. Reading on
only ever adds what was already a click away, so what a crawler is served
at a URL and what a reader ends up looking at stay the same document.
"""
import html as html_lib
import json
import re
from pathlib import Path

import pytest

from corpus.publishing import html_view

SITE = Path(__file__).resolve().parent.parent / "static" / "site"
READON_JS = SITE / "readon.js"


@pytest.fixture
def crimes_act():
    parsed = json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "parsed" / "crimes-act.json")
        .read_text(encoding="utf-8"))
    return parsed


def render(parsed, section_slug):
    return html_view.render_section(
        parsed, "Crimes Act 1958", "/browse/crimes-act", section_slug,
        crossrefs=[], amendment_index={}, timeline=[], version_urls={},
        superseded=None, version_dates={}, show_review_badge=False, notice=None)


# --- what the page says about itself ------------------------------------

def test_a_provision_is_one_element_that_can_be_lifted(crimes_act):
    html = render(crimes_act, "s3")
    assert html.count('<article class="reader-section"') == 1
    assert html.count("</article>") == 1


def scopes(html):
    """The chain of structural levels a rendered provision carries."""
    raw = re.search(r'data-scopes="([^"]*)"', html).group(1)
    return json.loads(html_lib.unescape(raw))


def test_it_names_itself_its_title_and_how_far_reading_on_can_go(crimes_act):
    html = render(crimes_act, "s3")
    article = re.search(r'<article class="reader-section"[^>]*>', html).group(0)
    assert 'data-section="s3"' in article
    assert 'data-title="3 Punishment for murder"' in article
    assert [s["id"] for s in scopes(article)][:2] == ["part_i", "part_i__div_1"]


def test_a_provision_carries_every_level_above_it_not_just_the_nearest(crimes_act):
    """A section can end a Division and its Part at once, and reading on
    can only say so if it is told about both. Outermost first, each level
    named apart from its heading -- the end marker sets one and the
    header sets both."""
    chain = scopes(render(crimes_act, "s3"))
    assert [s["label"] for s in chain] == ["Part I", "Division 1", "Subdivision (1)"]
    assert chain[1]["heading"] == "Offences against the person"


def test_provisions_in_one_division_share_a_scope(crimes_act):
    chains = {tuple(s["id"] for s in scopes(render(crimes_act, s)))
              for s in ("s314", "s315")}
    assert chains == {("part_i", "part_i__div_6")}


def test_the_next_division_is_a_different_scope(crimes_act):
    """The boundary reading on marks."""
    here = scopes(render(crimes_act, "s315"))
    beyond = scopes(render(crimes_act, "s316"))
    assert here[-1]["id"] != beyond[-1]["id"]


def test_the_nav_below_stays_outside_the_liftable_part(crimes_act):
    """It is the page's own chrome -- lifted with the provision it would
    be duplicated every time one arrived."""
    html = render(crimes_act, "s3")
    assert html.index("</article>") < html.index('class="section-nav"')


# --- the page a crawler is served ---------------------------------------

def test_every_provision_is_still_a_whole_page_on_its_own(crimes_act):
    """Nothing here is loaded in: the text, the breadcrumb and the
    heading are all in what the server sent."""
    html = render(crimes_act, "s315")
    assert "All evidence and proof whatsoever" in html
    assert "<h1>315 All evidence material with respect to perjury</h1>" in html
    assert 'class="breadcrumb"' in html


def test_a_page_says_which_address_it_is_the_copy_at():
    shell = html_view.page_shell("Title", "<p>body</p>", base_url="/browse/crimes-act",
                                 canonical="/browse/crimes-act/section/s3")
    assert '<link rel="canonical" href="/browse/crimes-act/section/s3">' in shell


def test_a_page_with_no_address_given_claims_none():
    assert "canonical" not in html_view.page_shell("Title", "<p>body</p>")


def test_the_shell_loads_the_script():
    assert "readon.js" in (SITE / "page.html").read_text(encoding="utf-8")


# --- what the script is careful about ------------------------------------

def test_it_runs_to_the_ends_of_the_act():
    """It used to stop at the Division. An Act is meant to be read like a
    book, so the only thing that ends a read is running out of Act."""
    js = READON_JS.read_text(encoding="utf-8")
    assert "dataset.scope !== scope" not in js, "no boundary stops the read"
    # What does stop it: no link to follow.
    assert "if (!href) { done[where] = true; return false; }" in js


def test_it_reads_backwards_as_well_as_forwards():
    """Opening a provision part-way through an Act from the contents used
    to leave nothing above it."""
    js = READON_JS.read_text(encoding="utf-8")
    assert "nav-prev" in js and "prevHref" in js
    assert 'extend("prev")' in js or 'fill("prev")' in js


def test_it_compensates_the_scroll_when_it_puts_something_above():
    """Inserting above the viewport moves everything below it down, and
    the page would jump out from under the reader.

    Measured on the provision being read, not on the document's height:
    the height moves whenever anything at all reflows, and a correction
    computed from it moves the reader by whatever that was."""
    js = READON_JS.read_text(encoding="utf-8")
    assert "anchor.getBoundingClientRect().top" in js
    assert "window.scrollTo(0, window.scrollY + moved)" in js
    assert "scrollHeight" not in js


def test_only_one_thing_corrects_the_scroll():
    """The browser's own scroll anchoring corrects for the same insert on
    its own reckoning, and the two together moved the reader twice."""
    css = (SITE / "reader.css").read_text(encoding="utf-8")
    assert "overflow-anchor: none" in css


def test_nothing_is_loaded_above_a_reader_who_has_not_moved_yet():
    """Landing on a provision from the contents used to put three more
    above it at the moment of arrival."""
    js = READON_JS.read_text(encoding="utf-8")
    assert 'if (where === "prev" && !scrolled) return false;' in js


def test_it_keeps_a_buffer_rather_than_fetching_one_at_a_time():
    """The pop-in was a provision being asked for only once the reader had
    arrived at the end of the one before it."""
    js = READON_JS.read_text(encoding="utf-8")
    assert "BUFFER" in js
    assert "for (var i = 0; i < BUFFER; i++)" in js


def test_a_division_ending_is_marked_by_its_id_not_its_label():
    """Every Part of an Act has a Division 1, so comparing printed labels
    would miss the break between one Part's last Division and the next
    Part's first."""
    js = READON_JS.read_text(encoding="utf-8")
    assert "ending[common].id === starting[common].id" in js


def test_it_marks_every_level_that_ends_and_heads_every_level_that_begins():
    """A section can be the last of a Division and of its Part at once,
    and the Part beginning under it is what a reader most needs naming."""
    js = READON_JS.read_text(encoding="utf-8")
    # The endings innermost first, the beginnings outermost first.
    assert "for (var i = ending.length - 1; i >= common; i--)" in js
    assert "for (var j = common; j < starting.length; j++)" in js
    assert '"End of " + ending[i].label' in js
    assert 'head.className = "read-on-scope"' in js


def test_the_end_of_a_part_comes_before_the_rule_not_after_it():
    """Directly after a Part's last provision is the line saying the Part
    has ended; the rule closes the two of them off together."""
    css = (SITE / "reader.css").read_text(encoding="utf-8")
    end_rule = re.search(r"\.read-on-end \{(.*?)\}", css, re.S).group(1)
    assert "border-top" not in end_rule
    assert "border-top: 1px solid var(--border);" in re.search(
        r"\.read-on-end \+ \.reader-section \{(.*?)\}", css, re.S).group(1)


def test_it_replaces_the_address_rather_than_pushing_it():
    """Scrolling is not navigation: pushState would fill the Back button
    with provisions somebody scrolled past and trap them on the page."""
    js = READON_JS.read_text(encoding="utf-8")
    assert "history.replaceState(" in js
    # Named in the comment saying why not; never called.
    assert "pushState(" not in js


def test_it_follows_the_outermost_provisions_link_not_the_first():
    """Following the first page's link forever would fetch the same
    provision over and over, at either end."""
    js = READON_JS.read_text(encoding="utf-8")
    assert "loaded[loaded.length - 1]" in js and "loaded[0]" in js


def test_it_leaves_a_reader_who_asked_not_to_be_moved_alone():
    assert "prefers-reduced-motion" in READON_JS.read_text(encoding="utf-8")


def test_it_keeps_the_nav_below_in_step_with_the_last_provision():
    js = READON_JS.read_text(encoding="utf-8")
    assert 'doc.querySelector(".section-nav")' in js
