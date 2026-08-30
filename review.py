"""
Interactive CLI to human-verify the AI's structural parse of an Act.

Usage:
    python review.py crimes-act

Walks through every node in data/ai_parsed/<act>.json one at a time, lets you
accept / edit / flag it, and writes the finalized result to
data/verified/<act>.json. Every decision (the AI's original guess vs. what
you approved) is appended to data/corrections.jsonl, which future
run_pipeline.py runs read back in as few-shot examples -- so the parser is
meant to get better at this over time, without any fine-tuning step.

Progress is saved after every node, so you can quit ('q') and resume later
from where you left off.
"""
import argparse
import json
import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt

from ai_pipeline.examples_store import add_correction, stats
from ai_pipeline.schema import NODE_TYPES

console = Console()

# Rich treats "[a]" as a markup style tag and silently drops it rather than
# printing it -- so a prompt string like "[a]ccept / [e]dit" doesn't lose
# its brackets, it loses the "a" and "e" too (the single letters the
# reviewer actually needs to see to know which key does what). Parentheses
# aren't special to Rich markup, so they survive.
ACTION_PROMPT = "(a)ccept / (e)dit / (s)plit-and-reassign / (f)lag-and-continue / (q)uit"
ACTION_CHOICES = ["a", "e", "s", "f", "q"]


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
    header = f"[{idx + 1}/{total}] {node['type'].upper()} {node.get('number') or ''} — {node.get('heading') or ''}".strip()
    pages = f"pages {node.get('page_start')}-{node.get('page_end')}"
    body = _reflow(node["text"])
    if len(body) > 1500:
        body = body[:1500] + "\n... [truncated for display; full text carries through unedited]"
    history = node.get("history") or []
    if history:
        body += "\n\n[dim]History:[/]\n" + "\n".join(f"  • {h['raw']}" for h in history)
    if findings:
        body += "\n\n[yellow]Flagged:[/]\n" + "\n".join(f"  ! ({f['severity']}) {f['message']}" for f in findings)
    console.print(Panel(body or "(no text)", title=header, subtitle=pages))


def _apply_action(action: str, node: dict, act: str, verified: list[dict]) -> bool:
    """Handles one accept/edit/flag/quit decision, appending the result to
    `verified` and logging a correction where relevant. Returns False on
    quit (caller should stop the loop), True otherwise."""
    if action == "a":
        verified.append(node)
        add_correction(act, ai_output=node, human_output=node, changed=False)
    elif action == "e":
        edited = edit_node(node)
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
    onto an earlier, already-verified node -- for the common run-on
    construct where a subsection opens with lead-in text, breaks into a
    lettered/roman list, and the list's last item is immediately followed
    by an independent clause that actually resumes the *lead-in's*
    sentence, not the list item's ("(1) A person who -- (a) does X; or
    (b) does Y -- is guilty of an offence."). The rules engine now catches
    the common case of this automatically (see rule_parser.py's
    _resolve_hanging_list), but nothing catches every case, and this is the
    manual fallback for whatever it misses.

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
        console.print(f"    {i + 1}: {line}")
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
        preview = _reflow(v.get("text") or "")[:70]
        console.print(f"    {offset + i + 1}: {v['type'].upper()} {v.get('number') or ''} — {preview}")
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
    add_correction(
        act,
        ai_output={"type": target["type"], "number": target.get("number"), "heading": target.get("heading"), "text": original_target_text},
        human_output=target,
        changed=True,
    )
    console.print(f"  Reassigned to {target['type'].upper()} {target.get('number') or ''}.")

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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    ap.add_argument("--restart", action="store_true", help="ignore existing progress and start from node 1")
    ap.add_argument(
        "--triage", action="store_true",
        help="review only the nodes flagged by data/diagnostics/<act>.json (duplicates, empty nodes, "
             "low-confidence history links) instead of walking every node",
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
        return

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


if __name__ == "__main__":
    main()
