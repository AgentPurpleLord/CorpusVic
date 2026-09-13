"""
Builds a static, public copy of the "browse" reading view (the same one
dashboard.py serves live at /browse/*) as a tree of plain HTML files,
suitable for GitHub Pages. Meant to be run by .github/workflows/pages.yml
on every push that changes data/ai_parsed/ or data/legislation.db, so
updating an Act's review state through the dashboard and committing it is
the whole publishing step -- no separate export command to remember.

Usage:
    python export_static_site.py --out _site
    python export_static_site.py --out _site --base-path /vic-legislation-parser
    SITE_PASSWORD='a long passphrase' python export_static_site.py --out _site

A passphrase (via $SITE_PASSWORD, or --password for a local build) puts
the whole site behind an unlock page: every page is encrypted at build
time, so the published files are ciphertext rather than readable text
with a decorative gate over it -- see ai_pipeline/site_crypto.py for what
that does and doesn't protect. Without one the site is open to anyone
with the URL, which is the right default once it's meant to be public.

--base-path is the path prefix the site will actually be served under.
GitHub Pages serves a repo's default Pages site at
https://<owner>.github.io/<repo>/, so every absolute link the generated
pages carry (nav, cross-references, the landing page) needs that "/repo"
prefix or it would point at the wrong place once deployed -- the workflow
passes it explicitly (derived from $GITHUB_REPOSITORY), computed here too
if that variable happens to be set. Left empty for a local preview served
from a directory root (`python -m http.server --directory _site`), or for
a custom domain mapped straight at the repo root.

Reuses dashboard.py's own private, file-backed helpers (_current_nodes,
_amendments, _act_title, _act_version, _superseded, _provision_timeline,
_section_crossrefs, _page_index, act_status, discover_slugs) rather than
re-deriving any of this -- they're already exactly what dashboard.py's own
/browse/* routes call per request (see browse_index/browse_section/
browse_endnotes), just with no FastAPI Request in sight, so calling them
directly and writing the returned HTML to a file instead of an
HTTPResponse is the whole job. tests/test_dashboard.py already imports
dashboard.py the same way for the same reason.

Publishes a work's newest parsed version, one approved provision at a
time: a Section appears once a reviewer has accepted every piece of it
(see approved_units), so an Act fills in as review proceeds rather than
waiting to be finished. A provision still to come keeps its place in the
contents, marked, with a page saying it hasn't been published yet -- a
silently absent section would read as a section that doesn't exist. A
document with nothing approved in it at all isn't published.

Hover-preview cards work here too, without a server: the card behind
every link the site actually contains is rendered at build time and
written as a preview.json beside the page it describes (see
_write_previews), encrypted like everything else on a gated build.

One thing the live dashboard offers that this doesn't attempt:
  - An unresolved citation's standing /legislation/<no> fallback address
    (dashboard.py's legislation_resolver) isn't pre-built here, so that
    one link 404s on the static host instead of explaining that the Act
    hasn't been parsed. It only ever applies to citations naming an Act
    this pipeline hasn't parsed at all, which is what it would have said
    anyway.
"""
import argparse
import base64
import html
import json
import os
import re
import shutil
from pathlib import Path

import dashboard
from ai_pipeline import html_view
from ai_pipeline.hierarchy import group_into_units
from ai_pipeline.site_crypto import ROBOTS_TXT, SiteGate
from ai_pipeline.versions import split_document_slug


def select_candidate_slugs(statuses: dict[str, dict]) -> list[str]:
    """Which documents are even eligible for the site: parsed, and the
    newest version of their work (never an older, superseded reprint --
    see README.md's "Versions of an Act"). Whether any of a candidate's
    text has actually been approved is a separate question, answered
    per provision by approved_page_slugs below.

    Pure and file-I/O-free so it's unit-testable on fabricated status
    dicts -- see tests/test_export_static_site.py. `statuses` is
    {slug: dashboard.act_status(slug)}."""
    newest_by_work: dict[str, str] = {}
    for slug, status in statuses.items():
        if not status["parsed"]:
            continue
        work, version = split_document_slug(slug)
        current = newest_by_work.get(work)
        if current is None:
            newest_by_work[work] = slug
            continue
        _current_work, current_version = split_document_slug(current)
        if version is not None and (current_version is None or version > current_version):
            newest_by_work[work] = slug
    return sorted(newest_by_work.values())


def approved_units(nodes: list, units: list[list[int]]) -> set[int]:
    """Which units a reviewer has actually approved -- positions into
    `units`, for the effective nodes review.build_effective_nodes_indexed
    returns (a merged-away node is None there, and doesn't count against
    the unit it used to be in).

    Approved means every node still in the unit carries verified_at and
    none is flagged for follow-up. The two are deliberately exclusive in
    review.py: flagging a piece means "not sure, revisit this", and
    commit_unit leaves such a node unstamped on purpose. So a flagged
    provision is not published, which is the whole point of the reviewer
    having flagged it."""
    approved = set()
    for u, unit in enumerate(units):
        live = [nodes[i] for i in unit if nodes[i] is not None]
        if live and all(n.get("verified_at") and not n.get("needs_followup") for n in live):
            approved.add(u)
    return approved


def approved_page_slugs(nodes: list, units: list[list[int]], by_node_index: dict[int, str]) -> set[str]:
    """The page ids (build_page_index's own "s14", "s14_2", ...) whose
    provision is approved and can carry real text. A page is one unit --
    a Section and everything nested under it -- so it's approved exactly
    when that unit is."""
    approved = approved_units(nodes, units)
    unit_of_root = {unit[0]: u for u, unit in enumerate(units)}
    return {
        page for node_index, page in by_node_index.items()
        if unit_of_root.get(node_index) in approved
    }


def _default_base_path() -> str:
    """"/repo-name" when $GITHUB_REPOSITORY (owner/repo, set by every
    GitHub Actions job) is present, matching a project site's default
    URL; "" otherwise, for a local preview or a custom-domain deployment
    mapped at the root."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"/{repo.split('/')[-1]}" if repo else ""


def _provision_label(node: dict) -> str:
    """"14 Determination of limits" -- enough to name a provision on its
    own placeholder page, so a reader who followed a link knows which one
    they were reaching for."""
    parts = [str(p) for p in (node.get("number"), node.get("heading")) if p]
    return " ".join(parts) or str(node.get("type", "Provision")).replace("_", " ").capitalize()


def _unpublished_page_body(node: dict, base_url: str, act_title: str) -> str:
    """What stands in for a provision nobody has approved yet. It exists
    rather than 404ing so that a gap reads as "not published here yet"
    and never as "no such provision" -- and so every link into it, from
    the contents list, a neighbouring page or another Act, keeps
    working."""
    return (
        f"<h1>{html.escape(_provision_label(node))}</h1>"
        '<div class="disclaimer">'
        "<strong>This provision hasn’t been published here yet.</strong> "
        "It has been parsed but not yet checked by a human, and this site only publishes "
        "provisions that have been. It says nothing about whether the provision is in force "
        f"— for the authorised text, see <a href=\"{OFFICIAL_SOURCE_URL}\" rel=\"noopener\">"
        f"{OFFICIAL_SOURCE_NAME}</a>."
        "</div>"
        f'<div class="section-nav"><a href="{base_url}/">{html.escape(act_title)} contents</a></div>'
    )


def _partial_notice_html(approved: int, total: int) -> str:
    return (
        '<div class="disclaimer">'
        f"<strong>Only part of this document has been published: {approved} of {total} provisions.</strong> "
        "The rest has been parsed but not yet checked by a human. Provisions still to come are listed "
        "in the contents below and marked, so nothing here is silently missing."
        "</div>"
    )


def _copy_template(out: Path) -> None:
    """The whole template directory -- the stylesheets, the browser-side
    scripts and Junicode -- published as "assets/", which is where every
    page's asset URLs point (see html_view._asset_base).

    Copied wholesale rather than file by file so that adding a stylesheet
    to static/site/ needs no change here; page.html is left out because
    Python renders it into each page rather than the browser fetching it.
    The fonts travel with their licence, and are self-hosted rather than
    pulled off a CDN so that reading the law here doesn't announce itself
    to a third party (see static/site/fonts/README.md)."""
    shutil.copytree(
        html_view.TEMPLATE_DIR, out / "assets",
        ignore=shutil.ignore_patterns("page.html", "__pycache__"),
        dirs_exist_ok=True,
    )

def _page(title: str, body: str, base_url: "str | None" = None, reader: bool = False,
          gate: "SiteGate | None" = None) -> str:
    """A finished page: the body, then the site footer. Every published
    page is built through here rather than calling page_shell directly,
    because the footer is the site's legal notice and the failure to
    design against is a new kind of page quietly shipping without it.

    The live dashboard's own /browse pages don't get this -- they're an
    internal preview behind a login, already labelled as one, not a thing
    the public reads."""
    return html_view.page_shell(
        title, body + _FOOTER_HTML, base_url=base_url, reader=reader,
        # No server here to render a hover card on demand, so the cards
        # are pre-built (see _write_previews) and the page says so.
        preview_source="static",
        site_salt=base64.b64encode(gate.salt).decode("ascii") if gate else None,
    )


def _write(path: Path, page_html: str, gate: "SiteGate | None" = None) -> None:
    """One page, encrypted behind the passphrase gate first if there is
    one (see ai_pipeline/site_crypto.py). Everything the site publishes
    goes through here, so a gated build has no page that was missed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gate.wrap(page_html) if gate else page_html, encoding="utf-8")


def _build_doc(slug: str, out_dir: Path, base_path: str, gate: "SiteGate | None" = None) -> "dict | None":
    """Every page for one document: its index, one per section, and its
    Endnotes if it has any -- exactly what browse_index/browse_section/
    browse_endnotes each build for one HTTP request, just written to
    files under out_dir/browse/<slug>/ instead.

    A provision nobody has approved yet still gets a page, saying so
    (see _unpublished_page_body) rather than its text. Returns the
    summary used for the site's own landing page, or None for a document
    with nothing approved in it at all -- which has nothing to show and
    isn't published.

    The summary carries "pages" (the page ids this document released) and
    "links" (everything its pages point at), which between them are what
    _write_previews needs -- gathered here because a page's HTML is only
    in hand before it is written and, on a gated build, encrypted."""
    links: set = set()
    base_url = f"{base_path}/browse/{slug}"
    doc_dir = out_dir / "browse" / slug
    nodes, _unattached, hierarchy = dashboard._current_nodes(slug)
    title = dashboard._act_title(slug)
    amendments = dashboard._amendments(slug)
    page_index = dashboard._page_index(slug)
    # Read once for the whole document rather than per section page: both
    # are the same answer on every page of it.
    version = dashboard._act_version(slug)
    version_dates = dashboard._version_dates(slug)

    # Units grouped over the same node list page_index was built from, so
    # the two agree on what a node index means. (build_effective_nodes_
    # indexed keeps original parse positions instead, which is what
    # run_ai_review.py needs and exactly what must not be mixed in here:
    # once anything has been merged the two numbering schemes diverge.)
    # _current_nodes returns the stored verified row wherever there is
    # one, so verified_at/needs_followup are readable straight off these.
    units = group_into_units(nodes)
    published_pages = approved_page_slugs(nodes, units, page_index["by_node_index"])
    all_pages = set(page_index["by_node_index"].values())
    if not published_pages:
        return None
    unpublished_pages = all_pages - published_pages

    index_body = html_view.render_index(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": amendments["endnotes"],
         "version": version},
        title, base_url, superseded=dashboard._superseded(slug),
        unpublished_pages=unpublished_pages, show_review_badge=False,
    )
    if unpublished_pages:
        index_body = _partial_notice_html(len(published_pages), len(all_pages)) + index_body
    links |= _link_targets(index_body, base_path)
    _write(doc_dir / "index.html", _page(title, index_body, base_url, gate=gate), gate)

    for node_index, section_slug in page_index["by_node_index"].items():
        node = nodes[node_index]
        if section_slug not in published_pages:
            _write(doc_dir / "section" / section_slug / "index.html",
                   _page(title, _unpublished_page_body(node, base_url, title), base_url, gate=gate), gate)
            continue
        section_number = node.get("number")
        schedule = page_index["schedule_by_node_index"].get(node_index)
        node_type = node["type"]
        entries, version_urls = dashboard._provision_timeline(slug, section_number, schedule, node_type)
        crossrefs = (
            dashboard._section_crossrefs(slug, section_number, schedule)
            if node_type in ("section", "clause") else []
        )
        body = html_view.render_section(
            {"nodes": nodes, "hierarchy": hierarchy, "version": version,
             "endnotes": amendments["endnotes"]},
            title, base_url, section_slug,
            crossrefs=crossrefs, amendment_index=amendments["index"],
            timeline=entries, version_urls=version_urls, superseded=dashboard._superseded(slug),
            version_dates=version_dates, unpublished_pages=unpublished_pages,
            show_review_badge=False,
        )
        if body is None:
            continue  # not expected -- page_index only ever names real sections
        links |= _link_targets(body, base_path)
        _write(doc_dir / "section" / section_slug / "index.html",
               _page(title, body, base_url, reader=True, gate=gate), gate)

    endnotes_body = html_view.render_endnotes(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": amendments["endnotes"]},
        title, base_url, amendments["summary"],
    )
    if endnotes_body is not None:
        links |= _link_targets(endnotes_body, base_path)
        _write(doc_dir / "endnotes" / "index.html",
               _page(f"{title} — Endnotes", endnotes_body, base_url, gate=gate), gate)

    status = dashboard.act_status(slug)
    return {
        "slug": slug, "title": title, "kind": status["kind"],
        "as_at": status["version_as_at"], "pages": 1 + len(all_pages),
        "published_provisions": len(published_pages), "total_provisions": len(all_pages),
        "published_pages": published_pages, "links": links,
    }


# The official source. Every page this pipeline produces is a reading of
# what's published there, so the disclaimer points at it by name rather
# than describing it vaguely -- a reader who needs the authorised text
# needs to be able to go straight to it.
OFFICIAL_SOURCE_URL = "https://www.legislation.vic.gov.au"
OFFICIAL_SOURCE_NAME = "legislation.vic.gov.au"

_NOT_OFFICIAL_HTML = (
    "<strong>These are not official legislative texts.</strong> "
    "For full, authorised legislative texts you must refer to "
    f'<a href="{OFFICIAL_SOURCE_URL}" rel="noopener">{OFFICIAL_SOURCE_NAME}</a>. '
    "This site provides a computer-based interpretation of that text, with enhanced linking."
)

# Repeated at the foot of the page as well as the head, because the two
# are read by different people: the header catches someone arriving, the
# footer catches someone who has just finished reading a provision and is
# deciding what to do with it.
_FOOTER_HTML = (
    '<footer class="site-footer">'
    f"<p>{_NOT_OFFICIAL_HTML}</p>"
    "<p>This site does not provide legal advice or commentary. Anyone relying on this "
    "site as the text of legislation does so at their own risk.</p>"
    "</footer>"
)


# ---------------------------------------------------------------------------
# Hover previews
# ---------------------------------------------------------------------------
# On the dashboard a hover card is rendered on demand by an endpoint. A
# static host has nothing to ask, so the same cards are built here, at
# build time, and written as small JSON files beside the pages they
# describe -- one per target page, holding every anchor within it that
# anything actually links to. That last part is what keeps them small:
# the links the site contains are a far smaller set than the provisions
# it has, so an Act with a hundred pages needs about a hundred short
# files rather than a preview of every provision in it.
#
# They go through the gate like everything else. A preview is the
# provision's own words, so publishing it in the clear beside an
# encrypted page would hand over exactly what the gate is there to keep
# back -- see _encrypted_json.

_LINK_RE = re.compile(r'href="([^"]+)"')


def _link_targets(html: str, base_path: str) -> set:
    """The (slug, section id, fragment) each link in this page points at,
    for the links preview.js will try to preview -- a link into a section
    page, or an index anchor. Read off the rendered HTML rather than
    tracked as it is built, because the linkifier produces these deep
    inside the renderers and the page is the honest record of what a
    reader can actually hover."""
    targets = set()
    prefix = f"{base_path}/browse/"
    for href in _LINK_RE.findall(html):
        if not href.startswith(prefix):
            continue
        path, _hash, fragment = href.partition("#")
        parts = path[len(prefix):].strip("/").split("/")
        if len(parts) == 3 and parts[1] == "section":
            targets.add((parts[0], parts[2], fragment))
        elif len(parts) == 1 and parts[0] and fragment:
            targets.add((parts[0], "", fragment))
    return targets


def _encrypted_json(payload: dict, gate: "SiteGate | None") -> str:
    """The JSON a page's previews are read from, encrypted with the same
    key the pages are so that one unlock covers both (preview.js finds it
    by the salt the page carries)."""
    text = json.dumps(payload, separators=(",", ":"))
    return text if gate is None else json.dumps(gate.encrypt(text), separators=(",", ":"))


def _write_previews(out: Path, base_path: str, targets: set, published: dict,
                    gate: "SiteGate | None" = None) -> int:
    """One preview.json per linked-to page. Returns how many previews were
    written, for the build log.

    published maps a slug to the page ids that document actually released
    -- a link into a document that isn't published, or into a provision
    nobody has approved yet, gets no preview file and so no card, which is
    the same answer the page behind it would give."""
    by_page = {}
    for slug, section, fragment in targets:
        if slug not in published or (section and section not in published[slug]):
            continue
        by_page.setdefault((slug, section), set()).add(fragment)

    written = 0
    for (slug, section), fragments in sorted(by_page.items()):
        nodes, _unattached, hierarchy = dashboard._current_nodes(slug)
        parsed = {"nodes": nodes, "hierarchy": hierarchy}
        title = dashboard._act_title(slug)
        previews = {}
        for fragment in sorted(fragments):
            card = html_view.render_preview(parsed, title, section or None, fragment or None)
            if card is not None:
                previews[fragment] = card
        if not previews:
            continue
        path = out / "browse" / slug
        if section:
            path = path / "section" / section
        path.mkdir(parents=True, exist_ok=True)
        (path / "preview.json").write_text(_encrypted_json(previews, gate), encoding="utf-8")
        written += 1
    return written


def _provision_count_html(doc: dict) -> str:
    """"12 of 112 provisions" for a document still being worked through,
    and nothing at all for a finished one -- a count beside every entry
    would just be noise once the answer is always "all of them"."""
    published, total = doc["published_provisions"], doc["total_provisions"]
    if published >= total:
        return ""
    return f" &middot; {published} of {total} provisions"


def _landing_page_html(published: list[dict], base_path: str) -> str:
    kind_labels = {"act": "Act", "bill": "Bill", "em": "Explanatory Memorandum"}
    rows = "".join(
        "<li>"
        f'<a href="{base_path}/browse/{doc["slug"]}/">{html.escape(doc["title"])}</a> '
        f'<span class="text-muted">{kind_labels.get(doc["kind"], doc["kind"])}'
        f'{" &middot; as at " + html.escape(doc["as_at"]) if doc["as_at"] else ""}'
        f"{_provision_count_html(doc)}</span>"
        "</li>"
        for doc in published
    )
    intro = (
        "Automatically generated from this project’s review pipeline. Only provisions a "
        "human has checked are published, so a document may appear here with part of its "
        "text still to come \u2014 where it does, the count says how much."
        if published else "Nothing has been checked and published yet."
    )
    body = (
        # First in the body, before the heading: a reader should meet the
        # caveat without scrolling, not after deciding what to click.
        f'<div class="disclaimer">{_NOT_OFFICIAL_HTML}</div>'
        "<h1>Published legislation</h1>"
        f"<p>{intro}</p>"
        + (f'<ul class="section-list">{rows}</ul>' if published else "")
    )
    return _page("Published legislation", body)


def build_site(out: Path, base_path: str, password: "str | None" = None) -> tuple:
    """The whole site. With a passphrase, every page is encrypted behind
    the unlock gate and a Disallow-everything robots.txt goes out beside
    them -- a site that isn't ready to be read isn't ready to be indexed
    either, and a crawler that got there first would keep serving a
    snapshot of it long after the gate went up.

    Returns (the documents published, how many preview files were
    written)."""
    gate = SiteGate(password) if password else None
    _copy_template(out)
    statuses = {slug: dashboard.act_status(slug) for slug in dashboard.discover_slugs()}
    slugs = select_candidate_slugs(statuses)
    # _build_doc returns None for a candidate with nothing approved in it.
    published = [doc for doc in (_build_doc(slug, out, base_path, gate) for slug in slugs) if doc]
    landing = _landing_page_html(published, base_path)
    _write(out / "index.html", landing, gate)
    # Across the whole site, not per document: the links most worth
    # previewing are the ones into another document (a Bill clause, an
    # Explanatory Memorandum's note), and those can only be resolved once
    # every document's own pages are known.
    targets = _link_targets(landing, base_path).union(*(doc["links"] for doc in published)) if published else set()
    preview_files = _write_previews(
        out, base_path, targets, {doc["slug"]: doc["published_pages"] for doc in published}, gate)
    if gate:
        # Never encrypted: a crawler has to be able to read the one file
        # that tells it to go away.
        _write(out / "robots.txt", ROBOTS_TXT)
    return published, preview_files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="_site", help="output directory (default: _site)")
    ap.add_argument("--base-path", default=None, help="URL path prefix the site will be served under (default: derived from $GITHUB_REPOSITORY, else empty)")
    ap.add_argument("--password", default=None, help="passphrase to encrypt every page behind (default: $SITE_PASSWORD; unset means an open, ungated site)")
    args = ap.parse_args()

    base_path = args.base_path if args.base_path is not None else _default_base_path()
    # Preferred over --password: a passphrase on the command line is
    # visible to anything that can list processes, and in CI it comes
    # from a repository secret rather than from the workflow file.
    password = args.password or os.environ.get("SITE_PASSWORD") or None
    out = Path(args.out)

    all_slugs = dashboard.discover_slugs()
    published, preview_files = build_site(out, base_path, password)
    published_slugs = {doc["slug"] for doc in published}
    skipped = [s for s in all_slugs if s not in published_slugs]

    print(f"Published {len(published)} document(s) to {out}/ (base path: {base_path or '(none)'}):")
    for doc in published:
        print(f"  {doc['slug']} -- {doc['published_provisions']}/{doc['total_provisions']} provision(s) published")
    print(f"{preview_files} page(s) carry hover-preview data for the links that reach them.")
    if skipped:
        print(f"Skipped {len(skipped)} document(s) (not parsed, an older version, or nothing approved in it yet):")
        for slug in skipped:
            print(f"  {slug}")
    print(
        "Every page is encrypted behind the passphrase, and robots.txt disallows crawlers."
        if password else
        "No passphrase set -- the site is open to anyone who has the URL."
    )


if __name__ == "__main__":
    main()
