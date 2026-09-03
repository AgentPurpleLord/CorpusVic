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
