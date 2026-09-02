"""
Span-level "this should become a link" annotations -- references to other
Acts, defined terms, Bills, Explanatory Memoranda -- layered on top of an
Act's already-parsed nodes (data/ai_parsed/<act>.json) without touching
them.

Each annotation is a span within one node's stored `text` -- character
offsets, not the reflowed/display text review.py shows for reading -- and
records a *label* (which kind of link it'll become) but not yet a
*target* (which specific Act/definition/Bill it resolves to). Resolving
targets and actually generating hyperlinks is a separate, later pass;
this only captures where a human said "this text should eventually link
somewhere."

The actual storage (now data/legislation.db, a shared SQLite file --
formerly one data/links/<act>.json per Act) lives in ai_pipeline/db.py
alongside the other durable, human-created review data (verified state,
the correction log); this module re-exports its link-annotation API
under the name every existing caller already imports from, so nothing
importing add_link/delete_link/load_links/save_links/LABELS/LinkError
from here needed to change."""
from .db import LABELS, LinkError, add_link, delete_link, load_links, save_links

__all__ = ["LABELS", "LinkError", "add_link", "delete_link", "load_links", "save_links"]
