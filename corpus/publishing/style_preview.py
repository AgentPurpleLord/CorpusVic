"""Static copies of every kind of page, to style against (docs/STYLING.md).

    python -m corpus.publishing.style_preview        # writes style-preview/

Each copy is the real page: the public site, the dashboard and review,
rendered from the committed data/ in a throwaway directory and captured
from a headless browser. Then it is frozen -- its scripts taken out,
since they would ask a server for data and redraw, and its stylesheets
pointed at the files in static/ by relative path. So an edited stylesheet
shows on a reload, while the markup only changes when this runs again.

Needs Playwright and Chromium, which only this command does.
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from corpus import PROJECT_ROOT

OUT = PROJECT_ROOT / "style-preview"
REVIEWED = "criminal-procedure-act-v114"

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>\s*", re.S | re.I)
_STYLESHEET_RE = re.compile(r'(<link\b[^>]*\brel="stylesheet"[^>]*\bhref=")([^"]+)(")', re.I)
_BODY_RE = re.compile(r"<body\b[^>]*>", re.I)
_HEAD_END_RE = re.compile(r"</head>", re.I)

# Kept in step with the theme the pages themselves use (data-theme on
# <html>), and remembered across the copies so switching page keeps it.
_THEME_HEAD = """<script>
try { if (localStorage.getItem("stylePreviewTheme") === "dark") document.documentElement.dataset.theme = "dark"; } catch (e) {}
</script>
"""
_BAR = """<div id="style-preview-bar" style="position:fixed;right:10px;bottom:10px;z-index:2147483647;
  font:12px/1.4 system-ui,sans-serif;background:#fff;color:#111;border:1px solid #999;border-radius:4px;
  padding:5px 8px;box-shadow:0 1px 4px rgba(0,0,0,.2)">
  <b>{name}</b> &middot; <a href="../index.html" style="color:#0645ad">all pages</a> &middot;
  <button type="button" style="font:inherit" onclick="
    var d = document.documentElement.dataset; if (d.theme === 'dark') delete d.theme; else d.theme = 'dark';
    try { localStorage.setItem('stylePreviewTheme', d.theme || 'light'); } catch (e) {}">Light / dark</button>
</div>
"""


def static_path(href: str, page_url: str) -> "str | None":
    """Where under static/ a stylesheet the page linked lives: the site's
    own at /assets/, the admin pages' at /static/."""
    path = urlsplit(urljoin(page_url, href)).path
    for served, local in (("/assets/", "static/site/"), ("/static/", "static/")):
        if served in path:
            return local + path.split(served, 1)[1]
    return None


def freeze(page_html: str, page_url: str, name: str, depth: int = 2) -> str:
    """A captured page as a file that styles itself from static/: no
    scripts, stylesheets by relative path, and the preview's own bar."""
    up = "../" * depth
    out = _SCRIPT_RE.sub("", page_html)

    def relink(m):
        local = static_path(m.group(2), page_url)
        return m.group(1) + (up + local if local else m.group(2)) + m.group(3)

    out = _STYLESHEET_RE.sub(relink, out)
    out = _HEAD_END_RE.sub(lambda m: _THEME_HEAD + m.group(0), out, count=1)
    return _BODY_RE.sub(lambda m: m.group(0) + "\n" + _BAR.replace("{name}", html.escape(name)), out, count=1)


# -- A throwaway copy of the corpus, served by the real apps ----------------

def _copy_data(base: Path) -> None:
    """The committed data/ only: the real database is never read or
    written, and a review in progress on this machine doesn't leak into
    the copies."""
    tracked = subprocess.run(["git", "ls-files", "-z", "data"], cwd=PROJECT_ROOT, check=True,
                             capture_output=True).stdout.decode().split("\0")
    for rel in filter(None, tracked):
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / rel, base / rel)


def _serve_from(base: Path):
    """The dashboard, the public site and review, all pointed at `base`."""
    os.chdir(base)   # the database and review resolve data/ from here
    from corpus.review import review, review_sync
    from corpus.search import search
    from corpus.web import dashboard, public

    review_sync.import_(base, backup=False)
    dashboard.BASE_DIR = public.BASE_DIR = base
    dashboard._DASHBOARD_USERNAME = None
    public.configure(None, open_site=True)
    try:
        search.rebuild(base, source=dashboard)
    except Exception as e:   # search results are one page of many
        print(f"  (no search index: {e})", file=sys.stderr)
    public._INDEX = search.Index(base)
    review._load_state(REVIEWED)
    return {"site": public.app, "dash": dashboard.app, "review": review.app}, dashboard, review


def _spans(dashboard, slug: str) -> list[tuple[str, list[dict]]]:
    """Each page of a document with the nodes on it."""
    nodes = dashboard._parsed(slug)["nodes"]
    starts = sorted(dashboard._page_index(slug)["by_node_index"].items())
    return [(page, nodes[i:(starts[k + 1][0] if k + 1 < len(starts) else len(nodes))])
            for k, (i, page) in enumerate(starts)]


def _first_page(dashboard, slugs, test, taken: set) -> "tuple[str, str] | None":
    """The first page showing something, and not already shown for
    something else -- a definitions section has notes too."""
    for slug in slugs:
        for page, span in _spans(dashboard, slug):
            if (slug, page) not in taken and test(span):
                taken.add((slug, page))
                return slug, page
    return None


def _count(span, kind):
    return sum(1 for n in span if n.get("type") == kind)


def _public_pages(dashboard, public) -> list[tuple[str, str, str]]:
    from corpus.publishing.html_view import BULLETS

    addresses = public._published_slugs()
    held = sorted(addresses)
    bullet = tuple(BULLETS)
    wanted = [
        ("section-definitions", "A definitions section", [REVIEWED], lambda s: _count(s, "definition") >= 5),
        ("section-table", "A provision with a table", [REVIEWED], lambda s: _count(s, "table")),
        ("section-notes", "Numbered notes", [REVIEWED], lambda s: _count(s, "note") >= 2),
        ("section-repealed", "Repealed rows and their notes", [REVIEWED],
         lambda s: _count(s, "repealed") and _count(s, "note")),
        ("section-penalty", "A penalty", [REVIEWED], lambda s: _count(s, "penalty")),
        ("schedule-subitems", "Schedule sub-items with paragraphs", [REVIEWED],
         lambda s: _count(s, "subitem") and _count(s, "paragraph")),
        ("section-example", "An Example", held, lambda s: _count(s, "example")),
        ("section-bullets", "Dot points", held,
         lambda s: any(l.lstrip().startswith(bullet) for n in s for l in (n.get("text") or "").split("\n")[1:])),
    ]
    pages = [("landing", "The site's front page", "http://site.preview/"),
             ("contents", "A document's contents page", f"http://site.preview/browse/{addresses[REVIEWED]}/")]
    taken = set()
    for name, what, slugs, test in wanted:
        found = _first_page(dashboard, [s for s in slugs if s in addresses], test, taken)
        if found:
            pages.append((name, what, f"http://site.preview/browse/{addresses[found[0]]}/section/{found[1]}"))
        else:
            print(f"  (nothing held shows {what.lower()})", file=sys.stderr)
    older = next((s for s in held if s.startswith("criminal-procedure-act-v") and s != REVIEWED), None)
    if older:
        page = _spans(dashboard, older)[5][0]
        pages.append(("older-version", "An older version, with its banner",
                      f"http://site.preview/browse/{addresses[older]}/section/{page}"))
    pages += [("endnotes", "Endnotes", f"http://site.preview/browse/{addresses[REVIEWED]}/endnotes"),
              ("search", "Search results", "http://site.preview/search?q=bail")]
    return pages


def _admin_pages(dashboard, review, base: Path) -> list[tuple]:
    """(name, what, url, what to do before the copy is taken)."""
    _sample_teaching(dashboard, base)
    s45 = next(i for i in review._order if review._parse_node(i).get("type") == "section"
               and review._parse_node(i).get("number") == "45")
    unit = review._unit_of_index[s45]
    return [
        ("dashboard", "The dashboard", "http://dash.preview/", None),
        ("dashboard-modal", "A dashboard modal (Add)", "http://dash.preview/",
         "openModal('add-modal')"),
        ("review", "Review: a unit with notes", "http://review.preview/", f"loadUnit({unit})"),
        ("review-edit", "Review: the Edit window", "http://review.preview/",
         f"loadUnit({unit}).then(() => document.querySelector('.edit-piece-btn').click())"),
        ("history", "History review", "http://dash.preview/history/criminal-procedure-act/", None),
        ("teaching", "Teaching (a sample check)", f"http://dash.preview/teaching/{REVIEWED}/", None),
        ("lessons", "Lessons (a sample proposal)", "http://dash.preview/lessons/", None),
    ]


def _sample_teaching(dashboard, base: Path) -> None:
    """Something for Teaching and Lessons to show: they only have content
    after a check has been run against a PDF, which a copy doesn't have."""
    seen = {"page": 64, "y0": 400.0, "x0": 190.1, "x1": 300.0, "size": 12.0, "bold": False, "lbi": None,
            "text": "(2) and then continues", "above": {"text": "under subsection", "x1": 300.0, "bold": False,
                                                        "size": 12.0},
            "open": {"type": "subsection", "x0": 190.1}, "margin": 456.0, "body": 12.0}
    failures = [{"id": f"sample{n}", "act": REVIEWED, "kind": "removed", "parser": {"type": "subsection", "number": n},
                 "expected": None, "got": {"type": "subsection", "number": n},
                 "seen": {**seen, "page": 60 + int(n), "text": f"({n}) and then continues"}} for n in "234"]
    last = base / "data" / "teaching" / ".last" / f"{REVIEWED}.json"
    last.parent.mkdir(parents=True, exist_ok=True)
    last.write_text(json.dumps({"passed": [], "failures": failures}))
    (base / "data" / "teaching" / ".model.json").write_text(json.dumps({
        "tree": {"label": "paragraph", "support": 10, "purity": 1.0}, "examples": 1841, "acts": [REVIEWED],
        "held_out": [{"held_out": f"a fifth of {REVIEWED}'s lines", "lines": 383, "model": 0.987, "parser": 1.0}],
        "labels": {"paragraph": 747}}))
    dashboard._teaching_checks[REVIEWED] = {"state": "done", "harvest": {}, "error": None, "score": {
        "act": REVIEWED, "total": 1841, "passed": 1838, "failed": 3,
        "newly_broken": [{"id": "sample2"}],
        "failures": [{"id": f["id"], "kind": f["kind"], "parser": f["parser"], "expected": f["expected"],
                      "got": f["got"], "page": f["seen"]["page"], "text": f["seen"]["text"]} for f in failures]}}


# -- Capture -----------------------------------------------------------------

def _router(apps: dict):
    from fastapi.testclient import TestClient

    clients = {host: TestClient(app, base_url=f"http://{host}.preview", follow_redirects=False)
               for host, app in apps.items()}

    def handle(route):
        request = route.request
        url = urlsplit(request.url)
        client = clients.get((url.hostname or "").split(".")[0]) if (url.hostname or "").endswith(".preview") else None
        if client is None:
            return route.abort()
        target = url.path + (f"?{url.query}" if url.query else "")
        r = client.request(request.method, target, headers=request.headers, content=request.post_data_buffer)
        route.fulfill(status=r.status_code, headers=dict(r.headers), body=r.content)
    return handle


def capture(pages: list[tuple], apps: dict, out: Path) -> list[tuple]:
    from playwright.sync_api import sync_playwright

    done = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.route("**/*", _router(apps))
        for group, name, what, url, action in pages:
            page.goto(url, wait_until="networkidle")
            if action:
                page.evaluate(f"(async () => {{ await {action}; }})()")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(300)
            path = out / group / f"{name}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(freeze(page.content(), url, f"{group}/{name}"), encoding="utf-8")
            done.append((group, name, what))
            print(f"  {group}/{name}")
        browser.close()
    return done


def _index_html(done: list[tuple]) -> str:
    groups = {}
    for group, name, what in done:
        groups.setdefault(group, []).append(
            f'<li><a href="{group}/{name}.html">{html.escape(what)}</a> <code>{group}/{name}</code></li>')
    sections = "".join(f"<h2>{g.capitalize()}</h2><ul>{''.join(items)}</ul>" for g, items in groups.items())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Style preview</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:720px;margin:2em auto;padding:0 1em}}
code{{color:#666;font-size:12px}}</style></head><body>
<h1>Style preview</h1>
<p>Frozen copies of real pages. Edit a stylesheet in <code>static/site/</code> (the public site) or
<code>static/admin/</code> (the tools), save, and reload the page here. The bar in the corner switches
light and dark. The markup is from the last run of <code>python -m corpus.publishing.style_preview</code>;
run it again after changing a page's HTML. See <code>docs/STYLING.md</code>.</p>
{sections}</body></html>
"""


def build(out: Path = OUT) -> list[tuple]:
    here = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="style-preview-") as tmp:
        base = Path(tmp)
        try:
            _copy_data(base)
            apps, dashboard, review = _serve_from(base)
            from corpus.web import public
            pages = [("public", *p, None) for p in _public_pages(dashboard, public)]
            pages += [("admin", *p) for p in _admin_pages(dashboard, review, base)]
            if out.exists():
                shutil.rmtree(out)
            done = capture(pages, apps, out)
        finally:
            os.chdir(here)
    (out / "index.html").write_text(_index_html(done), encoding="utf-8")
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    done = build(ap.parse_args().out)
    print(f"{len(done)} pages in {ap.parse_args().out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
