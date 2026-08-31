"""
Local web GUI for tagging spans of an Act's parsed text that should become
links later -- references to other Acts, defined terms, Bills,
Explanatory Memoranda. Complements review.py rather than replacing it:
this tool never edits node structure or text, only records spans (see
ai_pipeline/link_annotations.py) layered on top of whatever
data/ai_parsed/<act>.json already holds. Run review.py's own structural
pass on a Section first so the text being highlighted here is settled --
this tool has no way to fix a wrongly-split node, only to tag spans
within whatever nodes already exist.

Usage:
    python link_review.py crimes-act
    python link_review.py crimes-act --port 8001

Then open the printed URL (http://127.0.0.1:8000/ by default) in a
browser: drag-select a span of text and a small menu offers the labels in
ai_pipeline.link_annotations.LABELS; click an already-highlighted span to
remove its label. Every unit (a Section plus its own nested pieces, same
grouping as review.py's default mode -- see group_into_units) is browsable
via the Prev/Next controls; there's no accept/verify step here, spans are
saved to data/links/<act>.json the moment they're labelled.

Labelling a span also attempts to *resolve* it immediately (see
ai_pipeline/link_targets.py): an act_citation is matched against
ai_pipeline/known_acts.yaml, a defined_term against this Act's own
definitions. A resolved target is shown in the highlight's tooltip;
bill_reference/em_reference (no corpus exists for either yet) and any
unmatched citation/term stay unresolved -- still saved, just without a
target until the text is fixed or that corpus exists.
"""
import argparse
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ai_pipeline.link_annotations import LABELS, LinkError, add_link, delete_link, load_links
from ai_pipeline.link_targets import resolve_link, build_definition_index
from review import compute_unit_labels, group_into_units, load_parsed

app = FastAPI(title="Legislation link review")
STATIC_DIR = Path(__file__).parent / "static"

# Set once at startup by main() -- this tool serves exactly one Act per
# running process (see the module docstring's usage), so there's no
# per-request act parameter to plumb through. _definition_index is built
# once from this Act's own nodes (see link_targets.build_definition_index)
# rather than recomputed on every POST /api/links.
_act: str | None = None
_nodes: list[dict] = []
_units: list[list[int]] = []
_definition_index: dict[str, int] = {}


class LinkRequest(BaseModel):
    node_index: int
    start: int
    end: int
    label: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "link_review.html")


@app.get("/api/meta")
def get_meta():
    return {"act": _act, "labels": LABELS, "unit_count": len(_units)}


@app.get("/api/units/{unit_no}")
def get_unit(unit_no: int):
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    indices = _units[unit_no]
    unit_nodes = [_nodes[i] for i in indices]
    labels = compute_unit_labels(unit_nodes)
    return {
        "unit_no": unit_no,
        "unit_count": len(_units),
        "pieces": [
            {
                "node_index": idx,
                "label": lbl,
                "type": n["type"],
                "number": n.get("number"),
                "heading": n.get("heading"),
                "text": n.get("text") or "",
            }
            for idx, n, lbl in zip(indices, unit_nodes, labels)
        ],
    }


@app.get("/api/links")
def get_links():
    return load_links(_act)


@app.post("/api/links")
def post_link(req: LinkRequest):
    if not (0 <= req.node_index < len(_nodes)):
        raise HTTPException(404, "No such node")
    node_text = _nodes[req.node_index].get("text") or ""
    span_text = node_text[req.start : req.end]
    target = resolve_link(req.label, span_text, _nodes, _definition_index)
    try:
        return add_link(_act, req.node_index, req.start, req.end, req.label, node_text, target=target)
    except LinkError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/links/{link_id}")
def remove_link(link_id: str):
    if not delete_link(_act, link_id):
        raise HTTPException(404, "No such link")
    return {"ok": True}


def main():
    global _act, _nodes, _units, _definition_index
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    _act = args.act
    _nodes, _ = load_parsed(args.act)
    _units = group_into_units(_nodes)
    _definition_index = build_definition_index(_nodes)

    import uvicorn

    print(f"Serving {args.act}: {len(_nodes)} nodes, {len(_units)} units.")
    print(f"Open http://127.0.0.1:{args.port}/ in a browser.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
