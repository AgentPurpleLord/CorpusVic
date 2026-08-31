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
# A running header occasionally wraps onto a second line (a long Part/
# Division title doesn't fit on one), and that second line can sit just
# below the TOP_MASTHEAD_FRACTION cutoff -- close enough behind the first
# header line that no real body paragraph would ever open with this little
# a gap (a genuine section/clause heading always follows the previous
# block by a full body-text line height or more). Catches the wrap without
# needing to just push the cutoff fraction down, which would risk eating
# real body content on pages with an unusually tall masthead.
HEADER_WRAP_GAP = 6.0
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


# A Bill's introduction/reprint pages carry a small numeral in the left
# margin every 5th line, purely for parliamentary debate reference (this
# has no equivalent in an enacted Act's own PDF). PyMuPDF's own block-
# grouping sometimes folds that stray numeral into the *same block* as
# nearby body text it happens to sit beside vertically, which drags the
# whole block's bounding box left enough to get misclassified as a left-
# margin note in extract_pages below -- silently dropping real clause
# text, not just the line number. Detected structurally (short, purely
# numeric, sitting well left of whatever else shares its block) rather
# than by a specific font name, so this holds for any similarly-
# formatted Bill print, not just one particular font choice.
_MARGIN_LINE_NUMBER_X0_GAP = 50.0


def _is_margin_line_number_candidate(text: str) -> bool:
    return text.isdigit() and len(text) <= 3


def _drop_margin_line_numbers(block_lines: list[dict]) -> list[dict]:
    """A paragraph spanning several of the every-5th-line markers has more
    than one candidate in the same block -- comparing each candidate only
    against its *other* siblings (which, for a block with two or more
    line-number stragglers, includes another line-number straggler) lets
    them mask each other: candidate A's nearest "sibling" becomes
    candidate B, a few points away, never the real body text far to the
    right, so the gap check never fires for either. Splitting into real
    content vs. candidates first, then comparing every candidate against
    only the real content's own x0, avoids that -- one stray numeral
    can't hide another."""
    content = [l for l in block_lines if not _is_margin_line_number_candidate(l["text"])]
    if not content:
        return block_lines
    content_min_x0 = min(l["bbox"][0] for l in content)
    return [
        l for l in block_lines
        if not (_is_margin_line_number_candidate(l["text"]) and l["bbox"][0] < content_min_x0 - _MARGIN_LINE_NUMBER_X0_GAP)
    ]


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

            # Drop any margin line-number stray(s) before they can influence
            # this block's bbox -- see _drop_margin_line_numbers's docstring.
            block_lines = _drop_margin_line_numbers(block_lines)

            if block_lines:
                # Margin notes and header/footer boilerplate are typeset as
                # short wrapped phrases -- classify (and, if it's not body
                # text, reconstruct) at the whole-block level, since a block
                # is one coherent note/heading even though it spans several
                # internal lines. Body text still needs per-line granularity
                # for its own font-based classification, done below.
                #
                # bbox is recomputed from the surviving lines rather than
                # trusting PyMuPDF's own block bbox verbatim, precisely so a
                # dropped margin line-number (which could sit well outside
                # the real content's bounds) can't skew it.
                xs0 = [l["bbox"][0] for l in block_lines]
                ys0 = [l["bbox"][1] for l in block_lines]
                xs1 = [l["bbox"][2] for l in block_lines]
                ys1 = [l["bbox"][3] for l in block_lines]
                bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
                blocks.append({"bbox": bbox, "text": " ".join(l["text"] for l in block_lines), "lines": block_lines})
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
    # A Bill's "introduction print" cover matter -- the Parliament masthead
    # and which house it was introduced in -- sits once, on the same page
    # as the actual long title and Chapter 1, rather than repeating on
    # every page the way a running header does. That means it never clears
    # BOILERPLATE_MIN_FREQUENCY above (it can appear on as few as one page
    # out of hundreds), even though it's exactly the same kind of noise --
    # standard, fixed wording that carries no legislative content of its
    # own. Listed explicitly since frequency alone can't catch it; matched
    # after the same digit-collapsing normalisation so it survives a
    # reprint's different date/stage suffix.
    boilerplate |= {
        _normalize_for_frequency(text)
        for text in ("PARLIAMENT OF VICTORIA", "Introduced in the Assembly", "Introduced in the Council")
    }

    pages = []
    for page_no, w, h, blocks in raw_pages:
        body, margin, header, footer = [], [], [], []
        last_header_y1 = None
        for blk in blocks:
            x0, y0, x1, y1 = blk["bbox"]
            text = blk["text"]
            if _normalize_for_frequency(text) in boilerplate:
                header.append((y0, text))
                last_header_y1 = y1 if last_header_y1 is None else max(last_header_y1, y1)
            elif y1 <= h * TOP_MASTHEAD_FRACTION or (
                last_header_y1 is not None and 0 <= y0 - last_header_y1 < HEADER_WRAP_GAP
            ):
                header.append((y0, text))
                last_header_y1 = max(last_header_y1, y1) if last_header_y1 is not None else y1
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
