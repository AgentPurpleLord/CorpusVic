# vic-legislation-parser

Turns Victorian legislation PDFs into structured, browsable, exportable
documents — and gives a human the tools to check the machine's work.

Input is an Authorised Version Act, a Bill, or a Bill's Explanatory
Memorandum, exactly as published by the Office of the Chief Parliamentary
Counsel. Output is a hierarchy of provisions (Chapter → Part → Division →
Section → subsection → paragraph → …) with the amendment history attached
to the provisions it changed, cross-references between Act, Bill and EM,
and exports to Markdown and Akoma Ntoso XML.

## The one guarantee

**Every input line lands in exactly one output node.** The parser tracks
it (`lines_total` vs `lines_consumed`) and `run_pipeline.py` aborts if the
two ever disagree, so a regression that silently drops half a Schedule
fails the build rather than quietly shipping. CI runs the full pipeline
over four real Acts on every push for exactly this reason.

Parsing is deterministic — pattern matching over the PDF's own font
weight, size and x-position, with no model call anywhere. There used to be
a model-backed engine alongside it; it was removed rather than fixed,
because it read the page as plain text and so never saw the signal the
rules engine actually classifies on. See `run_pipeline.py`'s docstring.

## How it flows

```
acts/<name>.pdf
  │  extract.py         strip headers/footers/margin notes -> BodyLine (text + geometry + font)
  │  toc.py             find where the front matter ends and the body starts
  │  rule_parser.py     classify each line by pattern + boldness/size -> a flat, ordered node list
  │  profiles.py        per-Act regex overrides (YAML), so a new numbering style is config, not code
  │  tree.py            reconstruct the hierarchy, attach the amendment-history margin notes
  │  endnotes.py        the closing Endnotes: General information, Table of Amendments, Explanatory details
  │  diagnostics.py     completeness + structural anomalies + history-linking confidence
  v
data/ai_parsed/<name>.json     the parse
data/legislation.db            the human's review work
  │  review.py                 verify / edit / split / merge / renest, tag link spans
  v
  ├─ html_view.py        live AustLII-style browse view (reads in-progress review state)
  ├─ markdown_export.py  one page per section, cross-references hyperlinked
  └─ akn_export.py       Akoma Ntoso XML, validated against the OASIS schema
```

## Running it

```bash
pip install -r requirements-gui.txt      # or requirements.txt for the pipeline alone

python run_pipeline.py acts/crimes-act.pdf                       # an Act
python run_pipeline.py acts/my-bill.pdf --document-type bill     # a Bill
python run_em_pipeline.py acts/my-bill-em.pdf                    # an Explanatory Memorandum

python run_bill_linking.py my-bill my-act --em my-bill-em        # link the three together

python export_markdown.py crimes-act
python export_akn.py crimes-act
```

Then, for the web interface:

```bash
python dashboard.py          # http://127.0.0.1:8000 -- everything from one port
```

The dashboard is the front door: upload a PDF, run the pipeline, review a
document, browse it, export it. `python review.py <slug>` runs one Act's
review GUI on its own if you prefer.

An Act whose numbering doesn't match the defaults gets a profile rather
than a code change — copy `ai_pipeline/profiles/TEMPLATE.yaml`, and see
`acts/profiles/basic-structure.yaml` for how Victorian Acts are actually
structured.

## The rule that will bite you

`data/ai_parsed/<slug>.json` and `data/legislation.db` are **committed
together, as a pair.** Review rows are keyed by a *position* into that
exact parse, so a checkout with the database but a regenerated parse would
silently show one provision's review against another.

Re-parsing is safe: `ai_pipeline/reparse.py` fingerprints each parse and
re-anchors stored review work onto the new one, withdrawing acceptance
from anything whose wording changed rather than re-applying it to words
nobody read. But run `python checkpoint_db.py` before committing the
database — it is opened in WAL mode, and that folds any pending write into
the file.

## Testing

```bash
pytest
```

Scoped to pure logic — the parsers, the exporters' document model, the
review state machine. The FastAPI endpoints and module-global server state
are deliberately not unit-tested; they are exercised end to end against
real parsed Acts and a real browser instead. Each test file says which it
is and why.

## Layout

| Path | What's in it |
| --- | --- |
| `ai_pipeline/` | Everything reusable: parsing, linking, export, storage |
| `acts/` | Source PDFs, and `acts/profiles/basic-structure.yaml` — how Victorian Acts are put together |
| `data/` | Parses, the review database, generated exports |
| `static/` | The review GUI and the dashboard (plain HTML/JS, no build step) |
| `tests/` | The suite, plus `conftest.py`'s builders modelled on real page measurements |
| `deploy/` | Running the dashboard on a VPS behind Caddy with HTTPS — see `deploy/README.md` |

Most modules carry a substantial docstring explaining not just what they
do but why they do it that way, and why the alternatives were rejected.
Those are the real documentation; start with the module you're changing.
