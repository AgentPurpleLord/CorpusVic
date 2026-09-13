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
from export_static_site import (
    OFFICIAL_SOURCE_URL,
    _landing_page_html,
    _page,
    select_published_slugs,
)


def _status(parsed=True, review_status="reviewed"):
    return {"parsed": parsed, "review_status": review_status}


def test_select_published_slugs_skips_an_unparsed_document():
    statuses = {"crimes-act": _status(parsed=False)}
    assert select_published_slugs(statuses) == []


def test_select_published_slugs_includes_a_reviewed_unversioned_document():
    statuses = {"crimes-act": _status(review_status="reviewed")}
    assert select_published_slugs(statuses) == ["crimes-act"]


def test_select_published_slugs_excludes_a_document_not_fully_reviewed():
    statuses = {
        "crimes-act": _status(review_status="in-progress"),
        "evidence-act": _status(review_status="not-started"),
    }
    assert select_published_slugs(statuses) == []


def test_select_published_slugs_publishes_only_the_newest_version_of_a_work():
    statuses = {
        "criminal-procedure-act-v110": _status(review_status="reviewed"),
        "criminal-procedure-act-v114": _status(review_status="reviewed"),
    }
    assert select_published_slugs(statuses) == ["criminal-procedure-act-v114"]


def test_select_published_slugs_excludes_a_work_whose_newest_version_isnt_reviewed():
    """Even though an older version was fully reviewed, that's not what
    would be shown -- browsing this work always means its newest version,
    so nothing is published until *that* one is ready."""
    statuses = {
        "criminal-procedure-act-v110": _status(review_status="reviewed"),
        "criminal-procedure-act-v114": _status(review_status="in-progress"),
    }
    assert select_published_slugs(statuses) == []


def test_select_published_slugs_treats_independent_works_independently():
    statuses = {
        "crimes-act": _status(review_status="reviewed"),
        "evidence-act": _status(review_status="not-started"),
    }
    assert select_published_slugs(statuses) == ["crimes-act"]


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

_DOC = {"slug": "crimes-act", "title": "Crimes Act 1958", "kind": "act", "as_at": "1 May 2026", "pages": 3}


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
