"""The structural node types a parsed document may use."""

# The structural container types the parser emits, plus the non-hierarchy
# node types (heading_group / definition / note / penalty / repealed /
# example).
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
    # Text that resumes a provision's own sentence after its lettered
    # list has finished -- "(a) does X; or (b) does Y -- is guilty of an
    # offence." It belongs after the list, not before it, which is the
    # whole reason it needs a node of its own (see rule_parser's
    # _consume_as_continuation).
    "continuation",
    "note",
    "example",
    # The penalty for an offence, which Victorian drafting sets on its
    # own line under the provision creating it ("Penalty: Level 3
    # imprisonment (20 years maximum)."). Its own type because it is its
    # own thing: what the provision forbids and what happens to you if
    # you do it are separate facts about an offence, and running them
    # together as one block of text makes the second unfindable.
    "penalty",
    "repealed",
    # No longer emitted -- an EM's entries are typed "clause" now (see
    # em_parser.py's own docstring). Kept so an EM parsed before that
    # change still offers its own type back in review.py's relabel
    # dropdown rather than showing a type the list doesn't know.
    "em_entry",
]


# Which of the above types a given kind of document actually uses, for
# the relabel dropdown and the "Legislation part types" window in
# review.py. Offering every reviewer the same full list used to mean an
# Act's reviewer could pick "clause" (which an Act never has -- that's a
# Bill's word for the same thing, see hierarchy.make_ranks), and a
# Bill's reviewer could pick "section". Same list as above, just
# filtered to what the document in hand can actually contain.
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
    "act": [*_STRUCTURE, "section", *_SUBLEVELS, "definition", "continuation", "note", "example",
            "penalty", "repealed"],
    # A Bill numbers its top-level provisions clauses until it is enacted.
    # Nothing in it is repealed yet.
    "bill": [*_STRUCTURE, "clause", *_SUBLEVELS, "definition", "continuation", "note", "example",
             "penalty"],
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
