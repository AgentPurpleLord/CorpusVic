"""
An optional second opinion from a local model on a piece diagnostics
has already flagged as uncertain (see review.py's _is_elevated_risk) --
never a replacement for a human's own judgement, and never offered
until *after* a reviewer has already recorded their own independent
blind-review guess (see db.save_blind_review). That ordering matters:
the whole point of the blind-review step is a reviewer forming their
own view from the text alone before anything else's opinion can anchor
them, and showing a model's guess first would just be a different
flavour of the exact thing that step exists to prevent. Once a human's
own guess is already locked in, an AI suggestion is a third data point
to weigh alongside it and the parser's own answer -- not a fourth vote
that outweighs the other three.

This only ever reasons over text the rules engine already extracted --
the flagged node's own words, its immediate siblings, and (for a
history-linking finding) the margin note's own raw citation -- never
raw font or position data. That is a deliberate, narrower job than the
rules engine's own: geometry is what tells a heading from a sentence
in the first place, and this only ever runs *after* that question has
already been settled. What's actually uncertain at this point -- which
of several candidate subsections a citation meant, which of two
identically-numbered nodes is the mistake -- is a plain reading
question over already-clean text, exactly what a language model is
suited to. Feeding it raw geometry instead would just repeat the old
model-backed parser's own mistake in a new place; feeding it a scanned
image (OCR) would only throw signal away, since these are born-digital
PDFs with clean text already sitting in data/parsed/<act>.json.

Every answer is a plain, bounded classification -- see _SUGGESTION_SCHEMA
-- never free-form advice or a rewrite of the text, and every result is
stored (see db.save_ai_suggestion) with the exact model that produced
it, so a reviewer reading it back later knows what asked the question,
not just what it said.
"""
from ..hierarchy import UNIT_BOUNDARY_TYPES
from .backend import OllamaBackend

_SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["answer", "reasoning", "confidence"],
}

_SYSTEM_PROMPT = (
    "You are assisting a human reviewer of Victorian legislation who is checking a piece of "
    "automatically parsed text that a diagnostics check has flagged as uncertain. You are not "
    "the parser, and your answer is never applied automatically -- a human reads it and decides "
    "what, if anything, to do. Answer only from the text given to you; do not invent section "
    "numbers, Act names or facts that aren't in it. If you aren't confident, say so honestly in "
    "'confidence' rather than guessing."
)

# Categories with a node_index attached that this can actually say
# something useful about -- see diagnostics.py's own run_diagnostics.
# "empty-node" is info-level and never gates a reviewer in the first
# place (see review.py's _is_elevated_risk), so it's never asked about
# here either; a generic fallback question still covers it if a caller
# passes one anyway.
SUPPORTED_CATEGORIES = {"duplicate-number", "history-low-confidence"}


def _norm_number(s: "str | None") -> str:
    return (s or "").strip("() ").lower()


def _duplicate_number_context(node_index: int, nodes: list[dict]) -> tuple[list[str], str]:
    node = nodes[node_index]
    lines = [f"Flagged {node['type']} {node.get('number')!r}: {node.get('text') or '(no body text)'}"]
    for i, other in enumerate(nodes):
        if i == node_index or other["type"] != node["type"]:
            continue
        if _norm_number(other.get("number")) == _norm_number(node.get("number")):
            lines.append(
                f"\nAnother {other['type']} also numbered {other.get('number')!r} "
                f"(page {other.get('page_start')}): {other.get('text') or '(no body text)'}"
            )
    question = (
        "These two provisions share a number, which is usually a parsing mistake rather than "
        "something the Act actually did. From their text alone, which one (if either) looks like "
        "the one that was probably mis-numbered or mis-split, and why? If both look like genuinely "
        "separate, correctly-numbered provisions, say so."
    )
    return lines, question


def _history_low_confidence_context(node_index: int, nodes: list[dict]) -> tuple[list[str], str]:
    node = nodes[node_index]
    lines = [f"Flagged {node['type']} {node.get('number')!r}: {node.get('text') or '(no body text)'}"]
    for h in node.get("history") or []:
        if h.get("confidence") == "low":
            lines.append(f"\nMargin note attached here as the closest match, though it names something more specific: {h.get('raw')}")
    # Whatever's already nested under this node in the flat node list --
    # the candidates the note's own citation might actually have meant,
    # since the parser attached it to this broader node only because the
    # specific one it named couldn't be matched (see tree.py's
    # attach_history).
    i = node_index + 1
    nested = []
    while i < len(nodes) and nodes[i]["type"] not in UNIT_BOUNDARY_TYPES:
        child = nodes[i]
        if child.get("number"):
            nested.append(f"  - {child['type']} {child['number']!r}: {child.get('text') or '(no body text)'}")
        i += 1
    if nested:
        lines.append("\nProvisions already nested under it:")
        lines.extend(nested)
    question = (
        "The margin note above cites a more specific provision than the parser could find, so it "
        "was attached here as the closest match instead. Looking at the note's own citation and "
        "the provisions nested under this one, which (if any) does the note most likely actually "
        "belong to? If none of them fit, say so rather than guessing."
    )
    return lines, question


_CONTEXT_BUILDERS = {
    "duplicate-number": _duplicate_number_context,
    "history-low-confidence": _history_low_confidence_context,
}


def build_suggestion(finding: dict, node_index: int, nodes: list[dict], backend: "OllamaBackend | None" = None) -> dict:
    """Asks the local model one bounded question about one flagged node,
    returning {"answer", "reasoning", "confidence", "model"}. Raises
    llm_backend.OllamaUnavailable if the backend isn't ready -- the
    caller (review.py) turns that into a 503 with the message intact,
    since it already names the exact next command to run.

    `backend` is injectable so this stays testable without a running
    Ollama server (see tests/test_ai_assist.py) -- the default
    constructs a real OllamaBackend, same as every other caller gets."""
    backend = backend or OllamaBackend()
    category = finding.get("category")
    builder = _CONTEXT_BUILDERS.get(category)
    if builder is not None:
        context_lines, question = builder(node_index, nodes)
    else:
        node = nodes[node_index]
        context_lines = [f"Flagged {node['type']} {node.get('number')!r}: {node.get('text') or '(no body text)'}"]
        question = "From the text alone, what (if anything) looks wrong here?"
    user_prompt = "\n".join([f"Diagnostic finding: {finding.get('message')}", "", *context_lines, "", f"Question: {question}"])
    result = backend.ask(_SYSTEM_PROMPT, user_prompt, _SUGGESTION_SCHEMA)
    return {
        "answer": result.get("answer", ""),
        "reasoning": result.get("reasoning", ""),
        "confidence": result.get("confidence", "low"),
        "model": backend.model,
    }
