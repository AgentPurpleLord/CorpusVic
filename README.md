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
    corpus/domain/rules/  per-Act pattern overrides
    data/parsed/    the parser's output, one JSON file per document
    data/review/    every decision a human made about it, as text
    data/legislation.db   the same decisions, in the store three
                          processes write to -- derived, gitignored
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
    static/site/          the site's template: its shell, stylesheets and fonts

**Changing how the site looks** — fonts, colours, buttons, the header,
the footer, the wording — is `docs/STYLING.md`. Almost none of it is
Python, and almost none of it needs a restart: the stylesheets are served
off disk and the templates are re-read when they change, so it is edit,
save, reload.

## Telling the parser what to look for

What each level of an Act looks like on the page is written down in
`corpus/domain/rules/recognition/victorian-act.yaml`, as conditions
rather than as code. Each block is a sentence out of
`corpus/domain/domain.md` -- "Sections are identified by a bolded section
numbers and a heading", "(a) ... indented once from a subsection" --
quoted above the conditions it produced.

A condition reads one thing off the printed line: `bold`, `bold_italic`,
`min_size_ratio` and `max_size_ratio` (size against this document's own
body text), `min_indent` and `max_indent`, `indented_from_parent`,
`centred`, `parent_types`, `after_clean_break`, and `pattern` for the
text itself. Each has a `not_` form for what a type must *not* look like.
Anything not set is not looked at.

An Act that does something of its own gets a file of its own next to the
base, named after it, overriding block by block and condition by
condition.

Two commands, and no need to re-run a PDF for either:

    python -m corpus.parsing.show_profile crimes-act --explain "(c) to determine how"

prints every type the line was weighed against and the one condition that
ruled each one out -- with the line's real size, weight and position,
read out of the extracted document.

    python -m corpus.parsing.check_rules

re-judges every provision already parsed and reports where the rules and
the recorded parse disagree. It is a comparison, not a verdict: either
side can be the one that is wrong.

## Teaching the parser from review

Every piece you accept, correct, merge away or delete is kept as an
example of what its printed line is, in `data/teaching/<act>.jsonl`,
keyed by the line rather than the parser's name for it. The **Teaching**
button on an Act re-reads its PDF with the parser as it stands and checks
it against all of them, naming any that passed last time and now fail.
A piece you kept whole also fails if the parser now opens anything part-way
down it -- a false split opens a line no example is about.

Where the parser and you disagree the same way on three or more lines
that look alike, the **Lessons** page proposes a rule: "a line matching
`^(\d+(?:\.\d+)*[A-Z]*)`, not bold, after a line that finished: starts a
new subitem". Preview it to see every line across the held Acts it would
change and which examples it fixes or breaks; approve it for one Act, its
work, or every Act; or reject it, and it isn't proposed again. Approved
rules live in `data/teaching/rules.json` and are tried before the
parser's own judgement.

In review, retyping, renumbering or merging away a piece looks for the
other undecided pieces in the Act printed the same way -- the same
opening, weight, size and indent, met under the same kind of provision,
and for a merge the same kind of line above -- and offers them for the
same fix. They become examples as their units are accepted.

A second opinion learns from the same examples: a shallow decision tree
(`corpus/teaching/model.py`, retrained after each Teaching check). Review's
"Model disagrees" filter lists the units where it is sure a piece is
something other than what the parser made it, and each flag says which
features decided. It advises and never changes a parse. The Lessons page
shows how often it is right on an Act it wasn't trained on, next to how
often the parser was.

## How a provision is named

Review work is attached to provisions, and a provision is named by what
it is rather than by where it sits in the parse:

    pt2/div1/s97              Part 2, Division 1, section 97
    s97/d/i                   section 97(d)(i)
    s15/definition-injury/a   paragraph (a) of the definition of "injury"
    s97/d+note-1aa            a note a reviewer added after s 97(d)

`corpus/parsing/identity.py` builds these. The reason for them is
measurable: between two reprints of the Criminal Procedure Act, a
position in the node list still points at the same provision 9% to 18%
of the time, and a name does 97% to 100%.

Where a parse gives one name to two provisions -- almost always because
it read a wrapped citation as a fresh subsection -- the first keeps the
plain name and the rest carry a short digest of their own wording. That
happens to 1.7% of provisions, and each one is worth looking at.

The name is what the seven review tables are keyed by, and the position
stays beside it as a record of where the provision sat in the parse a row
was written against. Whatever has a parse loaded works in positions --
within one parse a position is exact and cheap -- and turns them into
names at the edge of the database.

    python -m corpus.storage.node_names           what it would name
    python -m corpus.storage.node_names --write   name them

names the review rows written before the column existed, from the parse
their position still points into. It reports anything it cannot name and
never guesses. Opening a database it has not been run on is refused
rather than half-migrated, and says so.

A row about a provision the current parse does not contain is never
attached to whatever now sits at its old position, and never dropped
either: the review server keeps it as it is, says how many there are at
startup, and gives it back if the provision returns.

## Versions kept as what changed

The Act prints a margin note beside every official change of wording,
citing the amending Act. So one version of a work -- the base, the one
reviewed in full -- is held whole, and every other keeps only the pieces
whose notes cite an Act they didn't cite in the neighbouring version,
its notes, and those pages of its PDF (the rest blank, so page numbers
hold). Its text anywhere else is the neighbour's toward the base, as
reviewed there (`corpus/history/delta.py`, `corpus/storage/parsed.py`).
A difference the parser reads anywhere else is the parser's.

A fetched version is slimmed as it arrives. History review's "Keep
versions slim" converts the versions already held: each is parsed again
with the current parser, then cut down (`python -m corpus.history.slim
<work> --reparse` does the same). The base is the version with the most
review done unless set (`--base`), and is remembered. A slim version is
re-read by fetching it again whole.

## A provision's history

A Principal Act is reviewed at its current version, against its own
PDF and nothing else. Its other Authorised Versions are for **History
review** (`corpus/history/`, from the Act's card): every change between
consecutive versions, one sub-provision at a time, the two wordings side
by side with both printed pages, the amending Acts' instructions and the
new margin notes beside it. Each is confirmed as Parliament's or denied
as the parser's, and a public history shows only confirmed changes -- an
unconfirmed one is as if the two wordings were one. The Acts' evidence
advises; it decides nothing. An instruction found under the wrong piece
offers to put the piece where the Act says.

History review's **Fetch versions** lists every version
legislation.vic.gov.au holds and fetches and parses the ones ticked;
**Fetch amending Acts** gets every Act the newest reprint's Table of
Amendments lists, bar those already in force by the oldest reprint held
(`corpus/amending/`).

Or add one from the dashboard's **Versions** on an Act's card: the
PDF is placed by the version number it states, and refused if it names a
different Act. A work held under its plain name becomes version N of
itself first (`python -m corpus.review.adopt_version <slug> --dry-run`
shows what that moves). A provision renumbered between versions is, to
a comparison by number, a repeal and an insertion; the reviewer can
record it as carried from its old number, which joins the two.

On the site, a provision that has read more than one way gets a
**History** chip. It opens every wording it has had, oldest to newest,
each saying which versions carried it and what the next version's
margin notes say changed it, with any wording comparable to the current
one or its neighbours: removed words struck through in red, added words
in green. A section repealed outright keeps its address and its place in
the contents, greyed, and its page is its history.

Where the versions were read by different parsers, their raw text
differs where the Act does not, so a history is shown only once a human
has checked every wording in it. Re-parsing the older versions fixes
that and shrinks their review queues to what Parliament changed.

## Reviewing against the page

`review.py` shows the PDF with a box drawn over every provision the
parser found, and those boxes are the controls. It opens on the page.

Each box carries a tick and a flag in the margin beside it, so a
provision is accepted or flagged from the page itself; the box turns
green or amber as you go, and **Accept page** decides everything still
outstanding on the page in one go. A page of a printed Act routinely
carries the tail of one Section, the whole of the next and the head of a
third, and having read all of it there is no reason to decide it in
three.

Right-click a box to go to it, to edit or move or delete it, or to
redraw it -- and once it is drawn where the provision actually is,
**Read this piece from its box** takes the provision's words from under
it. So a provision the parser split in the wrong place is corrected by
pointing at the right words rather than by retyping them.

    python -m corpus.review.review <act> --read-only

serves the whole tool without letting anything change it -- for reading
the corpus, or for driving the interface, without writing a decision
nobody made.

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
published flag, set from the dashboard and stored alongside the review
work, so it travels with a push. Nothing reaches the site by having been
parsed.

**A provision is a page, and reading on is a scroll.** Every provision
has its own address serving its own complete page -- which is what makes
the site worth indexing, and what a citation points at. Reach the end of
one and the next arrives under it, to the end of the Division, with the
address bar following as each one passes. A Division because that is the
unit an Act is written in and its end is the author's own; past one,
reading on is a link rather than a scroll.

Nothing about what is served changes: the text, the breadcrumb and the
heading are all in what the server sent, reading on only ever adds what
was already a click away through the "next" link it follows, and each
page names itself as the copy at its own address. So a page read to the
end and a page fetched by a crawler are the same document. It works the
same on the static archive, which has no server to ask.

**The review work travels as text, and the database is derived from it.**
`data/review/<act>/<table>.jsonl` is one file per act per table and one
line per row -- so a diff names the provisions a commit changed, and two
machines that reviewed different provisions merge. The database was
committed as a single 1.7 MB binary until this: twenty-two commits
carrying 10.8 MB of blobs for it, no readable diff, and -- because git
cannot merge a binary -- a pull that had to be fast-forward only and
refused on *any* divergence, so reviewing on the server while a code
change landed elsewhere was enough to jam the sync. One provision edited
now moves 2 lines and 2 KB where it used to move 1.7 MB.

The database stays, because it is what three processes write to and two
of them at once -- `review.py` while you review, `run_ai_review.py`
scanning in the background, both writing the same table. That is what
sqlite's write-ahead log is for and what two processes appending to a
JSONL file would interleave into nonsense. One file had been doing two
unrelated jobs: the working store, and the format the work ships in. It
is good at the first and cannot do the second at all.

A fresh clone therefore has the text and no database:

    python3 -m corpus.review.review_sync import

A `git pull` from a terminal is only half of a pull: it brings the text
and leaves the database behind it, so follow it with the import above.
The dashboard's Pull button does both. If the two ever get out of step
the export refuses rather than writing the older one over the newer --
which it learned the hard way, having once deleted 193 verified
provisions that way.

After that the export runs by itself, before every question the dashboard
asks about what is pending -- which is the part that had to be right,
because with the database gitignored a day's reviewing produces no
pending change until an export runs, and "Everything is pushed" over
unpushed work would be a quieter failure than the merge refusals it
replaced. `python3 -m corpus.review.review_sync check` exports, imports into a
scratch database and compares every row, on demand.

**Defined terms are hyperlinked back to where they are defined**, and
which words those are is a guess. `corpus/definitions.py` reads drafting
convention -- "*term* means...", "has the same meaning as in section N",
inside a Section headed Definitions or Interpretation -- and says so in
its own docstring: a navigation aid, not a guarantee. On the Family
Violence Protection Act it finds 130 terms and misses "safety notice",
which that Act uses throughout and defines in a way the patterns do not
recognise.

Neither the misses nor the false positives are fixable in general, and
both are obvious to somebody reading the Act. So **Defined terms** on
each dashboard card lists what the matcher found, what it links to, and
what a person has decided instead -- remove a word it should not be
linking, add one it missed by naming the Section that defines it. The
decisions are stored per document in `definition_overrides` and travel
with the review work like everything else.

An addition naming a Section the document does not have is dropped when
the page is built rather than linked to something else, which is the rule
the matcher already follows for its own pointers: a missing hyperlink is
a missing hyperlink, and a term linked to the wrong provision tells a
reader something untrue about the law.

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
