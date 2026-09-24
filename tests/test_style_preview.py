"""The style preview's frozen pages (corpus/publishing/style_preview.py):
the capture itself needs a browser, so what is pinned here is what
freezing does to a captured page."""
from corpus.publishing.style_preview import freeze, static_path


def test_stylesheets_are_found_where_each_app_serves_them():
    assert static_path("/assets/page.css", "http://site.preview/browse/x/") == "static/site/page.css"
    assert static_path("../../static/admin/teaching.css", "http://dash.preview/teaching/act/") \
        == "static/admin/teaching.css"
    assert static_path("https://fonts.example/x.css", "http://site.preview/") is None


def test_a_frozen_page_styles_itself_from_static_and_runs_nothing_of_its_own():
    captured = ('<html><head><link rel="stylesheet" href="static/admin/review.css">'
                '<script>fetch("api/meta")</script></head><body class="x"><p>text</p>'
                '<script src="app.js"></script></body></html>')

    frozen = freeze(captured, "http://review.preview/", "admin/review")

    assert 'href="../../static/admin/review.css"' in frozen
    assert "fetch(" not in frozen and "app.js" not in frozen
    assert '<body class="x">\n<div id="style-preview-bar"' in frozen and "admin/review" in frozen
    assert frozen.index("stylePreviewTheme") < frozen.index("</head>"), "theme set before first paint"
