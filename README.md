# Disclaimer / Copyright

Corpus does not provide legal advice. 

Corpus does not provide data repository services and does not give permission for the value added to the documents to be republished (e.g. hyperlinks, etc.).

Corpus is not the copyright owner of the source documents that are published or interpreted. 

Corpus does claim copyright to all value-added content that is added to those documents, such as the linking of various acts or other documents. 

The State of Victoria owns all copyright to the official, authorised versions of legislation. 


# Corpus

Corpus is a legislation parsing tool (focused on Victoria, Australia). The code parses PDFs into a database which can then be transposed into a variety of formats.

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
    export_static_site.py the public site (see above)
    export_markdown.py, export_akn.py

The reviewed material is published at **[www.corpusvic.au](https://www.corpusvic.au)**.

That address lives in one place: the `CNAME` file in this repository's
root. GitHub Pages reads it to keep serving the domain, and
`export_static_site.py` reads it to decide that links need no path
prefix -- a custom domain is mapped at its own root, so a page links to
`/browse/<act>/` rather than `/<repo>/browse/<act>/`. Remove the file and
the site goes back to being a project site at
`https://<owner>.github.io/<repo>/`, with every link prefixed to match.
It is copied into each build, because a Pages deployment serves exactly
what the build uploaded.

Publishing is a push: `.github/workflows/pages.yml` rebuilds the site
whenever the parsed data or the review database changes on `main`. Only
fully-reviewed documents are included.
