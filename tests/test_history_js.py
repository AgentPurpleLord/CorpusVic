"""static/site/history.js, and the parts of the page it relies on.

Asserted against the source, like the reading-on tests: there is no
browser here. They pin what was decided and why it breaks if undone, not
whether the timeline feels right under a trackpad.
"""
import re
from pathlib import Path

from corpus.publishing import html_view

from conftest import make_node

SITE = Path(__file__).resolve().parent.parent / "static" / "site"
HISTORY = (SITE / "history.js").read_text(encoding="utf-8")
READON = (SITE / "readon.js").read_text(encoding="utf-8")
CSS = (SITE / "page.css").read_text(encoding="utf-8")


# --- reading on --------------------------------------------------------

def test_a_provision_that_scrolls_in_is_announced():
    """history.js set provisions up once, at load, so a repealed provision
    scrolled into showed its plain fallback list, not its timeline."""
    assert 'new CustomEvent("readon:arrived"' in READON


def test_and_history_js_is_listening():
    assert 'addEventListener("readon:arrived"' in HISTORY


# --- sideways ----------------------------------------------------------

def test_the_wordings_are_one_row_that_scrolls_sideways():
    rule = re.search(r"\n\.hist-js \.hist-track \{([^}]*)\}", CSS).group(1)
    assert "overflow-x: auto" in rule
    assert "scroll-snap-type: x mandatory" in rule


def test_each_wording_is_somewhere_to_stop():
    rule = re.search(r"\n\.hist-js \.hist-panel \{([^}]*)\}", CSS).group(1)
    assert "scroll-snap-align: start" in rule


def test_the_page_scroll_is_never_taken_over():
    """Vertical scrolling is how reading on moves through the Act. A wheel
    handler here would be the way to break it."""
    assert "wheel" not in HISTORY


def test_the_dots_follow_the_strip_rather_than_leading_it():
    """A swipe moves the strip itself; the dots have to find out where it
    came to rest."""
    assert re.search(r'trackOf\(history\)\.addEventListener\("scroll"', HISTORY)


# --- the live toggle ---------------------------------------------------

def test_the_reading_bar_carries_the_toggle():
    html = html_view.render_section(
        {"nodes": [make_node("section", "1", "Purposes", "The purposes are-")], "hierarchy": None},
        "Test Act", "/browse/a", "s1", show_review_badge=False)
    assert 'id="reader-history"' in html


def test_it_is_remembered_between_pages():
    assert "localStorage" in HISTORY and '"readerHistory"' in HISTORY


def test_a_repealed_provision_cannot_be_switched_out_of_history():
    """It has no text of its own; turning the mode off would leave its
    page with nothing on it."""
    assert "if (!on && isGhost(article)) return;" in HISTORY
