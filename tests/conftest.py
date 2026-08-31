"""
Shared test fixtures and builders.

rule_parser.py works off BodyLine objects (text + position + font info)
rather than raw strings, so its tests need to construct those directly
instead of feeding it a real PDF -- this file's `line`/`page` helpers do
that, using indentation constants lifted from real measurements taken off
actual Crimes Act pages during development (see rule_parser.py's
_resolve_hanging_list docstring for the construct these model): a
top-level item's own wrap indent sits ~20pt right of its opening marker,
and each nesting level (Subsection -> Paragraph -> Subparagraph) opens
roughly where the level above it wraps.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_pipeline import examples_store
from ai_pipeline.extract import BodyLine, PageText

BODY_SIZE = 12.0

# Indentation levels, modelled on real measurements (see module docstring).
HEAD_X0 = 190.2  # Subsection-level opening indent
WRAP_X0 = 209.8  # Subsection-level wrap (continuation-line) indent
PARA_X0 = 215.7  # Paragraph-level opening indent
PARA_WRAP_X0 = 235.3  # Paragraph-level wrap indent
SUBPARA_X0 = 237.2  # Subparagraph-level opening indent
SUBPARA_WRAP_X0 = 260.8  # Subparagraph-level wrap indent


def line(
    text: str, x0: float = HEAD_X0, bold: bool = False, size: float = BODY_SIZE, page_no: int = 1,
    leading_bold_italic: str | None = None,
) -> BodyLine:
    return BodyLine(
        text=text, x0=x0, x1=x0 + 200.0, y0=0.0, y1=10.0, page_no=page_no, size=size, bold=bold,
        leading_bold_italic=leading_bold_italic,
    )


def page(lines: list[BodyLine], page_no: int = 1) -> PageText:
    return PageText(page_no=page_no, body="", body_lines=lines)


def make_node(
    type_: str,
    number: str | None = None,
    heading: str | None = None,
    text: str = "",
    page_start: int = 1,
    page_end: int | None = None,
    verified_at: str | None = None,
    history: list[dict] | None = None,
) -> dict:
    """A minimal flat node dict in the same shape rule_parser.py/review.py
    produce -- for tests that exercise markdown_export.py/akn_export.py/
    review.py directly without needing a full PDF-derived parse."""
    node = {
        "type": type_,
        "number": number,
        "heading": heading,
        "text": text,
        "page_start": page_start,
        "page_end": page_end if page_end is not None else page_start,
        "source": "rules",
    }
    if verified_at is not None:
        node["verified_at"] = verified_at
    if history is not None:
        node["history"] = history
    return node


@pytest.fixture
def isolate_corrections(tmp_path, monkeypatch):
    """Redirects examples_store's correction log to a scratch file so tests
    that go through review.py's commit/accept paths (which call
    add_correction) never touch the real project's data/corrections.jsonl."""
    path = tmp_path / "corrections.jsonl"
    monkeypatch.setattr(examples_store, "CORRECTIONS_PATH", path)
    return path
