"""
The search form and its results, as HTML.

Two things render them: the public site and the admin tool. One copy,
because two copies of a results page is exactly the drift corpus/reader.py
was written to stop -- the same renderers, edited on different days for
different reasons, quietly diverging.

Two things differ between the callers, and both are about where a result
points. The admin tool is served under a path prefix and the public site
is not, so `base` goes in front of every address -- the same job
`rewrite` does in corpus/reader.py. And the two disagree about what to
call a document: the public site serves an Act's newest reprint at the
work's own name, the admin tool serves every parse under its own, so
`slug_key` picks which. Getting the second one wrong produces a page of
results that look right and all lead to 404s, which is why the address is
composed here from the parts rather than taken ready-made off the hit.

Nothing here knows how to search. It is handed what corpus/search.py
returned and turns it into a page; that separation is what lets the whole
of it be tested without an index.
"""
import html
from urllib.parse import urlencode

# How many results a page holds. Here rather than in the caller because
# the pager's arithmetic has to agree with it, and two places that have
# to agree is one too many.
PAGE_SIZE = 20


def _esc(value) -> str:
    return html.escape(str(value or ""))


def _address(hit: dict, slug_key: str) -> str:
    """Where one result points, before any prefix.

    Composed from the parts rather than taken from the hit's own `href`,
    because that one is written for the public site and there is a second
    caller that serves the same document under a different name. A result
    row that carries a ready-made address invites exactly one mistake:
    using it where it does not apply."""
    from .search import address_of

    return address_of(hit[slug_key], hit["page"], hit.get("fragment") or "")


def form_html(action: str, query: str, include_superseded: bool) -> str:
    """The search form on the results page itself.

    A plain GET form. No script, no fetch, no JSON -- a reference work
    about the law should still be searchable in a browser with
    JavaScript turned off, and making that the baseline costs nothing."""
    checked = " checked" if include_superseded else ""
    return (
        "<h1>Search</h1>"
        f"<form class='search-page-form' action='{html.escape(action, quote=True)}' "
        "method='get' role='search'>"
        f"<input type='search' name='q' value='{html.escape(query or '', quote=True)}' "
        "placeholder='Search the corpus' autofocus>"
        f"<label><input type='checkbox' name='superseded' value='1'{checked}> "
        "Include superseded reprints</label>"
        "<button type='submit'>Search</button>"
        "</form>"
    )


def results_html(found: dict, query: str, include_superseded: bool,
                 offset: int, action: str, base: str = "",
                 slug_key: str = "site_slug") -> str:
    """The results, or the reason there are none.

    Two things decide where a result points, and they are separate.

    `base` is the path prefix this is being served under -- "" for the
    public site, "/admin" for the dashboard.

    `slug_key` is which of the document's two names to address it by, and
    getting it wrong is the failure this argument exists to prevent. The
    public site serves an Act's newest reprint at the work's own name
    ("criminal-procedure-act"); the admin tool serves every parse under
    its own ("criminal-procedure-act-v114"). A link built with the wrong
    one is a 404 on a page full of results that otherwise look right."""
    if found.get("error"):
        return "<p class='search-empty'>That search could not be read. Try plainer words.</p>"
    if not found["total"]:
        return f"<p class='search-empty'>Nothing matches <strong>{_esc(query)}</strong>.</p>"

    out = [corrections_html(found.get("corrections")),
           f"<p class='search-count'><strong>{found['total']}</strong> provision(s) match "
           f"<strong>{_esc(query)}</strong>.</p>",
           "<ol class='search-results'>"]
    for hit in found["results"]:
        version = "" if hit["is_current"] else " <span class='search-superseded'>superseded</span>"
        as_at = f" &middot; as at {_esc(hit['as_at'])}" if hit["as_at"] else ""
        crumb = (f"<div class='search-crumb'>{_esc(hit['breadcrumb'])}</div>"
                 if hit["breadcrumb"] else "")
        href = html.escape(base + _address(hit, slug_key), quote=True)
        out.append(
            f"<li><a href='{href}'>{_esc(hit['label'])}</a>"
            f"<div class='search-doc'>{_esc(hit['title'])}{as_at}{version}</div>"
            f"{crumb}"
            # Already HTML: corpus/search.py escaped the provision's text
            # before turning sqlite's marks into <mark> tags. Escaping it
            # again here would publish the tags as visible text.
            + (f"<div class='search-snippet'>{hit['snippet_html']}</div>"
               if hit["snippet_html"] else "")
            + "</li>"
        )
    out.append("</ol>")
    out.append(pager_html(found, query, include_superseded, offset, action))
    return "".join(out)


def corrections_html(corrections: "dict | None") -> str:
    """"Showing results for ...", when a word was read as another.

    Only ever fires on a word the corpus does not contain anywhere, so
    it never appears for a query that was already precise -- and saying
    so matters, because a reader who typed a term of art needs to know
    the search quietly read it as something else."""
    if not corrections:
        return ""
    pairs = ", ".join(f"<strong>{_esc(fixed)}</strong> for {_esc(typed)}"
                      for typed, fixed in sorted(corrections.items()))
    return f"<p class='search-corrected'>Showing results for {pairs}.</p>"


def pager_html(found: dict, query: str, include_superseded: bool,
               offset: int, action: str) -> str:
    """Plain links, because a page of search results is a page and the
    back button should mean what it says."""
    def link(params: dict, label: str) -> str:
        if include_superseded:
            params["superseded"] = "1"
        # Escaped whole, separators included. A bare "&" between query
        # parameters is invalid in an attribute and is read as the start
        # of an entity -- harmless with today's parameter names, and a
        # trap the moment one of them begins with the name of one.
        href = html.escape(f"{action}?{urlencode(params)}", quote=True)
        return f"<a href='{href}'>{label}</a>"

    links = []
    if offset > 0:
        links.append(link({"q": query, "offset": max(offset - PAGE_SIZE, 0)},
                          "&larr; Previous"))
    if found["truncated"]:
        links.append(link({"q": query, "offset": offset + PAGE_SIZE}, "Next &rarr;"))
    return f"<nav class='search-pager'>{' '.join(links)}</nav>" if links else ""


def unavailable_html(reason: str) -> str:
    """No index yet. An ordinary state on a fresh server rather than a
    fault, so it reads as one."""
    return f"<p class='search-empty'>{_esc(reason)}</p>"


def page_body(index, query: str, include_superseded: bool, offset: int,
              action: str, base: str = "", unavailable=Exception,
              slug_key: str = "site_slug") -> str:
    """The whole body of a search page: the form, and whatever answering
    the query produced.

    `index` is a corpus.search.Index (or anything with its `search`), and
    `unavailable` is the exception it raises when there is nothing to
    read -- passed in so that this module needs no import of its own."""
    body = [form_html(action, query, include_superseded)]
    if (query or "").strip():
        try:
            found = index.search(query, include_superseded=include_superseded,
                                 limit=PAGE_SIZE, offset=max(offset, 0))
        except unavailable as e:
            body.append(unavailable_html(str(e)))
        else:
            body.append(results_html(found, query, include_superseded,
                                     max(offset, 0), action, base, slug_key))
    return "".join(body)


def wants_superseded(value: str) -> bool:
    """What the checkbox sends, in the handful of forms a browser or a
    hand-typed URL might send it as."""
    return (value or "").lower() in ("1", "on", "true", "yes")
