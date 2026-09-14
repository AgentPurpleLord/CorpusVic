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

from corpus.html_view import _legislation_href, _site_prefix
from corpus.site_crypto import SiteGate, derive_key
from conftest import make_node
import export_static_site
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


def test_the_newest_version_is_published_without_a_version_in_its_address():
    """"/browse/criminal-procedure-act/" is the Act as it now stands and
    stays that address as new reprints land; an older one keeps its
    versioned name, so a link to it still means that version a year from
    now. It is also the address known_acts.yaml has always pointed every
    cross-Act reference at."""
    from export_static_site import site_slugs

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
    from export_static_site import _rewrite_urls

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

def _doc(published=3, total=3, slug="crimes-act", site_slug=None):
    return {
        "slug": slug, "site_slug": site_slug or slug,
        "title": "Crimes Act 1958", "kind": "act",
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
    from export_static_site import _copy_template

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
    from export_static_site import _link_targets

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
    html = '<a href="/vic-legislation-parser/browse/a/section/s5#s5-1">s 5(1)</a>'

    assert _targets(html, "/vic-legislation-parser") == {("a", "s5", "s5-1")}
    assert _targets(html, "") == set()


def _preview_site(tmp_path, targets, published, gate=None):
    from export_static_site import _write_previews

    written = _write_previews(tmp_path, "", targets, published, gate)
    return written, tmp_path


def test_a_preview_is_not_written_for_an_unpublished_provision(tmp_path, monkeypatch):
    """A link can point at a provision nobody has approved yet. Its page
    already says so instead of showing the text; a hover card that showed
    it anyway would be a hole straight through that."""
    import export_static_site as ess

    calls = []
    monkeypatch.setattr(ess.html_view, "render_preview",
                        lambda *a, **k: calls.append(a) or {"title": "x", "html": ""})
    monkeypatch.setattr(ess.dashboard, "_current_nodes", lambda slug: ([], [], None))
    monkeypatch.setattr(ess.dashboard, "_act_title", lambda slug: "Test Act")

    written, out = _preview_site(
        tmp_path,
        {("a", "s1", ""), ("a", "s2", ""), ("b", "s1", "")},
        # s2 is not approved; document b is not published at all.
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

    import export_static_site as ess
    from corpus.site_crypto import SiteGate

    monkeypatch.setattr(ess.html_view, "render_preview",
                        lambda *a, **k: {"title": "Act", "html": "<p>a secret provision</p>"})
    monkeypatch.setattr(ess.dashboard, "_current_nodes", lambda slug: ([], [], None))
    monkeypatch.setattr(ess.dashboard, "_act_title", lambda slug: "Test Act")

    gate = SiteGate("a long enough passphrase")
    _preview_site(tmp_path, {("a", "s1", "")}, {"a": ("a-v3", {"s1"})}, gate)
    payload = json.loads((tmp_path / "browse/a/section/s1/preview.json").read_text(encoding="utf-8"))

    assert set(payload) == {"iv", "ct"}
    assert "secret" not in (tmp_path / "browse/a/section/s1/preview.json").read_text(encoding="utf-8")


def test_a_published_page_says_where_its_previews_come_from(tmp_path):
    """The script can't tell a static host from a server by looking, and
    guessing wrong means either a 404 on every hover or no card at all."""
    from export_static_site import _page

    page = _page("Test Act", "<p>body</p>", "/browse/a", reader=True)

    assert 'data-preview="static"' in page


def test_a_published_section_carries_no_review_badge():
    """On a site that only publishes checked provisions the badge reads
    "Fully reviewed" on every page, which tells a reader nothing -- the
    same reason the contents page already drops it."""
    from corpus.html_view import render_section

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
    import export_static_site as ess

    published = [
        _doc(slug="crimes-act"),
        dict(_doc(slug="crimes-bill"), kind="bill"),
        dict(_doc(slug="crimes-bill-em"), kind="em"),
    ]
    original = ess.dashboard.related_documents
    ess.dashboard.related_documents = lambda slug: (
        [{"slug": "crimes-bill", "kind": "bill"}, {"slug": "crimes-bill-em", "kind": "em"}]
        if slug == "crimes-act" else []
    )
    try:
        page = ess._landing_page_html(published, "")
    finally:
        ess.dashboard.related_documents = original
    entries = page.split('<ul class="section-list">')[1].split("</ul>")[0]

    assert "/browse/crimes-act/" in entries
    assert "crimes-bill" not in entries


def test_a_bill_no_published_act_claims_is_still_listed():
    """Better an odd entry on the front page than a document nothing
    reaches."""
    import export_static_site as ess

    original = ess.dashboard.related_documents
    ess.dashboard.related_documents = lambda slug: []
    try:
        page = ess._landing_page_html([dict(_doc(slug="orphan-bill"), kind="bill")], "")
    finally:
        ess.dashboard.related_documents = original

    assert "/browse/orphan-bill/" in page


# ---------------------------------------------------------------------
# Where the site is published
# ---------------------------------------------------------------------

def test_a_custom_domain_means_no_path_prefix(tmp_path, monkeypatch):
    """A custom domain is mapped at its own root. With the "/repo"
    prefix a project site needs, every link on the site resolved to
    https://www.corpusvic.au/vic-legislation-parser/browse/..., which is
    nowhere."""
    monkeypatch.setattr(export_static_site, "CNAME_FILE", tmp_path / "CNAME")
    (tmp_path / "CNAME").write_text("www.corpusvic.au\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", "AgentPurpleLord/vic-legislation-parser")

    assert export_static_site.custom_domain() == "www.corpusvic.au"
    assert export_static_site._default_base_path() == ""


def test_without_a_custom_domain_a_project_site_keeps_its_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(export_static_site, "CNAME_FILE", tmp_path / "CNAME")
    monkeypatch.setenv("GITHUB_REPOSITORY", "AgentPurpleLord/vic-legislation-parser")

    assert export_static_site.custom_domain() is None
    assert export_static_site._default_base_path() == "/vic-legislation-parser"


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
