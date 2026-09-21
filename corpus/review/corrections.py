"""
Records every human review decision -- what the parser produced versus
what you approved -- as a log of where it actually gets things wrong.

This used to be fed back into an AI-model-based parser as example
corrections. That parser is gone (see run_pipeline.py's own docstring),
so nothing automatic reads this log any more -- it's just evidence for a
person to look at. If the same correction keeps coming up, that's the
sign to add a profile override for that Act instead (corpus/
profiles.py), which fixes it for every future parse for certain, rather
than hoping a model gets it right.

The actual storage (data/legislation.db, a shared SQLite file -- it used
to be the plain text file data/corrections.jsonl) lives in
corpus/db.py alongside the other review data people create by hand
(verified state, link labels). This module just re-exports that
correction-log part of it under the name existing callers already use."""
from corpus.storage.db import add_correction, stats

__all__ = ["add_correction", "stats"]
