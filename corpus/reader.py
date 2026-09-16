"""
One assembly of each reader page, so the things that serve them cannot
drift apart.

Three callers want the same pages. `dashboard.py` serves them live at
`/browse/*` for a reviewer; `export_static_site.py` writes them to files
for the published archive; and `public.py` serves them live to the world.
Until now the first two each built the pages themselves, from the same
renderers but with their own gathering code either side -- which is a
duplication that only ever gets worse, because the two copies are edited
on different days for different reasons. A third copy would have settled
it.

What varies between them is small and stays explicit here, as keyword
arguments rather than as branches on who is calling:

  - `rewrite`, because the static build publishes an Act's newest version
    at the work's own address and has to rewrite every URL the data hands
    back to match, while a live server serves the address it was asked
    for;
  - `show_review_badge`, because a reviewer wants to see what has been
    checked and a reader is told a different way (below);
  - `notice`, the "nobody has checked this yet" line a published page
    carries above its own heading.

What does not vary is everything else: the same nodes, the same
cross-references, the same amendment history, the same timeline.

`source` is whatever provides the document data -- in practice the
`dashboard` module, which owns those lookups and their caches. Passed in
rather than imported so that this module has no opinion about who is
asking, and so a test can hand it a stand-in.
"""
from . import html_view
from .versions import split_document_slug


def _identity(value):
    return value


def document_title(source, slug: str) -> str:
    """The document's own title, as every page of it shows it."""
    return source._act_title(slug)


def contents_page(source, slug: str, base_url: str, *, rewrite=None,
                  show_review_badge: bool = True, related=None,
                  notice: "str | None" = None) -> str:
    """The document's contents page: its outline, and the way in to
    everything else.

    `related` is the Bill/Act/EM row, already decided by the caller --
    the static build shows only documents it actually published, because
    a link to a page it did not write is a link to a 404, and a live
    server has no such constraint. That difference is real, so it is the
    caller's to make rather than something inferred here."""
    site = rewrite or _identity
    nodes, _unattached, hierarchy = source._current_nodes(slug)
    amendments = source._amendments(slug)
    body = html_view.render_index(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": amendments["endnotes"],
         "version": source._act_version(slug)},
        document_title(source, slug), base_url,
        superseded=site(source._superseded(slug)),
        show_review_badge=show_review_badge,
        related=related,
    )
    return (notice or "") + body


def section_page(source, slug: str, base_url: str, section_slug: str, *,
                 rewrite=None, show_review_badge: bool = True,
                 notice: "str | None" = None) -> "str | None":
    """One provision's page, with everything that hangs off it: the
    commentary a Bill or an Explanatory Memorandum wrote about it, its
    amendment history, and how its wording moved across the versions of
    the Act held here.

    None means there is no such page -- a 404 for a live server, and a
    section the static build skips."""
    site = rewrite or _identity
    nodes, _unattached, hierarchy = source._current_nodes(slug)
    title = document_title(source, slug)
    page_index = source._page_index(slug)

    # Which provision this page is, so its Bill/EM commentary can be
    # looked up by number (see corpus/commentary.py for why by number).
    node_index = next(
        (i for i, page in page_index["by_node_index"].items() if page == section_slug), None)
    node = nodes[node_index] if node_index is not None else None
    section_number = node.get("number") if node is not None else None
    # Which Schedule (if any) this page's own provision sits in -- see
    # _section_crossrefs on why the number alone doesn't identify it.
    schedule = page_index["schedule_by_node_index"].get(node_index)
    # A Schedule is its own provision rather than a clause of itself, so
    # its node type decides which identity the timeline is looked up
    # under (see diffing).
    node_type = node["type"] if node is not None else "section"
    entries, version_urls = source._provision_timeline(slug, section_number, schedule, node_type)
    # Bill/EM commentary is only ever matched against an ordinary
    # numbered provision (see bill_linking.py) and never against a
    # Schedule as a whole -- a pageable Schedule
    # (hierarchy.schedule_is_pageable) is addressed by its own number
    # with schedule=None, the same (schedule, number) pair a
    # same-numbered body section would use, and _commentary_index's own
    # key has no kind to tell them apart the way build_page_index's
    # by_key now does. Skipping the lookup outright for anything that
    # isn't a genuine Section/Clause page avoids borrowing that section's
    # commentary onto the Schedule's page.
    crossrefs = (source._section_crossrefs(slug, section_number, schedule)
                 if node_type in ("section", "clause") else [])
    amendments = source._amendments(slug)

    return html_view.render_section(
        # version and endnotes are what the reading bar's "Text as at"
        # line and the outline's Endnotes link are built from.
        {"nodes": nodes, "hierarchy": hierarchy, "version": source._act_version(slug),
         "endnotes": amendments["endnotes"]},
        title, base_url, section_slug,
        crossrefs=site(crossrefs),
        amendment_index=amendments["index"],
        timeline=entries,
        version_urls=site(version_urls),
        superseded=site(source._superseded(slug)),
        version_dates=source._version_dates(slug),
        show_review_badge=show_review_badge,
        timeline_unavailable=source._timeline(
            split_document_slug(slug)[0]).get("mixed_parsers", False),
        notice=notice,
    )


def endnotes_page(source, slug: str, base_url: str) -> "str | None":
    """The Act's own Endnotes -- General information, the Table of
    Amendments read as a real table, and Explanatory details. None for a
    document that has none: a Bill, an Explanatory Memorandum, or an Act
    parsed before corpus/endnotes.py existed."""
    nodes, _unattached, hierarchy = source._current_nodes(slug)
    amendments = source._amendments(slug)
    return html_view.render_endnotes(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": amendments["endnotes"]},
        document_title(source, slug), base_url, amendments["summary"],
    )
