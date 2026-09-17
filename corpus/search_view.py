"""
The search form and its results, as HTML.

The public site renders them. It had a second caller -- the admin tool --
and the two differed over where a result points, which cost this module
two parameters and cost the tool a page of results that looked right and
all led to 404s. The admin search has since been removed, so the
addresses are now composed the one way the index stores them.

A result's address is still built here from the parts rather than taken
ready-made off the hit, because the hit's own `href` is a convenience and
composing it is the thing that has to stay correct.

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


def _address(hit: dict) -> str:
    """Where one result points.

    `site_slug` and not `slug`: the site serves an Act's newest reprint at
    the work's own name ("criminal-procedure-act"), while the index also
    records the parse's own ("criminal-procedure-act-v114"). Addressing a
    result by the second is a 404 on a page of results that otherwise
    look right."""
    from .search import address_of

    return address_of(hit["site_slug"], hit["page"], hit.get("fragment") or "")


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
                 offset: int, action: str) -> str:
    """The results, or the reason there are none."""
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
        href = html.escape(_address(hit), quote=True)
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
              action: str, unavailable=Exception) -> str:
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
                                     max(offset, 0), action))
    return "".join(body)


def wants_superseded(value: str) -> bool:
    """What the checkbox sends, in the handful of forms a browser or a
    hand-typed URL might send it as."""
    return (value or "").lower() in ("1", "on", "true", "yes")
