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
deliberate.** It is derived from a merge of two committed things -- the
parses and the review work -- so a committed copy would be a third that
can disagree with them, and the copy that arrived would be whichever
machine built it last, looking authoritative. It rebuilds from scratch in
a few seconds, which is why there is no incremental update path to get
wrong. `data/legislation.db` is gitignored for a related but different
reason: it is rebuilt from `data/review/` (see corpus/review/review_sync.py).

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
from dataclasses import dataclass
from pathlib import Path

from corpus.exporters.markdown_export import _iter_body_units, compute_section_slugs
from corpus.parsing.versions import split_document_slug

INDEX_FILENAME = "search.db"
SCHEMA_VERSION = "2"

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

-- The words the corpus actually contains, and how often. Only a typo
-- correction reads this, and it exists because the obvious source for it
-- does not work: node_fts is tokenized with `porter`, so fts5vocab hands
-- back stems -- "famili", "violenc", "offenc". Checking a typed word
-- against those says "family" is not in the corpus, which is the exact
-- opposite of the truth and breaks the one rule that makes always-on
-- correction safe. Worse, it then offers the reader a stem as a
-- correction. So the surface words are counted at build time and kept
-- here, where they are words a reader would recognise.
CREATE TABLE IF NOT EXISTS word (
    term TEXT PRIMARY KEY,
    cnt  INTEGER NOT NULL
);
"""


class SearchUnavailable(RuntimeError):
    """There is no index to read yet."""


@dataclass(frozen=True)
class Scope:
    """Which documents a search is allowed to look at.

    The corpus holds four kinds of thing and only one of them is the law
    as it stands. Beside each Act sit the Bill it began as and that
    Bill's explanatory memorandum, which restate the same provisions in
    almost the same words -- so a default that searched everything
    answered "family violence intervention order" with Section 39 *and*
    Clause 39, twice over, and pushed the Act's own provisions down the
    page to make room for drafts of themselves.

    So the default is the current Acts, and everything else is something
    you ask for. Which is also how a reader thinks about it: a Bill is of
    interest when you want to know what was intended, not when you want
    to know what the law is.

    Frozen, and one object rather than three booleans threaded through
    five call sites. Three parameters in a row is how a caller ends up
    passing `False, True` and meaning the other one."""

    bills: bool = False
    explanatory: bool = False
    superseded: bool = False

    # The URL parameter each toggle is carried by, and the kind in the
    # index each one admits. Kept together so that adding a fourth kind
    # of document is one line here rather than four edits apart.
    KINDS = (("bills", "bill"), ("explanatory", "em"))

    @classmethod
    def from_params(cls, params) -> "Scope":
        """What the query string asked for.

        `params` is anything with a `.get`, which is what both a
        Starlette request and a plain dict are."""
        def on(name):
            return _is_on(params.get(name))

        return cls(bills=on("bills"), explanatory=on("em"), superseded=on("superseded"))

    def params(self) -> dict:
        """The toggles that are on, for putting back into a link. Only
        the ones that are on, so an ordinary search keeps an ordinary
        URL."""
        out = {}
        if self.bills:
            out["bills"] = "1"
        if self.explanatory:
            out["em"] = "1"
        if self.superseded:
            out["superseded"] = "1"
        return out

    def sql(self) -> str:
        """The clause that narrows a query to this scope.

        Written as exclusions rather than as a list of allowed kinds
        because a kind nobody has thought of yet should show up in an
        ordinary search rather than silently vanish from it."""
        clauses = []
        if not self.superseded:
            clauses.append("d.is_current = 1")
        for attribute, kind in self.KINDS:
            if not getattr(self, attribute):
                clauses.append(f"d.kind <> '{kind}'")
        return "".join(f" AND {clause}" for clause in clauses)

    def __bool__(self) -> bool:
        """Whether this is anything other than the default."""
        return bool(self.bills or self.explanatory or self.superseded)


def _is_on(value) -> bool:
    """What a checkbox sends, in the handful of forms a browser or a
    hand-typed URL might send it as."""
    return str(value or "").lower() in ("1", "on", "true", "yes")


def index_path(base_dir) -> Path:
    return Path(base_dir) / "data" / INDEX_FILENAME


# ---------------------------------------------------------------------------
# Turning what somebody typed into something FTS5 will accept
# ---------------------------------------------------------------------------

def parse_query(raw: str) -> str:
    """What somebody typed, as something FTS5 will accept.

    Kept as the narrow answer to "will sqlite take this", which is what
    the rest of the module and its tests want from it. What the query
    *means* -- its stopwords, its synonyms, whether it is asking for a
    definition -- is corpus/query.py's business, and this is that
    module's MATCH expression."""
    from corpus.search.query import analyse

    return analyse(raw).match


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
    from corpus.publishing import html_view
    from corpus.storage import db

    if source is None:
        from corpus.web import dashboard as source

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
    words: dict = {}
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
                for r in rows:
                    _count_words(words, r["heading"], r["body"])
                conn.executemany(
                    "INSERT INTO node_fts (heading, body, doc_id, page, fragment, label, breadcrumb, ntype) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r["heading"], r["body"], doc_id, r["page"], r["fragment"],
                      r["label"], r["breadcrumb"], r["ntype"]) for r in rows],
                )
            conn.execute("INSERT INTO node_fts(node_fts) VALUES('optimize')")
            conn.executemany("INSERT INTO word (term, cnt) VALUES (?, ?)",
                             sorted(words.items()))
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("schema_version", SCHEMA_VERSION), ("signature", signature(base_dir, source))],
            )
    finally:
        conn.close()

    os.replace(scratch, target)
    return {"documents": len(slugs), "provisions": provisions, "words": len(words),
            "works": sorted(works), "path": str(target)}


# Letters only, deliberately. The corpus is full of tokens like
# "offence8" and "definitions5", where a footnote marker has ended up
# glued to a word -- and a reader told "showing results for offence8"
# would rightly conclude the search is broken. Splitting on digits counts
# the word and drops the marker.
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _count_words(words: dict, *texts) -> None:
    """Tallies the surface words of one provision into `words`.

    The corpus's own vocabulary, which is the only list a typo may be
    corrected to. Counted here rather than read back out of fts5vocab
    because that returns porter stems -- see the `word` table in _SCHEMA
    for why that is not merely inconvenient but wrong."""
    for text in texts:
        for word in _WORD_RE.findall((text or "").lower()):
            words[word] = words.get(word, 0) + 1


def signature(base_dir, source=None) -> str:
    """A cheap stamp of everything the index is built from, so staleness
    is a comparison rather than a guess: every parse file, the review
    database and its write-ahead log, and which works are published.

    Read from wherever the corpus lives, for the same reason rebuild
    is -- a stamp of the wrong directory is a stamp of nothing."""
    from corpus.storage import db

    base_dir = corpus_dir(source, base_dir) if source is not None else Path(base_dir)
    parts = []
    parsed_dir = base_dir / "data" / "parsed"
    for path in sorted(parsed_dir.glob("*.json")) if parsed_dir.exists() else []:
        st = path.stat()
        parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
    path = base_dir / "data" / "legislation.db"
    if path.exists():
        st = path.stat()
        parts.append(f"legislation.db:{st.st_mtime_ns}:{st.st_size}")
    # The write-ahead log, but only when it holds something. A -wal is
    # created when a connection opens and removed when the last one
    # closes, so stamping its mere existence made the index read stale
    # whenever nothing happened to have the database open -- a staleness
    # light that comes on by itself is one you learn to ignore, which is
    # how a genuinely stale index ends up being served. An empty one
    # holds no decisions, so it is the same as none.
    wal = base_dir / "data" / "legislation.db-wal"
    if wal.exists():
        st = wal.stat()
        if st.st_size:
            parts.append(f"legislation.db-wal:{st.st_mtime_ns}:{st.st_size}")
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
        self.base_dir = Path(base_dir)
        self.path = index_path(base_dir)
        self._conn = None
        self._stamp = None
        self._vocabulary = None
        self._vectors = None

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
            self._vocabulary = None
        return self._conn

    def vocabulary(self) -> dict:
        """The corpus's words, read once per index rather than per query.

        Every query needs the whole list -- correction has to know
        whether a word is absent, which is a question about all of them
        -- and re-reading 7,000 rows to answer it was an eighth of the
        time a search took. Dropped when the file changes, by the same
        stamp that decides the connection is stale, so a rebuilt index is
        never answered from the old one's vocabulary."""
        conn = self.connection()
        if self._vocabulary is None:
            self._vocabulary = vocabulary(conn)
        return self._vocabulary

    def available(self) -> bool:
        return self._identity() is not None

    def vectors(self):
        """The semantic half, or None when there is no model.

        Built lazily and kept, because loading it means memory-mapping a
        file and starting an ONNX session -- neither of which should
        happen on a server that has no model, which is most of them."""
        if self._vectors is None:
            from corpus.search import embeddings

            self._vectors = embeddings.Vectors(self.base_dir)
        return self._vectors if self._vectors.available() else None

    def search(self, raw: str, scope: "Scope | None" = None,
               limit: int = 20, offset: int = 0) -> dict:
        return search(self.connection(), raw, scope, limit, offset,
                      vocab=self.vocabulary(), vectors=self.vectors())


# How far a structural match is worth moving a result. Large, because
# these are not "this provision mentions your words" but "this provision
# is the one that defines the thing you asked about" -- and measured: it
# is what takes "what is family violence" from rank 10 to rank 1.
_DEFINING_HEADING_BOOST = 25.0
_DEFINITION_NODE_BOOST = 18.0

# A heading that answers the question the query asked -- "Who may appeal"
# for "who can appeal ...". Below the defining-heading boost, because
# "Meaning of X" for "definition of X" is a near-certainty and this is a
# strong hint.
_ANSWERING_HEADING_BOOST = 20.0

# Provisions considered for re-ranking before the page is cut. Beyond
# this, results are in bm25 order, which is where they were before any of
# this existed.
_RERANK_DEPTH = 300

# How many heading-answered provisions to fetch. Small on purpose: a
# heading that opens with the question's own word is rare, and if there
# are more than this the question was not specific enough for the trick
# to be what saves it.
_HEADING_DEPTH = 40

# How many semantically-proposed sections to go and fetch when the
# lexical pool does not already hold them. Bounded because each is a
# query, and because a suggestion that is fortieth is not a suggestion.
_SEMANTIC_MERGE = 20


def vocabulary(conn: sqlite3.Connection) -> dict:
    """Every word the corpus contains, and how often -- the only words a
    typo may be corrected to.

    Around 24,000 of them for this whole corpus, small enough that the
    nearest word is found by looking and correction needs no index of its
    own.

    Read from the `word` table rather than from fts5vocab. That
    distinction is the whole safety of the feature: fts5vocab over
    node_fts returns porter *stems*, so asking it whether the corpus
    contains "family" answers no -- and correction, which is only ever
    supposed to fire on a word the corpus does not have, would fire on
    nearly every word anyone typed, offering "famili" back as the
    correction. An index built before that table existed simply does no
    correcting, which is what search did before any of this."""
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT term, cnt FROM word")}
    except sqlite3.Error:
        return {}


def _heading_match(analysis) -> str:
    """An FTS5 query for provisions whose *heading* answers the question,
    or "" when the query is not a question.

    A second query rather than a deeper scan of the first. bm25 puts
    Section 114 "Who may appeal" at position 1,109 for "who can appeal a
    family violence order" -- below all six thousand provisions that
    merely mention a family violence order -- so no re-ranking depth this
    side of reading the whole match would ever reach it. Asking for the
    handful of provisions headed with the question's own word costs one
    indexed lookup and finds it directly."""
    if not analysis.asks or not analysis.subject:
        return ""
    subject = " OR ".join(f'"{w}"' for w in analysis.subject)
    return f'heading: ("{analysis.asks}" AND ({subject}))'


def _answers(heading: str, analysis) -> bool:
    """Whether this provision's heading answers the question that was
    asked -- "Who may appeal" for "who can appeal ...".

    The heading has to open with the same word. A heading that merely
    contains "who" is a sentence about somebody, not an answer to a
    question about who."""
    low = (heading or "").lower()
    if not analysis.asks or not low.startswith(analysis.asks + " "):
        return False
    return bool(set(analysis.subject) & set(re.findall(r"[^\W_]+", low)))


def _defines(heading: str, subject: list) -> bool:
    """Whether this provision is the one that defines what was asked
    about -- "Meaning of family violence" for "definition of family
    violence", rather than the eighty provisions that use the phrase."""
    low = (heading or "").lower()
    if not subject or not low.startswith(("meaning of", "definition of", "definitions of")):
        return False
    return set(subject) <= set(re.findall(r"[^\W_]+", low))


def _rerank(rows: list, analysis) -> list:
    """bm25, adjusted for what the question was asking for.

    Only ever adjusted upward, and only on a structural match: nothing
    here can push a good lexical hit down the page, it can only lift the
    provision that answers the question above the ones that mention it."""
    if not analysis.wants_definition and not analysis.asks:
        return rows
    scored = []
    for row in rows:
        score = -row["rank"]
        heading = row["heading"] or ""
        if analysis.wants_definition and _defines(heading, analysis.subject):
            score += _DEFINING_HEADING_BOOST
        elif analysis.wants_definition and row["ntype"] == "definition" and analysis.subject \
                and set(analysis.subject) <= set(re.findall(r"[^\W_]+", heading.lower())):
            score += _DEFINITION_NODE_BOOST
        elif _answers(heading, analysis):
            score += _ANSWERING_HEADING_BOOST
        scored.append((-score, row))
    scored.sort(key=lambda pair: pair[0])
    return [row for _score, row in scored]


def _fuse_semantically(conn, ranked: list, vectors, analysis, scope) -> list:
    """Reciprocal rank fusion of the lexical ordering with the sections
    the embeddings think the question is about.

    The sections it names are usually nowhere in the lexical pool -- that
    is the whole point, since "can I call a lawyer after being arrested"
    matches none of the words in "Right to communicate with a friend,
    relative or legal practitioner". So they are fetched and merged in
    before the two orderings are fused.

    Fetched **without the MATCH**, which is the thing to get right here:
    a query constrained by the words the reader typed can only ever
    return rows containing those words, which is precisely the set the
    semantic half exists to escape. Asking that way returns nothing and
    looks exactly like an embedding model with no opinion.

    Any failure leaves the lexical results as they were. A model that
    will not load, vectors that disagree with the index, a query that
    embeds to nothing: none of those is a reason for search to stop
    working, and losing the semantic half quietly beats a 500 on a page
    somebody is looking at."""
    try:
        nearest = vectors.nearest(analysis.raw)
    except Exception:  # noqa: BLE001 -- see the docstring: never fatal
        return ranked
    if not nearest:
        return ranked

    have = {(row["site_slug"], row["page"]) for row in ranked}
    sql = ("""SELECT d.slug, d.site_slug, d.title, d.as_at, d.is_current, d.version, d.kind,
                     f.page, f.fragment, f.label, f.breadcrumb, f.heading, f.ntype,
                     '' AS body_snip, 0.0 AS rank
              FROM node_fts f JOIN doc d ON d.id = f.doc_id
              WHERE d.site_slug = ? AND f.page = ?"""
           + scope.sql()
           + " ORDER BY f.rowid LIMIT 1")
    for hit in [h for h in nearest if (h["site_slug"], h["page"]) not in have][:_SEMANTIC_MERGE]:
        # One row for the section, not every provision in it. The
        # semantic side has no opinion about which subsection answers the
        # question, and a reader who follows the link lands on the
        # section page either way.
        #
        # No snippet: the query's words are not in this provision, which
        # is why it took an embedding to find it. An extract highlighting
        # nothing would only look like a bug.
        found = conn.execute(sql, (hit["site_slug"], hit["page"])).fetchone()
        if found:
            ranked.append(dict(found))

    from corpus.search.embeddings import fuse

    return fuse(ranked, nearest, lambda row: (row["site_slug"], row["page"]))


def address_of(slug: str, page: str, fragment: str = "") -> str:
    """Where a hit lives, as a path.

    One function, so an address is composed the same way wherever it is
    composed. `slug` is a parameter because the index records two names
    for a document -- the work's own, which is where the site serves the
    newest reprint, and the parse's own."""
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


def search(conn: sqlite3.Connection, raw: str, scope: "Scope | None" = None,
           limit: int = 20, offset: int = 0, vocab: "dict | None" = None,
           vectors=None) -> dict:
    """What matches, best first.

    Ranked by bm25 with the heading weighted above the body, because
    somebody searching "committal proceeding" usually wants the provision
    called that rather than the eighty that mention it.

    `scope` says which documents are eligible; the default is the current
    Acts, which is what somebody asking a question about the law means.
    See Scope."""
    from corpus.search.query import analyse

    # `vocab` is passed by Index, which keeps it for the life of the
    # file; reading it here is the path a caller holding a bare
    # connection takes.
    scope = Scope() if scope is None else scope
    analysis = analyse(raw, vocabulary(conn) if vocab is None else vocab)
    if not analysis:
        return {"query": raw, "parsed": "", "total": 0, "results": [],
                "truncated": False, "corrections": {}}

    where = "node_fts MATCH ?" + scope.sql()
    columns = f"""SELECT d.slug, d.site_slug, d.title, d.as_at, d.is_current, d.version, d.kind,
                         f.page, f.fragment, f.label, f.breadcrumb, f.heading, f.ntype,
                         snippet(node_fts, 1, '{_MARK_OPEN}', '{_MARK_CLOSE}', '…', 18) AS body_snip,
                         bm25(node_fts, 8.0, 1.0) AS rank
                  FROM node_fts f JOIN doc d ON d.id = f.doc_id"""
    params = [analysis.match]
    # Enough to re-rank the page that is about to be shown, and the ones
    # just past it, without reading the whole match.
    depth = max(_RERANK_DEPTH, offset + limit * 3)
    try:
        total = conn.execute(
            f"SELECT count(*) FROM node_fts f JOIN doc d ON d.id = f.doc_id WHERE {where}",
            params).fetchone()[0]
        candidates = list(conn.execute(
            # bm25 first, and the current text only as the tie-break.
            # The other order -- every current provision, then every
            # superseded one -- was a no-op whenever superseded reprints
            # were excluded (they are not in the result set to sort) and
            # actively defeated the switch whenever they were included:
            # the first superseded hit for "committal proceeding" was at
            # position 1,613, eighty pages down. Ties are the common case
            # here, because a reprint that did not change a section has
            # word for word the same text and scores identically, and
            # there the current one should lead.
            f"{columns} WHERE {where} ORDER BY rank, d.is_current DESC LIMIT ?",
            params + [depth]))
        # The provisions headed the way the question was asked, which
        # bm25 can bury a thousand deep. A handful of rows, merged into
        # the pool so the re-ranking below can lift them.
        #
        # They come in at the bottom of it, whatever bm25 said about
        # them. Their score is from a different MATCH expression and is
        # not on the same scale as the pool's -- mixing the two put a
        # provision that merely mentions police above the one that says
        # when police may issue a safety notice. So the only thing that
        # can lift a merged row is the boost for actually answering the
        # question; on its own it sits below everything already found.
        heading_match = _heading_match(analysis)
        if heading_match:
            seen = {(row["slug"], row["page"], row["fragment"]) for row in candidates}
            floor = max((row["rank"] for row in candidates), default=0.0)
            for position, row in enumerate(conn.execute(
                    f"{columns} WHERE {where} ORDER BY rank LIMIT ?",
                    [heading_match, _HEADING_DEPTH])):
                if (row["slug"], row["page"], row["fragment"]) not in seen:
                    merged = dict(row)
                    # All below the pool, but keeping the order the
                    # heading query put them in -- which is meaningful
                    # among themselves even though it is not comparable
                    # with the pool's. Without it, "Who may appeal" and
                    # "Who may apply to vary, revoke or extend" arrive
                    # tied and come out in whatever order they were read.
                    merged["rank"] = floor + position * 0.001
                    candidates.append(merged)
                    total += 1
    except sqlite3.OperationalError as e:
        # corpus/query.py is meant to make this impossible. If it ever
        # gets through, an empty result page beats a 500 -- and says what
        # happened rather than pretending there were no matches.
        return {"query": raw, "parsed": analysis.match, "total": 0, "results": [],
                "truncated": False, "corrections": analysis.corrections, "error": str(e)}

    ranked = _rerank(candidates, analysis)
    # The semantic half, when there is one. Everything above is what
    # search does without a model, and this is the only place the two
    # meet -- so with no model the path below is one `if` and the results
    # are byte for byte what they were.
    if vectors is not None and not analysis.verbatim:
        ranked = _fuse_semantically(conn, ranked, vectors, analysis, scope)
    rows = ranked[offset:offset + limit]

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
            # Both names for the document. site_slug is where the site
            # serves it -- an Act's newest reprint lives at the work's
            # own name -- and is what every link is built from. slug is
            # the parse this row actually came from, which is what tells
            # you *which reprint* answered, and is the only one that
            # distinguishes the superseded ones from each other.
            "slug": row["slug"],
            "site_slug": row["site_slug"],
            "page": row["page"],
            "fragment": row["fragment"],
            "href": address_of(row["site_slug"], row["page"], row["fragment"]),
        })
    return {"query": raw, "parsed": analysis.match, "total": total, "results": results,
            "truncated": total > offset + len(results),
            # {what was typed: what it was read as}, for the page to say
            # "showing results for". Empty unless a word matched nothing
            # in the corpus at all.
            "corrections": analysis.corrections}


def main():
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Build or query the corpus search index.")
    ap.add_argument("query", nargs="*", help="words to search for; omit to build the index")
    ap.add_argument("--build", action="store_true", help="rebuild the index")
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--superseded", action="store_true", help="include superseded reprints")
    ap.add_argument("--bills", action="store_true", help="include Bills")
    ap.add_argument("--em", action="store_true", help="include explanatory memoranda")
    args = ap.parse_args()

    if args.build or not args.query:
        started = time.time()
        stats = rebuild(args.base_dir)
        print(f"{stats['provisions']} provisions from {stats['documents']} document(s) "
              f"in {time.time() - started:.1f}s -> {stats['path']}")
        if not args.query:
            return

    found = Index(args.base_dir).search(
        " ".join(args.query),
        Scope(bills=args.bills, explanatory=args.em, superseded=args.superseded))
    print(f"{found['total']} match(es) for {found['parsed']}")
    for result in found["results"]:
        print(f"\n  {result['title']} -- {result['label']}")
        if result["breadcrumb"]:
            print(f"    {result['breadcrumb']}")
        print("    " + re.sub(r"</?mark>", "*", result["snippet_html"])[:150])
        print(f"    {result['href']}")


if __name__ == "__main__":
    main()
