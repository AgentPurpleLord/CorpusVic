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
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from ai_pipeline.examples_store import add_correction, stats
from ai_pipeline.schema import NODE_TYPES

console = Console()


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


def render_node(node: dict, idx: int, total: int) -> None:
    header = f"[{idx + 1}/{total}] {node['type'].upper()} {node.get('number') or ''} — {node.get('heading') or ''}".strip()
    pages = f"pages {node.get('page_start')}-{node.get('page_end')}"
    body = node["text"]
    if len(body) > 1500:
        body = body[:1500] + "\n... [truncated for display; full text carries through unedited]"
    history = node.get("history") or []
    if history:
        body += "\n\n[dim]History:[/]\n" + "\n".join(f"  • {h['raw']}" for h in history)
    console.print(Panel(body or "(no text)", title=header, subtitle=pages))


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
    args = ap.parse_args()

    nodes, unattached_notes = load_parsed(args.act)
    verified = [] if args.restart else load_verified(args.act)
    start_idx = len(verified)

    s = stats()
    console.print(f"Corrections logged so far across all Acts: {s['total']} ({s['changed']} changed)")
    if unattached_notes:
        console.print(
            f"[yellow]{len(unattached_notes)} amendment-history note(s) couldn't be auto-linked to a node[/] "
            f"-- see data/ai_parsed/{args.act}.json -> unattached_notes"
        )
    if start_idx:
        console.print(f"Resuming at node {start_idx + 1}/{len(nodes)} (use --restart to start over)")

    for idx in range(start_idx, len(nodes)):
        node = nodes[idx]
        render_node(node, idx, len(nodes))
        action = Prompt.ask(
            "[a]ccept / [e]dit / [f]lag-and-continue / [q]uit",
            choices=["a", "e", "f", "q"],
            default="a",
        )
        if action == "a":
            verified.append(node)
            add_correction(args.act, ai_output=node, human_output=node, changed=False)
        elif action == "e":
            edited = edit_node(node)
            verified.append(edited)
            changed = any(edited[k] != node.get(k) for k in ("type", "number", "heading", "text"))
            add_correction(args.act, ai_output=node, human_output=edited, changed=changed)
        elif action == "f":
            flagged = dict(node)
            flagged["needs_followup"] = True
            verified.append(flagged)
            console.print("  [yellow]Flagged for follow-up; kept AI's version for now.[/]")
        elif action == "q":
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
