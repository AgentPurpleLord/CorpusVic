"""What a parse found, as data, for the dashboard to show.

The pipelines' printed log is written for a terminal, and the dashboard
used to show it as one: a reviewer had to read prose to learn how many
provisions came out or whether anything went wrong. The pipeline now
ends its log with one marked line of JSON as well, and the dashboard
draws that (static/dashboard.html, renderParseResult). Riding on stdout
rather than a file means a summary can never be a previous run's.
"""
import json
from collections import Counter

from corpus.domain.hierarchy import HIERARCHY_ORDER

MARKER = "PARSE-SUMMARY "

# How many of a list (parser warnings, changed wordings) come with it:
# enough to act on, not the log over again.
_SAMPLE = 8


def node_counts(nodes: list[dict], hierarchy: "list[str] | None" = None) -> dict:
    """Counts of what the parse is made of. "provisions" are pages, as
    the site and the dashboard's progress count them: a Section, a
    Schedule's clause or item, a one-page Schedule, a Preamble."""
    from corpus.exporters.akn_export import build_hierarchy_tree
    from corpus.exporters.markdown_export import _structural_types, collect_sections

    order = hierarchy or HIERARCHY_ORDER
    roots, _collisions = build_hierarchy_tree([dict(n) for n in nodes], order)
    by_type = Counter(n.get("type") for n in nodes)
    return {
        "nodes": len(nodes),
        "provisions": len(collect_sections(roots, _structural_types(order))),
        "by_type": dict(by_type),
        "history_notes": sum(len(n.get("history") or []) for n in nodes),
    }


def sample(items: list) -> dict:
    return {"count": len(items), "items": [str(i) for i in items[:_SAMPLE]]}


def remap_summary(report: "dict | None") -> "dict | None":
    if report is None:
        return None
    keys = ("matched", "moved", "text_changed", "kept", "refreshed", "orphaned")
    out = {k: report.get(k) or 0 for k in keys}
    out["changed"] = sample(report.get("changed") or [])
    out["orphans"] = sample(report.get("orphans") or [])
    if report.get("source"):
        out["source"] = report["source"]
    return out


def emit(summary: dict) -> None:
    print(MARKER + json.dumps(summary, default=str), flush=True)


def split_log(log: str) -> "tuple[str, dict | None]":
    """(the log without the summary line, the summary or None)."""
    summary, kept = None, []
    for line in (log or "").splitlines():
        if line.startswith(MARKER):
            try:
                summary = json.loads(line[len(MARKER):])
            except ValueError:
                kept.append(line)
        else:
            kept.append(line)
    return "\n".join(kept), summary
