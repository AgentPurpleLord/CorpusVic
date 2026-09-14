"""
Turns the parser's own bookkeeping into a report a human can act on,
instead of either trusting the parse blindly or reading a 900-page tree
by hand. Three kinds of check:

1. Completeness -- did every line of the input actually make it into the
   output. This can be checked exactly (lines_total vs lines_consumed)
   because the rule parser counts as it goes; nothing here is a guess.
2. Structural oddities -- things that are allowed but worth a human
   glance: duplicate numbers under the same parent, empty leaf nodes, a
   large chunk of text filed under the synthetic preamble bucket.
3. How confident the amendment-history linking is -- a note attached to
   the wrong provision because the one it actually names couldn't be
   found, or a note that couldn't be attached at all.

Nothing here is silently dropped or silently fixed -- every finding names
the exact node and page so review.py can jump straight to it.
"""
from dataclasses import dataclass, field

from .hierarchy import heading_levels
from .rule_parser import ParseResult


@dataclass
class Finding:
    severity: str  # "error" | "warning" | "info"
    category: str
    message: str
    page: int | None = None
    node_index: int | None = None  # index into the nodes list, when the finding is about one specific node


@dataclass
class DiagnosticsReport:
    findings: list[Finding] = field(default_factory=list)
    lines_total: int = 0
    lines_consumed: int = 0

    @property
    def complete(self) -> bool:
        return self.lines_total == self.lines_consumed

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def to_dicts(self) -> list[dict]:
        return [
            {"severity": f.severity, "category": f.category, "message": f.message, "page": f.page, "node_index": f.node_index}
            for f in self.findings
        ]


# "example" isn't exempted from the empty-body check the way "note" is --
# unlike a note (which can legitimately be an empty placeholder), an
# Example callout with nothing under it is always a parsing mistake, not
# a valid drafting shape. Nor is "penalty", for a stronger reason still:
# its own opening line *is* its text ("Penalty: Level 3 imprisonment..."),
# so an empty one cannot have come from a real penalty line at all.
_LEAF_TYPES = {
    "section", "clause", "subsection", "paragraph", "subparagraph", "sub_subparagraph",
    "note", "definition", "repealed", "example", "penalty", "table",
}


def run_diagnostics(parse_result: ParseResult, nodes: list[dict], unattached_notes: list[dict]) -> DiagnosticsReport:
    report = DiagnosticsReport(lines_total=parse_result.lines_total, lines_consumed=parse_result.lines_consumed)

    if not report.complete:
        report.findings.append(Finding(
            "error", "completeness",
            f"{parse_result.lines_total - parse_result.lines_consumed} line(s) were never consumed by the parser "
            "-- this means the algorithm's own invariant broke, not just a mis-classification. Treat the output "
            "as unreliable until this is fixed.",
        ))
    for w in parse_result.warnings:
        report.findings.append(Finding("warning", "preamble", w))

    preamble = next((n for n in nodes if n["type"] == "part" and n.get("number") is None), None)
    if preamble is not None and len(preamble["text"]) > 200:
        report.findings.append(Finding(
            "warning", "preamble",
            f"{len(preamble['text'])} characters of text were filed under the synthetic preamble node -- "
            "this usually means real content appeared before the parser recognised any Part/Division/Section. "
            "Check --start-page.",
            page=preamble.get("page_start"),
        ))

    container_types = heading_levels(parse_result.hierarchy)  # chapter/part/division/subdivision/section
    seen: dict[tuple, dict] = {}
    for idx, node in enumerate(nodes):
        if node["type"] not in container_types:
            continue
        key = (node["type"], tuple(sorted((k, v) for k, v in node["path"].items() if k != node["type"])), node["number"])
        if key in seen:
            report.findings.append(Finding(
                "warning", "duplicate-number",
                f"{node['type']} {node['number']!r} appears more than once under the same parent "
                f"(pages {seen[key]['page_start']} and {node['page_start']}) -- likely a mis-detected boundary.",
                page=node["page_start"], node_index=idx,
            ))
        else:
            seen[key] = node

    for idx, node in enumerate(nodes):
        if node["type"] in _LEAF_TYPES and not node["text"] and node["type"] != "note":
            # Deliberately short. It is read beside the piece it is about,
            # which already shows the type, the number and the page, so
            # repeating them there filled a line with what was on screen
            # anyway; the Finding still carries both as fields for any
            # reader that has no piece in front of it.
            report.findings.append(Finding(
                "info", "empty-node",
                "no body text, either a heading-only, or the next line's classification swallowed its text.",
                page=node["page_start"], node_index=idx,
            ))

    for idx, node in enumerate(nodes):
        for h in node.get("history", []):
            if h.get("confidence") == "low":
                report.findings.append(Finding(
                    "warning", "history-low-confidence",
                    f"Note {h['raw'][:80]!r} cited a more specific provision than was found; "
                    f"attached to {node['type']} {node['number']!r} (page {node['page_start']}) as the closest match.",
                    page=node["page_start"], node_index=idx,
                ))

    for note in unattached_notes:
        if note.get("kind") == "provenance":
            # Records where the provision came from ("No. 6103 s. 15.",
            # "cf. [1819] 60 George III ...") rather than how it has been
            # amended -- it names no provision of this Act, so there is
            # nothing for it to link to and nothing here went wrong.
            continue
        report.findings.append(Finding(
            "warning", "history-unattached",
            f"Note {note['raw'][:80]!r} (page {note['page']}) could not be linked to any node.",
            page=note["page"],
        ))

    return report
