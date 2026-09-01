"""
Records every human review decision (the AI's original guess vs. what you
approved) so future parsing runs can fold recent corrections back in as
few-shot examples. This is the "learning" loop: there's no fine-tuning here,
just an accumulating, self-improving prompt built from your own verified
data.

The actual storage (now data/legislation.db, a shared SQLite file --
formerly the append-only data/corrections.jsonl) lives in ai_pipeline/db.py
alongside the other durable, human-created review data (verified state,
link annotations); this module re-exports its correction-log API under
the name every existing caller already imports from, so nothing importing
add_correction/load_examples/stats from here needed to change."""
from .db import add_correction, load_examples, stats

__all__ = ["add_correction", "load_examples", "stats"]
