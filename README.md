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
third copy of something two committed files already say. It covers the
current version of each work by default; superseded reprints are a
checkbox, since five reprints of one Act would otherwise answer nearly
every query five times over.

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
