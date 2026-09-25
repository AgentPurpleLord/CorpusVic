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

import pymupdf

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


# A node's stored text keeps the source PDF's own line-wrap points as
# literal "\n"s. That is deliberate -- review.py's link annotations index
# into the stored string by character offset, so it must not be rewritten
# -- but the wraps are a layout artefact of the page, not sentence
# structure, and no consumer should ever *render* or *export* them. Every
# renderer and exporter runs its text through here first; review.py's own
# reflow_with_map does the same collapse, and additionally maps display
# offsets back to raw ones so a selection can still be stored.
_WRAP_RE = re.compile(r"\s*\n\s*")


# A line that ends in a hyphen was broken at a character the words
# already contained -- "charge-sheet", "cross-examine", "Broad-based" --
# so the next line joins straight onto it. Every hyphen-ending line
# across this project's parsed corpus is such a compound; none is a word
# a typesetter split for fit, which is why undoing the break would be
# wrong.
#
# An em dash is not in here, though a line ending in one looks similar.
# In legislative drafting it opens a list ("means—", "if—", "as
# follows—"), and what follows on the next line is the list, not the rest
# of the word. Where the two do end up in one node -- text resuming after
# the list has finished -- closing up gave "described as being—in either
# case", running the sentence into its own wrap-up.
_JOINS_TIGHT = ("-",)

# The markers a printed list uses instead of a number. A line that opens
# with one is a new item, never a continuation of the line above it --
# joining them produced "as follows—• evidence relevant to ... ; • a
# summary of ...", a paragraph with bullets stranded inside it.
BULLETS = ("\u2022", "\u25cf", "\u25aa", "\u00b7")


def join_printed_line(existing: str, addition: str) -> str:
    """One more printed line added to a node's text as running prose.

    The line break itself is not part of the document -- it is where the
    PDF's column ran out -- so it is not kept. The exceptions are the two
    places a break carries meaning: a word already hyphenated closes up,
    and a new list item starts a line of its own.
    """
    if not existing:
        return addition
    if addition.lstrip().startswith(BULLETS):
        return existing + "\n" + addition
    if existing.endswith(BULLETS):
        # A bullet printed alone on its own line, with the item's words
        # on the next one.
        return existing + " " + addition
    return existing + ("" if existing.endswith(_JOINS_TIGHT) else " ") + addition


def reflow(text: str | None) -> str:
    """The stored text as prose: every line-wrap point collapsed to a
    single space."""
    return _WRAP_RE.sub(" ", (text or "").strip())


def reflow_keeping_bullets(text: str | None) -> str:
    """As reflow, but each dot point keeps a line of its own: "Examples—"
    followed by bullets (Family Violence Protection Act s 6) is a list,
    and as one run of prose its items ran together."""
    chunks = re.split(r"\n(?=\s*[" + "".join(BULLETS) + "])", (text or "").strip())
    return "\n".join(reflow(chunk) for chunk in chunks)


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
    # The text of this line's own leading run of consecutive bold+italic
    # spans, or None if the line doesn't open with one -- see
    # _leading_bold_italic's own docstring for why this needs its own
    # field rather than reusing `bold` above.
    leading_bold_italic: str | None = None


@dataclass
class PageText:
    page_no: int
    body: str
    page_width: float = 0.0
    margin_notes: list = field(default_factory=list)
    # Where each of those notes is printed, in the same order -- the Act
    # prints them in the margin beside the provision they amend, and that
    # is the only thing that says which provision a note belongs to when
    # its own text doesn't name one. Kept beside the text rather than
    # folded into it so nothing that already reads margin_notes as plain
    # strings has to change.
    margin_note_rects: list = field(default_factory=list)
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


# A PDF's own convention for a "symbolic" TrueType font's (3,0) cmap
# subtable: character codes are looked up prefixed with 0xF000 rather
# than through a normal Unicode cmap (PDF 32000-1:2008 sec 9.6.6.4).
# PyMuPDF passes that raw 0xF0xx value straight through as if it were a
# real Unicode codepoint, landing in the Private Use Area instead of
# whatever glyph the page actually shows -- Victorian Acts occasionally
# set a decimal fraction like "0.2 penalty unit" or a cross-reference
# like "Part 3.10" using the "SymbolMT" font's own built-in glyphs
# rather than the body TimesNewRoman font. The entries below are the
# ones actually seen in these documents, confirmed by rendering the
# source PDF page as an image and reading the glyph directly rather
# than guessing from the codepoint alone (0xF0D7, for instance, is
# nowhere near ASCII/Latin-1 0xD7's "multiplication sign" -- Symbol's
# own encoding puts a centered dot there, used here as a raised decimal
# point). Any other Private Use Area code is left untouched: an
# unmapped code stays a visible, honest signal that something needs the
# same treatment, whereas a wrong guess would silently corrupt the
# Act's own wording.
_SYMBOL_FONT_PUA = {
    0xF02E: ".",
    **{0xF030 + i: str(i) for i in range(10)},
    0xF0D7: "·",  # MIDDLE DOT -- used here as a decimal point
    # Symbol's capital tau, set for a capital T (Family Violence Protection
    # Act s 125's examples, "Τhe protected person..."): kept as the
    # private-use glyph, the word read "\uf054he".
    0xF054: "T",
}


def _fix_symbol_font_pua(text: str, font: str) -> str:
    if "Symbol" not in font:
        return text
    return "".join(_SYMBOL_FONT_PUA.get(ord(c), c) for c in text)


def _line_text(line) -> str:
    return "".join(_fix_symbol_font_pua(span["text"], span["font"]) for span in line["spans"]).strip()


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


def _leading_bold_italic(line) -> str | None:
    """The text of a line's own leading run of consecutive bold+italic
    spans, or None if the line doesn't open with one -- the reliable
    typesetting signal Victorian drafting uses for a defined term's own
    introduction inside a Definitions/Interpretation section (see
    corpus/definitions.py's module docstring): "accused means a
    person who—" sets "accused" bold+italic and the rest of the line
    plain, distinct from the bold-only emphasis used for Act-name
    citations elsewhere and the italic-only case citations that also
    appear in body text.

    This can't reuse _line_font's dominant-span approach: a definition's
    own term and its "means ..." continuation almost always share one
    physical line, so the *line's* dominant span by character count is
    nearly always the longer plain continuation text, never the short
    bold+italic term at its head -- exactly the opposite of what's needed
    here. PyMuPDF's span flags: bit 4 (16) is bold, bit 1 (2) is italic."""
    lead = []
    for s in line["spans"]:
        if not s["text"]:
            continue
        if s["flags"] & 16 and s["flags"] & 2:
            lead.append(s["text"])
        else:
            break
    joined = "".join(lead).strip()
    return joined or None


BOILERPLATE_MIN_LENGTH = 8


def _normalize_for_frequency(text: str) -> str:
    """Collapses digits so e.g. page numbers don't defeat exact-text
    matching. Short results (a lone page number, a note item's leading "1")
    are excluded by the caller -- collapsing digits makes them collide with
    every other short numeric line in the document, which would otherwise
    push things like note numbers over the frequency threshold and get them
    wrongly stripped as boilerplate."""
    return re.sub(r"\d+", "#", text).strip()


# How far apart two lines' tops can be and still be one printed row. Rows
# of body text are 11pt and more apart, so this cannot merge two.
_SAME_ROW = 2.0


def _rows_left_to_right(body: list[tuple]) -> list[tuple]:
    """Body lines top to bottom, and each row left to right.

    A note's number sits in the margin on its first line's row, a hair
    lower -- "1" at y0 678.3 beside "See section 14..." at 678.2 -- so by
    height alone it came after that line, and the parser opened the note
    one line late: s 45's note 1 split in two, s 124's note 1 took note
    2's first line. Items are (x0, y0, ...)."""
    rows: list[list[tuple]] = []
    for item in sorted(body, key=lambda t: t[1]):
        if rows and item[1] - rows[-1][0][1] < _SAME_ROW:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [item for row in rows for item in sorted(row, key=lambda t: t[0])]


def extract_pages(pdf_path: str) -> list[PageText]:
    doc = pymupdf.open(pdf_path)
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
                    leading_bold_italic = _leading_bold_italic(line)
                    block_lines.append({
                        "bbox": line["bbox"], "text": text, "size": size, "bold": bold,
                        "leading_bold_italic": leading_bold_italic,
                    })

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
                margin.append((y0, text, (x0, y0, x1, y1)))
            else:
                for l in blk["lines"]:
                    lx0, ly0, lx1, ly1 = l["bbox"]
                    body.append((lx0, ly0, lx1, ly1, l["text"], l["size"], l["bold"], l["leading_bold_italic"]))
        margin.sort(key=lambda t: t[0])
        header.sort(key=lambda t: t[0])
        footer.sort(key=lambda t: t[0])
        body = _rows_left_to_right(body)
        body_lines = [
            BodyLine(text=text, x0=x0, x1=x1, y0=y0, y1=y1, page_no=page_no, size=size, bold=bold, leading_bold_italic=lbi)
            for x0, y0, x1, y1, text, size, bold, lbi in body
        ]
        pages.append(
            PageText(
                page_no=page_no,
                body="\n".join(l.text for l in body_lines),
                page_width=w,
                margin_notes=[t for _y, t, _r in margin],
                margin_note_rects=[
                    {"page": page_no, "x0": round(x0, 1), "y0": round(y0, 1),
                     "x1": round(x1, 1), "y1": round(y1, 1)}
                    for _y, _t, (x0, y0, x1, y1) in margin
                ],
                header=[t for _, t in header],
                footer=[t for _, t in footer],
                body_lines=body_lines,
            )
        )
    return pages


# How much of a printed line a box has to cover before the line counts as
# being in it. Half: a box drawn by hand round a provision clips the odd
# descender or overhangs into the margin, and neither should change the
# answer -- but half a line is never ambiguous about which column it is
# in, which is what this has to get right on a page that has two.
_LINE_IN_RECT_OVERLAP = 0.5


def lines_in_rects(lines, rects) -> list:
    """The printed lines a set of boxes covers, in reading order.

    This is what lets a box a reviewer drew decide what a provision
    *says*, not merely where it is. The parser reads a page once and
    groups lines into provisions by its own rules; where it gets that
    wrong, the fix used to be retyping the text. Drawing the box round
    the right lines and reading them back out is the same correction made
    the way the page presents it.

    A line is in a box when its middle is between the box's top and
    bottom and it lies at least halfway inside it horizontally. The
    vertical test is on the middle rather than the whole line so a box
    clipping a descender doesn't drop the line; the horizontal one is
    there because a legislative page has a body column and a margin, and
    the one thing that must never happen is a box over the body pulling
    in an amendment note printed beside it.

    Lines and rects are dicts or objects with the same fields either way
    (a BodyLine, or the dict form pages_to_dicts writes) -- so this works
    on what is in memory and on what is on disk without either having to
    convert.

    Order is the page's own: down the page, and left to right across a
    line, which is what makes two cells of a table row come back in the
    order they are printed."""
    def get(item, name):
        return item[name] if isinstance(item, dict) else getattr(item, name)

    found, seen = [], set()
    for rect in rects:
        page, x0, y0, x1, y1 = (get(rect, k) for k in ("page", "x0", "y0", "x1", "y1"))
        for index, line in enumerate(lines):
            if index in seen or get(line, "page_no") != page:
                continue
            ly0, ly1 = get(line, "y0"), get(line, "y1")
            if not y0 <= (ly0 + ly1) / 2 <= y1:
                continue
            lx0, lx1 = get(line, "x0"), get(line, "x1")
            width = lx1 - lx0
            overlap = min(lx1, x1) - max(lx0, x0)
            if width > 0 and overlap / width < _LINE_IN_RECT_OVERLAP:
                continue
            seen.add(index)
            found.append(line)
    found.sort(key=lambda l: (get(l, "page_no"), get(l, "y0"), get(l, "x0")))
    return found


def text_in_rects(lines, rects) -> str:
    """What a set of boxes says, joined the way the parser joins printed
    lines -- so a provision left alone reads exactly as it did, and one
    corrected by redrawing its box reads like every other provision."""
    text = ""
    for line in lines_in_rects(lines, rects):
        piece = (line["text"] if isinstance(line, dict) else line.text).strip()
        if piece:
            text = join_printed_line(text, piece)
    return text


def pages_to_dicts(pages: list[PageText]) -> list[dict]:
    return [asdict(p) for p in pages]


def pages_from_dicts(dicts: list[dict]) -> list[PageText]:
    pages = []
    for d in dicts:
        d = dict(d)
        d["body_lines"] = [BodyLine(**bl) for bl in d.get("body_lines", [])]
        pages.append(PageText(**d))
    return pages
