"""What a reviewer flagged in review and denied in History review, for one
Act across its versions, as a Markdown file to hand back.

Written to be read without the server: each item carries where it is,
what the reviewer said, what the parser read and what they read, the
printed lines it came from (position, size, weight -- what the parser's
rules decide on), what the recogniser saw where the parse kept it, and
the pieces either side. Enough to find why the parser read it wrong.
"""
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from corpus.domain import diffing
from corpus.history.changes import key_label
from corpus.parsing.identity import annotate_ids
from corpus.parsing.versions import split_document_slug
from corpus.storage import db, parsed as parsed_files

_CONTEXT_LINES = 2


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _name(node: dict) -> str:
    return node.get("_node_id") or node.get("id") or ""


def _excerpt(text: "str | None", limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _quote(text: "str | None") -> str:
    lines = (text or "").strip().splitlines() or [""]
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines)


def _plain(html_text: "str | None") -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", html_text or "")).strip()


class _Document:
    """One version's parse, printed lines and diagnostics, read once."""

    def __init__(self, slug: str, base_dir: Path):
        self.slug = slug
        path = base_dir / "data" / "parsed" / f"{slug}.json"
        try:
            data = parsed_files.load(path)
        except (OSError, ValueError):
            data = {}
        self.nodes = data.get("nodes") or []
        annotate_ids(self.nodes, data.get("hierarchy") or None)
        self.by_name = {_name(n): k for k, n in enumerate(self.nodes) if _name(n)}
        self.owner = {}
        for key, p in diffing.provisions(self.nodes).items():
            for k in range(p["node_index"], diffing.unit_end(self.nodes, p["node_index"])):
                self.owner.setdefault(k, key)
        self.lines = {}
        for page in _load_json(base_dir / "data" / "extracted" / f"{slug}.json", []):
            self.lines[page.get("page_no")] = page.get("body_lines") or []
        self.diagnostics = {}
        for d in _load_json(base_dir / "data" / "diagnostics" / f"{slug}.json", []):
            if isinstance(d, dict) and d.get("node_index") is not None:
                self.diagnostics.setdefault(d["node_index"], []).append(d)

    def printed(self, rects: list, page: "int | None" = None, text: str = "") -> str:
        """The printed lines inside `rects` and a couple either side, or --
        with no boxes -- around the first words of `text` on `page`."""
        out = []
        pages = sorted({r["page"] for r in rects}) if rects else ([page] if page else [])
        for p in pages:
            lines = sorted(self.lines.get(p) or [], key=lambda l: (round(l.get("y0", 0)), l.get("x0", 0)))
            if not lines:
                continue
            if rects:
                boxes = [r for r in rects if r["page"] == p]
                inside = [k for k, l in enumerate(lines)
                          if any(l.get("y1", 0) >= b["y0"] - 1 and l.get("y0", 0) <= b["y1"] + 1 for b in boxes)]
            else:
                start = " ".join((text or "").split()[:3])
                inside = [k for k, l in enumerate(lines) if start and start in " ".join((l.get("text") or "").split())]
            if not inside:
                continue
            lo, hi = max(inside[0] - _CONTEXT_LINES, 0), min(inside[-1] + _CONTEXT_LINES, len(lines) - 1)
            out.append(f"page {p}")
            for k in range(lo, hi + 1):
                l = lines[k]
                mark = "*" if k in inside else " "
                out.append(f"{mark} x0={l.get('x0', 0):6.1f} y0={l.get('y0', 0):6.1f} size={l.get('size', 0):4.1f} "
                           f"{'B' if l.get('bold') else '-'}  {l.get('text', '')}")
        return "\n".join(out)


def _piece_label(doc: _Document, k: "int | None", row: dict) -> str:
    key = doc.owner.get(k) if k is not None else None
    path = row.get("path") or (doc.nodes[k].get("path") if k is not None else None) or {}
    chain = "".join(f"({path[level]})" for level in ("subsection", "paragraph", "subparagraph", "sub_subparagraph")
                    if path.get(level))
    if row.get("type") == "definition":
        chain = f" definition of “{' '.join((row.get('heading') or '').split())}”"
    where = key_label(key) if key else (_name(row) or "?")
    return f"{where}{chain}"


def _flagged(doc: _Document, base_dir: Path) -> list[str]:
    rows = [r for r in db.load_verified(doc.slug, base_dir) if r.get("needs_followup")]
    notes = db.load_review_notes(doc.slug, base_dir)
    out = []
    for row in rows:
        name = row.get("_node_id") or ""
        k = doc.by_name.get(name)
        node = doc.nodes[k] if k is not None else {}
        parts = [f"#### {_piece_label(doc, k, row)} — `{doc.slug}`", ""]
        note = notes.get(name)
        parts.append(f"- **Your note:** {note}" if note else "- **Your note:** (none)")
        pages = sorted({r["page"] for r in node.get("rects") or []}) or [row.get("page_start")]
        parts.append(f"- **Piece:** `{name}` ({row.get('type')}"
                     f"{' ' + str(row.get('number')) if row.get('number') else ''}), "
                     f"page{'s' if len(pages) > 1 else ''} {', '.join(str(p) for p in pages if p)}")
        if node:
            parts.append(f"- **Parser read it as:** {node.get('type')}"
                         f"{' ' + str(node.get('number')) if node.get('number') else ''}"
                         f"{' — heading: ' + repr(node.get('heading')) if node.get('heading') else ''}")
            parts += ["", "Parser's text:", _quote(node.get("text"))]
            if " ".join((row.get("text") or "").split()) != " ".join((node.get("text") or "").split()):
                parts += ["", "Your text:", _quote(row.get("text"))]
        else:
            parts += ["- **Not in the current parse** (re-parsed since?)", "", "Your text:", _quote(row.get("text"))]
        if node.get("seen"):
            parts += ["", f"What the recogniser saw: `{json.dumps(node['seen'], ensure_ascii=False)}`"]
        for d in doc.diagnostics.get(k, []):
            parts.append(f"- Diagnostic ({d.get('severity')}, {d.get('category')}): {d.get('message')}")
        if k is not None:
            around = [(j, doc.nodes[j]) for j in (k - 2, k - 1, k + 1, k + 2) if 0 <= j < len(doc.nodes)]
            parts += ["", "Either side:"]
            parts += [f"- {'before' if j < k else 'after'}: {n.get('type')} {n.get('number') or ''} "
                      f"`{_name(n)}` — {_excerpt(n.get('text') or n.get('heading'))}" for j, n in around]
        printed = doc.printed(node.get("rects") or [], row.get("page_start"), node.get("text") or row.get("text") or "")
        if printed:
            parts += ["", "Printed lines (`*` inside its boxes):", "```", printed, "```"]
        out.append("\n".join(parts))
    return out


def _side(doc: "_Document | None", at: "dict | None", label: str) -> list[str]:
    if at is None:
        return [f"- {label}: no page"]
    parts = [f"- {label}: page {at.get('page')}{' (where it would be: not in this version)' if at.get('absent') else ''}"]
    printed = doc.printed(at.get("rects") or [], at.get("page"), at.get("text") or "") if doc else ""
    if printed:
        parts += ["", "```", printed, "```"]
    return parts


def _denied(items: list[dict], docs: dict) -> list[str]:
    out = []
    for i in items:
        if i.get("decision") != "denied":
            continue
        piece = "" if i.get("piece") == "whole" else "heading" if i.get("piece") == "heading" else i.get("label") or ""
        parts = [f"#### {i['section']} {piece} — v{i['from']} → v{i['to']} ({i.get('op') or 'changed'})", ""]
        parts.append(f"- **Your note:** {i['note']}" if i.get("note") else "- **Your note:** (none)")
        parts.append(f"- **Provision key:** `{i['provision']}`, piece `{i['piece']}`")
        parts += ["", f"Version {i['from']}:", _quote(_plain(i.get("old_html")) or "(not in this version)"),
                  "", f"Version {i['to']}:", _quote(_plain(i.get("new_html")) or "(not in this version)")]
        if i.get("notes"):
            parts += ["", f"Margin notes new in v{i['to']}:"] + [f"- {n}" for n in i["notes"]]
        if i.get("instructions"):
            parts += ["", "Amending Acts' instructions:"]
            parts += [f"- No. {a.get('act')} {a.get('provision')}: {a.get('raw')} — {a.get('status')}"
                      for a in i["instructions"]]
        parts += [""] + _side(docs.get(i["from"]), i.get("old_at"), f"v{i['from']}") \
            + _side(docs.get(i["to"]), i.get("new_at"), f"v{i['to']}")
        out.append("\n".join(parts))
    return out


def build(work: str, slugs: list[str], base_dir, history_items: "list[dict] | None" = None,
          decisions: "dict | None" = None, history_notes: "dict | None" = None,
          code_version: "str | None" = None, base: "int | None" = None) -> str:
    """The report. `history_items` are History review's, each with its
    "decision" and "note"; `decisions` and `history_notes` the stored ones,
    to list a denial whose change the data no longer shows."""
    base_dir = Path(base_dir)
    docs = {slug: _Document(slug, base_dir) for slug in slugs}
    flagged = {slug: _flagged(doc, base_dir) for slug, doc in docs.items()}
    by_version = {split_document_slug(s)[1]: d for s, d in docs.items() if split_document_slug(s)[1] is not None}
    denied = _denied(history_items or [], by_version)
    shown = {(i["provision"], i["from"], i["to"], i["piece"]) for i in history_items or []}
    gone = [key for key, decision in (decisions or {}).items() if decision == "denied" and key not in shown]

    versions = [split_document_slug(s)[1] for s in slugs]
    count = sum(len(v) for v in flagged.values())
    head = [
        f"# Review report: {work}",
        "",
        f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Versions held: {', '.join(f'v{v}' for v in versions if v is not None) or 'one (unversioned)'}"
        + (f"; base v{base}" if base else ""),
        f"- Code: {code_version or 'unknown'}",
        f"- Flagged in review: {count}; denied in History review: {len(denied) + len(gone)}",
        "",
    ]
    body = ["## Flagged in review", ""]
    for slug, entries in flagged.items():
        if entries:
            body += [f"### {slug} ({len(entries)})", "", *[e + "\n" for e in entries]]
    if not count:
        body += ["Nothing flagged.", ""]
    body += ["## Denied in History review", ""]
    body += [e + "\n" for e in denied] or ["Nothing denied.", ""]
    if gone:
        body += ["### Denied, but no longer shown as a change", ""]
        body += [f"- `{p}` v{f} → v{t}, piece `{piece}`"
                 + (f" — note: {(history_notes or {}).get((p, f, t, piece))}"
                    if (history_notes or {}).get((p, f, t, piece)) else "")
                 for p, f, t, piece in gone]
    return "\n".join(head + body).rstrip() + "\n"
