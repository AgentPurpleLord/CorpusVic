"""
Corpus: Victorian legislation, from a printed PDF to something a machine
can read, without asking a model what the law says.

Nearly all of this is deterministic. The parser works from the page's own
geometry and typography -- where a line sits, whether it is bold, how far
it is indented -- checked against patterns written down in a profile for
the Act in hand, and given the same PDF it gives the same answer every
time. Every line of input lands in exactly one output node, and that is
checked rather than hoped for. See rule_parser's module docstring for why
that matters more here than raw accuracy would.

The parts that do use a model live in `corpus.ai`, and they are all
optional and all advisory: a second opinion on a piece the diagnostics
already flagged, and a whole-document audit run offline. Nothing there
decides anything.

Roughly in the order a document moves through:

    extract      the PDF's pages, as lines that know where they are
    toc          where the front matter ends and the Act begins
    profiles     the patterns for one Act, and the hierarchy it uses
    rule_parser  lines -> a flat list of nodes, each knowing its own box
    tables       the ones whose meaning is in their geometry
    hierarchy    how those nodes group into levels and review units
    tree         each node's path through the levels above it
    endnotes,
    history_notes,
    amendments   the Act's own record of how it got this way
    definitions,
    link_targets,
    link_annotations,
    commentary,
    bill_linking a provision's ties to the rest of the corpus
    diagnostics  what a human should look at, and why
    db           every decision a human made about any of it
    structure    what they said the parser got structurally wrong
    reparse      keeping all of that attached when the parse changes
    versions,
    diffing      the same provision across reprints of an Act
    html_view,
    markdown_export,
    akn_export   the reading view, and the two exports

This package was called `ai_pipeline`, which said the opposite of what is
true of almost all of it.
"""
from pathlib import Path

# Where the project's own files live: acts/, data/, static/, deploy/.
#
# Derived from this package rather than from each module's own location,
# because a module's depth inside corpus/ changes when it is moved and
# the project root does not. Anything reaching for a project file asks
# here instead of counting `.parent`s.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
