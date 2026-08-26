"""
Records every human review decision (the AI's original guess vs. what you
approved) so future parsing runs can fold recent corrections back in as
few-shot examples. This is the "learning" loop: there's no fine-tuning here,
just an accumulating, self-improving prompt built from your own verified
data.
"""
import json
import time
from pathlib import Path

CORRECTIONS_PATH = Path("data/corrections.jsonl")


def add_correction(act: str, ai_output: dict, human_output: dict, changed: bool) -> None:
    CORRECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "act": act,
        "changed": changed,
        "ai_output": {k: ai_output.get(k) for k in ("type", "number", "heading", "text")},
        "human_output": {k: human_output.get(k) for k in ("type", "number", "heading", "text")},
    }
    with CORRECTIONS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_examples(k: int = 6) -> list[dict]:
    """Most recent corrected examples first, plus a few confirmed-correct ones."""
    if not CORRECTIONS_PATH.exists():
        return []
    records = [
        json.loads(line) for line in CORRECTIONS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    changed = [r for r in records if r["changed"]]
    unchanged = [r for r in records if not r["changed"]]
    picks = list(reversed(changed))[:k] + list(reversed(unchanged))[: max(0, k // 2)]
    return picks


def stats() -> dict:
    if not CORRECTIONS_PATH.exists():
        return {"total": 0, "changed": 0}
    records = [
        json.loads(line) for line in CORRECTIONS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return {"total": len(records), "changed": sum(1 for r in records if r["changed"])}
