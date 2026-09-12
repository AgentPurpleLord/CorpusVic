# vic-legislation-parser

Turns Victorian legislation PDFs into structured, browsable, exportable
documents — and gives a human the tools to check the machine's work.

Input is an Act, a Bill, or a Bill's Explanatory Memorandum, exactly as
published by the Office of the Chief Parliamentary Counsel. Output is a
tree of provisions (Chapter → Part → Division → Section → subsection →
paragraph → …) with amendment history attached to the right provisions,
cross-references between the Act, Bill and EM, and exports to Markdown
and Akoma Ntoso XML.

## The one guarantee

**Every line of the input ends up in exactly one place in the output.**
The parser counts lines in and lines out (`lines_total` vs
`lines_consumed`), and `run_pipeline.py` stops if the two numbers don't
match. That way, a bug that drops half a Schedule fails loudly instead
of shipping silently. CI runs the whole pipeline over four real Acts on
every push to check this.

Parsing itself doesn't use an AI model — it's plain pattern matching
over each PDF's font size, boldness and position on the page. An
earlier version used a model instead, but it read the page as plain
text and missed the formatting clues the current parser relies on, so
it was removed. See `run_pipeline.py`'s docstring for more.

A local model has a narrower, optional role instead: a second opinion
a reviewer can ask for on a piece diagnostics has already flagged as
uncertain, never a replacement for the parser or for a human's own
judgement. See "An optional second opinion from a local model" below.

## How it flows

```
acts/<name>.pdf
  │  extract.py         strip headers/footers/margin notes -> BodyLine (text + position + font)
  │  toc.py             find where the front matter ends and the body starts
  │  rule_parser.py     classify each line by pattern + boldness/size -> a flat, ordered list of provisions
  │  profiles.py        per-Act settings (YAML), so a new numbering style is config, not code
  │  tree.py            build the Chapter/Part/... tree, attach the amendment-history margin notes
  │  endnotes.py        the closing Endnotes: General information, Table of Amendments, Explanatory details
  │  diagnostics.py     checks: nothing missing, no odd structure, history notes matched with confidence
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

A Bill, the Act it became, and its Explanatory Memorandum are shown as
one row rather than three unrelated cards, grouped from whatever
`run_bill_linking.py` has recorded (`data/bill_links/`). Anything not
part of such a group still shows up, just on its own.

## Versions of an Act

An Act is a *work*; each reprint of it (an Authorised Version, as the
government publishes it) becomes a *version* once parsed here. To turn
on version tracking for an Act, put its PDFs in a directory named after
it:

```
acts/criminal-procedure-act/cpa-110.pdf     version 110, as at 4 March 2026
acts/criminal-procedure-act/cpa-114.pdf     version 114, as at 1 July 2026
```

Each one then parses under the work's name plus its own version number —
`criminal-procedure-act-v114` — read from the PDF's own front matter, not
the filename. That combined name is the document's identity everywhere:
the parse's filename, the review database's key, the browse URL. A PDF
sitting directly in `acts/` just keeps its filename as its identity, no
matter what version number it states — putting a PDF in a directory is
what opts it into version tracking, and nothing already parsed changed
when this feature was added.

A profile is looked up under the work's name before the individual
document's, so one `criminal-procedure-act.yaml` covers all five of its
versions.

Only the newest version parsed here shows up in the dashboard or the
browse index — there's no menu of every version held, because most
readers just want the current wording. An older version is still
reachable, though, from any provision that changed: `ai_pipeline/diffing.py`
works out what changed between two versions (nothing records this
directly, since each reprint just restates the whole Act). It matches
provisions by (Schedule, number) rather than by position, so an inserted
section doesn't make the rest of the Act look rewritten, and it compares
on the actual words, so a reprint that repaginates, doubles a space, or
uses a different font is never mistaken for a real amendment. This shows
up as a collapsible timeline on any provision that changed, with each
entry linking both the exact words that changed and the amending Act
named in the margin note. That's also where a reader ends up if they
land on a superseded version — marked plainly as no longer in force,
with a link back to the current one.

That includes a Schedule whose content is unnumbered prose sitting on
one node (Schedule 3 of the Criminal Procedure Act, "Persons who may
witness statements...") rather than in ordinary numbered items — see
`hierarchy.schedule_is_pageable`. This used to be invisible in any
paginated view (browse and Markdown skipped it; only the AKN export,
which serialises the whole tree in one go, ever showed it). Now it gets
its own page like any other provision, margin notes and timeline
included.

A new version's review work doesn't start from scratch either.
`ai_pipeline/reparse.py`'s `apply_carry_forward` seeds a freshly-parsed
version from the nearest earlier reviewed version, using the same
matching that `remap_verified` uses to survive a parser change: a
provision whose wording hasn't changed carries its review status across
unchanged; one whose wording moved has its acceptance withdrawn and gets
flagged for another look; one that's no longer present (typically a
repeal) is kept and marked orphaned rather than dropped. It never
overwrites a version's own review work, so it only does anything the
first time a new version is parsed.

## Linking a citation this pipeline hasn't parsed yet

A margin note or a Schedule's prose often names another Act that this
pipeline may never parse. Every citation whose shape this pipeline can
recognise — a bare "No. 68/2009" in a margin note, or another Act's name
anywhere in the text — becomes a link, never plain dead text. It links
either to that Act's own page, if it's been parsed here
(`ai_pipeline/known_acts.yaml`, a small hand-curated list), or to the
standing address `/legislation/<act_no>[-<year>]` (see dashboard.py's
`legislation_resolver`), which says plainly that the Act hasn't been
parsed here yet rather than showing a bare 404 page. Parsing that Act
later needs no other change — the same link just starts working.

An Act's name in the body of a provision is recognised by its shape, not
by checking it against a fixed list: any run of Capitalised words (plus
a few lowercase connector words — "of", "the", "and", ...) ending in
"Act <year>" or "Bill <year>" is a candidate, so this isn't limited to
Acts already in `known_acts.yaml`. A candidate only becomes a link if it
exactly matches a real title in `known_acts.yaml` or in the much larger
`ai_pipeline/act_registry.json` (roughly 8000 Victorian Acts, extracted
from the OCPC's own "List of Acts in chronological order" — see
`extract_act_registry.py`). Anything that doesn't match either list is
left as plain text. This split — match loosely, then check strictly
against a trusted list — means the pattern can be generous about what
counts as a candidate title without ever linking to the wrong Act by
mistake.

An Act whose numbering doesn't match the usual pattern gets its own
profile instead of a code change — copy `ai_pipeline/profiles/TEMPLATE.yaml`,
and see `acts/profiles/basic-structure.yaml` for how Victorian Acts are
normally structured.

## An optional second opinion from a local model

review.py's "Ask local AI for a second opinion" button gives a reviewer
a local model's read on a piece diagnostics has already flagged as
uncertain — a duplicate section number, an amendment note that named a
more specific provision than the parser could find. It never parses
anything itself, its answer is never applied automatically, and it
only appears *after* the reviewer's own independent blind-review guess
is already recorded — showing it any earlier would just be a different
way of anchoring the judgement that blind-review step exists to
protect (see `ai_pipeline/ai_assist.py`'s own docstring).

This is not the same thing as the model-backed parser mentioned above,
brought back. That one failed for three reasons: it was fed plain
text and lost the font/position signal that actually identifies
structure; it never ran through the completeness check or diagnostics;
and nothing ever tagged its output for the extra scrutiny it needed,
so it got none. This feature avoids all three by construction —
parsing stays 100% the rules engine's job, so the completeness
guarantee and diagnostics still cover every document in full; the
model only ever answers a narrow, already-scoped question over text
the rules engine already extracted (never raw font or position data,
since geometry's job — telling a heading from a sentence — is already
done by the time diagnostics flags something as worth asking about);
and every answer is tagged with the model that gave it and shown
beside the reviewer's own guess, never folded into the parse.

It also only ever runs locally. [Ollama](https://ollama.com) serves an
open-source model file from disk on this machine; nothing is sent
anywhere else, and the feature is entirely optional — nothing in the
pipeline depends on it. Set it up with:

```bash
python install_ai_model.py          # checks Ollama, pulls the default model
```

which explains exactly what's missing if Ollama itself isn't installed
or running (that part is left to you — installing and starting a
background service is a bigger decision than a script should make on
its own). The same check and a "Download model" button are on the
dashboard too, under "AI-assist model". Change the model with
`--model` (see `ai_pipeline/llm_backend.py` for the default and what
size of model it needs).

### A whole-document AI scan

The button above only ever looks at a piece diagnostics has already
flagged. `run_ai_review.py` goes further: it reads *every* unit of an
Act — not just the flagged ones — and asks the same local model
whether each one's own type/number/heading/text classification looks
right, the same "does this look like a parsing mistake" question
`ai_pipeline/ai_assist.py` asks, just asked of the whole document
instead of only what was already flagged (see `ai_pipeline/ai_scan.py`'s
own docstring for exactly what it looks for, and what it deliberately
ignores).

```bash
python run_ai_review.py crimes-act
python run_ai_review.py crimes-act --batch-size 20 --model llama3.1:8b-instruct
python run_ai_review.py crimes-act --restart   # ignore prior progress, scan everything again
```

It's a separate, offline script rather than a button that blocks
review.py, because scanning everything is genuinely slow — several
units are batched into each model call to keep it practical, but a
real Act can still mean dozens of calls, and a local model's own speed
depends entirely on the machine running it. It's resumable and safe to
interrupt: every unit's result (including a "clean" one, for a unit the
model looked at and found nothing wrong with) is saved to
`data/legislation.db` as soon as its batch comes back, and a later run
just picks up where it left off. The dashboard offers the same thing as
a background job per Act ("AI scan"), with a progress count instead of
a terminal to watch.

Its findings land exactly where diagnostics.py's own do: review.py
loads them at startup and they gate blind-review the same way, staying
hidden from a reviewer until their own independent judgement is already
recorded (tagged with a small robot mark in the findings list, so it's
still obvious which ones came from a model rather than a deterministic
check). Nothing here is ever applied to the parse automatically, same
guarantee as the single-piece second opinion above — this just asks
the question of everything, instead of waiting to be asked about one
piece at a time.

## The rule that will bite you

`data/ai_parsed/<slug>.json` and `data/legislation.db` must be
**committed together, as a pair.** Review rows are keyed by their
*position* in that exact parse, so if the database and the parse ever
get out of sync, one provision's review notes could silently attach to
a different provision.

Re-parsing is safe: `ai_pipeline/reparse.py` fingerprints each parse and
re-attaches stored review work to the new one, withdrawing approval from
anything whose wording changed instead of carrying it over blindly. But
run `python checkpoint_db.py` before committing the database — it's kept
open in WAL mode, and this command folds any pending writes into the
main file.

## Testing

```bash
pytest
```

The tests cover pure logic — the parsers, the exporters, the review
state machine. The FastAPI endpoints and the server's shared state are
not unit-tested on purpose; they're checked instead by running the whole
thing end to end against real parsed Acts and a real browser. Each test
file explains what it covers and why.

## Layout

| Path | What's in it |
| --- | --- |
| `ai_pipeline/` | Everything reusable: parsing, linking, export, storage |
| `acts/` | Source PDFs (a directory per version-tracked Act), and `acts/profiles/basic-structure.yaml` — how Victorian Acts are put together |
| `data/` | Parses, the review database, generated exports |
| `static/` | The review GUI and the dashboard (plain HTML/JS, no build step) |
| `tests/` | The suite, plus `conftest.py`'s builders modelled on real page measurements |
| `deploy/` | Running the dashboard on a VPS behind Caddy with HTTPS — see `deploy/README.md` |

Most modules have a docstring explaining not just what they do but why
they're built that way. Those are the real documentation — start with
the module you're changing.
