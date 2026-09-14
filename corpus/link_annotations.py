"""
Marks a piece of text as "this should become a link" -- a reference to
another Act, a defined term, a Bill, an Explanatory Memorandum -- on top
of an Act's already-parsed content (data/parsed/<act>.json), without
changing that content.

Each mark covers a stretch of text within one node's stored `text` --
by character position, not the cleaned-up text review.py displays for
reading -- and records a *label* (what kind of link it should become)
but not yet a *target* (which specific Act, definition or Bill it points
to). Working out the target and actually building the hyperlink happens
later, in a separate step. This only records that a human said "this
text should eventually link to something."

The actual storage (now data/legislation.db, a shared SQLite file -- it
used to be one data/links/<act>.json file per Act) lives in
corpus/db.py alongside the other review data people create by hand
(verified state, the correction log). This module just re-exports that
part of it under the name existing callers already use, so nothing
importing add_link/delete_link/load_links/save_links/LABELS/LinkError
from here needed to change."""
from .db import LABELS, LinkError, add_link, delete_link, load_links, save_links

__all__ = ["LABELS", "LinkError", "add_link", "delete_link", "load_links", "save_links"]
