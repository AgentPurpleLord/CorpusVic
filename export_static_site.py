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

Only publishes a work's newest parsed version, and only once that
version's own review_status is "reviewed" -- see select_published_slugs.
A document that isn't ready yet simply isn't in the output; there's no
"almost done" page for the public site the way the live dashboard's own
"reflects your saved review progress" banner allows for.

Two things the live dashboard offers that this doesn't attempt:
  - Hover-preview cards (ai_pipeline/html_view.py's PREVIEW_SCRIPT) fetch
    /api/browse/<slug>/preview, which only exists on a running server.
    That fetch already fails gracefully on a 404 (see PREVIEW_SCRIPT's own
    .catch()) -- the card just never appears, every other link still
    works, so this is a silent feature reduction rather than a broken
    page.
  - An unresolved citation's standing /legislation/<no> fallback address
    (dashboard.py's legislation_resolver) isn't pre-built here, so that
    one link 404s on the static host instead of explaining that the Act
    hasn't been parsed. It only ever applies to citations naming an Act
    this pipeline hasn't parsed at all, which is what it would have said
    anyway.
"""
import argparse
import html
import os
from pathlib import Path

import dashboard
from ai_pipeline import html_view
from ai_pipeline.site_crypto import ROBOTS_TXT, SiteGate
from ai_pipeline.versions import split_document_slug


def select_published_slugs(statuses: dict[str, dict]) -> list[str]:
    """Which of dashboard.act_status()'s own results are worth a public
    page: parsed, the newest version of its work (never an older,
    superseded reprint -- see README.md's "Versions of an Act"), and
    fully reviewed. Pure and file-I/O-free so it's unit-testable on
    fabricated status dicts -- see tests/test_export_static_site.py.

    `statuses` is {slug: dashboard.act_status(slug)}."""
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
    return sorted(
        slug for slug in newest_by_work.values()
        if statuses[slug]["review_status"] == "reviewed"
    )


def _default_base_path() -> str:
    """"/repo-name" when $GITHUB_REPOSITORY (owner/repo, set by every
    GitHub Actions job) is present, matching a project site's default
    URL; "" otherwise, for a local preview or a custom-domain deployment
    mapped at the root."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"/{repo.split('/')[-1]}" if repo else ""


def _page(title: str, body: str, base_url: "str | None" = None) -> str:
    """A finished page: the body, then the site footer. Every published
    page is built through here rather than calling page_shell directly,
    because the footer is the site's legal notice and the failure to
    design against is a new kind of page quietly shipping without it.

    The live dashboard's own /browse pages don't get this -- they're an
    internal preview behind a login, already labelled as one, not a thing
    the public reads."""
    return html_view.page_shell(title, body + _FOOTER_HTML, base_url=base_url)


def _write(path: Path, page_html: str, gate: "SiteGate | None" = None) -> None:
    """One page, encrypted behind the passphrase gate first if there is
    one (see ai_pipeline/site_crypto.py). Everything the site publishes
    goes through here, so a gated build has no page that was missed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gate.wrap(page_html) if gate else page_html, encoding="utf-8")


def _build_doc(slug: str, out_dir: Path, base_path: str, gate: "SiteGate | None" = None) -> dict:
    """Every page for one document: its index, one per section, and its
    Endnotes if it has any -- exactly what browse_index/browse_section/
    browse_endnotes each build for one HTTP request, just written to
    files under out_dir/browse/<slug>/ instead. Returns the summary used
    for the site's own landing page."""
    base_url = f"{base_path}/browse/{slug}"
    doc_dir = out_dir / "browse" / slug
    nodes, _unattached, hierarchy = dashboard._current_nodes(slug)
    title = dashboard._act_title(slug)
    amendments = dashboard._amendments(slug)
    page_index = dashboard._page_index(slug)

    index_body = html_view.render_index(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": amendments["endnotes"],
         "version": dashboard._act_version(slug)},
        title, base_url, superseded=dashboard._superseded(slug),
    )
    _write(doc_dir / "index.html", _page(title, index_body, base_url), gate)

    for node_index, section_slug in page_index["by_node_index"].items():
        node = nodes[node_index]
        section_number = node.get("number")
        schedule = page_index["schedule_by_node_index"].get(node_index)
        node_type = node["type"]
        entries, version_urls = dashboard._provision_timeline(slug, section_number, schedule, node_type)
        crossrefs = (
            dashboard._section_crossrefs(slug, section_number, schedule)
            if node_type in ("section", "clause") else []
        )
        body = html_view.render_section(
            {"nodes": nodes, "hierarchy": hierarchy}, title, base_url, section_slug,
            crossrefs=crossrefs, amendment_index=amendments["index"],
            timeline=entries, version_urls=version_urls, superseded=dashboard._superseded(slug),
        )
        if body is None:
            continue  # not expected -- page_index only ever names real sections
        _write(doc_dir / "section" / section_slug / "index.html",
               _page(title, body, base_url), gate)

    endnotes_body = html_view.render_endnotes(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": amendments["endnotes"]},
        title, base_url, amendments["summary"],
    )
    if endnotes_body is not None:
        _write(doc_dir / "endnotes" / "index.html",
               _page(f"{title} — Endnotes", endnotes_body, base_url), gate)

    status = dashboard.act_status(slug)
    return {
        "slug": slug, "title": title, "kind": status["kind"],
        "as_at": status["version_as_at"], "pages": 1 + len(page_index["by_node_index"]),
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


def _landing_page_html(published: list[dict], base_path: str) -> str:
    kind_labels = {"act": "Act", "bill": "Bill", "em": "Explanatory Memorandum"}
    rows = "".join(
        "<li>"
        f'<a href="{base_path}/browse/{doc["slug"]}/">{html.escape(doc["title"])}</a> '
        f'<span class="text-muted">{kind_labels.get(doc["kind"], doc["kind"])}'
        f'{" &middot; as at " + html.escape(doc["as_at"]) if doc["as_at"] else ""}</span>'
        "</li>"
        for doc in published
    )
    intro = (
        "Automatically generated from this project's review pipeline -- "
        "every document below has been fully reviewed."
        if published else "Nothing has been fully reviewed yet."
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


def build_site(out: Path, base_path: str, password: "str | None" = None) -> list[dict]:
    """The whole site. With a passphrase, every page is encrypted behind
    the unlock gate and a Disallow-everything robots.txt goes out beside
    them -- a site that isn't ready to be read isn't ready to be indexed
    either, and a crawler that got there first would keep serving a
    snapshot of it long after the gate went up."""
    gate = SiteGate(password) if password else None
    statuses = {slug: dashboard.act_status(slug) for slug in dashboard.discover_slugs()}
    slugs = select_published_slugs(statuses)
    published = [_build_doc(slug, out, base_path, gate) for slug in slugs]
    _write(out / "index.html", _landing_page_html(published, base_path), gate)
    if gate:
        # Never encrypted: a crawler has to be able to read the one file
        # that tells it to go away.
        _write(out / "robots.txt", ROBOTS_TXT)
    return published


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
    published = build_site(out, base_path, password)
    published_slugs = {doc["slug"] for doc in published}
    skipped = [s for s in all_slugs if s not in published_slugs]

    print(f"Published {len(published)} document(s) to {out}/ (base path: {base_path or '(none)'}):")
    for doc in published:
        print(f"  {doc['slug']} -- {doc['pages']} page(s)")
    if skipped:
        print(f"Skipped {len(skipped)} document(s) (not parsed, an older version, or not yet fully reviewed):")
        for slug in skipped:
            print(f"  {slug}")
    print(
        "Every page is encrypted behind the passphrase, and robots.txt disallows crawlers."
        if password else
        "No passphrase set -- the site is open to anyone who has the URL."
    )


if __name__ == "__main__":
    main()
