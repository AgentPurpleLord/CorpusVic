# Disclaimer / Copyright

Corpus does not provide legal advice. 

Corpus does not provide data repository services and does not give permission for the value added to the documents to be republished (e.g. hyperlinks, etc.).

Corpus is not the copyright owner of the source documents that are published or interpreted. 

Corpus does claim copyright to all value-added content that is added to those documents, such as the linking of various acts or other documents. 

The State of Victoria owns all copyright to the official, authorised versions of legislation. 


# Corpus of Victoria

Corpus is a legislative parsing tool. Legislation is Victoria, Australia is released in PDF format. Corpus extracts the text from the PDF and places it into a database. An administrator can then review the extracted text to ensure its accuracy. 

Almost none of it uses a model. The parser works from the page's own
geometry and typography -- where a line sits, whether it is bold, how far
it is indented -- checked against patterns written down per Act, and given
the same PDF it gives the same answer every time. The parts that *do* ask
a model live in `corpus/ai/`, and they are all optional and all advisory:
a second opinion on a piece the diagnostics already flagged, and a
whole-document audit run offline. Nothing in there decides anything.

    corpus/         the library: extract, parse, review, export
    corpus/ai/      the parts that use a model, and only those
    corpus/profiles/  per-Act pattern overrides
    data/parsed/    the parser's output, one JSON file per document
    data/legislation.db   every decision a human made about it
    tests/

    run_pipeline.py       a PDF -> data/parsed/<act>.json
    review.py             the review GUI for one document
    dashboard.py          the hub, and the live browse view
    public.py             the public site (see above)
    corpus/search.py      the full-text index behind it
    corpus/query.py       and what it makes of a question in plain words
    corpus/embeddings.py  the optional semantic half of it
    export_static_site.py the same site as a static archive
    export_markdown.py, export_akn.py

## Reviewing against the page

`review.py` shows the PDF with a box drawn over every provision the
parser found, and those boxes are the controls. Right-click one to go to
it, to edit or move or delete it, or to redraw it -- and once it is
drawn where the provision actually is, **Read this piece from its box**
takes the provision's words from under it. So a provision the parser
split in the wrong place is corrected by pointing at the right words
rather than by retyping them.

A provision can carry several boxes, which is how one printed in more
than one place is marked up -- across a column or a page break, or a
subsection resumed after its own list. Boxes are read top to bottom
whatever order they were drawn in, and any one of them can be removed
without touching the provision.

Reading a box changes what a provision *says*, never what it is or
whether it exists. A subparagraph read on its own is indistinguishable
from a paragraph, and a note without the "Notes" heading above it is
just text: those are decided from the surroundings a box excludes by
design. Adding and removing provisions stays a separate, deliberate act.

The reviewed material is published at **[www.corpusvic.au](https://www.corpusvic.au)**,
served by `public.py` -- a read-only application reading the same
database the review tool writes. There is no build step between a review
decision and the page a reader sees.

**What is on it is a decision, not a consequence.** Each work carries a
published flag, set from the dashboard and stored in
`data/legislation.db` alongside the review work, so it travels with a
push. Nothing reaches the site by having been parsed.

**Search** is `corpus/search.py`: an FTS5 index over every provision of
every published work, built from the merged text a reader actually sees
rather than from the raw parse. It rebuilds from scratch in a few seconds
-- which is why there is no incremental update path to get wrong -- and
lives in a gitignored `data/search.db`, because a committed index is a
third copy of something two committed files already say. 

**What a search is of, by default, is the law as it stands** -- the Acts,
as they are now. The corpus also holds the Bill each Act began as, that
Bill's explanatory memorandum, and every superseded reprint, and each is
a checkbox under "Also search…" on the search page. Searching all of it
by default was the old behaviour and it was worse in a way that is
measurable rather than arguable: a Bill restates its Act in almost the
same words, so every question was answered twice over with drafts of
itself, and narrowing the default moved five of the twenty eval queries
up the page and none down (MRR 0.785 to 0.824).

The default is not a filter somebody switched on. It is the corpus a
question about the law is asked of; a Bill is what you want when you are
asking what was *intended*, which is a different question and worth
having to ask for.

What somebody types is read by `corpus/query.py` before it reaches FTS5,
and that is where the difference between a search box and a useful one
turned out to live. FTS5 requires every term, so "when can police issue a
safety notice" returned nothing at all; and a question asked in ordinary
words rarely uses the statute's own -- Section 5 of the Family Violence
Protection Act is headed "Meaning of family violence", so searching for
its *definition* never reached it. So stopwords go, what is left is OR'd
and left to bm25 to rank, a small map of legal synonyms is applied, and
the shape of the question is read: "what is X", "definition of X" and
"meaning of X" all ask where X is defined, which the parser already knows
because it recorded which nodes are definitions.

A question that opens with "who", "when" or "how" gets a second, tiny
query over headings alone, because this legislation heads provisions the
same way a person asks -- "Who may appeal", "When is a person a protected
person". It is a second query rather than a deeper scan because bm25 put
Section 114 "Who may appeal" at position 1,109 for "who can appeal a
family violence order", below every one of the six thousand provisions
that merely mention such an order. Typos are corrected
against the corpus's own words, and only ever a word the corpus does not
contain -- so a precise query is never softened into a vague one.

Whether any of that is an improvement is a measurement, not an opinion:
`data/search_eval.yaml` holds queries whose answers are known and
`tests/test_search_relevance.py` scores against it. Mean reciprocal rank
went from 0.29 to 0.79 on those queries, and the floor in that file is a
ratchet.

It also records what search **cannot** do. A second group of queries in
that file is the vocabulary gap: where the reader's words and the
statute's are simply different -- "breach" for contravention, "call a
lawyer" for "communicate with a legal practitioner", "I was forced to
commit the crime by threats" for duress. Search scores 0.012 on them and
finds five of the six nowhere in twenty results. No synonym list scales
to that and no drafting convention helps, because there is nothing to
match on: it is what the provision is *about* that matters. That is the
case for semantic search, and it is written down as a number rather than
an impression -- the two groups are scored separately, because averaging
them would produce a figure that falls whenever a known weakness is
written down.

`corpus/embeddings.py` is the answer to it, and it is **optional in the
strict sense**: with no model on disk, search is the lexical search it
has always been, every test passes, and nothing in the UI mentions it.
Sections rather than provisions are embedded -- 6,173 vectors rather than
50,462, and a coherent unit, since a bare subsection embeds into nothing
useful on its own -- and the two orderings are combined by reciprocal
rank fusion, which compares positions and never a bm25 score against a
cosine. `download_search_model.py` fetches the model;
`python3 -m corpus.relevance` prints the scoreboard, so whether it earns
its place is a measurement. See deploy/README.md.

`export_static_site.py` still builds the whole site as static files, but
as an archive rather than as the site: a copy that survives the server,
published off it by `.github/workflows/pages.yml`. It keeps the
build-time encryption gate (`corpus/site_crypto.py`), because a static
host has no server to check a passphrase.

The domain lives in one place: the `CNAME` file in this repository's
root. GitHub Pages reads it to serve the archive at a custom domain, and
`export_static_site.py` reads it to decide that links need no path
prefix -- a custom domain is mapped at its own root, so a page links to
`/browse/<act>/` rather than `/<repo>/browse/<act>/`.

Every parsed document is published in full. A provision a human has not
yet checked against the PDF still carries its text, above a notice saying
it has not been checked; the contents page says how many of the
document's provisions that applies to. The alternative -- withholding the
text until someone had confirmed it -- made an Act read as though it had
holes in it, which for legislation is the dangerous misreading: a section
merely unchecked looked exactly like a section that does not exist.
