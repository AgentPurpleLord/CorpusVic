"""corpus/web/page_cache.py: a public page is served as last rendered,
and one gone stale is rendered again behind the reader, not before."""
import pytest
from fastapi import HTTPException

from corpus.web.page_cache import PageCache


def _cache(tmp_path, stamp):
    return PageCache(tmp_path / "pages.sqlite", lambda: stamp[0], min_refresh=0)


def test_a_page_is_rendered_once_and_kept_across_a_restart(tmp_path):
    stamp, renders = ["a"], []
    cache = _cache(tmp_path, stamp)
    render = lambda: renders.append(1) or "<p>one</p>"

    assert cache.get("/x", render)[0] == "<p>one</p>"
    assert cache.get("/x", render)[0] == "<p>one</p>"
    assert _cache(tmp_path, stamp).get("/x", render)[0] == "<p>one</p>", "a new process reads it too"
    assert len(renders) == 1


def test_a_stale_page_is_served_at_once_and_rendered_again_behind_it(tmp_path):
    """Rendering a page of a work with a hundred versions takes a minute;
    no reader waits for it after the first."""
    stamp = ["a"]
    cache = _cache(tmp_path, stamp)
    cache.get("/x", lambda: "old")
    stamp[0] = "b"
    queued = []
    cache._schedule = lambda url, render: queued.append((url, render))

    assert cache.get("/x", lambda: "new")[0] == "old"
    [(url, render)] = queued
    cache.refresh(url, render)
    assert cache.get("/x", lambda: pytest.fail("fresh now"))[0] == "new"


def test_a_page_no_longer_there_is_dropped_not_kept(tmp_path):
    stamp = ["a"]
    cache = _cache(tmp_path, stamp)
    cache.get("/x", lambda: "a provision")
    stamp[0] = "b"

    def gone():
        raise HTTPException(404, "re-parsed away")

    cache.refresh("/x", gone)
    assert cache.read("/x") is None


def test_filling_renders_only_what_was_never_rendered(tmp_path):
    """While someone reviews, every page is stale within seconds: the
    warmer rendering them all again would never stop."""
    stamp = ["a"]
    cache = _cache(tmp_path, stamp)
    cache.get("/kept", lambda: "kept")
    stamp[0] = "b"
    rendered = []
    cache.fill([("/kept", lambda: rendered.append("/kept") or "x"), ("/new", lambda: rendered.append("/new") or "y")])

    assert rendered == ["/new"]


def test_the_public_site_serves_from_it_and_says_so_to_a_returning_reader(tmp_path, monkeypatch):
    """A published page is rendered once; a reader coming back gets a 304.
    Taking the work off the site is still immediate, cache or not."""
    from fastapi.testclient import TestClient
    from corpus.web import public

    monkeypatch.setattr(public, "BASE_DIR", tmp_path)
    monkeypatch.setattr(public, "_KEY", None)
    published = {"crimes-act": "crimes-act"}
    monkeypatch.setattr(public, "_published_slugs", lambda: published)
    renders = []
    monkeypatch.setattr(public, "_contents_html", lambda site_slug: renders.append(site_slug) or "<p>contents</p>")
    client = TestClient(public.app)

    first = client.get("/browse/crimes-act/")
    again = client.get("/browse/crimes-act/", headers={"If-None-Match": first.headers["etag"]})
    assert first.text == "<p>contents</p>" and again.status_code == 304 and renders == ["crimes-act"]
    published.clear()
    assert client.get("/browse/crimes-act/").status_code == 404


def test_the_public_timeline_may_be_a_few_minutes_behind(monkeypatch):
    """Rebuilt after every review edit, a hundred versions' history was
    most of what the public server did while someone reviewed."""
    from corpus.web import dashboard

    monkeypatch.setattr(dashboard, "_timeline_cache", {})
    monkeypatch.setattr(dashboard, "_work_versions", lambda work: ["act-v1"])
    signature = ["one"]
    monkeypatch.setattr(dashboard, "_work_signature", lambda work: signature[0])
    first = dashboard._timeline("act")
    signature[0] = "two"

    monkeypatch.setattr(dashboard, "TIMELINE_MAX_AGE", 300.0)
    assert dashboard._timeline("act") is first
    monkeypatch.setattr(dashboard, "TIMELINE_MAX_AGE", 0.0)
    assert dashboard._timeline("act") is not first


def test_with_a_worker_a_stale_page_is_left_to_it(tmp_path):
    """Rendered in a thread of the process serving, a history being worked
    out made every page served meanwhile eight times slower."""
    class Alive:
        def is_alive(self):
            return True

    stamp = ["a"]
    server = _cache(tmp_path, stamp)
    server.worker = Alive()
    server.get("/x", lambda: "old")
    stamp[0] = "b"
    assert server.get("/x", lambda: pytest.fail("not rendered here"))[0] == "old"

    worker = _cache(tmp_path, stamp)   # the other process, the same file
    assert worker._take_wanted(lambda url: f"new {url}") == 1
    assert server.read("/x")[2] == "new /x"


def test_every_cached_page_can_be_rendered_again_by_its_url(monkeypatch):
    from corpus.web import public

    monkeypatch.setattr(public, "_landing_html", lambda: "landing")
    monkeypatch.setattr(public, "_contents_html", lambda a: f"contents {a}")
    monkeypatch.setattr(public, "_endnotes_html", lambda a: f"endnotes {a}")
    monkeypatch.setattr(public, "_section_html", lambda a, p: f"section {a} {p}")
    monkeypatch.setattr(public, "_preview_json", lambda a, s, f: f"card {a} {s} {f}")

    assert [public._render_url(u) for u in (
        "/", "/browse/crimes-act/", "/browse/crimes-act/endnotes", "/browse/crimes-act/section/s3",
        "/api/browse/crimes-act/preview?section=s3&fragment=s3-1")] == [
        "landing", "contents crimes-act", "endnotes crimes-act", "section crimes-act s3",
        "card crimes-act s3 s3-1"]
    with pytest.raises(HTTPException):
        public._render_url("/search?q=bail")
