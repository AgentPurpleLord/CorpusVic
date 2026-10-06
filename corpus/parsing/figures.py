"""The charts and diagrams an Act prints, as figure nodes.

A chart in an Act PDF is an embedded image with no text in it (the Bail
Act's flowcharts after s 3D, the Evidence Act's Chapter 3 diagram), so
the line parser never sees it and the parse used to drop it without a
word. Here each one is lifted out of the PDF losslessly, stored once
under its content hash as WebP with a PNG fallback, and put in the node
list where it printed.

The node's text is its reference, "<sha12> <width>x<height>", not a
field of its own: a reviewed provision is stored as a row of text columns
(see the verified table in corpus/storage/db.py), and anything beside
the text would be lost the moment a reviewer approved it. As text it
survives review, carry-forward and the JSONL sync as it is, and a
reprint that changes a chart changes the provision.
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
    """[{page, bbox, pixmap}] for every chart-sized image on pages
    first_page..last_page (1-indexed, inclusive), in reading order."""
    import pymupdf

    found = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page_no in range(first_page, min(last_page, doc.page_count) + 1):
            page = doc[page_no - 1]
            for info in page.get_images(full=True):
                xref, smask = info[0], info[1]
                for rect in page.get_image_rects(xref):
                    if rect.width < MIN_SIDE_PT or rect.height < MIN_SIDE_PT:
                        continue
                    found.append({"page": page_no, "bbox": (rect.x0, rect.y0, rect.x1, rect.y1),
                                  "pixmap": _pixmap(pymupdf, doc, xref, smask)})
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


def store(pixmap, figures_dir=None) -> str:
    """Writes the image as <sha12>.png and <sha12>.webp and returns the
    reference text for its node. Named by its pixels, so the same chart in
    every reprint of a work is one pair of files."""
    figures_dir = figures_dir or FIGURES_DIR
    figures_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(f"{pixmap.width}x{pixmap.height}x{pixmap.n}".encode() + pixmap.samples).hexdigest()[:12]
    png_path, webp_path = figures_dir / f"{sha}.png", figures_dir / f"{sha}.webp"
    if not png_path.exists() or not webp_path.exists():
        png = pixmap.tobytes("png")
        try:
            from PIL import Image
        except ImportError:
            # PNG alone still shows everywhere; the page only offers WebP
            # where the file exists (see html_view._figure_html).
            print("  ! Pillow is not installed, so figures are stored as PNG only (pip install Pillow)")
            png_path.write_bytes(png)
        else:
            image = Image.open(io.BytesIO(png))
            candidates = [png]
            if image.mode in ("RGBA", "LA"):
                white = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(white, image.convert("RGBA")).convert("RGB")
                candidates = []
            buf = io.BytesIO()
            # Lossless: these are line art and small type, which lossy
            # WebP smears.
            image.save(buf, "WEBP", lossless=True, method=6)
            webp_path.write_bytes(buf.getvalue())
            # Whichever encoder does best on this image: none always does.
            for options in ({"optimize": True}, {"compress_level": 9}):
                buf = io.BytesIO()
                image.save(buf, "PNG", **options)
                candidates.append(buf.getvalue())
            png_path.write_bytes(min(candidates, key=len))
    return f"{sha} {pixmap.width}x{pixmap.height}"


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
            "type": "figure", "number": None, "heading": None, "text": fig["text"],
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
    found = find_figures(pdf_path, first_page, last_page)
    for fig in found:
        fig["text"] = store(fig.pop("pixmap"), figures_dir)
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
