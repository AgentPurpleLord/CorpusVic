"""JSON schema for the AI's structural parse of a chunk of Act text."""

# The structural container types the AI backend may emit, plus the
# non-hierarchy node types (heading_group / definition / note / repealed /
# example). "chapter" and "schedule" are the optional top levels some
# Acts use above Part, and use respectively (see hierarchy.py); an Act
# that defines further custom levels in its profile adds them on top of
# this list at review time, not here -- this list is the AI schema's
# fixed enum and review.py's baseline set of relabel choices.
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
    # em_parser.py's own docstring). Kept in the enum so an EM parsed
    # before that change still validates and still offers its own type
    # back in review.py's relabel dropdown.
    "em_entry",
]

NODE_FIELDS = ("type", "number", "heading", "text")

NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": NODE_TYPES},
                    "number": {"type": ["string", "null"]},
                    "heading": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                    "page_start": {"type": ["integer", "null"]},
                    "page_end": {"type": ["integer", "null"]},
                    "continued_from_previous": {"type": "boolean"},
                    "continues_in_next": {"type": "boolean"},
                },
                "required": [
                    "type",
                    "number",
                    "heading",
                    "text",
                    "page_start",
                    "page_end",
                    "continued_from_previous",
                    "continues_in_next",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["nodes"],
    "additionalProperties": False,
}
