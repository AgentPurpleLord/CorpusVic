"""
Full-text search over the corpus.

A separate SQLite database, built from the same merged nodes the reader
pages are rendered from, and thrown away and rebuilt whenever anything
changes. Rebuilding the whole thing takes about two seconds for the whole
corpus, which is the fact the rest of this module's design falls out of:
there is no incremental update path, no partial invalidation and no
queue, because there is no reason to have one. Every bug those would have
had is a bug that cannot be written.

Three things are worth knowing before reading further.

**It indexes the merged nodes, not `data/parsed/*.json`.** A reviewer's
corrections live in the database and override the parse, and the merge
also drops nodes folded into their neighbours -- 189 of them in one
reprint of the Criminal Procedure Act alone. Indexing the raw parse would
mean searching words nobody is shown and missing words that are.

**It lives in `data/search.db`, which is gitignored, and that is
deliberate.** `data/legislation.db` is committed and pushed as one whole
file; an index inside it would add megabytes to every push. Worse, it is
derived from a merge of two committed things, so a committed copy is a
third thing that can disagree with them -- and `sync.pull` replaces the
database wholesale, so the copy that arrived would be whichever machine
built it last, looking authoritative.

**It holds only what is on the public site.** Publication is decided per
work (see corpus/db.py) and the index is rebuilt when that changes, so a
search can never surface a provision the site would not serve. Filtering
at query time instead would mean two places that have to agree about what
is published, which is one more than can be kept true.
"""
import html
import os
import re
import sqlite3
from pathlib import Path

from .markdown_export import _iter_body_units, compute_section_slugs
from .versions import split_document_slug

INDEX_FILENAME = "search.db"
SCHEMA_VERSION = "1"

# What a snippet's highlights are marked with on the way out of sqlite.
# Two characters that cannot occur in legislation, so that the text can be
# escaped as HTML *after* sqlite has marked it and before the marks become
# tags. Marking with "<mark>" directly would have the escape destroy the
# marks; not escaping at all would publish any "<" in the text as markup.
_MARK_OPEN = "\x02"
_MARK_CLOSE = "\x03"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS doc (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    work       TEXT NOT NULL,
    version    INTEGER,
    site_slug  TEXT NOT NULL,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    as_at      TEXT,
    -- Whether this is the newest version of its work that is held. What
    -- the "include superseded reprints" switch filters on, and the
    -- reason the default is off: five reprints of one Act answer nearly
    -- every query five times.
    is_current INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    heading,
    body,
    doc_id     UNINDEXED,
    page       UNINDEXED,   -- the section page this provision is on
    fragment   UNINDEXED,   -- its anchor within that page, or ''
    label      UNINDEXED,   -- "Section 242 Committal proceeding"
    breadcrumb UNINDEXED,   -- "Chapter 5 > Part 5.2"
    ntype      UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


class SearchUnavailable(RuntimeError):
    """There is no index to read yet."""


def index_path(base_dir) -> Path:
    return Path(base_dir) / "data" / INDEX_FILENAME


# ---------------------------------------------------------------------------
# Turning what somebody typed into something FTS5 will accept
# ---------------------------------------------------------------------------

# FTS5's MATCH is a query language, not a string, and legislation is full
# of characters that are punctuation to it: "s 3(1)" is a syntax error,
# and so is a single unbalanced quote. Raw input can never reach MATCH.
_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
_TOKEN_RE = re.compile(r'"[^"]*"?|\S+')
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def parse_query(raw: str) -> str:
    """What somebody typed, as something FTS5 will accept.

    Kept deliberately small. A phrase in double quotes stays a phrase; a
    trailing `*` stays a prefix search; `AND`/`OR`/`NOT` in capitals stay
    operators, because that is how every search box in the world behaves
    and someone who types them means them. Everything else is reduced to
    quoted words, which is what makes `s 3(1)` a search for "s" and "3"
    and "1" rather than a syntax error.

    Returns "" for input with nothing searchable in it, which callers
    treat as "no search" rather than as an error."""
    parts = []
    for token in _TOKEN_RE.findall(raw or ""):
        if token.startswith('"'):
            inner = token.strip('"').strip()
            words = _WORD_RE.findall(inner)
            if words:
                parts.append('"' + " ".join(words) + '"')
            continue
        if token in _OPERATORS:
            # An operator with nothing before it can only be a syntax
            # error, so it is dropped rather than passed on.
            if parts and not parts[-1] in _OPERATORS:
                parts.append(token)
            continue
        if token.startswith("-") and len(token) > 1:
            words = _WORD_RE.findall(token[1:])
            if words and parts:
                parts.append("NOT")
                parts.append('"' + " ".join(words) + '"')
            continue
        prefix = token.endswith("*")
        words = _WORD_RE.findall(token)
        if not words:
            continue
        parts.append('"' + " ".join(words) + '"' + ("*" if prefix else ""))
    while parts and parts[-1] in _OPERATORS:
        parts.pop()
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Building it
# ---------------------------------------------------------------------------


def _breadcrumb_label(breadcrumb) -> str:
    """"Chapter 5 > Part 5.2" -- where in the Act a hit sits, for a
    result that has to be recognisable without opening it."""
    pieces = []
    for ancestor in breadcrumb or ():
        node = ancestor["node"] if isinstance(ancestor, dict) and "node" in ancestor else ancestor
        kind = (node.get("type") or "").replace("_", " ").title()
        number = node.get("number")
        heading = node.get("heading")
        label = " ".join(p for p in (kind, number) if p)
        if heading:
            label = f"{label} {heading}".strip()
        if label:
            pieces.append(label)
    return " › ".join(pieces)


def _page_label(node: dict) -> str:
    """"Section 242 Committal proceeding" -- the page a hit is on."""
    kind = (node.get("type") or "").replace("_", " ").title()
    number = node.get("number")
    heading = node.get("heading")
    return " ".join(p for p in (kind, number, heading) if p)


def _rows_for_document(source, slug: str, html_view):
    """One row per provision of one document: its words, and enough to
    build the address of the page it is on.

    Every provision, not every page. A section is one page but many
    provisions, and a reader searching for words in a subsection wants
    that subsection -- so each node is placed on the page that renders
    it, with the anchor it renders under, by walking the same units the
    renderer walks."""
    nodes, _unattached, hierarchy = source._current_nodes(slug)
    parsed = {"nodes": nodes, "hierarchy": hierarchy}
    title = source._act_title(slug)
    by_node_index = source._page_index(slug)["by_node_index"]
    context = html_view._build_context(parsed, title)

    # The tree holds the very same node dicts as the list, so identity is
    # the reliable way across. Position is not: the tree carries each
    # node's *parse* index, while the page index is keyed by position in
    # the merged list, and the two diverge the moment anything has been
    # merged away.
    index_of = {id(node): index for index, node in enumerate(nodes)}

    # node index -> where it renders. Anchors are derived the same way
    # the page derives them (compute_section_slugs is what html_view
    # calls), so a result lands on the provision rather than at the top
    # of the section holding it.
    placement: dict[int, tuple] = {}
    for tree_node, breadcrumb in context["sections"]:
        section_index = index_of.get(id(tree_node["node"]))
        page = by_node_index.get(section_index)
        if not page:
            continue
        slugs = compute_section_slugs(tree_node)
        crumb = _breadcrumb_label(breadcrumb)
        label = _page_label(tree_node["node"])
        # The section itself, whose own heading and lead-in text are as
        # searchable as anything under it.
        placement[section_index] = (page, "", label, crumb)
        for unit in _iter_body_units(tree_node):
            index = index_of.get(id(unit["tree_node"]["node"]))
            if index is None:
                continue
            anchor = slugs.get((unit["tree_node"]["eid"], unit["clause_index"])) or ""
            placement[index] = (page, anchor, label, crumb)

    for index, node in enumerate(nodes):
        where = placement.get(index)
        if where is None:
            continue  # a Part or Division heading -- structure, not text
        body = (node.get("text") or "").strip()
        heading = (node.get("heading") or "").strip()
        if not body and not heading:
            continue
        page, fragment, label, crumb = where
        yield {
            "heading": heading,
            "body": body,
            "page": page,
            "fragment": fragment,
            "label": label,
            "breadcrumb": crumb,
            "ntype": node.get("type") or "",
        }


def corpus_dir(source, fallback) -> Path:
    """Where the documents are read from, which is not always where the
    index is written to.

    The source owns the corpus and knows where it keeps it. Assuming the
    two directories are the same held for the dashboard, where they are,
    and silently indexed nothing anywhere else -- a test building an
    index into a temporary directory got an empty one, and an empty index
    is indistinguishable from a search that finds nothing."""
    return Path(getattr(source, "BASE_DIR", fallback))


def rebuild(base_dir, source=None, published=None) -> dict:
    """Throws the index away and builds it again from what is on the
    site.

    `base_dir` is where the index is written. The documents come from
    `source`, wherever it keeps them.

    Written to a temporary file and moved into place, so a reader never
    opens a half-built index. Returns what it did, for whoever pressed
    the button."""
    from . import db, html_view

    if source is None:
        import dashboard as source  # the module that owns the document lookups

    base_dir = Path(base_dir)
    corpus = corpus_dir(source, base_dir)
    works = db.published_works(corpus) if published is None else set(published)
    slugs = [slug for slug in source.discover_slugs()
             if (corpus / "data" / "parsed" / f"{slug}.json").exists()
             and split_document_slug(slug)[0] in works]

    # Which of each work's reprints is the newest held. The site publishes
    # that one at the work's own address, and search leads with it for the
    # same reason: it is the law as it now stands.
    newest: dict[str, tuple] = {}
    for slug in slugs:
        work, version = split_document_slug(slug)
        if work not in newest or (version or -1) > (newest[work][1] or -1):
            newest[work] = (slug, version)
    current = {slug for slug, _version in newest.values()}

    target = index_path(base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".building")
    if scratch.exists():
        scratch.unlink()

    conn = sqlite3.connect(str(scratch))
    provisions = 0
    try:
        conn.executescript(_SCHEMA)
        with conn:
            for slug in slugs:
                work, version = split_document_slug(slug)
                status = source.act_status(slug)
                cursor = conn.execute(
                    "INSERT INTO doc (slug, work, version, site_slug, title, kind, as_at, is_current) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (slug, work, version, work if slug in current else slug,
                     source._act_title(slug), status["kind"], status["version_as_at"],
                     1 if slug in current else 0),
                )
                doc_id = cursor.lastrowid
                rows = list(_rows_for_document(source, slug, html_view))
                provisions += len(rows)
                conn.executemany(
                    "INSERT INTO node_fts (heading, body, doc_id, page, fragment, label, breadcrumb, ntype) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r["heading"], r["body"], doc_id, r["page"], r["fragment"],
                      r["label"], r["breadcrumb"], r["ntype"]) for r in rows],
                )
            conn.execute("INSERT INTO node_fts(node_fts) VALUES('optimize')")
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("schema_version", SCHEMA_VERSION), ("signature", signature(base_dir, source))],
            )
    finally:
        conn.close()

    os.replace(scratch, target)
    return {"documents": len(slugs), "provisions": provisions,
            "works": sorted(works), "path": str(target)}


def signature(base_dir, source=None) -> str:
    """A cheap stamp of everything the index is built from, so staleness
    is a comparison rather than a guess: every parse file, the review
    database and its write-ahead log, and which works are published.

    Read from wherever the corpus lives, for the same reason rebuild
    is -- a stamp of the wrong directory is a stamp of nothing."""
    from . import db

    base_dir = corpus_dir(source, base_dir) if source is not None else Path(base_dir)
    parts = []
    parsed_dir = base_dir / "data" / "parsed"
    for path in sorted(parsed_dir.glob("*.json")) if parsed_dir.exists() else []:
        st = path.stat()
        parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
    for name in ("legislation.db", "legislation.db-wal"):
        path = base_dir / "data" / name
        if path.exists():
            st = path.stat()
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
    parts.append("published:" + ",".join(sorted(db.published_works(base_dir))))
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Reading it
# ---------------------------------------------------------------------------


class Index:
    """A read-only handle on the index, which notices when it has been
    replaced.

    The builder writes a new file and moves it over the old one, so a
    connection opened earlier goes on reading the file it opened -- by
    its inode, which still exists because something has it open. That is
    a stale index that never gets less stale, and it looks exactly like a
    working one. So the file is stat()ed before each query and the
    connection reopened when it is no longer the same file."""

    def __init__(self, base_dir):
        self.path = index_path(base_dir)
        self._conn = None
        self._stamp = None

    def _identity(self):
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_dev, st.st_mtime_ns, st.st_size)

    def connection(self) -> sqlite3.Connection:
        stamp = self._identity()
        if stamp is None:
            raise SearchUnavailable(
                "The search index hasn't been built yet. Build it from the dashboard.")
        if self._conn is None or stamp != self._stamp:
            if self._conn is not None:
                self._conn.close()
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._stamp = stamp
        return self._conn

    def available(self) -> bool:
        return self._identity() is not None

    def search(self, raw: str, include_superseded: bool = False,
               limit: int = 20, offset: int = 0) -> dict:
        return search(self.connection(), raw, include_superseded, limit, offset)


def address_of(slug: str, page: str, fragment: str = "") -> str:
    """Where a hit lives, as a path with no prefix on it.

    One function so the two callers cannot compose it differently, and
    `slug` is a parameter because they do not agree on which name for the
    document to use: the public site serves an Act's newest reprint at
    the work's own name, and the admin tool serves every parse under its
    own."""
    address = f"/browse/{slug}/section/{page}"
    return f"{address}#{fragment}" if fragment else address


def _snippet_html(marked: str) -> str:
    """The highlighted extract, as HTML.

    Escaped first and marked second. The other order publishes whatever
    "<" the legislation contains as markup, and marking with tags before
    escaping turns the marks into visible text."""
    return (html.escape(marked)
            .replace(_MARK_OPEN, "<mark>")
            .replace(_MARK_CLOSE, "</mark>"))


def search(conn: sqlite3.Connection, raw: str, include_superseded: bool = False,
           limit: int = 20, offset: int = 0) -> dict:
    """What matches, best first.

    Ranked by bm25 with the heading weighted above the body, because
    somebody searching "committal proceeding" usually wants the provision
    called that rather than the eighty that mention it. Current text
    first, always: an older reprint is never the better answer to a
    question somebody asked today."""
    query = parse_query(raw)
    if not query:
        return {"query": raw, "parsed": "", "total": 0, "results": [], "truncated": False}

    where = "node_fts MATCH ?" + ("" if include_superseded else " AND d.is_current = 1")
    params = [query]
    try:
        total = conn.execute(
            f"SELECT count(*) FROM node_fts f JOIN doc d ON d.id = f.doc_id WHERE {where}",
            params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT d.slug, d.site_slug, d.title, d.as_at, d.is_current, d.version, d.kind,
                       f.page, f.fragment, f.label, f.breadcrumb, f.heading,
                       snippet(node_fts, 1, '{_MARK_OPEN}', '{_MARK_CLOSE}', '…', 18) AS body_snip,
                       bm25(node_fts, 8.0, 1.0) AS rank
                FROM node_fts f JOIN doc d ON d.id = f.doc_id
                WHERE {where}
                ORDER BY d.is_current DESC, rank
                LIMIT ? OFFSET ?""",
            params + [limit, offset]).fetchall()
    except sqlite3.OperationalError as e:
        # parse_query is meant to make this impossible. If it ever gets
        # through, an empty result page beats a 500 -- and says what
        # happened rather than pretending there were no matches.
        return {"query": raw, "parsed": query, "total": 0, "results": [],
                "truncated": False, "error": str(e)}

    results = []
    for row in rows:
        results.append({
            "title": row["title"],
            "kind": row["kind"],
            "as_at": row["as_at"],
            "is_current": bool(row["is_current"]),
            "version": row["version"],
            "label": row["label"],
            "breadcrumb": row["breadcrumb"],
            # Empty for a provision matched on its heading alone -- the
            # label above already shows those words, and repeating them
            # underneath reads as a bug rather than as an extract.
            "snippet_html": _snippet_html(row["body_snip"] or ""),
            # Both names for the document, because the two things that
            # show these results address it differently. The public site
            # serves an Act's newest reprint at the work's own name, so
            # site_slug is its address; the admin tool serves every parse
            # under its own name, so a link there built from site_slug
            # points at a document that does not exist. Which to use is
            # the caller's -- see corpus/search_view.py's `slug_key`.
            "slug": row["slug"],
            "site_slug": row["site_slug"],
            "page": row["page"],
            "fragment": row["fragment"],
            "href": address_of(row["site_slug"], row["page"], row["fragment"]),
        })
    return {"query": raw, "parsed": query, "total": total, "results": results,
            "truncated": total > offset + len(results)}


def main():
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Build or query the corpus search index.")
    ap.add_argument("query", nargs="*", help="words to search for; omit to build the index")
    ap.add_argument("--build", action="store_true", help="rebuild the index")
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--superseded", action="store_true", help="include superseded reprints")
    args = ap.parse_args()

    if args.build or not args.query:
        started = time.time()
        stats = rebuild(args.base_dir)
        print(f"{stats['provisions']} provisions from {stats['documents']} document(s) "
              f"in {time.time() - started:.1f}s -> {stats['path']}")
        if not args.query:
            return

    found = Index(args.base_dir).search(" ".join(args.query), args.superseded)
    print(f"{found['total']} match(es) for {found['parsed']}")
    for result in found["results"]:
        print(f"\n  {result['title']} -- {result['label']}")
        if result["breadcrumb"]:
            print(f"    {result['breadcrumb']}")
        print("    " + re.sub(r"</?mark>", "*", result["snippet_html"])[:150])
        print(f"    {result['href']}")


if __name__ == "__main__":
    main()
