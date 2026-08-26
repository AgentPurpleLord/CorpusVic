"""
Extracts clean body text from a born-digital Victorian Act PDF using
PyMuPDF block coordinates -- no OCR involved.

Each Act page has a fixed template: a running header (act title, current
Part/Division context), a running footer ("Authorised by the Chief
Parliamentary Counsel" + page number), a narrow right-hand column of
amendment-history notes, and the actual body text. This splits each page
into those four buckets so only the real body text is handed to the AI
structuring step -- the coordinate work here is just noise removal, not
structure recognition.
"""
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

import fitz

TOP_MASTHEAD_FRACTION = 0.17
BOTTOM_FOOTER_FRACTION = 0.83
# Amendment-history notes sit in the outer margin, which alternates sides
# page to page (right margin on odd/recto pages, left margin on even/verso
# pages) -- standard book-style typesetting. Catch both sides by x0; body
# text (even short, indented lines) never starts left of ~0.20 of the page
# width, so this margin stays clear of real content.
MARGIN_RIGHT_X0_FRACTION = 0.70
MARGIN_LEFT_X0_FRACTION = 0.20
BOILERPLATE_MIN_FREQUENCY = 0.5


@dataclass
class BodyLine:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    page_no: int
    size: float = 0.0
    bold: bool = False


@dataclass
class PageText:
    page_no: int
    body: str
    page_width: float = 0.0
    margin_notes: list = field(default_factory=list)
    header: list = field(default_factory=list)
    footer: list = field(default_factory=list)
    body_lines: list = field(default_factory=list)  # list[BodyLine]


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()


def _line_text(line) -> str:
    return "".join(span["text"] for span in line["spans"]).strip()


def _line_font(line) -> tuple[float, bool]:
    """Dominant font size and bold-ness for a line, weighted by character
    count -- this is what actually encodes the drafter's intended hierarchy
    (Part/Division/Section headings are set bold at distinct sizes), far
    more reliably than the line's position on the page."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    if not spans:
        return 0.0, False
    dominant = max(spans, key=lambda s: len(s["text"]))
    is_bold = bool(dominant["flags"] & 16)
    return dominant["size"], is_bold


BOILERPLATE_MIN_LENGTH = 8


def _normalize_for_frequency(text: str) -> str:
    """Collapses digits so e.g. page numbers don't defeat exact-text
    matching. Short results (a lone page number, a note item's leading "1")
    are excluded by the caller -- collapsing digits makes them collide with
    every other short numeric line in the document, which would otherwise
    push things like note numbers over the frequency threshold and get them
    wrongly stripped as boilerplate."""
    return re.sub(r"\d+", "#", text).strip()


def extract_pages(pdf_path: str) -> list[PageText]:
    doc = fitz.open(pdf_path)
    raw_pages = []
    for page in doc:
        w, h = page.rect.width, page.rect.height
        blocks = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            block_lines = []
            for line in b["lines"]:
                text = _line_text(line)
                if text:
                    size, bold = _line_font(line)
                    block_lines.append({"bbox": line["bbox"], "text": text, "size": size, "bold": bold})
            if block_lines:
                # Margin notes and header/footer boilerplate are typeset as
                # short wrapped phrases -- classify (and, if it's not body
                # text, reconstruct) at the whole-block level, since a block
                # is one coherent note/heading even though it spans several
                # internal lines. Body text still needs per-line granularity
                # for its own font-based classification, done below.
                blocks.append({"bbox": b["bbox"], "text": " ".join(l["text"] for l in block_lines), "lines": block_lines})
        raw_pages.append((page.number + 1, w, h, blocks))

    # Boilerplate that repeats near-identically on most pages (title lines,
    # "Authorised by the Chief Parliamentary Counsel") gets stripped
    # regardless of where it happens to sit, as a safety net alongside the
    # coordinate bands below -- different Acts can shift the layout slightly.
    freq = Counter()
    for _, _, _, blocks in raw_pages:
        seen = set()
        for blk in blocks:
            key = _normalize_for_frequency(blk["text"])
            if len(key) >= BOILERPLATE_MIN_LENGTH and key not in seen:
                freq[key] += 1
                seen.add(key)
    n_pages = max(len(raw_pages), 1)
    boilerplate = {
        key for key, count in freq.items() if count / n_pages >= BOILERPLATE_MIN_FREQUENCY
    }

    pages = []
    for page_no, w, h, blocks in raw_pages:
        body, margin, header, footer = [], [], [], []
        for blk in blocks:
            x0, y0, x1, y1 = blk["bbox"]
            text = blk["text"]
            if _normalize_for_frequency(text) in boilerplate:
                header.append((y0, text))
            elif y1 <= h * TOP_MASTHEAD_FRACTION:
                header.append((y0, text))
            elif y0 >= h * BOTTOM_FOOTER_FRACTION:
                footer.append((y0, text))
            elif x0 >= w * MARGIN_RIGHT_X0_FRACTION or x0 <= w * MARGIN_LEFT_X0_FRACTION:
                margin.append((y0, text))
            else:
                for l in blk["lines"]:
                    lx0, ly0, lx1, ly1 = l["bbox"]
                    body.append((lx0, ly0, lx1, ly1, l["text"], l["size"], l["bold"]))
        margin.sort(key=lambda t: t[0])
        header.sort(key=lambda t: t[0])
        footer.sort(key=lambda t: t[0])
        body.sort(key=lambda t: t[1])
        body_lines = [
            BodyLine(text=text, x0=x0, x1=x1, y0=y0, y1=y1, page_no=page_no, size=size, bold=bold)
            for x0, y0, x1, y1, text, size, bold in body
        ]
        pages.append(
            PageText(
                page_no=page_no,
                body="\n".join(l.text for l in body_lines),
                page_width=w,
                margin_notes=[t for _, t in margin],
                header=[t for _, t in header],
                footer=[t for _, t in footer],
                body_lines=body_lines,
            )
        )
    return pages


def pages_to_dicts(pages: list[PageText]) -> list[dict]:
    return [asdict(p) for p in pages]


def pages_from_dicts(dicts: list[dict]) -> list[PageText]:
    pages = []
    for d in dicts:
        d = dict(d)
        d["body_lines"] = [BodyLine(**bl) for bl in d.get("body_lines", [])]
        pages.append(PageText(**d))
    return pages
