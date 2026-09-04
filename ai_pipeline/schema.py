"""The structural node types a parsed document may use."""

# The structural container types the parser emits, plus the non-hierarchy
# node types (heading_group / definition / note / repealed / example).
# "chapter" and "schedule" are the optional top levels some Acts use above
# Part, and use respectively (see hierarchy.py). An Act that defines
# further levels in its profile adds them on top of this list when it's
# parsed, and a reviewer can add labels of their own for one Act (see
# review.py's node-type endpoints) -- this is the baseline every document
# starts from.
NODE_TYPES = [
    "schedule",
    "chapter",
    "part",
    "division",
    "subdivision",
    "heading_group",
    "section",
    "clause",
    "subsection",
    "paragraph",
    "subparagraph",
    "sub_subparagraph",
    "definition",
    "note",
    "example",
    "repealed",
    # No longer emitted -- an EM's entries are typed "clause" now (see
    # em_parser.py's own docstring). Kept so an EM parsed before that
    # change still offers its own type back in review.py's relabel
    # dropdown rather than showing a type the list doesn't know.
    "em_entry",
]


# Which of the above a given kind of document actually uses, for the
# relabel dropdown and the "Legislation part types" window in review.py.
# One flat enum offered every reviewer every type: an Act was asked
# whether a provision might be a "clause" (it never is -- that is a
# Bill's word for the same thing, see hierarchy.make_ranks), and a Bill
# was offered "section". Same list, filtered to what the document in
# hand can contain.
#
# A type this pipeline no longer emits stays available to the documents
# that already carry it ("em_entry" -- see em_parser.py), and review.py
# adds back any type actually in use, so filtering can never leave a
# node's own type missing from the list it is relabelled with.
_STRUCTURE = ["schedule", "chapter", "part", "division", "subdivision", "heading_group"]
_SUBLEVELS = ["subsection", "paragraph", "subparagraph", "sub_subparagraph"]

TYPES_BY_DOCUMENT = {
    # A consolidated Act: sections, and the "* * * *" markers standing in
    # for provisions since repealed.
    "act": [*_STRUCTURE, "section", *_SUBLEVELS, "definition", "note", "example", "repealed"],
    # A Bill numbers its top-level provisions clauses until it is enacted.
    # Nothing in it is repealed yet.
    "bill": [*_STRUCTURE, "clause", *_SUBLEVELS, "definition", "note", "example"],
    # An Explanatory Memorandum is a flat sequence of notes on the Bill's
    # own clauses, under organisational headings, with bulleted lists
    # inside an entry parsed as paragraphs (see em_parser.py).
    "em": [*_STRUCTURE, "clause", "paragraph", "subparagraph", "sub_subparagraph", "note", "example", "em_entry"],
}


def types_for_document(document_type: str | None) -> list[str]:
    """The type list a document of this kind offers. An unrecognised (or
    absent) kind gets the full enum rather than a guess -- a parse from
    before document_type was recorded should lose nothing."""
    return list(TYPES_BY_DOCUMENT.get(document_type or "", NODE_TYPES))
