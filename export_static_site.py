"""
Builds a static, public copy of the "browse" reading view (the same one
dashboard.py serves live at /browse/*) as a tree of plain HTML files,
suitable for GitHub Pages. Meant to be run by .github/workflows/pages.yml
on every push that changes data/parsed/ or data/legislation.db, so
updating an Act's review state through the dashboard and committing it is
the whole publishing step -- no separate export command to remember.

Usage:
    python export_static_site.py --out _site
    python export_static_site.py --out _site --base-path /some-prefix
    SITE_PASSWORD='a long passphrase' python export_static_site.py --out _site

A passphrase (via $SITE_PASSWORD, or --password for a local build) puts
the whole site behind an unlock page: every page is encrypted at build
time, so the published files are ciphertext rather than readable text
with a decorative gate over it -- see corpus/site_crypto.py for what
that does and doesn't protect. Without one the site is open to anyone
with the URL, which is the right default once it's meant to be public.

--base-path is the path prefix the site will actually be served under,
and it is worked out from the repository rather than passed in.

A CNAME file in the repository root means a custom domain, and a custom
domain is mapped at its own root, so the prefix is nothing:
www.corpusvic.au/browse/... Without one, GitHub Pages serves a repo's
site at https://<owner>.github.io/<repo>/, and every absolute link the
generated pages carry (nav, cross-references, the landing page) needs
that "/repo" prefix or it points at the wrong place once deployed. A
local preview served from a directory root (`python -m http.server
--directory _site`) gets nothing, since $GITHUB_REPOSITORY is not set.

The CNAME file is also copied into the built site, because a Pages
deployment serves exactly what the build uploaded -- one left behind in
the source tree is a custom domain that stops being configured the first
time this runs.

Reuses dashboard.py's own private, file-backed helpers (_current_nodes,
_amendments, _act_title, _act_version, _superseded, _provision_timeline,
_section_crossrefs, _page_index, act_status, discover_slugs) rather than
re-deriving any of this -- they're already exactly what dashboard.py's own
/browse/* routes call per request (see browse_index/browse_section/
browse_endnotes), just with no FastAPI Request in sight, so calling them
directly and writing the returned HTML to a file instead of an
HTTPResponse is the whole job. tests/test_dashboard.py already imports
dashboard.py the same way for the same reason.

Publishes a work's newest parsed version in full: every provision the
parser found, with its text, whether or not a human has checked it yet.
A provision nobody has checked carries a notice saying so, above its own
heading (see _unverified_notice_html), and the contents page says how
much of the document that applies to.

Publishing only the checked provisions was the older rule, and it made
the site read as an Act with holes in it -- for legislation the
dangerous reading, since a section that is merely unchecked looked the
same as a section that does not exist. Saying what has been checked, on
the provisions it is true of, tells a reader more than withholding the
text does, and tells it where they are actually reading.

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
from corpus import html_view, reader
from corpus.hierarchy import group_into_units
from corpus.site_crypto import ROBOTS_TXT, ROBOTS_TXT_ALLOW_ALL, SiteGate
from corpus.versions import split_document_slug


def select_candidate_slugs(statuses: dict[str, dict]) -> list[str]:
    """Which documents are even eligible for the site: every parsed one,
    including older versions of a work. How much of a candidate a human
    has checked is a separate question, answered per provision by
    approved_page_slugs below, and it decides what each page says about
    itself rather than whether it exists.

    Older reprints are published because a reader needs to be able to go
    and read one: "Compare with another version" on a provision offers
    every version this pipeline holds, and an offer that 404s is worse
    than no offer. They are published at their own versioned addresses,
    and are not listed on the landing page -- see site_slugs, which is
    what decides those addresses, and _landing_page_html.

    Pure and file-I/O-free so it's unit-testable on fabricated status
    dicts -- see tests/test_export_static_site.py. `statuses` is
    {slug: dashboard.act_status(slug)}."""
    return sorted(slug for slug, status in statuses.items() if status["parsed"])


def site_slugs(candidates: list[str]) -> dict[str, str]:
    """{parse slug -> the path segment it is published under}.

    The newest version of a work is published under the work's own name,
    with no version in the address at all: /browse/criminal-procedure-act/
    is the Act as it now stands, and stays that address as new reprints
    land. Anything older keeps its versioned name, so a link to
    /browse/criminal-procedure-act-v112/ still means version 112 a year
    from now, which is exactly what a citation to a point in time needs.

    It also makes the cross-Act links work: known_acts.yaml names a work
    ("criminal-procedure-act"), so every reference to the Act from another
    Act's text has always pointed at the unversioned address -- which,
    until now, nothing was published at."""
    newest: dict[str, str] = {}
    for slug in candidates:
        work, version = split_document_slug(slug)
        held = newest.get(work)
        if held is None:
            newest[work] = slug
            continue
        _w, held_version = split_document_slug(held)
        if version is not None and (held_version is None or version > held_version):
            newest[work] = slug
    current = {slug: work for work, slug in newest.items()}
    return {slug: current.get(slug, slug) for slug in candidates}


def approved_units(nodes: list, units: list[list[int]]) -> set[int]:
    """Which units a reviewer has actually approved -- positions into
    `units`, for the effective nodes review.build_effective_nodes_indexed
    returns (a merged-away node is None there, and doesn't count against
    the unit it used to be in).

    Approved means every node still in the unit carries verified_at and
    none is flagged for follow-up. The two are deliberately exclusive in
    review.py: flagging a piece means "not sure, revisit this", and
    commit_unit leaves such a node unstamped on purpose. So a flagged
    provision counts as unchecked and says so on its own page, which is
    the point of the reviewer having flagged it."""
    approved = set()
    for u, unit in enumerate(units):
        live = [nodes[i] for i in unit if nodes[i] is not None]
        if live and all(n.get("verified_at") and not n.get("needs_followup") for n in live):
            approved.add(u)
    return approved


def approved_page_slugs(nodes: list, units: list[list[int]], by_node_index: dict[int, str]) -> set[str]:
    """The page ids (build_page_index's own "s14", "s14_2", ...) whose
    provision a human has checked. A page is one unit -- a Section and
    everything nested under it -- so it is checked exactly when that unit
    is. Every page carries its text either way; this decides which of
    them have to say they haven't been confirmed."""
    approved = approved_units(nodes, units)
    unit_of_root = {unit[0]: u for u, unit in enumerate(units)}
    return {
        page for node_index, page in by_node_index.items()
        if unit_of_root.get(node_index) in approved
    }


# dashboard.py builds its browse URLs for the live dashboard: rooted at
# the domain, and naming a document by its parse slug. Neither is right
# here -- the site may sit under a repository path, and the newest version
# of a work is published under the work's own name (see site_slugs). Every
# such URL that reaches a published page therefore goes through here
# first. It is a rewrite rather than a parameter threaded through
# dashboard.py because those helpers serve a running server that is right
# as it stands, and one rule applied at the boundary is easier to keep
# whole than a prefix passed through a dozen call sites.
_BROWSE_URL_RE = re.compile(r"^/browse/([^/]+)(/.*)?$")


def _rewrite_url(url: "str | None", base_path: str, slugs: dict) -> "str | None":
    if not url:
        return url
    m = _BROWSE_URL_RE.match(url)
    if not m:
        return url
    slug, rest = m.group(1), m.group(2) or "/"
    return f"{base_path}/browse/{slugs.get(slug, slug)}{rest}"


def _rewrite_urls(value, base_path: str, slugs: dict):
    """The same rewrite over the shapes dashboard.py hands back: a plain
    URL, the {version -> URL} map behind "Compare with another version",
    and the crossref chips' own hrefs."""
    if isinstance(value, str):
        return _rewrite_url(value, base_path, slugs)
    if isinstance(value, dict):
        return {k: _rewrite_urls(v, base_path, slugs) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_urls(v, base_path, slugs) for v in value]
    return value


def publishes_anything(slug: str) -> bool:
    """Whether this document has even one provision, and so will
    produce pages at all. The same question _build_doc answers on its way
    past; asked separately because an Act's contents page has to link to
    its Bill and Explanatory Memorandum, and cannot know whether those
    exist until every document has been looked at."""
    return bool(dashboard._page_index(slug)["by_node_index"])


CNAME_FILE = Path(__file__).parent / "CNAME"


def custom_domain() -> "str | None":
    """The domain this site is published at, from the repository's CNAME
    file, or None if it is published at a github.io address.

    CNAME is GitHub Pages' own way of recording a custom domain -- it is
    the file the Settings page writes when you set one -- so it is read
    here rather than duplicated into a second setting that could disagree
    with it."""
    if not CNAME_FILE.exists():
        return None
    return CNAME_FILE.read_text(encoding="utf-8").strip() or None


def _default_base_path() -> str:
    """The path prefix the site will be served under.

    Nothing, when there is a custom domain: it is mapped at that domain's
    own root, so a link needs no prefix at all. This is what the CNAME
    file decides, and getting it wrong is not subtle -- with a "/repo"
    prefix against a custom domain, every link on the site resolved to
    https://www.corpusvic.au/<repo-name>/browse/..., which is nowhere.

    Otherwise "/repo-name" when $GITHUB_REPOSITORY (owner/repo, set by
    every GitHub Actions job) is present, matching the default URL of a
    project site at https://<owner>.github.io/<repo>/; and "" outside
    Actions, for a local preview served from a directory root."""
    if custom_domain():
        return ""
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"/{repo.split('/')[-1]}" if repo else ""


# Where the server keeps the passphrase, so that it belongs to the
# machine rather than to whoever happens to be typing the build command.
# Gitignored; deploy/site.env.example is the tracked template.
SITE_ENV_FILE = Path(__file__).parent / "deploy" / "site.env"

# What a gated page carries and an open one cannot: the encrypted payload
# the unlock script reads (see corpus/site_crypto.py's _GATE_TEMPLATE).
_GATED_MARKER = 'id="payload"'


def password_from_env_file(path: "Path | None" = None) -> "str | None":
    """SITE_PASSWORD as recorded on this machine.

    A shell variable lives as long as the shell, which is how the gate
    came off the published site: the build was carried over to the server
    but the passphrase was not, and an ungated build is not an error --
    it is a supported configuration, so nothing complained."""
    path = Path(path) if path else SITE_ENV_FILE
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "SITE_PASSWORD":
            return value.strip().strip("'\"") or None
    return None


def already_gated(out: Path) -> bool:
    """Whether the build already at `out` is behind a passphrase."""
    landing = Path(out) / "index.html"
    try:
        return _GATED_MARKER in landing.read_text(encoding="utf-8")
    except OSError:
        return False


def resolve_password(cli_password: "str | None", no_password: bool, out: Path) -> "str | None":
    """The passphrase this build should use, and a refusal where using
    none would quietly publish what was behind one.

    Taking the gate off is a real choice and stays available, but it has
    to be made rather than arrived at: pages served once in the clear are
    served, and no later rebuild takes that back."""
    if no_password:
        return None
    # A passphrase on the command line is visible to anything that can
    # list processes, so it is the last resort rather than the first.
    password = os.environ.get("SITE_PASSWORD") or password_from_env_file() or cli_password
    if not password and already_gated(out):
        raise SystemExit(
            f"Refusing to rebuild {out}/ without a passphrase: what is there now is gated, and "
            "this build would replace it with pages anyone can read.\n"
            f"  - to keep the gate: put SITE_PASSWORD in {SITE_ENV_FILE} (see "
            "deploy/site.env.example), or set it in the environment\n"
            "  - to open the site deliberately: pass --no-password"
        )
    return password


def _provision_label(node: dict) -> str:
    """"14 Determination of limits" -- enough to name a provision on its
    own placeholder page, so a reader who followed a link knows which one
    they were reaching for."""
    parts = [str(p) for p in (node.get("number"), node.get("heading")) if p]
    return " ".join(parts) or str(node.get("type", "Provision")).replace("_", " ").capitalize()


def _unverified_notice_html() -> str:
    """Set at the top of a provision nobody has checked yet, above its own
    heading, because it qualifies every word below it.

    The text underneath is real: it is what the parser read off the
    official PDF, not a placeholder and not a guess at what the provision
    might say. What it has not had is a human reading it against the page
    to confirm the parser got it right -- which is a different and
    smaller claim than "this may be wrong", and the notice says the
    smaller one, because overstating the doubt would be as misleading as
    hiding it."""
    return (
        '<div class="disclaimer">'
        "<strong>This provision has not been checked by a human.</strong> "
        "The text below was read automatically from the official PDF and has not yet been "
        "verified against it, so it may differ from the provision as published — in its "
        "wording, its numbering, or where one provision ends and the next begins. "
        f'For the authorised text, see <a href="{OFFICIAL_SOURCE_URL}" rel="noopener">'
        f"{OFFICIAL_SOURCE_NAME}</a>."
        "</div>"
    )


def _partial_notice_html(checked: int, total: int) -> str:
    """The same caveat on the contents page, where it is about the
    document rather than about one provision.

    Only where some of it is unchecked, and phrased as a count, because
    the state a reader needs to know is not "this Act is under review"
    but "how much of what you are about to read has been looked at".
    Every provision is here either way; the ones that have not been
    checked say so on themselves, which is where it matters."""
    return (
        '<div class="disclaimer">'
        f"<strong>{checked} of {total} provisions in this document have been checked by a human.</strong> "
        "The rest were read automatically from the official PDF and have not yet been verified "
        "against it. Every provision is published here; the ones still to be checked say so at "
        "the top of their own page."
        "</div>"
    )


def _copy_template(out: Path) -> None:
    """The whole template directory -- the stylesheets, the browser-side
    scripts and Junicode -- published as "assets/", which is where every
    page's asset URLs point (see html_view.page_shell).

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
          gate: "SiteGate | None" = None, site_prefix: "str | None" = None) -> str:
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
        site_prefix=site_prefix,
    )


def _write(path: Path, page_html: str, gate: "SiteGate | None" = None) -> None:
    """One page, encrypted behind the passphrase gate first if there is
    one (see corpus/site_crypto.py). Everything the site publishes
    goes through here, so a gated build has no page that was missed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gate.wrap(page_html) if gate else page_html, encoding="utf-8")


def _build_doc(slug: str, out_dir: Path, base_path: str, gate: "SiteGate | None" = None,
               slugs: "dict | None" = None, published_slugs: "set[str] | None" = None) -> "dict | None":
    """Every page for one document: its index, one per section, and its
    Endnotes if it has any -- exactly what browse_index/browse_section/
    browse_endnotes each build for one HTTP request, just written to
    files under out_dir/browse/<slug>/ instead.

    Every provision gets its real text. One nobody has checked yet
    carries a notice saying so, above its own heading (see
    _unverified_notice_html). Returns the summary used for the site's own
    landing page, or None for a document with no provisions at all.

    The summary carries "pages" (the page ids this document released) and
    "links" (everything its pages point at), which between them are what
    _write_previews needs -- gathered here because a page's HTML is only
    in hand before it is written and, on a gated build, encrypted.

    slugs is site_slugs()'s {parse slug -> published path segment}: this
    document is written under its own entry, and every URL dashboard.py
    hands back is rewritten through the whole map."""
    links: set = set()
    slugs = slugs or {}
    site_slug = slugs.get(slug, slug)
    base_url = f"{base_path}/browse/{site_slug}"
    doc_dir = out_dir / "browse" / site_slug

    def site(value):
        return _rewrite_urls(value, base_path, slugs)
    # What this function still needs for itself. Everything a page is
    # built from is corpus/reader.py's business now, and each of these
    # lookups is cached against the data it reads (see dashboard.py's
    # signature-keyed caches), so asking per page costs nothing.
    nodes, _unattached, _hierarchy = dashboard._current_nodes(slug)
    title = dashboard._act_title(slug)
    page_index = dashboard._page_index(slug)

    # Units grouped over the same node list page_index was built from, so
    # the two agree on what a node index means. (build_effective_nodes_
    # indexed keeps original parse positions instead, which is what
    # run_ai_review.py needs and exactly what must not be mixed in here:
    # once anything has been merged the two numbering schemes diverge.)
    # _current_nodes returns the stored verified row wherever there is
    # one, so verified_at/needs_followup are readable straight off these.
    units = group_into_units(nodes)
    all_pages = set(page_index["by_node_index"].values())
    if not all_pages:
        return None
    # Which provisions a human has confirmed. It no longer decides what
    # is published -- everything is -- only which pages have to say they
    # haven't been checked.
    checked_pages = approved_page_slugs(nodes, units, page_index["by_node_index"])

    index_body = reader.contents_page(
        dashboard, slug, base_url, rewrite=site, show_review_badge=False,
        # Only documents this build actually published: a link from an
        # Act's contents to a Bill nobody has reviewed yet would be a
        # link to a page that isn't there.
        related=[
            {"slug": d["slug"], "kind": d["kind"], "title": dashboard._act_title(d["slug"]),
             "href": f"{base_path}/browse/{slugs.get(d['slug'], d['slug'])}/"}
            for d in dashboard.related_documents(slug)
            if d["slug"] in (published_slugs or ())
        ],
        notice=(None if checked_pages == all_pages
                else _partial_notice_html(len(checked_pages), len(all_pages))),
    )
    links |= _link_targets(index_body, base_path)
    _write(doc_dir / "index.html", _page(title, index_body, base_url, gate=gate), gate)

    for _node_index, section_slug in page_index["by_node_index"].items():
        body = reader.section_page(
            dashboard, slug, base_url, section_slug,
            rewrite=site, show_review_badge=False,
            notice=None if section_slug in checked_pages else _unverified_notice_html(),
        )
        if body is None:
            continue  # not expected -- page_index only ever names real sections
        links |= _link_targets(body, base_path)
        _write(doc_dir / "section" / section_slug / "index.html",
               _page(title, body, base_url, reader=True, gate=gate), gate)

    endnotes_body = reader.endnotes_page(dashboard, slug, base_url)
    if endnotes_body is not None:
        links |= _link_targets(endnotes_body, base_path)
        _write(doc_dir / "endnotes" / "index.html",
               _page(f"{title} — Endnotes", endnotes_body, base_url, gate=gate), gate)

    status = dashboard.act_status(slug)
    return {
        "slug": slug, "site_slug": site_slug, "title": title, "kind": status["kind"],
        "as_at": status["version_as_at"], "pages": 1 + len(all_pages),
        "checked_provisions": len(checked_pages), "total_provisions": len(all_pages),
        # Every page this document published, which is all of them: what
        # _write_previews needs to know a link has somewhere to land.
        "published_pages": all_pages, "links": links,
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

    published maps a document's published path segment to (its parse slug,
    the page ids it actually released) -- the two differ for the newest
    version of a work, which is published under the work's own name (see
    site_slugs), and the targets are read off links and so name the
    published one.

    A link into a document that isn't published gets no preview file and
    so no card, which is the same answer the page behind it would
    give."""
    by_page = {}
    for site_slug, section, fragment in targets:
        entry = published.get(site_slug)
        if entry is None or (section and section not in entry[1]):
            continue
        by_page.setdefault((site_slug, section), set()).add(fragment)

    written = 0
    for (site_slug, section), fragments in sorted(by_page.items()):
        slug = published[site_slug][0]
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
        path = out / "browse" / site_slug
        if section:
            path = path / "section" / section
        path.mkdir(parents=True, exist_ok=True)
        (path / "preview.json").write_text(_encrypted_json(previews, gate), encoding="utf-8")
        written += 1
    return written


def _provision_count_html(doc: dict) -> str:
    """"12 of 112 provisions checked" for a document still being worked
    through, and nothing at all for one where every provision has been --
    a count beside every entry would just be noise once the answer is
    always "all of them".

    Checked, not published: the whole document is published either way,
    and what differs between these entries is how much of it a human has
    confirmed against the PDF."""
    checked, total = doc["checked_provisions"], doc["total_provisions"]
    if checked >= total:
        return ""
    return f" &middot; {checked} of {total} provisions checked"


def _landing_page_html(published: list[dict], base_path: str) -> str:
    """The way in. Only current documents are listed: an older reprint is
    published and readable, but it is reached by asking for it -- from the
    provision you are on, where "Compare with another version" knows which
    provision you mean. A list that offered five reprints of one Act side
    by side would make choosing the right one the reader's first problem.

    A document is current here exactly when site_slugs gave it the work's
    own unversioned address.

    Bills and Explanatory Memorandums are left off too. An Explanatory
    Memorandum is written about a Bill and a Bill becomes an Act: they
    belong to that Act, and its own contents page offers them (see
    render_index's `related`). Listing all three side by side here would
    present them as separate publications and make choosing between them
    a reader's first problem. One that no published Act claims is listed
    after all -- better an odd entry than a page nothing reaches."""
    kind_labels = {"act": "Act", "bill": "Bill", "em": "Explanatory Memorandum"}
    claimed = {
        d["slug"]
        for doc in published
        for d in dashboard.related_documents(doc["slug"])
    }
    current = [
        doc for doc in published
        if doc["site_slug"] == split_document_slug(doc["slug"])[0]
        and (doc["kind"] == "act" or doc["slug"] not in claimed)
    ]
    rows = "".join(
        "<li>"
        f'<a href="{base_path}/browse/{doc["site_slug"]}/">{html.escape(doc["title"])}</a> '
        f'<span class="text-muted">{kind_labels.get(doc["kind"], doc["kind"])}'
        f'{" &middot; as at " + html.escape(doc["as_at"]) if doc["as_at"] else ""}'
        f"{_provision_count_html(doc)}</span>"
        "</li>"
        for doc in current
    )
    intro = (
        "Automatically generated from this project’s review pipeline. Each document is "
        "published in full, and every provision a human has not yet checked against the "
        "official PDF says so at the top of its own page \u2014 the counts below say how "
        "much of each document that is."
        if current else "Nothing has been parsed and published yet."
    )
    body = (
        # First in the body, before the heading: a reader should meet the
        # caveat without scrolling, not after deciding what to click.
        f'<div class="disclaimer">{_NOT_OFFICIAL_HTML}</div>'
        "<h1>Published legislation</h1>"
        f"<p>{intro}</p>"
        + (f'<ul class="section-list">{rows}</ul>' if current else "")
    )
    return _page("Published legislation", body, site_prefix=base_path)


def robots_txt_for(gated: bool, allow_indexing: bool) -> str:
    """What to tell crawlers. Always something: no file at all means the
    crawler decides.

    It used to be written only on a gated build, which had the two cases
    exactly backwards. A gated site publishes ciphertext, so a crawler
    that ignored the file would index gibberish; an open site publishes
    thousands of provisions of mostly unchecked legal text, and that was
    the build with no robots.txt at all.

    So indexing is asked for rather than arrived at, and asking for it on
    a gated site is a contradiction resolved the safe way round. A crawl
    cannot be taken back: the pages come down and the snapshot stays
    up."""
    return ROBOTS_TXT_ALLOW_ALL if (allow_indexing and not gated) else ROBOTS_TXT


def build_site(out: Path, base_path: str, password: "str | None" = None,
               allow_indexing: bool = False) -> tuple:
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
    candidates = select_candidate_slugs(statuses)
    slugs = site_slugs(candidates)
    # Which candidates will publish anything, worked out before any page
    # is written: an Act's contents links to its Bill and Explanatory
    # Memorandum, and it can only do that for documents this build is
    # actually going to produce. Everything it reads is cached, so the
    # pass costs almost nothing.
    will_publish = {slug for slug in candidates if publishes_anything(slug)}
    published = [
        doc for doc in
        (_build_doc(slug, out, base_path, gate, slugs, will_publish) for slug in candidates)
        if doc
    ]
    landing = _landing_page_html(published, base_path)
    _write(out / "index.html", landing, gate)
    # Across the whole site, not per document: the links most worth
    # previewing are the ones into another document (a Bill clause, an
    # Explanatory Memorandum's note), and those can only be resolved once
    # every document's own pages are known.
    targets = _link_targets(landing, base_path).union(*(doc["links"] for doc in published)) if published else set()
    preview_files = _write_previews(
        out, base_path, targets,
        {doc["site_slug"]: (doc["slug"], doc["published_pages"]) for doc in published}, gate)
    # Never encrypted: a crawler has to be able to read the one file that
    # tells it what to do.
    _write(out / "robots.txt", robots_txt_for(gate is not None, allow_indexing))
    domain = custom_domain()
    if domain:
        # Published with the site, not just kept in the repository. A
        # Pages deployment serves exactly what the build uploaded, so a
        # CNAME that stays behind in the source tree is a custom domain
        # that stops being configured the first time this runs.
        (out / "CNAME").write_text(domain + "\n", encoding="utf-8")
    return published, preview_files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="_site", help="output directory (default: _site)")
    ap.add_argument("--base-path", default=None, help="URL path prefix the site will be served under (default: empty when a CNAME sets a custom domain, else derived from $GITHUB_REPOSITORY, else empty)")
    ap.add_argument("--password", default=None, help="passphrase to encrypt every page behind (default: $SITE_PASSWORD, else deploy/site.env)")
    ap.add_argument("--no-password", action="store_true",
                    help="publish an open, ungated site, even where the build being replaced was gated")
    ap.add_argument("--allow-indexing", action="store_true",
                    help="let search engines index the site (open builds only; the default asks them not to)")
    args = ap.parse_args()

    base_path = args.base_path if args.base_path is not None else _default_base_path()
    out = Path(args.out)
    password = resolve_password(args.password, args.no_password, out)

    all_slugs = dashboard.discover_slugs()
    published, preview_files = build_site(out, base_path, password, args.allow_indexing)
    published_slugs = {doc["slug"] for doc in published}
    skipped = [s for s in all_slugs if s not in published_slugs]

    print(f"Published {len(published)} document(s) to {out}/ (base path: {base_path or '(none)'}):")
    for doc in published:
        at = "" if doc["site_slug"] == doc["slug"] else f" (at /browse/{doc['site_slug']}/)"
        print(f"  {doc['slug']}{at} -- {doc['total_provisions']} provision(s), "
              f"{doc['checked_provisions']} checked by a human")
    print(f"{preview_files} page(s) carry hover-preview data for the links that reach them.")
    if skipped:
        print(f"Skipped {len(skipped)} document(s) (not parsed):")
        for slug in skipped:
            print(f"  {slug}")
    if password:
        print("Every page is encrypted behind the passphrase, and robots.txt disallows crawlers.")
    elif args.allow_indexing:
        print("OPEN SITE, and robots.txt invites search engines in. Anyone can read it.")
    else:
        print("OPEN SITE -- anyone with the URL can read it. robots.txt asks crawlers to stay out, "
              "which is a request, not a lock. Pass a passphrase to gate it.")


if __name__ == "__main__":
    main()
