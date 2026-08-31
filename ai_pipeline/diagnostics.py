"""
Turns the parser's own bookkeeping into a report you can act on, instead of
either trusting it blindly or re-reading a 900-page tree by hand. Three
kinds of check:

1. Completeness -- did every input line make it into the output at all.
   This is checkable exactly (lines_total vs lines_consumed) because the
   rule parser tracks it as it goes; nothing here is inferred after the
   fact.
2. Structural anomalies -- things that are *possible* but worth a human
   glance: duplicate numbers under the same parent, empty leaf nodes, a
   non-trivial synthetic preamble bucket.
3. History-linking confidence -- notes attached on a guess (fell back to a
   broader node because the one they actually cited couldn't be found),
   and notes that couldn't be linked at all.

Nothing here is silently dropped or silently "fixed" -- every finding names
the exact node/page so review.py can jump straight to it.
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


_LEAF_TYPES = {"section", "clause", "subsection", "paragraph", "subparagraph", "note", "definition"}


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
            report.findings.append(Finding(
                "info", "empty-node",
                f"{node['type']} {node['number']!r} has no body text (page {node['page_start']}) -- "
                "either a genuinely heading-only provision, or the next line's classification swallowed its text.",
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
        report.findings.append(Finding(
            "warning", "history-unattached",
            f"Note {note['raw'][:80]!r} (page {note['page']}) could not be linked to any node.",
            page=note["page"],
        ))

    return report
