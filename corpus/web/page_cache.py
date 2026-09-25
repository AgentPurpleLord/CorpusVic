"""The public site's pages, kept as last rendered.

Rendering a page of a work with many versions means its history: every
version put back together and compared, a minute for the Criminal
Procedure Act's hundred-odd reprints. And every review edit, in any Act,
makes that stale (the review database is one file). So a reader is never
made to wait for it: a page is served as last rendered, from a small
SQLite file that outlives a restart, and one that is out of date is
rendered again behind it for the next reader.

Behind it means in another process (work(), started by corpus/web/
public.py): rendered in a thread of the one serving, a history being
worked out made every page served meanwhile eight times slower. Only a
page never rendered at all is rendered while its reader waits, and the
worker renders those ahead of anyone asking (fill()).
"""
import hashlib
import sqlite3
import threading
import time
import traceback
import zlib
from pathlib import Path

from fastapi import HTTPException


class PageCache:
    def __init__(self, path: Path, stamp, min_refresh: float = 10.0):
        """`stamp()` says what the site's data is now; a page rendered
        against another is stale. `min_refresh` is the fewest seconds
        between two renders of one page: while a reviewer works every page
        is stale again within seconds, and rendering it on every visit
        would be the cost this exists to avoid."""
        self.path = Path(path)
        self.stamp = stamp
        self.min_refresh = min_refresh
        # Set to the worker process once one is running (see work()); until
        # then, and if it dies, stale pages are rendered in a thread here.
        self.worker = None
        # One page is rendered at a time: rendering shares the dashboard's
        # caches, which were never meant for two threads at once.
        self.render_lock = threading.RLock()
        self._db_lock = threading.Lock()
        self._conn = None
        self._pending: dict = {}
        self._last: dict = {}
        self._wake = threading.Event()
        self._thread = None

    # -- storage -------------------------------------------------------------

    def _db(self):
        if self._conn is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = self._open(str(self.path))
            except (OSError, sqlite3.Error):
                # Somewhere it cannot write: kept for this run only, which
                # is slower after a restart but never an error page.
                self._conn = self._open(":memory:")
        return self._conn

    @staticmethod
    def _open(where: str):
        conn = sqlite3.connect(where, check_same_thread=False, timeout=10)
        if where != ":memory:":
            # Read by the server while the worker writes.
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, stamp TEXT, "
                     "etag TEXT, body BLOB, built_at REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS wanted (url TEXT PRIMARY KEY, at REAL)")
        conn.commit()
        return conn

    def read(self, url: str) -> "tuple[str, str, str] | None":
        """(stamp, etag, html) as last rendered, or None."""
        with self._db_lock:
            row = self._db().execute("SELECT stamp, etag, body FROM pages WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        return row[0], row[1], zlib.decompress(row[2]).decode("utf-8")

    def _write(self, url: str, stamp: str, html: str) -> str:
        etag = hashlib.sha1(html.encode("utf-8")).hexdigest()[:16]
        with self._db_lock, self._db():
            self._db().execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?)",
                               (url, stamp, etag, zlib.compress(html.encode("utf-8"), 6), time.time()))
        return etag

    def forget(self, url: str) -> None:
        with self._db_lock, self._db():
            self._db().execute("DELETE FROM pages WHERE url = ?", (url,))

    # -- serving -------------------------------------------------------------

    def get(self, url: str, render) -> tuple[str, str]:
        """(html, etag) for `url`: as last rendered if there is one --
        rendered again behind it if stale -- else rendered now.
        `render()` returns the page's HTML, or raises HTTPException."""
        stamp = self.stamp()
        hit = self.read(url)
        if hit is not None:
            if hit[0] != stamp:
                self._schedule(url, render)
            return hit[2], hit[1]
        with self.render_lock:
            html = render()
        return html, self._write(url, stamp, html)

    def _schedule(self, url: str, render) -> None:
        if time.monotonic() - self._last.get(url, -1e9) < self.min_refresh:
            return
        self._last[url] = time.monotonic()
        if self.worker is not None and self.worker.is_alive():
            with self._db_lock, self._db():
                self._db().execute("INSERT OR REPLACE INTO wanted VALUES (?, ?)", (url, time.time()))
            return
        self._pending[url] = render
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="page-cache", daemon=True)
            self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=60)
            self._wake.clear()
            while self._pending:
                url, render = self._pending.popitem()
                self.refresh(url, render)

    def refresh(self, url: str, render, missing_only: bool = False) -> None:
        """Renders `url` again if it is stale or missing. A page that is no
        longer there -- a provision gone in a re-parse, a work taken off
        the site -- is dropped, not kept as it was."""
        stamp = self.stamp()
        hit = self.read(url)
        if hit is not None and (hit[0] == stamp or missing_only):
            return
        try:
            with self.render_lock:
                html = render()
        except HTTPException:
            self.forget(url)
            return
        except Exception:
            traceback.print_exc()
            return
        self._write(url, stamp, html)

    def fill(self, pages) -> None:
        """Every (url, render) in `pages` never rendered, rendered, in the
        order given, most read first. Not the stale ones: while a reviewer
        works that is every page, over and over, and a stale page is
        rendered again when it is next read."""
        for url, render in pages:
            self._take_wanted(render_url=None)
            self.refresh(url, render, missing_only=True)

    # -- the worker process --------------------------------------------------

    def work(self, render_url, pages, every: float = 600.0) -> None:
        """The worker's loop: the pages readers found stale, as they come,
        and every `every` seconds the pages worth having ready (`pages()`,
        see fill) that are not yet. `render_url(url)` renders any page by
        its URL. Never returns."""
        self._render_url = render_url
        next_fill = 0.0
        while True:
            if time.monotonic() >= next_fill:
                try:
                    self.fill(pages())
                except Exception:
                    traceback.print_exc()
                next_fill = time.monotonic() + every
            if not self._take_wanted(render_url):
                time.sleep(0.5)

    def _take_wanted(self, render_url) -> int:
        """Renders what readers found stale, first. Returns how many."""
        render_url = render_url or getattr(self, "_render_url", None)
        if render_url is None:
            return 0
        with self._db_lock, self._db():
            urls = [r[0] for r in self._db().execute("SELECT url FROM wanted ORDER BY at")]
            self._db().execute("DELETE FROM wanted")
        for url in urls:
            self.refresh(url, lambda u=url: render_url(u))
        return len(urls)
