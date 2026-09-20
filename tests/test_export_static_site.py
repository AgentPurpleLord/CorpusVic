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

from corpus.publishing import html_view
from corpus.publishing.html_view import _legislation_href, _site_prefix
from corpus.publishing.site_crypto import SiteGate, derive_key
from conftest import make_node
from corpus.exporters import export_static_site, export_static_site as ess
from corpus.exporters.export_static_site import (
    OFFICIAL_SOURCE_URL,
    _landing_page_html,
    _page,
    approved_page_slugs,
    approved_units,
    select_candidate_slugs,
)


def _status(parsed=True, review_status="reviewed", published=True):
    """Published by default, so that the tests either side of this are
    about the question they were written to ask -- whether a document is
    eligible at all -- rather than about publication, which has its own
    tests below."""
    return {"parsed": parsed, "review_status": review_status, "published": published}


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


def test_every_parsed_version_is_a_candidate():
    """Older reprints are published too: "Compare with another version"
    offers every version held, and an offer that 404s is worse than no
    offer."""
    statuses = {
        "criminal-procedure-act-v110": _status(),
        "criminal-procedure-act-v114": _status(),
        "half-parsed-act": _status(parsed=False),
    }
    assert select_candidate_slugs(statuses) == [
        "criminal-procedure-act-v110", "criminal-procedure-act-v114"]


def test_a_work_nobody_put_on_the_site_is_not_built():
    """Publication is a decision somebody makes on the dashboard, not
    something a document earns by being parsed."""
    statuses = {
        "crimes-act": _status(published=True),
        "evidence-act": _status(published=False),
    }
    assert select_candidate_slugs(statuses) == ["crimes-act"]


def test_a_status_with_no_publication_answer_is_not_built():
    """A document from before the flag existed, or one whose status came
    from somewhere that doesn't fill it in. Silence is not consent."""
    assert select_candidate_slugs({"crimes-act": {"parsed": True, "review_status": "reviewed"}}) == []


def test_an_archive_of_everything_has_to_be_asked_for():
    """A build that published more than the site does would be a way to
    publish something by accident, so it is a flag rather than a
    default."""
    statuses = {
        "crimes-act": _status(published=True),
        "evidence-act": _status(published=False),
        "not-parsed-act": _status(parsed=False, published=True),
    }
    assert select_candidate_slugs(statuses, include_unpublished=True) == [
        "crimes-act", "evidence-act"]


def test_publication_never_splits_a_works_version_set():
    """It is decided per work, so every reprint answers the same -- which
    is what lets site_slugs keep its promise that the newest version
    holds the work's own address."""
    statuses = {
        "criminal-procedure-act-v110": _status(published=True),
        "criminal-procedure-act-v114": _status(published=True),
        "evidence-act": _status(published=False),
    }
    chosen = select_candidate_slugs(statuses)
    assert chosen == ["criminal-procedure-act-v110", "criminal-procedure-act-v114"]
    from corpus.exporters.export_static_site import site_slugs
    assert site_slugs(chosen)["criminal-procedure-act-v114"] == "criminal-procedure-act"


def test_the_newest_version_is_published_without_a_version_in_its_address():
    """"/browse/criminal-procedure-act/" is the Act as it now stands and
    stays that address as new reprints land; an older one keeps its
    versioned name, so a link to it still means that version a year from
    now. It is also the address known_acts.yaml has always pointed every
    cross-Act reference at."""
    from corpus.exporters.export_static_site import site_slugs

    assert site_slugs([
        "crimes-act", "criminal-procedure-act-v110", "criminal-procedure-act-v114",
    ]) == {
        "crimes-act": "crimes-act",
        "criminal-procedure-act-v110": "criminal-procedure-act-v110",
        "criminal-procedure-act-v114": "criminal-procedure-act",
    }


def test_a_dashboard_url_is_rewritten_to_the_published_address():
    """dashboard.py builds its URLs for a server at a domain root, naming
    documents by their parse slug. The published site is neither."""
    from corpus.exporters.export_static_site import _rewrite_urls

    slugs = {"criminal-procedure-act-v114": "criminal-procedure-act"}
    rewrite = lambda v: _rewrite_urls(v, "/repo", slugs)

    assert rewrite("/browse/criminal-procedure-act-v114/section/s5") == \
        "/repo/browse/criminal-procedure-act/section/s5"
    assert rewrite("/browse/criminal-procedure-act-v110/") == \
        "/repo/browse/criminal-procedure-act-v110/"
    # The shapes dashboard.py actually hands back.
    assert rewrite({114: "/browse/criminal-procedure-act-v114/"}) == \
        {114: "/repo/browse/criminal-procedure-act/"}
    assert rewrite([{"href": "/browse/criminal-procedure-act-v114/section/s5"}]) == \
        [{"href": "/repo/browse/criminal-procedure-act/section/s5"}]
    # Anything that isn't a browse URL is left exactly as it was.
    assert rewrite("https://www.legislation.vic.gov.au") == "https://www.legislation.vic.gov.au"
    assert rewrite("/legislation/6231") == "/legislation/6231"
    assert rewrite(None) is None


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
    assert _site_prefix("/corpusvic/browse/crimes-act") == "/corpusvic"


def test_legislation_href_has_no_prefix_by_default():
    assert _legislation_href({"act_no": "68", "year": 2009}) == "/legislation/68-2009"


def test_legislation_href_omits_the_year_when_not_known():
    assert _legislation_href({"act_no": "68", "year": None}) == "/legislation/68"


def test_legislation_href_carries_the_site_prefix_from_base_url():
    href = _legislation_href({"act_no": "68", "year": 2009}, "/corpusvic/browse/crimes-act")
    assert href == "/corpusvic/legislation/68-2009"


# ---------------------------------------------------------------------
# The landing page's disclaimers. Pinned down rather than left to eyeball
# because they're the site's legal caveat: a refactor that quietly
# dropped one would leave unofficial text looking authoritative.
# ---------------------------------------------------------------------

def _doc(checked=3, total=3, slug="crimes-act", site_slug=None):
    return {
        "slug": slug, "site_slug": site_slug or slug,
        "title": "Crimes Act 1958", "kind": "act",
        "as_at": "1 May 2026", "pages": total + 1,
        "checked_provisions": checked, "total_provisions": total,
    }


_DOC = _doc()


def test_the_landing_page_says_how_much_of_an_act_has_been_checked():
    """The whole Act is published either way -- what this counts is how
    much of it a human has confirmed against the PDF."""
    page = _landing_page_html([_doc(checked=12, total=112)], "")
    assert "12 of 112 provisions checked" in page


def test_a_fully_checked_act_gets_no_provision_count():
    """Once the answer is always "all of them", the count is noise.
    Asserted against the document's own list entry rather than the rest
    of the page, which carries scripts of its own that say "provisions"
    for unrelated reasons."""
    page = _landing_page_html([_doc(checked=112, total=112)], "")
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


# The footer's wording lives in static/site/footer.html and is meant to
# be edited without asking anyone. So these check what the footer has to
# *do* -- say the text is not official, and point at the text that is --
# rather than one exact sentence. A test that fails on every rewording is
# a test that gets deleted the first time it is inconvenient, and then
# the page ships with no notice at all.


def _says_it_is_not_official(html: str) -> bool:
    lowered = html.lower()
    return "not" in lowered and ("official" in lowered or "authoris" in lowered)


def test_the_landing_page_footer_repeats_the_caveat_and_adds_the_rest():
    page = _landing_page_html([_DOC], "")
    footer = page[page.index('<footer class="site-footer">'):]
    assert _says_it_is_not_official(footer)
    assert "own risk" in footer
    assert OFFICIAL_SOURCE_URL in footer, "the notice has to reach the authorised text"


def test_the_disclaimers_are_there_even_with_nothing_published():
    """An empty site is still making the same claim about itself."""
    page = _landing_page_html([], "")
    assert _says_it_is_not_official(page)
    assert "own risk" in page


def test_every_page_built_through_the_shell_carries_the_footer():
    """The landing page isn't where most readers arrive -- a shared link
    to one provision is -- so the footer belongs on whatever page they
    land on, not just the front door."""
    page = _page("Crimes Act 1958", "<h1>3 Definitions</h1>", "/browse/crimes-act")
    assert '<footer class="site-footer">' in page
    assert _says_it_is_not_official(page)
    assert "own risk" in page


def test_the_footer_is_read_from_the_file_so_it_can_be_edited(tmp_path, monkeypatch):
    """The point of moving it out of Python. It used to be a constant in
    export_static_site.py while an editable footer.html sat beside the
    stylesheets loaded by nothing -- so editing the obvious file did
    nothing, silently."""
    from corpus import html_view

    original = (html_view.TEMPLATE_DIR / "footer.html").read_text(encoding="utf-8")
    try:
        (html_view.TEMPLATE_DIR / "footer.html").write_text(
            '<!-- a note to self -->\n'
            '<footer class="site-footer"><p>Rewritten by hand.</p></footer>',
            encoding="utf-8")
        page = _page("Crimes Act 1958", "<h1>3 Definitions</h1>", "/browse/crimes-act")
    finally:
        (html_view.TEMPLATE_DIR / "footer.html").write_text(original, encoding="utf-8")

    assert "Rewritten by hand." in page
    assert "a note to self" not in page, "authoring comments are for the editor, not the reader"


def test_the_landing_page_carries_the_footer_exactly_once():
    """It builds its own disclaimer banner and goes through the same
    shell as everything else -- easy to end up with two footers."""
    assert _landing_page_html([_DOC], "").count('<footer class="site-footer">') == 1


# ---------------------------------------------------------------------
# The passphrase gate (corpus/site_crypto.py). Fewer PBKDF2
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
    from corpus.exporters.export_static_site import _copy_template

    _copy_template(tmp_path)
    published = {p.name for p in (tmp_path / "assets").rglob("*")}

    assert {"tokens.css", "page.css", "reader.css", "theme.js", "copy.js",
            "reader.js", "preview.js"} <= published
    assert {"Junicode-Roman.woff2", "Inter.woff2"} <= published
    assert {"Junicode-OFL.txt", "Inter-OFL.txt"} <= published, \
        "each font's licence has to travel with it"
    assert "page.html" not in published


# ---------------------------------------------------------------------------
# Hover previews
# ---------------------------------------------------------------------------
def _targets(html: str, base_path: str = "") -> set:
    from corpus.exporters.export_static_site import _link_targets

    return _link_targets(html, base_path)


def test_a_link_into_a_provision_is_a_preview_target():
    html = '<a href="/browse/crimes-act/section/s3#def-accused">accused</a>'

    assert _targets(html) == {("crimes-act", "s3", "def-accused")}


def test_a_bare_section_link_and_an_index_anchor_are_targets_too():
    html = ('<a href="/browse/a/section/s5">section 5</a>'
            '<a href="/browse/a/#part-2">Part 2</a>')

    assert _targets(html) == {("a", "s5", ""), ("a", "", "part-2")}


def test_links_outside_the_site_are_not_preview_targets():
    html = ('<a href="https://www.legislation.vic.gov.au">official</a>'
            '<a href="/legislation/6231">Crimes Act 1958</a>'
            '<a href="/browse/a/endnotes">Endnotes</a>')

    assert _targets(html) == set()


def test_targets_are_read_against_the_sites_own_prefix():
    """On a GitHub Pages project site every link carries /<repo>/, and a
    collector that ignored that would treat the repo name as the slug."""
    html = '<a href="/corpusvic/browse/a/section/s5#s5-1">s 5(1)</a>'

    assert _targets(html, "/corpusvic") == {("a", "s5", "s5-1")}
    assert _targets(html, "") == set()


def _preview_site(tmp_path, targets, published, gate=None):
    from corpus.exporters.export_static_site import _write_previews

    written = _write_previews(tmp_path, "", targets, published, gate)
    return written, tmp_path


def test_a_preview_is_not_written_for_a_page_the_site_does_not_have(tmp_path, monkeypatch):
    """A link can point somewhere this build didn't produce -- a document
    that isn't published, or a page id it doesn't hold. A hover card for
    it would show text behind a link that 404s."""

    calls = []
    monkeypatch.setattr(corpus.publishing.html_view, "render_preview",
                        lambda *a, **k: calls.append(a) or {"title": "x", "html": ""})
    monkeypatch.setattr(corpus.web.dashboard, "_current_nodes", lambda slug: ([], [], None))
    monkeypatch.setattr(corpus.web.dashboard, "_act_title", lambda slug: "Test Act")

    written, out = _preview_site(
        tmp_path,
        {("a", "s1", ""), ("a", "s2", ""), ("b", "s1", "")},
        # The site has a/s1 only: s2 is not among its pages, and
        # document b was not published at all.
        {"a": ("a-v3", {"s1"})},
    )

    assert written == 1
    # Written under the published address, from the parse the site slug
    # stands for.
    assert calls and calls[0][1] == "Test Act"
    assert (out / "browse/a/section/s1/preview.json").exists()
    assert not (out / "browse/a/section/s2").exists()
    assert not (out / "browse/b").exists()


def test_a_gated_build_encrypts_its_previews(tmp_path, monkeypatch):
    """A preview is the provision's own words. Publishing it in the clear
    beside an encrypted page would hand over exactly what the gate is
    there to keep back."""
    import json

    from corpus.publishing.site_crypto import SiteGate

    monkeypatch.setattr(corpus.publishing.html_view, "render_preview",
                        lambda *a, **k: {"title": "Act", "html": "<p>a secret provision</p>"})
    monkeypatch.setattr(corpus.web.dashboard, "_current_nodes", lambda slug: ([], [], None))
    monkeypatch.setattr(corpus.web.dashboard, "_act_title", lambda slug: "Test Act")

    gate = SiteGate("a long enough passphrase")
    _preview_site(tmp_path, {("a", "s1", "")}, {"a": ("a-v3", {"s1"})}, gate)
    payload = json.loads((tmp_path / "browse/a/section/s1/preview.json").read_text(encoding="utf-8"))

    assert set(payload) == {"iv", "ct"}
    assert "secret" not in (tmp_path / "browse/a/section/s1/preview.json").read_text(encoding="utf-8")


def test_a_published_page_says_where_its_previews_come_from(tmp_path):
    """The script can't tell a static host from a server by looking, and
    guessing wrong means either a 404 on every hover or no card at all."""
    from corpus.exporters.export_static_site import _page

    page = _page("Test Act", "<p>body</p>", "/browse/a", reader=True)

    assert 'data-preview="static"' in page


def test_a_published_section_carries_no_review_badge():
    """On a site that only publishes checked provisions the badge reads
    "Fully reviewed" on every page, which tells a reader nothing -- the
    same reason the contents page already drops it."""
    from corpus.publishing.html_view import render_section

    nodes = [
        {"type": "part", "number": "1", "heading": "Preliminary", "text": None},
        {"type": "section", "number": "1", "heading": "Purposes", "text": "This Act—"},
    ]
    parsed = {"nodes": nodes, "hierarchy": None}
    assert "verify-badge" in render_section(parsed, "A", "/browse/a", "s1")
    assert "verify-badge" not in render_section(parsed, "A", "/browse/a", "s1", show_review_badge=False)


def test_a_bill_and_its_em_are_not_listed_beside_their_act():
    """An Explanatory Memorandum is written about a Bill, and a Bill
    becomes an Act: they belong to that Act, whose own contents page
    offers them. Listing all three side by side would present them as
    separate publications and make choosing between them the reader's
    first problem."""

    published = [
        _doc(slug="crimes-act"),
        dict(_doc(slug="crimes-bill"), kind="bill"),
        dict(_doc(slug="crimes-bill-em"), kind="em"),
    ]
    original = corpus.web.dashboard.related_documents
    corpus.web.dashboard.related_documents = lambda slug: (
        [{"slug": "crimes-bill", "kind": "bill"}, {"slug": "crimes-bill-em", "kind": "em"}]
        if slug == "crimes-act" else []
    )
    try:
        page = ess._landing_page_html(published, "")
    finally:
        corpus.web.dashboard.related_documents = original
    entries = page.split('<ul class="section-list">')[1].split("</ul>")[0]

    assert "/browse/crimes-act/" in entries
    assert "crimes-bill" not in entries


def test_a_bill_no_published_act_claims_is_still_listed():
    """Better an odd entry on the front page than a document nothing
    reaches."""
    import corpus.exporters.export_static_site as ess

    original = corpus.web.dashboard.related_documents
    corpus.web.dashboard.related_documents = lambda slug: []
    try:
        page = ess._landing_page_html([dict(_doc(slug="orphan-bill"), kind="bill")], "")
    finally:
        corpus.web.dashboard.related_documents = original

    assert "/browse/orphan-bill/" in page


# ---------------------------------------------------------------------
# Where the site is published
# ---------------------------------------------------------------------

def test_a_custom_domain_means_no_path_prefix(tmp_path, monkeypatch):
    """A custom domain is mapped at its own root. With the "/repo"
    prefix a project site needs, every link on the site resolved to
    https://www.corpusvic.au/corpusvic/browse/..., which is
    nowhere."""
    monkeypatch.setattr(export_static_site, "CNAME_FILE", tmp_path / "CNAME")
    (tmp_path / "CNAME").write_text("www.corpusvic.au\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", "AgentPurpleLord/corpusvic")

    assert export_static_site.custom_domain() == "www.corpusvic.au"
    assert export_static_site._default_base_path() == ""


def test_without_a_custom_domain_a_project_site_keeps_its_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(export_static_site, "CNAME_FILE", tmp_path / "CNAME")
    monkeypatch.setenv("GITHUB_REPOSITORY", "AgentPurpleLord/corpusvic")

    assert export_static_site.custom_domain() is None
    assert export_static_site._default_base_path() == "/corpusvic"


def test_a_local_preview_has_no_prefix_either(tmp_path, monkeypatch):
    monkeypatch.setattr(export_static_site, "CNAME_FILE", tmp_path / "CNAME")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert export_static_site._default_base_path() == ""


def test_an_empty_cname_is_not_a_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(export_static_site, "CNAME_FILE", tmp_path / "CNAME")
    (tmp_path / "CNAME").write_text("\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert export_static_site.custom_domain() is None


def test_the_repository_names_the_domain_the_site_is_published_at():
    """The real CNAME, checked as itself: it is what tells GitHub Pages
    to keep serving www.corpusvic.au, and what keeps every link on the
    site unprefixed."""
    assert export_static_site.custom_domain() == "www.corpusvic.au"


# ---------------------------------------------------------------------
# Publishing unchecked text
# ---------------------------------------------------------------------
# The site used to hold back any provision a human hadn't confirmed,
# leaving a placeholder page in its place. It reads as an Act with holes
# in it, and for legislation that is the dangerous reading: a section
# that is merely unchecked looked exactly like a section that does not
# exist. Everything is published now, and what has not been checked says
# so on itself.

def _fake_document(monkeypatch, verified: set):
    """One tiny two-section Act, with `verified` naming the section
    numbers a reviewer has confirmed. Every dashboard helper _build_doc
    reaches for is answered here, so the test is about what gets written
    rather than about the real corpus."""

    def node(number, heading, text):
        n = make_node("section", number, heading)
        body = make_node("subsection", "1", None, text)
        if number in verified:
            n["verified_at"] = body["verified_at"] = "2026-01-01T00:00:00+00:00"
        return [n, body]

    nodes = node("1", "Short title", "This Act may be cited as the Test Act.") \
        + node("2", "Commencement", "This Act comes into operation on 1 July.")

    monkeypatch.setattr(corpus.web.dashboard, "_current_nodes", lambda slug: (nodes, [], None))
    monkeypatch.setattr(corpus.web.dashboard, "_act_title", lambda slug: "Test Act")
    monkeypatch.setattr(corpus.web.dashboard, "_amendments",
                        lambda slug: {"endnotes": None, "index": {}, "summary": {}})
    monkeypatch.setattr(corpus.web.dashboard, "_act_version", lambda slug: {})
    monkeypatch.setattr(corpus.web.dashboard, "_version_dates", lambda slug: {})
    monkeypatch.setattr(corpus.web.dashboard, "_timeline", lambda work: {})
    monkeypatch.setattr(corpus.web.dashboard, "_superseded", lambda slug: None)
    monkeypatch.setattr(corpus.web.dashboard, "related_documents", lambda slug: [])
    monkeypatch.setattr(corpus.web.dashboard, "_provision_timeline", lambda *a: ([], {}))
    monkeypatch.setattr(corpus.web.dashboard, "_section_crossrefs", lambda *a: [])
    monkeypatch.setattr(corpus.web.dashboard, "act_status",
                        lambda slug: {"kind": "act", "version_as_at": "1 July 2026"})
    monkeypatch.setattr(
        corpus.web.dashboard, "_page_index",
        lambda slug: html_view.build_page_index({"nodes": nodes, "hierarchy": None}, "Test Act"))
    return nodes


def _built(tmp_path, monkeypatch, verified: set):
    from corpus.exporters.export_static_site import _build_doc

    _fake_document(monkeypatch, verified)
    summary = _build_doc("test-act", tmp_path, "")
    read = lambda p: (tmp_path / "browse/test-act" / p).read_text(encoding="utf-8")
    return summary, read


def test_an_unchecked_provision_is_published_with_its_own_text(tmp_path, monkeypatch):
    """The whole point. Its text used to be withheld behind a page saying
    it hadn't been published yet."""
    _summary, read = _built(tmp_path, monkeypatch, verified={"1"})
    page = read("section/s2/index.html")

    assert "This Act comes into operation on 1 July." in page
    assert "hasn’t been published here yet" not in page


def test_an_unchecked_provision_says_it_has_not_been_checked(tmp_path, monkeypatch):
    _summary, read = _built(tmp_path, monkeypatch, verified={"1"})
    page = read("section/s2/index.html")

    assert "This provision has not been checked by a human." in page
    assert "legislation.vic.gov.au" in page, "and says where the authorised text is"


def test_a_checked_provision_carries_no_such_notice(tmp_path, monkeypatch):
    """The notice has to mean something, which it stops doing the moment
    it is on every page."""
    _summary, read = _built(tmp_path, monkeypatch, verified={"1"})
    page = read("section/s1/index.html")

    assert "This Act may be cited as the Test Act." in page
    assert "has not been checked by a human" not in page


def test_the_contents_page_tags_nothing(tmp_path, monkeypatch):
    """Nothing is held back, so nothing is marked as held back."""
    _summary, read = _built(tmp_path, monkeypatch, verified={"1"})
    index = read("index.html")

    assert "not yet published" not in index
    assert "unpublished-tag" not in index
    assert 'href="/browse/test-act/section/s2"' in index, "still listed and still linked"


def test_the_contents_page_says_how_much_has_been_checked(tmp_path, monkeypatch):
    _summary, read = _built(tmp_path, monkeypatch, verified={"1"})

    assert "Only part of this document has been reviewed." in read("index.html")
    assert "1 of 2 provisions have been checked by a human" in read("index.html")


def test_a_fully_checked_document_says_nothing_about_checking(tmp_path, monkeypatch):
    _summary, read = _built(tmp_path, monkeypatch, verified={"1", "2"})
    index = read("index.html")

    assert "have been checked by a human" not in index
    assert "has not been checked by a human" not in read("section/s2/index.html")


def test_a_document_nobody_has_checked_at_all_is_still_published(tmp_path, monkeypatch):
    """It used to publish nothing, so an Act awaiting review was absent
    from the site entirely."""
    summary, read = _built(tmp_path, monkeypatch, verified=set())

    assert summary is not None
    assert summary["checked_provisions"] == 0
    assert summary["total_provisions"] == 2
    assert "This Act may be cited as the Test Act." in read("section/s1/index.html")
    assert "0 of 2 provisions have been checked by a human" in read("index.html")


def test_every_page_is_offered_for_preview(tmp_path, monkeypatch):
    """A hover card used to be withheld from an unapproved provision
    because its page withheld the text. The page shows it now, so the
    card that stands for that page can too."""
    summary, _read = _built(tmp_path, monkeypatch, verified={"1"})

    assert summary["published_pages"] == {"s1", "s2"}


# ---------------------------------------------------------------------
# Keeping the published site's passphrase
# ---------------------------------------------------------------------
# The gate came off the live site for a dull reason: the build was moved
# to a server, the passphrase was a shell variable that did not come with
# it, and an ungated build is a supported configuration rather than an
# error -- so nothing said anything. Pages served once in the clear stay
# served, so that has to be a choice rather than something arrived at.

def _gated_site(tmp_path):
    from corpus.publishing.site_crypto import SiteGate

    (tmp_path / "index.html").write_text(SiteGate("a passphrase").wrap("<h1>Hi</h1>"), encoding="utf-8")
    return tmp_path


def test_a_gated_build_is_recognised(tmp_path):
    from corpus.exporters.export_static_site import already_gated

    assert already_gated(_gated_site(tmp_path)) is True


def test_an_open_build_is_not_mistaken_for_a_gated_one(tmp_path):
    from corpus.exporters.export_static_site import already_gated

    (tmp_path / "index.html").write_text("<h1>Published legislation</h1>", encoding="utf-8")

    assert already_gated(tmp_path) is False


def test_nothing_built_yet_is_not_gated(tmp_path):
    from corpus.exporters.export_static_site import already_gated

    assert already_gated(tmp_path / "never-built") is False


@pytest.mark.parametrize("contents, expected", [
    ("SITE_PASSWORD=plain value\n", "plain value"),
    ("# a comment\nSITE_PASSWORD='quoted value'\n", "quoted value"),
    ('SITE_PASSWORD="double quoted"\n', "double quoted"),
    ("SITE_PASSWORD=\n", None),
    ("SOMETHING_ELSE=x\n", None),
])
def test_the_passphrase_is_read_from_the_servers_own_file(tmp_path, contents, expected):
    from corpus.exporters.export_static_site import password_from_env_file

    path = tmp_path / "site.env"
    path.write_text(contents, encoding="utf-8")

    assert password_from_env_file(path) == expected


def test_no_file_means_no_passphrase(tmp_path):
    from corpus.exporters.export_static_site import password_from_env_file

    assert password_from_env_file(tmp_path / "absent.env") is None


def test_rebuilding_a_gated_site_with_no_passphrase_is_refused(tmp_path, monkeypatch):
    """The exact thing that happened. It has to stop rather than publish,
    and stop with a non-zero exit, because the build that would do this
    unattended is a nightly timer with nobody reading its output."""
    from corpus.exporters.export_static_site import resolve_password

    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    monkeypatch.setattr("export_static_site.SITE_ENV_FILE", tmp_path / "absent.env")

    with pytest.raises(SystemExit) as refused:
        resolve_password(None, False, _gated_site(tmp_path))

    assert "--no-password" in str(refused.value), "and says how to do it deliberately"


def test_opening_a_gated_site_deliberately_is_allowed(tmp_path, monkeypatch):
    from corpus.exporters.export_static_site import resolve_password

    monkeypatch.delenv("SITE_PASSWORD", raising=False)

    assert resolve_password(None, True, _gated_site(tmp_path)) is None


def test_an_ungated_site_rebuilds_ungated_without_complaint(tmp_path, monkeypatch):
    """Only a gate that already exists is protected. A site that was
    never behind one is not suddenly required to be."""
    from corpus.exporters.export_static_site import resolve_password

    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    monkeypatch.setattr("export_static_site.SITE_ENV_FILE", tmp_path / "absent.env")
    (tmp_path / "index.html").write_text("<h1>Open</h1>", encoding="utf-8")

    assert resolve_password(None, False, tmp_path) is None


def test_the_environment_is_preferred_to_the_command_line(tmp_path, monkeypatch):
    """A passphrase on the command line is visible to anything that can
    list processes, so it is the last resort rather than the first."""
    from corpus.exporters.export_static_site import resolve_password

    monkeypatch.setenv("SITE_PASSWORD", "from the environment")

    assert resolve_password("typed on the line", False, tmp_path) == "from the environment"


def test_the_servers_file_is_preferred_to_the_command_line(tmp_path, monkeypatch):
    from corpus.exporters.export_static_site import resolve_password

    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    path = tmp_path / "site.env"
    path.write_text("SITE_PASSWORD=from the file\n", encoding="utf-8")
    monkeypatch.setattr("export_static_site.SITE_ENV_FILE", path)

    assert resolve_password("typed on the line", False, tmp_path) == "from the file"


# ---------------------------------------------------------------------
# Who is allowed to crawl it
# ---------------------------------------------------------------------
# robots.txt used to be written only on a gated build, which had the two
# cases backwards: the gated site publishes ciphertext, so a crawler
# ignoring the file would index gibberish, while the open site publishes
# thousands of provisions of mostly unchecked legal text -- and that was
# the build that got no robots.txt at all. The site was live in that
# state.

@pytest.mark.parametrize("gated, allow_indexing, indexable", [
    # The state it was actually in, and the one that matters most.
    (False, False, False),
    (False, True, True),
    (True, False, False),
    # Asking to index a site that is behind a passphrase is a
    # contradiction, resolved the safe way round.
    (True, True, False),
])
def test_crawling_is_asked_for_rather_than_arrived_at(gated, allow_indexing, indexable):
    from corpus.exporters.export_static_site import robots_txt_for

    robots = robots_txt_for(gated, allow_indexing)

    assert ("Disallow: /" in robots) is not indexable
    assert robots.startswith("User-agent: *"), "whatever the answer, it is stated"


# ---------------------------------------------------------------------
# What the contents page says about how much has been checked
# ---------------------------------------------------------------------


def test_the_partial_notice_leads_with_the_state_not_the_arithmetic():
    """A sentence that opens on two numbers makes a reader do the
    division before they learn anything. What they need first is that
    part of this is unreviewed."""
    from corpus.exporters.export_static_site import _partial_notice_html
    import re

    text = re.sub("<[^>]+>", "", _partial_notice_html(1, 434))

    assert text.startswith("Only part of this document has been reviewed.")
    assert "1 of 434" in text, "the count stays -- 'under review' alone could mean anything"


def test_the_partial_notice_never_says_the_unreviewed_pages_are_empty():
    """The trap in this wording, and the reason it is a test rather than
    a docstring.

    Before unchecked provisions were published, the true thing to say was
    that they were listed but had no text. It stopped being true the day
    they were published in full, and a notice still saying it would send
    a reader away from a page that has exactly what they came for --
    which is worse than the over-long sentence it replaced."""
    from corpus.exporters.export_static_site import _partial_notice_html
    import re

    text = re.sub("<[^>]+>", "", _partial_notice_html(1, 434)).lower()

    assert "in full" in text
    for false_claim in ("do not contain", "no body text", "still to come",
                        "not yet published", "empty"):
        assert false_claim not in text, f"the notice claims {false_claim!r}, which is not true"
