"""
Interactive CLI to human-verify the AI's structural parse of an Act.

Usage:
    python review.py crimes-act

Default mode reviews one Section at a time: the Section's own lead-in text
plus every Subsection/Paragraph/Subparagraph/Note nested under it are shown
together in one colour-coded panel (colour by type), since a Section and
its own components are what a reviewer actually needs to see side by side
to judge whether the parser attached each piece to the right place. Accept
the whole Section in one go, or drill into a specific piece by its own
legislative label ("(1)", "(1)(a)", "(1)(a)(iii)") to edit or split it.
Standalone structural nodes that aren't a Section's own content (Part/
Division/Subdivision headings, bare topical headings) are still reviewed
one at a time, same as before.

    python review.py crimes-act --flat     reviews every node one at a time
                                            instead (the original behaviour)
    python review.py crimes-act --triage   reviews only the nodes flagged by
                                            data/diagnostics/<act>.json

Writes the finalized result to data/verified/<act>.json. Every decision
(the AI's original guess vs. what you approved) is appended to
data/corrections.jsonl, which future run_pipeline.py runs read back in as
few-shot examples -- so the parser is meant to get better at this over
time, without any fine-tuning step.

Progress is saved after every Section (or, in --flat/--triage mode, every
node), so you can quit ('q') and resume later from where you left off.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.text import Text

from ai_pipeline.examples_store import add_correction, stats
from ai_pipeline.schema import NODE_TYPES

console = Console()

# Rich treats "[a]" as a markup style tag and silently drops it rather than
# printing it -- so a prompt string like "[a]ccept / [e]dit" doesn't lose
# its brackets, it loses the "a" and "e" too (the single letters the
# reviewer actually needs to see to know which key does what). Parentheses
# aren't special to Rich markup, so they survive. The same risk applies to
# any *legislative* text or computed label interpolated into a string
# that reaches console.print/Panel -- real Act text routinely contains
# "[...]"-shaped citations and bracketed annotations, and Rich's markup
# parser is inconsistent about which of those it swallows (case- and
# shape-dependent, not simply "any brackets"). Every such value below goes
# through rich.markup.escape() before interpolation rather than relying on
# guessing which shapes happen to be safe.
ACTION_PROMPT = "(a)ccept / (e)dit / (s)plit-and-reassign / (f)lag-and-continue / (q)uit"
ACTION_CHOICES = ["a", "e", "s", "f", "q"]

UNIT_ACTION_PROMPT = "(a)ccept section / (e)dit a piece / (s)plit a piece / (f)lag section / (q)uit"
UNIT_ACTION_CHOICES = ["a", "e", "s", "f", "q"]

# Colour by type, not by depth -- depth is already shown by indentation, but
# colour is what lets a reviewer's eye jump straight to "is this line a
# Subsection, a Paragraph, or a Note" without reading the label first.
TYPE_STYLES = {
    "subsection": "cyan",
    "paragraph": "green",
    "subparagraph": "yellow",
    "note": "magenta",
    "definition": "blue",
    "heading_group": "bold white",
}
_DEPTH_BY_TYPE = {"subsection": 0, "paragraph": 1, "subparagraph": 2}


def _now_iso() -> str:
    """UTC, ISO 8601, always "+00:00" -- so verification timestamps sort
    correctly as plain strings (used by markdown_export.py to find the most
    recent one across a Section's pieces without parsing dates)."""
    return datetime.now(timezone.utc).isoformat()


def load_parsed(act: str):
    path = Path("data/ai_parsed") / f"{act}.json"
    if not path.exists():
        raise SystemExit(f"No AI-parsed output found at {path} -- run run_pipeline.py first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["nodes"], data.get("unattached_notes", [])


def load_verified(act: str) -> list[dict]:
    path = Path("data/verified") / f"{act}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def save_verified(act: str, verified: list[dict]) -> None:
    path = Path("data/verified") / f"{act}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verified, indent=2), encoding="utf-8")


def load_diagnostics(act: str) -> list[dict]:
    path = Path("data/diagnostics") / f"{act}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _reflow(text: str) -> str:
    """The stored text's "\\n"s are just the source PDF's own line-wrap
    points, not paragraph breaks -- displaying them raw makes every node
    look like a jagged list of half-sentences. Join them back into normal
    flowing prose for display; the underlying data (what gets saved, what
    split_node's line numbers index into) is untouched."""
    return re.sub(r"\s*\n\s*", " ", text or "").strip()


def render_node(node: dict, idx: int, total: int, findings: list[dict] | None = None) -> None:
    header = f"[{idx + 1}/{total}] {node['type'].upper()} {escape(node.get('number') or '')} — {escape(node.get('heading') or '')}".strip()
    pages = f"pages {node.get('page_start')}-{node.get('page_end')}"
    body = escape(_reflow(node["text"]))
    if len(body) > 1500:
        body = body[:1500] + "\n... (truncated for display; full text carries through unedited)"
    history = node.get("history") or []
    if history:
        body += "\n\n[dim]History:[/]\n" + "\n".join(f"  • {escape(h['raw'])}" for h in history)
    if findings:
        body += "\n\n[yellow]Flagged:[/]\n" + "\n".join(f"  ! ({escape(f['severity'])}) {escape(f['message'])}" for f in findings)
    console.print(Panel(body or "(no text)", title=header, subtitle=pages))


# ---------------------------------------------------------------------------
# Section-level grouped review -- the default mode.
# ---------------------------------------------------------------------------

# A Section's own lead-in text plus everything nested under it (Subsection/
# Paragraph/Subparagraph/Note/Definition) forms one review unit; every other
# node type is a boundary that starts (and, for Part/Division/Subdivision/
# heading_group, immediately ends) its own single-node unit. This mirrors
# exactly how the rules engine's own stack nests things -- see
# ai_pipeline/rule_parser.py's HIERARCHY_ORDER -- without needing to
# reconstruct the full tree (build_hierarchy_tree in akn_export.py) just to
# find "everything under this Section": the flat node list is already in
# document order, so a single pass is enough.
_UNIT_BOUNDARY_TYPES = {"part", "division", "subdivision", "section", "heading_group"}


def group_into_units(nodes: list[dict]) -> list[list[int]]:
    units: list[list[int]] = []
    current: list[int] | None = None
    for i, node in enumerate(nodes):
        t = node["type"]
        if t == "section":
            current = [i]
            units.append(current)
        elif t in _UNIT_BOUNDARY_TYPES:
            current = None
            units.append([i])
        elif current is not None:
            current.append(i)
        else:
            units.append([i])
    return units


def _resume_point(units: list[list[int]], verified: list[dict]) -> int:
    """Which unit to resume at, given `verified` already holds `len(verified)`
    nodes' worth of decisions. Normally that count lands exactly on a unit
    boundary (units are always committed whole -- see commit_unit), but a
    prior --flat run can leave it mid-unit; trim back to the last complete
    unit in that case rather than risk duplicating a partially-reviewed one."""
    cumulative = 0
    boundary_units = 0
    for u in units:
        if cumulative + len(u) > len(verified):
            break
        cumulative += len(u)
        boundary_units += 1
    if cumulative != len(verified):
        console.print(
            f"[yellow]Verified progress ({len(verified)} nodes) doesn't land on a section boundary "
            f"-- trimming back to the last complete section ({cumulative} nodes).[/]"
        )
        del verified[cumulative:]
    return boundary_units


def _own_label(node: dict) -> str | None:
    num = node.get("number")
    return f"({num})" if num else None


def compute_unit_labels(unit_nodes: list[dict]) -> list[str]:
    """One label per node in the unit (index 0 is the Section itself,
    labelled "SECTION"), used both for display and for _find_in_unit's
    selection. A Subsection/Paragraph/Subparagraph's own legislative
    numbering is unique by construction, so its path-derived chain
    ("(1)(a)") is used directly; anything else (Note, Definition, a stray
    heading_group) has no numbering of its own -- these commonly share
    the exact same inherited path context (several repealed-text
    "* * * *" markers in a row all sitting right after the same last-
    numbered piece), so a naive path-based label would collide between
    them and even with the real numbered piece they're attached to,
    making that piece impossible to select. These get a running per-type
    counter instead. A final de-duplication pass guards against a genuine
    collision anyway (e.g. a mis-parsed repeated number)."""
    labels = ["SECTION"]
    counters: dict[str, int] = {}
    for node in unit_nodes[1:]:
        if node["type"] in ("subsection", "paragraph", "subparagraph") and node.get("number"):
            path = node.get("path") or {}
            chain = "".join(f"({path[level]})" for level in ("subsection", "paragraph", "subparagraph") if path.get(level))
            labels.append(chain or f"({node['number']})")
        else:
            counters[node["type"]] = counters.get(node["type"], 0) + 1
            labels.append(f"[{node['type']} {counters[node['type']]}]")
    seen: dict[str, int] = {}
    for i, label in enumerate(labels):
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            labels[i] = f"{label}#{seen[label]}"
    return labels


def _find_in_unit(labels: list[str], unit_nodes: list[dict], raw: str) -> tuple[int | None, list[int]]:
    """Resolves a typed label to an index into unit_nodes (index 0 is
    always the Section itself, selected by typing "SECTION"). Returns
    (index, []) on a unique match, or (None, candidate_indices) when the
    input is ambiguous or matches nothing -- candidates let the caller show
    the reviewer what else to try."""
    norm = raw.strip()
    if not norm:
        return None, []
    if norm.upper() == "SECTION":
        return 0, []
    exact = [i for i in range(1, len(labels)) if labels[i] == norm]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    # Fall back to the piece's own bracket alone (e.g. "(a)" instead of the
    # full "(1)(a)"), when that's unambiguous within this Section -- most
    # Sections only nest one level deep, so this is the common case.
    own_matches = [i for i in range(1, len(unit_nodes)) if _own_label(unit_nodes[i]) == norm]
    if len(own_matches) == 1:
        return own_matches[0], []
    return None, own_matches


def render_unit(
    unit_nodes: list[dict],
    unit_indices: list[int],
    labels: list[str],
    unit_no: int,
    total_units: int,
    findings_by_node: dict[int, list[dict]],
) -> None:
    root = unit_nodes[0]
    if root["type"] != "section":
        render_node(root, unit_no - 1, total_units, findings_by_node.get(unit_indices[0]))
        return

    header = f"[{unit_no}/{total_units}] SECTION {escape(root.get('number') or '')} — {escape(root.get('heading') or '')}".strip()
    pages = f"pages {root.get('page_start')}-{root.get('page_end')}"

    body = Text()
    if root.get("text"):
        body.append(_reflow(root["text"]))
        body.append("\n\n")

    for node, orig_idx, label in zip(unit_nodes[1:], unit_indices[1:], labels[1:]):
        style = TYPE_STYLES.get(node["type"], "white")
        indent = "  " * (_DEPTH_BY_TYPE.get(node["type"], 0) + 1)
        body.append(f"{indent}{label} ", style=f"bold {style}")
        body.append(_reflow(node.get("text") or "") or "(no text)", style=style)
        for f in findings_by_node.get(orig_idx) or []:
            body.append(f"\n{indent}  ! ({f['severity']}) {f['message']}", style="yellow")
        body.append("\n\n")

    console.print(Panel(body, title=header, subtitle=pages))


def edit_piece(unit_nodes: list[dict], labels: list[str]) -> None:
    raw = Prompt.ask("  Which piece? (SECTION, or a label like (1) or (1)(a) shown above)")
    idx, candidates = _find_in_unit(labels, unit_nodes, raw)
    if idx is None:
        if candidates:
            shown = ", ".join(escape(labels[i]) for i in candidates)
            console.print(f"  [red]Ambiguous -- matches: {shown}. Type the full label shown above.[/]")
        else:
            console.print("  [red]No matching piece -- nothing changed.[/]")
        return
    unit_nodes[idx] = edit_node(unit_nodes[idx])
    console.print(f"  Updated {escape(labels[idx])}.")


def split_piece(unit_nodes: list[dict], labels: list[str], act: str, verified: list[dict]) -> None:
    """Splits one piece's text at a chosen line boundary and reassigns the
    tail -- most often onto another piece in this same Section (the common
    "(1) A person who -- (a) does X; or (b) does Y -- is guilty of an
    offence" run-on, where the closing clause resumes the lead-in's
    sentence, not the last list item's), or, if left blank, onto an
    already-verified node from an earlier Section."""
    raw = Prompt.ask("  Split which piece? (a label like (1) or (1)(a) shown above)")
    idx, candidates = _find_in_unit(labels, unit_nodes, raw)
    if idx is None:
        if candidates:
            console.print(f"  [red]Ambiguous -- matches: {', '.join(escape(labels[i]) for i in candidates)}.[/]")
        else:
            console.print("  [red]No matching piece.[/]")
        return

    node = unit_nodes[idx]
    lines = (node.get("text") or "").split("\n")
    if len(lines) < 2:
        console.print("  [yellow]Only one line of text in this piece -- nothing to split.[/]")
        return

    console.print(f"  Lines in {escape(labels[idx])}:")
    for i, line in enumerate(lines):
        console.print(f"    {i + 1}: {escape(line)}")
    split_at = IntPrompt.ask("  Split before which line number? (this piece keeps everything before it; 0 to cancel)", default=0)
    if not (1 < split_at <= len(lines)):
        if split_at != 0:
            console.print("  [yellow]Not a valid split point -- cancelled.[/]")
        return

    console.print("  Reassign the tail to another piece in this Section:")
    for i, n in enumerate(unit_nodes):
        if i == idx:
            continue
        console.print(f"    {escape(labels[i])}: {escape(_reflow(n.get('text') or '')[:60])}")
    target_raw = Prompt.ask("  Label (blank to instead reassign to an earlier already-reviewed Section)", default="")

    logged_immediately = False
    if target_raw:
        target_idx, candidates = _find_in_unit(labels, unit_nodes, target_raw)
        if target_idx is None or target_idx == idx:
            if candidates:
                console.print(f"  [red]Ambiguous -- matches: {', '.join(escape(labels[i]) for i in candidates)}.[/]")
            else:
                console.print("  [red]No matching piece -- cancelled.[/]")
            return
        target = unit_nodes[target_idx]
        target_label = labels[target_idx]
    else:
        if not verified:
            console.print("  [yellow]Nothing verified yet to reassign to -- cancelled.[/]")
            return
        recent = verified[-8:]
        offset = len(verified) - len(recent)
        for i, v in enumerate(recent):
            preview = escape(_reflow(v.get("text") or "")[:60])
            console.print(f"    {offset + i + 1}: {v['type'].upper()} {escape(v.get('number') or '')} — {preview}")
        choice = Prompt.ask("  Verified-list number (blank to cancel)", default="")
        if not choice:
            console.print("  [yellow]Cancelled.[/]")
            return
        try:
            v_idx = int(choice) - 1
            if not (0 <= v_idx < len(verified)):
                raise ValueError
        except ValueError:
            console.print("  [red]Invalid choice -- cancelled.[/]")
            return
        target = verified[v_idx]
        target_label = f"{target['type'].upper()} {target.get('number') or ''}".strip()
        # This node was already committed (and logged) by an earlier
        # Section's own accept -- log this extra edit against it now, since
        # nothing will revisit it later to log it for us. Its content is
        # changing again right now, under direct human review, so its
        # verification timestamp is refreshed too.
        original_target_text = target.get("text") or ""
        tail_text = "\n".join(lines[split_at - 1 :]).strip()
        target["text"] = (original_target_text + "\n" + tail_text) if original_target_text else tail_text
        target["verified_at"] = _now_iso()
        add_correction(
            act,
            ai_output={"type": target["type"], "number": target.get("number"), "heading": target.get("heading"), "text": original_target_text},
            human_output=target,
            changed=True,
        )
        logged_immediately = True

    if not logged_immediately:
        tail_text = "\n".join(lines[split_at - 1 :]).strip()
        original_target_text = target.get("text") or ""
        target["text"] = (original_target_text + "\n" + tail_text) if original_target_text else tail_text

    node["text"] = "\n".join(lines[: split_at - 1]).strip()
    console.print(f"  Moved the tail to {escape(target_label)}.")


def commit_unit(unit_nodes: list[dict], unit_orig: list[dict], act: str, verified: list[dict], flagged: bool = False) -> None:
    """Appends every node in the unit to `verified` and logs one correction
    per node -- same per-node granularity data/corrections.jsonl has always
    had, just decided on in one batch instead of one prompt per node.

    Every accepted/edited node is stamped with when a human confirmed it --
    markdown_export.py reads this back to flag human-verified content in
    each page's front matter. A flagged node explicitly isn't confirmed
    (that's what flagging means -- "not sure, revisit this"), so it's kept
    unstamped even though the reviewer looked at it."""
    for original, current in zip(unit_orig, unit_nodes):
        node = dict(current)
        if flagged:
            node["needs_followup"] = True
        else:
            node["verified_at"] = _now_iso()
        verified.append(node)
        changed = any(node.get(k) != original.get(k) for k in ("type", "number", "heading", "text"))
        add_correction(act, ai_output=original, human_output=node, changed=changed)


def run_section_review(act: str, nodes: list[dict], verified: list[dict], findings_by_node: dict[int, list[dict]]) -> None:
    units = group_into_units(nodes)
    start_unit = _resume_point(units, verified)
    if start_unit:
        console.print(f"Resuming at section {start_unit + 1}/{len(units)} (use --restart to start over)")

    for u in range(start_unit, len(units)):
        indices = units[u]
        unit_orig = [nodes[i] for i in indices]
        unit_nodes = [dict(n) for n in unit_orig]
        while True:
            labels = compute_unit_labels(unit_nodes)
            render_unit(unit_nodes, indices, labels, u + 1, len(units), findings_by_node)
            action = Prompt.ask(UNIT_ACTION_PROMPT, choices=UNIT_ACTION_CHOICES, default="a")
            if action == "a":
                commit_unit(unit_nodes, unit_orig, act, verified)
                break
            if action == "e":
                edit_piece(unit_nodes, labels)
                continue
            if action == "s":
                split_piece(unit_nodes, labels, act, verified)
                continue
            if action == "f":
                commit_unit(unit_nodes, unit_orig, act, verified, flagged=True)
                console.print("  [yellow]Flagged for follow-up; kept current version.[/]")
                break
            if action == "q":
                save_verified(act, verified)
                console.print(f"Saved {len(verified)}/{len(nodes)} verified nodes to data/verified/{act}.json")
                return
        save_verified(act, verified)

    flagged_count = sum(1 for n in verified if n.get("needs_followup"))
    console.print(f"[green]Done.[/] All {len(units)} section(s)/heading(s) reviewed -> data/verified/{act}.json")
    if flagged_count:
        console.print(f"[yellow]{flagged_count} node(s) still flagged for follow-up.[/]")


# ---------------------------------------------------------------------------
# Flat, one-node-at-a-time review -- --flat and --triage both use this.
# ---------------------------------------------------------------------------

def _apply_action(action: str, node: dict, act: str, verified: list[dict]) -> bool:
    """Handles one accept/edit/flag/quit decision, appending the result to
    `verified` and logging a correction where relevant. Returns False on
    quit (caller should stop the loop), True otherwise.

    Accept/edit stamp when a human confirmed the node -- see commit_unit's
    docstring for why flagging doesn't."""
    if action == "a":
        node = dict(node)
        node["verified_at"] = _now_iso()
        verified.append(node)
        add_correction(act, ai_output=node, human_output=node, changed=False)
    elif action == "e":
        edited = edit_node(node)
        edited["verified_at"] = _now_iso()
        verified.append(edited)
        changed = any(edited[k] != node.get(k) for k in ("type", "number", "heading", "text"))
        add_correction(act, ai_output=node, human_output=edited, changed=changed)
    elif action == "f":
        flagged = dict(node)
        flagged["needs_followup"] = True
        verified.append(flagged)
        console.print("  [yellow]Flagged for follow-up; kept the parser's version for now.[/]")
    elif action == "q":
        return False
    return True


def split_node(node: dict, act: str, verified: list[dict]) -> dict | None:
    """Splits node["text"] at a chosen line boundary and reassigns the tail
    onto an earlier, already-verified node -- the --flat/--triage mode
    counterpart of split_piece above, searching the flat `verified` list
    (its most recent entries) instead of the current Section's own pieces,
    since flat mode has no "current Section" to offer as targets.

    Returns the shortened node (still needing its own accept/edit/flag/quit
    decision) if a split was made, or None if the reviewer cancelled --
    callers should re-render and re-prompt on a dict, and just re-prompt
    unchanged on None."""
    lines = (node.get("text") or "").split("\n")
    if len(lines) < 2:
        console.print("  [yellow]Only one line of text in this node -- nothing to split.[/]")
        return None

    console.print("  Lines in this node's text:")
    for i, line in enumerate(lines):
        console.print(f"    {i + 1}: {escape(line)}")
    split_at = IntPrompt.ask(
        "  Split before which line number? (this node keeps everything before it; 0 to cancel)",
        default=0,
    )
    if not (1 < split_at <= len(lines)):
        if split_at != 0:
            console.print("  [yellow]Not a valid split point -- cancelled.[/]")
        return None

    if not verified:
        console.print("  [yellow]Nothing verified yet to reassign the tail to -- cancelled.[/]")
        return None

    console.print("  Reassign the tail to which already-reviewed node?")
    recent = verified[-8:]
    offset = len(verified) - len(recent)
    for i, v in enumerate(recent):
        preview = escape(_reflow(v.get("text") or "")[:70])
        console.print(f"    {offset + i + 1}: {v['type'].upper()} {escape(v.get('number') or '')} — {preview}")
    choice = Prompt.ask("  Verified-list number (blank to cancel)", default="")
    if not choice:
        console.print("  [yellow]Cancelled.[/]")
        return None
    try:
        target_idx = int(choice) - 1
        if not (0 <= target_idx < len(verified)):
            raise ValueError
    except ValueError:
        console.print("  [red]Invalid choice -- cancelled.[/]")
        return None

    target = verified[target_idx]
    tail_text = "\n".join(lines[split_at - 1 :]).strip()
    original_target_text = target.get("text") or ""
    target["text"] = (original_target_text + "\n" + tail_text) if original_target_text else tail_text
    target["verified_at"] = _now_iso()
    add_correction(
        act,
        ai_output={"type": target["type"], "number": target.get("number"), "heading": target.get("heading"), "text": original_target_text},
        human_output=target,
        changed=True,
    )
    console.print(f"  Reassigned to {target['type'].upper()} {escape(target.get('number') or '')}.")

    head = dict(node)
    head["text"] = "\n".join(lines[: split_at - 1]).strip()
    return head


def edit_node(node: dict) -> dict:
    edited = dict(node)
    edited["type"] = Prompt.ask("  type", choices=NODE_TYPES, default=node["type"])
    number = Prompt.ask("  number", default=node.get("number") or "")
    edited["number"] = number or None
    heading = Prompt.ask("  heading", default=node.get("heading") or "")
    edited["heading"] = heading or None
    if Confirm.ask("  Edit the text body too?", default=False):
        console.print("  Enter replacement text, end with a blank line:")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)
        edited["text"] = "\n".join(lines)
    return edited


def run_flat_review(args, nodes: list[dict], verified: list[dict], findings_by_node: dict[int, list[dict]]) -> None:
    start_idx = len(verified)
    if start_idx:
        console.print(f"Resuming at node {start_idx + 1}/{len(nodes)} (use --restart to start over)")

    for idx in range(start_idx, len(nodes)):
        node = nodes[idx]
        while True:
            render_node(node, idx, len(nodes), findings_by_node.get(idx))
            action = Prompt.ask(ACTION_PROMPT, choices=ACTION_CHOICES, default="a")
            if action != "s":
                break
            split = split_node(node, args.act, verified)
            if split is not None:
                node = split
        if not _apply_action(action, node, args.act, verified):
            save_verified(args.act, verified)
            console.print(f"Saved {len(verified)}/{len(nodes)} verified nodes to data/verified/{args.act}.json")
            return
        save_verified(args.act, verified)

    flagged_count = sum(1 for n in verified if n.get("needs_followup"))
    console.print(f"[green]Done.[/] All {len(nodes)} nodes reviewed -> data/verified/{args.act}.json")
    if flagged_count:
        console.print(f"[yellow]{flagged_count} node(s) still flagged for follow-up.[/]")


def run_triage(args, nodes: list[dict], verified: list[dict], findings_by_node: dict[int, list[dict]]) -> None:
    indices = sorted(findings_by_node)
    if not indices:
        console.print("[green]No flagged nodes -- nothing to triage.[/]")
        return
    console.print(f"Triage mode: {len(indices)} flagged node(s) out of {len(nodes)} total.")
    reviewed_this_run = {n.get("_triage_index") for n in verified if "_triage_index" in n}
    for idx in indices:
        if idx in reviewed_this_run:
            continue
        node = nodes[idx]
        while True:
            render_node(node, idx, len(nodes), findings_by_node.get(idx))
            action = Prompt.ask(ACTION_PROMPT, choices=ACTION_CHOICES, default="a")
            if action != "s":
                break
            split = split_node(node, args.act, verified)
            if split is not None:
                node = split
        before = len(verified)
        if not _apply_action(action, node, args.act, verified):
            save_verified(args.act, verified)
            console.print(f"Saved {len(verified)}/{len(nodes)} verified nodes to data/verified/{args.act}.json")
            return
        verified[before]["_triage_index"] = idx
        save_verified(args.act, verified)
    console.print(f"[green]Triage pass complete.[/] {len(indices)} flagged node(s) reviewed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    ap.add_argument("--restart", action="store_true", help="ignore existing progress and start from the beginning")
    ap.add_argument(
        "--flat", action="store_true",
        help="review every node one at a time instead of grouping each Section with its own components",
    )
    ap.add_argument(
        "--triage", action="store_true",
        help="review only the nodes flagged by data/diagnostics/<act>.json (duplicates, empty nodes, "
             "low-confidence history links) instead of walking every node/section",
    )
    args = ap.parse_args()

    nodes, unattached_notes = load_parsed(args.act)
    verified = [] if args.restart else load_verified(args.act)

    s = stats()
    console.print(f"Corrections logged so far across all Acts: {s['total']} ({s['changed']} changed)")
    if unattached_notes:
        console.print(
            f"[yellow]{len(unattached_notes)} amendment-history note(s) couldn't be auto-linked to a node[/] "
            f"-- see data/ai_parsed/{args.act}.json -> unattached_notes"
        )

    findings_by_node: dict[int, list[dict]] = {}
    for finding in load_diagnostics(args.act):
        if finding.get("node_index") is not None:
            findings_by_node.setdefault(finding["node_index"], []).append(finding)

    if args.triage:
        run_triage(args, nodes, verified, findings_by_node)
    elif args.flat:
        run_flat_review(args, nodes, verified, findings_by_node)
    else:
        run_section_review(args.act, nodes, verified, findings_by_node)


if __name__ == "__main__":
    main()
