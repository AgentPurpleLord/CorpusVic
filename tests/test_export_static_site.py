"""Tests for export_static_site.py's own pure logic (which documents get
a public page, and the passphrase gate over them) and the html_view.py
link-prefixing it depends on. The actual page-writing loop isn't
unit-tested here, for the same reason dashboard.py's routes aren't (see
review.py's and dashboard.py's own module docstrings): it's a thin
wrapper over already-tested render functions, checked end to end instead
by actually running the script against real data (see the project's own
manual smoke-testing convention)."""
import base64
import json
import re

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_pipeline.html_view import _legislation_href, _site_prefix
from ai_pipeline.site_crypto import SiteGate, derive_key
from conftest import make_node
from export_static_site import (
    OFFICIAL_SOURCE_URL,
    _landing_page_html,
    _page,
    approved_page_slugs,
    approved_units,
    select_candidate_slugs,
)


def _status(parsed=True, review_status="reviewed"):
    return {"parsed": parsed, "review_status": review_status}


def test_select_candidate_slugs_skips_an_unparsed_document():
    statuses = {"crimes-act": _status(parsed=False)}
    assert select_candidate_slugs(statuses) == []


def test_select_candidate_slugs_does_not_care_how_far_review_has_got():
    """A document earns its place provision by provision now (see
    approved_units), so being part-way through review is no longer a
    reason to leave the whole thing out."""
    statuses = {
        "crimes-act": _status(review_status="in-progress"),
        "evidence-act": _status(review_status="not-started"),
    }
    assert select_candidate_slugs(statuses) == ["crimes-act", "evidence-act"]


def test_select_candidate_slugs_offers_only_the_newest_version_of_a_work():
    statuses = {
        "criminal-procedure-act-v110": _status(),
        "criminal-procedure-act-v114": _status(),
    }
    assert select_candidate_slugs(statuses) == ["criminal-procedure-act-v114"]


# ---------------------------------------------------------------------
# Which provisions are approved enough to publish.
# ---------------------------------------------------------------------

_STAMP = "2026-01-01T00:00:00+00:00"


def _node(**extra):
    return dict(make_node("subsection", "1", None, "some text"), **extra)


def test_a_unit_is_approved_when_every_node_in_it_is_verified():
    nodes = [_node(verified_at=_STAMP), _node(verified_at=_STAMP)]
    assert approved_units(nodes, [[0, 1]]) == {0}


def test_a_unit_with_an_unverified_node_is_not_approved():
    nodes = [_node(verified_at=_STAMP), _node()]
    assert approved_units(nodes, [[0, 1]]) == set()


def test_a_flagged_node_keeps_its_unit_unpublished():
    """Flagging means "not sure, revisit this" -- review.py deliberately
    leaves such a node unstamped -- so the reviewer's doubt has to keep
    the provision off the public site."""
    nodes = [_node(verified_at=_STAMP), _node(needs_followup=True)]
    assert approved_units(nodes, [[0, 1]]) == set()


def test_a_node_both_verified_and_flagged_still_counts_as_flagged():
    nodes = [_node(verified_at=_STAMP, needs_followup=True)]
    assert approved_units(nodes, [[0]]) == set()


def test_a_merged_away_node_does_not_hold_its_unit_back():
    """A merged-away position is None, and no longer a provision anyone
    has to approve separately."""
    nodes = [_node(verified_at=_STAMP), None]
    assert approved_units(nodes, [[0, 1]]) == {0}


def test_a_unit_that_was_entirely_merged_away_is_not_approved():
    assert approved_units([None, None], [[0, 1]]) == set()


def test_units_are_judged_independently():
    nodes = [_node(verified_at=_STAMP), _node(), _node(verified_at=_STAMP)]
    assert approved_units(nodes, [[0], [1], [2]]) == {0, 2}


def test_approved_page_slugs_maps_approved_units_back_to_their_pages():
    nodes = [_node(verified_at=_STAMP), _node(verified_at=_STAMP), _node()]
    units = [[0, 1], [2]]
    by_node_index = {0: "s1", 2: "s2"}
    assert approved_page_slugs(nodes, units, by_node_index) == {"s1"}


def test_a_page_whose_node_starts_no_unit_is_not_published():
    """Conservative on purpose: if the two ever stopped lining up, the
    failure should be a provision withheld, not unreviewed text shipped."""
    nodes = [_node(verified_at=_STAMP)]
    assert approved_page_slugs(nodes, [[0]], {99: "s99"}) == set()


def test_select_candidate_slugs_treats_independent_works_independently():
    statuses = {
        "crimes-act": _status(),
        "evidence-act": _status(parsed=False),
    }
    assert select_candidate_slugs(statuses) == ["crimes-act"]


def test_site_prefix_is_empty_when_base_url_has_no_browse_segment():
    assert _site_prefix("") == ""
    assert _site_prefix("something-else") == ""


def test_site_prefix_is_empty_for_the_live_dashboards_own_base_url():
    assert _site_prefix("/browse/crimes-act") == ""


def test_site_prefix_is_the_part_before_browse_for_a_project_pages_site():
    assert _site_prefix("/vic-legislation-parser/browse/crimes-act") == "/vic-legislation-parser"


def test_legislation_href_has_no_prefix_by_default():
    assert _legislation_href({"act_no": "68", "year": 2009}) == "/legislation/68-2009"


def test_legislation_href_omits_the_year_when_not_known():
    assert _legislation_href({"act_no": "68", "year": None}) == "/legislation/68"


def test_legislation_href_carries_the_site_prefix_from_base_url():
    href = _legislation_href({"act_no": "68", "year": 2009}, "/vic-legislation-parser/browse/crimes-act")
    assert href == "/vic-legislation-parser/legislation/68-2009"


# ---------------------------------------------------------------------
# The landing page's disclaimers. Pinned down rather than left to eyeball
# because they're the site's legal caveat: a refactor that quietly
# dropped one would leave unofficial text looking authoritative.
# ---------------------------------------------------------------------

def _doc(published=3, total=3):
    return {
        "slug": "crimes-act", "title": "Crimes Act 1958", "kind": "act",
        "as_at": "1 May 2026", "pages": total + 1,
        "published_provisions": published, "total_provisions": total,
    }


_DOC = _doc()


def test_the_landing_page_says_how_much_of_a_part_published_act_is_there():
    page = _landing_page_html([_doc(published=12, total=112)], "")
    assert "12 of 112 provisions" in page


def test_a_fully_published_act_gets_no_provision_count():
    """Once the answer is always "all of them", the count is noise.
    Asserted against the document's own list entry rather than the rest
    of the page, which carries scripts of its own that say "provisions"
    for unrelated reasons."""
    page = _landing_page_html([_doc(published=112, total=112)], "")
    entry = page.split('<ul class="section-list">')[1].split("</ul>")[0]
    assert "provisions" not in entry


def test_the_landing_page_disclaims_before_the_heading():
    """Above the fold means before <h1>, not somewhere further down."""
    page = _landing_page_html([_DOC], "")
    assert "These are not official legislative texts." in page
    assert page.index("not official legislative texts") < page.index("<h1>")


def test_the_landing_page_points_at_the_official_source():
    page = _landing_page_html([_DOC], "")
    assert OFFICIAL_SOURCE_URL in page
    assert "authorised legislative texts" in page


def test_the_landing_page_footer_repeats_the_caveat_and_adds_the_rest():
    page = _landing_page_html([_DOC], "")
    footer = page[page.index('<footer class="site-footer">'):]
    assert "These are not official legislative texts." in footer
    assert "does not provide legal advice or commentary" in footer
    assert "at their own risk" in footer


def test_the_disclaimers_are_there_even_with_nothing_published():
    """An empty site is still making the same claim about itself."""
    page = _landing_page_html([], "")
    assert "These are not official legislative texts." in page
    assert "at their own risk" in page


def test_every_page_built_through_the_shell_carries_the_footer():
    """The landing page isn't where most readers arrive -- a shared link
    to one provision is -- so the footer belongs on whatever page they
    land on, not just the front door."""
    page = _page("Crimes Act 1958", "<h1>3 Definitions</h1>", "/browse/crimes-act")
    assert "These are not official legislative texts." in page
    assert "does not provide legal advice or commentary" in page
    assert "at their own risk" in page


def test_the_landing_page_carries_the_footer_exactly_once():
    """It builds its own disclaimer banner and goes through the same
    shell as everything else -- easy to end up with two footers."""
    assert _landing_page_html([_DOC], "").count('<footer class="site-footer">') == 1


# ---------------------------------------------------------------------
# The passphrase gate (ai_pipeline/site_crypto.py). Fewer PBKDF2
# iterations than the real thing throughout, so the suite doesn't spend
# a second per test deriving keys it only needs to be *a* key.
# ---------------------------------------------------------------------

_ITERATIONS = 100
_PAGE = "<!doctype html><html><body><h1>Section 14</h1></body></html>"


def _payload(wrapped: str) -> dict:
    match = re.search(r'<script type="application/json" id="payload">(.*?)</script>', wrapped, re.DOTALL)
    assert match, "the gate page should carry its payload as embedded JSON"
    return json.loads(match.group(1))


def test_a_gate_refuses_to_be_built_without_a_passphrase():
    with pytest.raises(ValueError):
        SiteGate("")


def test_the_wrapped_page_does_not_contain_the_plaintext():
    """The whole point: what gets published is ciphertext, not the real
    page with something drawn over it. (The gate has markup of its own --
    an <h1> and so on -- so this checks for the content, not for tags.)"""
    wrapped = SiteGate("a long testing passphrase", iterations=_ITERATIONS).wrap(_PAGE)
    assert "Section 14" not in wrapped
    assert _PAGE not in wrapped


def test_the_wrapped_page_decrypts_back_to_the_original_with_the_right_passphrase():
    gate = SiteGate("a long testing passphrase", iterations=_ITERATIONS)
    payload = _payload(gate.wrap(_PAGE))

    key = derive_key("a long testing passphrase", base64.b64decode(payload["salt"]), payload["iterations"])
    plain = AESGCM(key).decrypt(base64.b64decode(payload["iv"]), base64.b64decode(payload["ct"]), None)

    assert plain.decode("utf-8") == _PAGE


def test_a_wrong_passphrase_fails_to_decrypt_rather_than_returning_garbage():
    """AES-GCM's own authentication tag is the password check -- there's
    no separate "is this right?" value for the unlock page to compare."""
    gate = SiteGate("the real passphrase", iterations=_ITERATIONS)
    payload = _payload(gate.wrap(_PAGE))

    wrong = derive_key("not the passphrase", base64.b64decode(payload["salt"]), payload["iterations"])
    with pytest.raises(InvalidTag):
        AESGCM(wrong).decrypt(base64.b64decode(payload["iv"]), base64.b64decode(payload["ct"]), None)


def test_every_page_gets_its_own_iv_under_one_build_key():
    """Reusing an IV across pages encrypted with the same key is the one
    way to actually break AES-GCM, so this is worth pinning down."""
    gate = SiteGate("a long testing passphrase", iterations=_ITERATIONS)
    ivs = {_payload(gate.wrap(f"<p>page {i}</p>"))["iv"] for i in range(5)}
    assert len(ivs) == 5


def test_one_builds_salt_is_shared_so_unlocking_once_unlocks_every_page():
    gate = SiteGate("a long testing passphrase", iterations=_ITERATIONS)
    salts = {_payload(gate.wrap(f"<p>page {i}</p>"))["salt"] for i in range(3)}
    assert len(salts) == 1


def test_two_builds_do_not_share_a_salt():
    a = SiteGate("a long testing passphrase", iterations=_ITERATIONS)
    b = SiteGate("a long testing passphrase", iterations=_ITERATIONS)
    assert a.salt != b.salt


def test_the_gate_page_says_nothing_about_the_page_it_holds():
    """Not even the title: the file set should give away how many pages
    exist and nothing else."""
    wrapped = SiteGate("a long testing passphrase", iterations=_ITERATIONS).wrap(
        "<!doctype html><html><head><title>Crimes Act 1958 — s 3</title></head><body>x</body></html>"
    )
    assert "Crimes Act" not in wrapped
    assert "<title>Password required</title>" in wrapped


def test_the_gate_page_asks_crawlers_not_to_index_it():
    wrapped = SiteGate("a long testing passphrase", iterations=_ITERATIONS).wrap(_PAGE)
    assert '<meta name="robots" content="noindex, nofollow">' in wrapped


def test_the_published_assets_are_the_template_directory(tmp_path):
    """Every page points its stylesheets, scripts and fonts at assets/, so
    a build that didn't copy them would publish text with no styling at
    all -- and page.html is the one file Python renders rather than the
    browser fetching, so it has no business being served."""
    from export_static_site import _copy_template

    _copy_template(tmp_path)
    published = {p.name for p in (tmp_path / "assets").rglob("*")}

    assert {"tokens.css", "page.css", "reader.css", "theme.js", "copy.js",
            "reader.js", "preview.js"} <= published
    assert "Junicode-Roman.woff2" in published
    assert "OFL.txt" in published, "the font's licence has to travel with it"
    assert "page.html" not in published
