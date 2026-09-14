"""
A whole-document AI audit pass -- the local model reads every unit of an
already-parsed Act (a Section/Clause and everything nested under it, the
same grouping review.py works through -- see hierarchy.group_into_units)
and says whether its classification looks right, not just the pieces
diagnostics.py already flagged. Run offline via run_ai_review.py, since
scanning a whole Act is genuinely slow with a local model -- see that
script's own docstring for why, and for the batching and resume support
that keeps it practical.

This is still the same kind of question corpus/ai/assist.py asks,
just asked of everything instead of only what's already flagged: does
this piece's type/number/heading look right given its own text, judged
from the text alone, never from font or position data (that question is
already settled by the time anything reaches here -- see ai_assist.py's
own docstring for why re-litigating it with a model would repeat the
old model-backed parser's mistake). The answer is never applied to the
parse automatically. It becomes a diagnostics finding instead (see
run_ai_review.py and review.py's own startup), so it gates blind-review
and stays hidden until a reviewer has formed their own independent view,
exactly like every other finding does -- an AI scan's opinion earns no
less scrutiny than the deterministic checks, and no more either.

Batched several units per model call (see BATCH_SIZE): one call per
unit would mean thousands of round trips for a real Act, most of them
spent on units nobody needed asking about. A local model can weigh
several short, independent judgements in one reply just as well, so
batching is close to free accuracy-wise and is what makes "scan
everything" practical rather than a multi-day job.
"""
from .backend import OllamaBackend

# Units per model call. Small enough that a single reply stays easy for
# the model to get right (a long list of independent judgements is where
# a small model starts dropping or conflating entries), large enough
# that scanning a ~700-unit Act like the Crimes Act takes dozens of
# calls rather than hundreds. Tune via run_ai_review.py's --batch-size.
BATCH_SIZE = 10

# A unit's own text is truncated to this many characters in the prompt --
# long enough to judge a genuine misclassification from (the shape of
# the opening sentences is almost always enough), short enough that a
# handful of very long Sections don't blow out a batch's own prompt size
# well past what the rest of the batch needed.
MAX_UNIT_CHARS = 1200

_SYSTEM_PROMPT = (
    "You are auditing the output of a legislation parser for Victorian Acts, Bills and Explanatory "
    "Memoranda. Each numbered item below is one already-parsed unit -- a provision's own type, number, "
    "heading and text, exactly as the parser produced it. Your only job is to say whether each one's "
    "classification looks right, judged from its own text. Look for things like: a heading that reads "
    "as a sentence rather than a caption (the parser may have mis-split a paragraph), a number that "
    "looks wrong for its type or contradicts its own text, a body that looks truncated or that runs on "
    "past where it should have ended, or a type that doesn't match what the text itself reads as. Do "
    "not comment on the substance of the law, drafting style, or anything that is just short or plain "
    "-- a short or heading-only unit is normal and not a concern on its own. Only report a unit whose "
    "*classification* looks likely to be a parsing mistake. Most units should have nothing wrong with "
    "them; only include a unit in your reply if you actually see something worth a human looking at. "
    "You are not the parser and your judgement is never applied automatically -- a human reads every "
    "report and decides."
)

_BATCH_SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "concerns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "concern": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "warning"]},
                },
                "required": ["index", "concern", "severity"],
            },
        },
    },
    "required": ["concerns"],
}


def unit_root(unit: list[int]) -> int:
    """The node index a unit's own judgement attaches to -- see
    ai_scan_findings' own table comment on why a scan judges a unit as a
    whole rather than each of its nested nodes separately."""
    return unit[0]


def _unit_text(nodes: list[dict], unit: list[int]) -> str:
    """type/number/heading plus the joined text of every node in the
    unit, truncated -- the same "whole provision" a reviewer sees as one
    piece, not just its own root node's frequently-empty lead-in (see
    corpus/ai/assist.py's own note on why a Section's own "text"
    field alone is often nearly nothing)."""
    root = nodes[unit[0]]
    parts = [nodes[i].get("text") or "" for i in unit]
    body = " ".join(p for p in parts if p).strip()
    if len(body) > MAX_UNIT_CHARS:
        body = body[:MAX_UNIT_CHARS].rsplit(" ", 1)[0] + "..."
    return f"type={root['type']!r} number={root.get('number')!r} heading={root.get('heading')!r} text={body!r}"


def _format_batch(nodes: list[dict], units: list[list[int]]) -> str:
    return "\n\n".join(f"{i + 1}. {_unit_text(nodes, unit)}" for i, unit in enumerate(units))


def scan_batch(nodes: list[dict], units: list[list[int]], backend: "OllamaBackend | None" = None) -> dict[int, dict]:
    """Asks the model about one batch of units (up to BATCH_SIZE of
    them), returning {batch-local index -> {"severity", "concern"}} for
    only the ones it flagged -- a unit missing from the result is the
    model's way of saying "nothing wrong here", not a unit it forgot to
    answer (see _SYSTEM_PROMPT: most units are expected to come back
    clean). Raises llm_backend.OllamaUnavailable if the backend isn't
    ready, same as ai_assist.build_suggestion.

    `backend` is injectable so this is testable without a running Ollama
    server -- see tests/test_ai_scan.py."""
    backend = backend or OllamaBackend()
    prompt = _format_batch(nodes, units)
    result = backend.ask(_SYSTEM_PROMPT, prompt, _BATCH_SCAN_SCHEMA)
    flagged: dict[int, dict] = {}
    for concern in result.get("concerns", []):
        idx = concern.get("index")
        # 1-based in the prompt (see _format_batch), and clamped to this
        # batch's own range -- a model naming an index outside the batch
        # it was actually given is a malformed reply, not a real finding
        # about some other unit it was never shown.
        if isinstance(idx, int) and 1 <= idx <= len(units):
            flagged[idx - 1] = {
                "severity": concern.get("severity") or "info",
                "concern": concern.get("concern") or "",
            }
    return flagged


def iter_unit_batches(units: list[list[int]], batch_size: int = BATCH_SIZE):
    for i in range(0, len(units), batch_size):
        yield i, units[i : i + batch_size]
