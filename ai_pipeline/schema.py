"""JSON schema for the AI's structural parse of a chunk of Act text."""

NODE_TYPES = [
    "part",
    "division",
    "subdivision",
    "heading_group",
    "section",
    "clause",
    "subsection",
    "paragraph",
    "subparagraph",
    "definition",
    "note",
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
