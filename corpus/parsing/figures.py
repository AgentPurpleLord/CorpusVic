"""The charts and diagrams an Act prints, as figure nodes.

A chart in an Act PDF is an embedded image with no text in it (the Bail
Act's flowcharts after s 3D, the Evidence Act's Chapter 3 diagram), so
the line parser never sees it and the parse used to drop it without a
word. Here each one is lifted out of the PDF losslessly, turned the way
the page draws it, stored once under its content hash as WebP with a PNG
fallback, and put in the node list where it printed.

The node's text is its reference, "<sha12> <width>x<height>", not a
field of its own: a reviewed provision is stored as a row of text columns
(see the verified table in corpus/storage/db.py), and anything beside
the text would be lost the moment a reviewer approved it. As text it
survives review, carry-forward and the JSONL sync as it is, and a
reprint that changes a chart changes the provision. Nothing shows it to
a reader or a reviewer.

Its heading is the chart's own ("Flow Chart 1 – Terrorism record or
terrorism risk"), which is drawn in the image, so it is read off it by
OCR (RapidOCR, optional) and checked in review like any other heading.
"""
import hashlib
import io
import re

from corpus import PROJECT_ROOT

FIGURES_DIR = PROJECT_ROOT / "data" / "figures"

# Smaller than this on either side is a rule, a logo or a speck, not a
# chart. In points: the smallest Bail Act chart is 200pt wide.
MIN_SIDE_PT = 40

_REF_RE = re.compile(r"^([0-9a-f]{12}) (\d+)x(\d+)$")


def figure_ref(node: dict) -> "dict | None":
    """{"src", "width", "height"} for a figure node, else None."""
    if node.get("type") != "figure":
        return None
    m = _REF_RE.match((node.get("text") or "").strip())
    if not m:
        return None
    return {"src": m.group(1), "width": int(m.group(2)), "height": int(m.group(3))}


def find_figures(pdf_path, first_page: int, last_page: int) -> list[dict]:
    """[{page, bbox, pixmap, matrix}] for every chart-sized image on pages
    first_page..last_page (1-indexed, inclusive), in reading order.
    `matrix` is how the page draws the image (see _orientation)."""
    import pymupdf

    found = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page_no in range(first_page, min(last_page, doc.page_count) + 1):
            page = doc[page_no - 1]
            for info in page.get_images(full=True):
                xref, smask = info[0], info[1]
                for rect, matrix in page.get_image_rects(xref, transform=True):
                    if rect.width < MIN_SIDE_PT or rect.height < MIN_SIDE_PT:
                        continue
                    found.append({"page": page_no, "bbox": (rect.x0, rect.y0, rect.x1, rect.y1),
                                  "pixmap": _pixmap(pymupdf, doc, xref, smask),
                                  "matrix": tuple(matrix)})
    found.sort(key=lambda f: (f["page"], f["bbox"][1]))
    return found


def _pixmap(pymupdf, doc, xref: int, smask: int):
    """The image as stored, in RGB, with its soft mask as alpha: a chart
    drawn on transparency is otherwise black on black. store() lays that
    on white, as the page did."""
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace and pix.colorspace.n > 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if smask:
        pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
    return pix


def _orientation(matrix) -> list[str]:
    """The Pillow transposes that turn the stored image the way the page
    draws it. An image is stored however its producer wrote it and the
    page's matrix puts it right: the Evidence Act's diagram is stored
    upside down and flipped back as it is drawn (a negative d), and
    copied out as stored it was upside down on the site."""
    if not matrix:
        return []
    a, b, c, d = matrix[:4]
    ops = []
    if abs(b) > abs(a):
        # Turned a quarter: the image's rows run down the page.
        ops.append("TRANSPOSE")
        a, d = c, b
    if a < 0:
        ops.append("FLIP_LEFT_RIGHT")
    if d < 0:
        ops.append("FLIP_TOP_BOTTOM")
    return ops


def _image(pixmap, matrix=None):
    """The figure as Pillow sees it: on white, the right way up."""
    from PIL import Image

    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    if image.mode in ("RGBA", "LA"):
        white = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(white, image.convert("RGBA")).convert("RGB")
    for op in _orientation(matrix):
        image = image.transpose(getattr(Image.Transpose, op))
    return image


def store(pixmap, figures_dir=None, matrix=None) -> str:
    """Writes the image as <sha12>.png and <sha12>.webp and returns the
    reference text for its node. Named by its pixels, so the same chart in
    every reprint of a work is one pair of files."""
    figures_dir = figures_dir or FIGURES_DIR
    figures_dir.mkdir(parents=True, exist_ok=True)
    image = _image(pixmap, matrix)
    sha = hashlib.sha256(f"{image.width}x{image.height}{image.mode}".encode() + image.tobytes()).hexdigest()[:12]
    png_path, webp_path = figures_dir / f"{sha}.png", figures_dir / f"{sha}.webp"
    if not png_path.exists() or not webp_path.exists():
        buf = io.BytesIO()
        # Lossless: these are line art and small type, which lossy WebP
        # smears.
        image.save(buf, "WEBP", lossless=True, method=6)
        webp_path.write_bytes(buf.getvalue())
        # Whichever encoder does best on this image: none always does.
        candidates = []
        if not _orientation(matrix) and not pixmap.alpha:
            candidates.append(pixmap.tobytes("png"))
        for options in ({"optimize": True}, {"compress_level": 9}):
            buf = io.BytesIO()
            image.save(buf, "PNG", **options)
            candidates.append(buf.getvalue())
        png_path.write_bytes(min(candidates, key=len))
    return f"{sha} {image.width}x{image.height}"


# What a chart's own heading starts with. A first line that doesn't is
# the chart's first box ("THE EVIDENCE IS ADMISSIBLE"), not a title, and
# a wrong heading is worse than none: the description falls back to the
# provision's title.
_HEADING_RE = re.compile(r"^(?:flow\s*chart|chart|figure|diagram)\b", re.I)
_OCR = None


def _ocr():
    """RapidOCR, loaded once, or None where it is not installed: a parse
    without it is the same parse, with headings left to the reviewer."""
    global _OCR
    if _OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        except ImportError as e:
            # Absent, or present without what OpenCV needs (libGL on a
            # bare server): said once, and the parse goes on.
            print(f"  ! Chart headings are not read: OCR is unavailable ({e})")
            _OCR = False
    return _OCR or None


def read_heading(image, vocabulary: "set[str] | None" = None, ocr=None) -> "str | None":
    """The chart's own heading, from the first line of type in its top
    fifth, or None."""
    ocr = ocr or _ocr()
    if ocr is None:
        return None
    import numpy as np

    top = image.crop((0, 0, image.width, max(1, image.height // 5))).convert("RGB")
    boxes, _elapsed = ocr(np.array(top))
    boxes = [b for b in boxes or [] if b[2] >= 0.8]
    if not boxes:
        return None
    first = min(min(p[1] for p in b[0]) for b in boxes)
    line_height = max(max(p[1] for p in b[0]) - min(p[1] for p in b[0]) for b in boxes)
    line = sorted((b for b in boxes if min(p[1] for p in b[0]) - first < line_height * 0.6),
                  key=lambda b: min(p[0] for p in b[0]))
    text = tidy_heading(" ".join(b[1] for b in line), vocabulary or set())
    return text if _HEADING_RE.match(text) else None


def tidy_heading(text: str, vocabulary: set) -> str:
    """OCR's reading made to read as printed. It drops spaces
    ("Terrorismrecord orterrorismrisk"), which are put back where the
    Act's own words fit exactly, and it reads the dash after "Flow
    Chart 1" as a hyphen."""
    text = re.sub(r"\bflow\s*chart\s*(\d+)\s*[-–—]\s*", r"Flow Chart \1 – ", text, flags=re.I)
    return " ".join(_respace(w, vocabulary) for w in text.split())


def _respace(token: str, vocabulary: set) -> str:
    word = token.lower()
    if len(word) < 6 or not word.isalpha() or word in vocabulary:
        return token
    # Fewest words that spell it, each one the Act uses.
    best: list = [[]] + [None] * len(word)
    for end in range(1, len(word) + 1):
        for start in range(max(0, end - 20), end):
            piece = word[start:end]
            if best[start] is not None and piece in vocabulary and (len(piece) > 1 or piece in ("a", "i")):
                if best[end] is None or len(best[start]) + 1 < len(best[end]):
                    best[end] = best[start] + [(start, end)]
    if not best[-1] or len(best[-1]) == 1:
        return token
    return " ".join(token[s:e] for s, e in best[-1])


def vocabulary(nodes: list[dict]) -> set:
    return {w.lower() for n in nodes for w in re.findall(r"[A-Za-z]+", f"{n.get('heading') or ''} {n.get('text') or ''}")}


def place_figures(nodes: list[dict], figures: list[dict]) -> list[dict]:
    """`nodes` with a figure node after whatever printed above each
    figure: the last node with a line above it on its page, or, for a
    figure at the top of a page, the last node on an earlier one.

    Each figure is {"page", "bbox", "text"}; "text" is store()'s
    reference."""
    after: dict[int, list[dict]] = {}
    for fig in figures:
        page, top = fig["page"], fig["bbox"][1]
        anchor = -1
        for i, node in enumerate(nodes):
            for r in node.get("rects") or ():
                if r["page"] < page or (r["page"] == page and r["y0"] <= top):
                    anchor = i
        x0, y0, x1, y1 = (round(v, 1) for v in fig["bbox"])
        node = {
            "type": "figure", "number": None, "heading": fig.get("heading"), "text": fig["text"],
            "page_start": page, "page_end": page,
            "rects": [{"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1}],
        }
        # Where it sits is where the node before it sits: a figure is no
        # level of its own (tree.annotate_paths gives it the same).
        if anchor >= 0 and "path" in nodes[anchor]:
            node["path"] = dict(nodes[anchor]["path"])
        after.setdefault(anchor, []).append(node)
    out = list(after.get(-1, []))
    for i, node in enumerate(nodes):
        out.append(node)
        out.extend(after.get(i, []))
    return out


def extract_figures(nodes: list[dict], pdf_path, first_page: int, last_page: int,
                    figures_dir=None) -> tuple[list[dict], list[int]]:
    """The pipeline's step: (nodes with their figures in place, the pages
    figures were found on)."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("  ! Pillow is not installed, so charts are left out (pip install -r requirements.txt)")
        return nodes, []
    found = find_figures(pdf_path, first_page, last_page)
    words = vocabulary(nodes) if found else set()
    for fig in found:
        pixmap, matrix = fig.pop("pixmap"), fig.pop("matrix")
        fig["text"] = store(pixmap, figures_dir, matrix)
        fig["heading"] = read_heading(_image(pixmap, matrix), words)
    return place_figures(nodes, found), [f["page"] for f in found]


_NAME_RE = re.compile(r"^[0-9a-f]{12}\.(png|webp)$")


def figure_response(name: str, figures_dir=None):
    """One stored figure, for every server that shows pages. Kept a year
    without asking: the name is the content's hash, so a different chart
    is a different URL."""
    # Here, not at the top: the exporters read figure_ref without a server.
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    m = _NAME_RE.match(name)
    path = (figures_dir or FIGURES_DIR) / name
    if not m or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type=f"image/{m.group(1)}",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})
