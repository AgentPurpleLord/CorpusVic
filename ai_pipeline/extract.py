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
class PageText:
    page_no: int
    body: str
    margin_notes: list = field(default_factory=list)
    header: list = field(default_factory=list)
    footer: list = field(default_factory=list)


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()


def _block_text(block) -> str:
    return "".join(
        span["text"] for line in block["lines"] for span in line["spans"]
    ).strip()


def _normalize_for_frequency(text: str) -> str:
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
            text = _block_text(b)
            if text:
                blocks.append({"bbox": b["bbox"], "text": text})
        raw_pages.append((page.number + 1, w, h, blocks))

    # Boilerplate that repeats near-identically on most pages (title lines,
    # "Authorised by the Chief Parliamentary Counsel") gets stripped
    # regardless of where it happens to sit, as a safety net alongside the
    # coordinate bands below -- different Acts can shift the layout slightly.
    freq = Counter()
    for _, _, _, blocks in raw_pages:
        seen = set()
        for b in blocks:
            key = _normalize_for_frequency(b["text"])
            if key and key not in seen:
                freq[key] += 1
                seen.add(key)
    n_pages = max(len(raw_pages), 1)
    boilerplate = {
        key for key, count in freq.items() if count / n_pages >= BOILERPLATE_MIN_FREQUENCY
    }

    pages = []
    for page_no, w, h, blocks in raw_pages:
        body, margin, header, footer = [], [], [], []
        for b in blocks:
            x0, y0, x1, y1 = b["bbox"]
            text = b["text"]
            if _normalize_for_frequency(text) in boilerplate:
                header.append((y0, text))
            elif y1 <= h * TOP_MASTHEAD_FRACTION:
                header.append((y0, text))
            elif y0 >= h * BOTTOM_FOOTER_FRACTION:
                footer.append((y0, text))
            elif x0 >= w * MARGIN_RIGHT_X0_FRACTION or x0 <= w * MARGIN_LEFT_X0_FRACTION:
                margin.append((y0, text))
            else:
                body.append((y0, text))
        for lst in (body, margin, header, footer):
            lst.sort(key=lambda t: t[0])
        pages.append(
            PageText(
                page_no=page_no,
                body="\n".join(t for _, t in body),
                margin_notes=[t for _, t in margin],
                header=[t for _, t in header],
                footer=[t for _, t in footer],
            )
        )
    return pages


def pages_to_dicts(pages: list[PageText]) -> list[dict]:
    return [asdict(p) for p in pages]


def pages_from_dicts(dicts: list[dict]) -> list[PageText]:
    return [PageText(**d) for d in dicts]
