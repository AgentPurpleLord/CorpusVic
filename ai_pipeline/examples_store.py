"""
Records every human review decision -- what the parser produced vs. what
you approved -- as a log of where it actually gets things wrong.

This used to feed a model-backed parser's prompt as few-shot examples.
That parser is gone (see run_pipeline.py's own docstring), so the log is
no longer read back by anything automatic: it is evidence for a person.
A pattern that keeps needing the same correction is the signal to add a
profile override for that Act (ai_pipeline/profiles.py), which fixes it
for every future parse deterministically rather than probabilistically.

The actual storage (data/legislation.db, a shared SQLite file -- formerly
the append-only data/corrections.jsonl) lives in ai_pipeline/db.py
alongside the other durable, human-created review data (verified state,
link annotations); this module re-exports its correction-log API under
the name every existing caller already imports from."""
from .db import add_correction, stats

__all__ = ["add_correction", "stats"]
