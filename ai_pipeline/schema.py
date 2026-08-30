"""JSON schema for the AI's structural parse of a chunk of Act text."""

# The structural container types the AI backend may emit, plus the three
# non-hierarchy node types (heading_group / definition / note). "chapter"
# is the optional top level some Acts use above Part (see hierarchy.py); an
# Act that defines further custom levels in its profile adds them on top of
# this list at review time, not here -- this list is the AI schema's fixed
# enum and review.py's baseline set of relabel choices.
NODE_TYPES = [
    "chapter",
    "part",
    "division",
    "subdivision",
    "heading_group",
    "section",
    "subsection",
    "paragraph",
    "subparagraph",
    "definition",
    "note",
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
