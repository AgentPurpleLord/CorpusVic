"""Charts printed as images: found in the PDF, stored as WebP and PNG,
placed in the parse, and shown on the page (corpus/parsing/figures.py)."""
import io
import re
import xml.etree.ElementTree as ET

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from corpus.exporters.akn_export import render_tree_node
from corpus.exporters.markdown_export import _iter_body_units
from corpus.parsing import figures
from corpus.parsing.figures import extract_figures, figure_ref, find_figures, place_figures, store
from corpus.parsing.identity import annotate_ids
from corpus.parsing.tree import annotate_paths
from corpus.publishing.html_view import render_index, render_section

from conftest import make_node


@pytest.fixture
def figures_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(figures, "FIGURES_DIR", tmp_path / "figures")
    return tmp_path / "figures"


def _chart_png(width=120, height=90) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    for x in range(width):
        image.putpixel((x, height // 2), (0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def _pdf_with_images(path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "3D Flow charts")
    page.insert_image(pymupdf.Rect(150, 200, 450, 425), stream=_chart_png())
    # A speck, not a chart.
    page.insert_image(pymupdf.Rect(500, 700, 510, 710), stream=_chart_png(8, 8))
    doc.save(str(path))


def _at(page, y0, y1, **node):
    return {**node, "rects": [{"page": page, "x0": 72, "y0": y0, "x1": 500, "y1": y1}]}


def test_find_figures_takes_charts_and_leaves_specks(tmp_path):
    _pdf_with_images(tmp_path / "act.pdf")

    found = find_figures(tmp_path / "act.pdf", 1, 1)

    assert len(found) == 1
    assert found[0]["page"] == 1
    assert [round(v) for v in found[0]["bbox"]] == [150, 200, 450, 425]
    assert (found[0]["pixmap"].width, found[0]["pixmap"].height) == (120, 90)


def test_store_writes_webp_and_png_of_the_same_pixels_once(tmp_path, figures_dir):
    _pdf_with_images(tmp_path / "act.pdf")
    pixmap = find_figures(tmp_path / "act.pdf", 1, 1)[0]["pixmap"]

    ref = store(pixmap)
    sha = ref.split()[0]

    assert re.fullmatch(r"[0-9a-f]{12} 120x90", ref)
    webp, png = Image.open(figures_dir / f"{sha}.webp"), Image.open(figures_dir / f"{sha}.png")
    assert webp.format == "WEBP" and png.format == "PNG"
    assert webp.convert("RGB").tobytes() == png.convert("RGB").tobytes()
    # The same chart in the next reprint is the same pair of files.
    mtime = (figures_dir / f"{sha}.png").stat().st_mtime_ns
    assert store(pixmap) == ref
    assert (figures_dir / f"{sha}.png").stat().st_mtime_ns == mtime


def test_a_figure_goes_after_what_printed_above_it():
    nodes = [
        _at(1, 90, 110, type="section", number="3D", text="Flow charts"),
        _at(1, 120, 180, type="subsection", number="1", text="Flow Chart 1 shows..."),
        _at(1, 450, 470, type="subsection", number="2", text="Flow Chart 2 shows..."),
        _at(2, 300, 320, type="subsection", number="3", text="After the chart."),
    ]
    figs = [
        {"page": 1, "bbox": (150, 200, 450, 425), "text": "aaaaaaaaaaaa 120x90"},
        # Top of page 2: belongs to what came before it.
        {"page": 2, "bbox": (150, 60, 450, 280), "text": "bbbbbbbbbbbb 120x90"},
    ]

    placed = place_figures(nodes, figs)

    assert [n.get("number") or n["text"][:4] for n in placed] == ["3D", "1", "aaaa", "2", "bbbb", "3"]
    assert placed[2]["rects"] == [{"page": 1, "x0": 150, "y0": 200, "x1": 450, "y1": 425}]


def test_two_figures_in_one_section_have_names_of_their_own():
    nodes = [
        _at(1, 90, 110, type="section", number="3D", heading="Flow charts", text=""),
        _at(1, 120, 180, type="subsection", number="1", text="Flow Chart 1 shows..."),
    ]
    annotate_paths(nodes)
    placed = place_figures(nodes, [
        {"page": 1, "bbox": (150, 200, 450, 425), "text": "aaaaaaaaaaaa 120x90"},
        {"page": 1, "bbox": (150, 450, 450, 700), "text": "bbbbbbbbbbbb 120x90"},
    ])
    annotate_ids(placed)

    names = [n["id"] for n in placed if n["type"] == "figure"]
    assert len(set(names)) == 2
    assert all(name.startswith("s3d/1/figure~") for name in names)


def test_extract_figures_puts_the_pdfs_chart_in_the_parse(tmp_path, figures_dir):
    _pdf_with_images(tmp_path / "act.pdf")
    nodes = [_at(1, 90, 110, type="section", number="3D", text="Flow charts")]

    placed, pages = extract_figures(nodes, tmp_path / "act.pdf", 1, 1)

    assert pages == [1]
    assert figure_ref(placed[1])["width"] == 120
    assert (figures_dir / f"{figure_ref(placed[1])['src']}.webp").exists()


def _act_with_chart(sha="0123456789ab"):
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "3D", "Bail decision flow charts", "Flow Chart 1 shows the process."),
        make_node("figure", None, None, f"{sha} 620x727"),
        make_node("section", "3E", "Next", "Something else."),
    ]


def test_the_page_shows_a_chart_as_webp_with_a_png_fallback(figures_dir):
    figures_dir.mkdir(parents=True)
    (figures_dir / "0123456789ab.webp").write_bytes(b"x")

    body = render_section({"nodes": _act_with_chart(), "hierarchy": None},
                          "Bail Act 1977", "/root/browse/bail-act", "s3d")

    picture = re.search(r"<picture>.*?</picture>", body, re.S).group(0)
    assert '<source type="image/webp" srcset="/root/figures/0123456789ab.webp">' in picture
    assert 'src="/root/figures/0123456789ab.png"' in picture
    assert 'width="620" height="727"' in picture
    assert 'loading="lazy"' in picture
    # Described by the provision it belongs to.
    assert 'alt="Chart in section 3D Bail decision flow charts"' in picture
    # Its reference is not printed as words.
    assert "620x727" not in body.replace('width="620" height="727"', "")


def test_without_a_webp_only_the_png_is_offered(figures_dir):
    body = render_section({"nodes": _act_with_chart(), "hierarchy": None},
                          "Bail Act 1977", "/browse/bail-act", "s3d")

    assert "<source" not in body
    assert 'src="/figures/0123456789ab.png"' in body


def test_a_chart_under_a_chapter_heading_is_on_the_contents_page(figures_dir):
    nodes = [
        make_node("chapter", "3", "Admissibility of evidence", "This diagram shows how this Chapter applies"),
        make_node("figure", None, None, "0123456789ab 1385x2014"),
        make_node("part", "3.1", "Relevance"),
        make_node("section", "55", "Relevant evidence", "Evidence that is relevant..."),
    ]

    body = render_index({"nodes": nodes, "hierarchy": None}, "Evidence Act 2008", "/browse/evidence-act")

    assert 'src="/figures/0123456789ab.png"' in body
    assert 'alt="Chart in Chapter 3 - Admissibility of evidence"' in body


def test_exports_point_at_the_png():
    node = make_node("figure", None, None, "0123456789ab 620x727")

    akn = ET.tostring(render_tree_node({"node": node, "eid": "fig_1", "children": []}), encoding="unicode")
    assert 'src="/figures/0123456789ab.png"' in akn
    units = list(_iter_body_units({"node": node, "eid": "fig_1", "children": []}, _is_page_root=False))
    assert units[0]["text"] == "![Chart](/figures/0123456789ab.png)"


def test_figures_are_served_for_a_year_and_nothing_else_is(figures_dir):
    from corpus.web import public

    figures_dir.mkdir(parents=True)
    (figures_dir / "0123456789ab.webp").write_bytes(b"RIFF")
    client = TestClient(public.app)

    ok = client.get("/figures/0123456789ab.webp")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/webp"
    assert ok.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/figures/0123456789ab.png").status_code == 404
    assert client.get("/figures/..%2Fsecret.png").status_code == 404
    assert client.get("/figures/0123456789ab.svg").status_code == 404


def test_the_archive_copies_the_figures_its_documents_print(tmp_path, figures_dir, monkeypatch):
    from corpus.web import dashboard, public

    figures_dir.mkdir(parents=True)
    for name in ("0123456789ab.png", "0123456789ab.webp", "ffffffffffff.png"):
        (figures_dir / name).write_bytes(b"x")
    monkeypatch.setattr(dashboard, "_current_nodes", lambda slug: (_act_with_chart(), [], None))

    assert public._copy_figures(tmp_path / "_site", ["bail-act"]) == 2
    assert sorted(p.name for p in (tmp_path / "_site" / "figures").iterdir()) == \
        ["0123456789ab.png", "0123456789ab.webp"]
