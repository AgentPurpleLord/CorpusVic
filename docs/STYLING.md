# Changing how the site looks

Fonts, colours, buttons, the header, the footer, the wording. None of it
needs Python, and most of it needs no restart.

## The loop

Edit the file, save, **reload the browser**. That is all.

The stylesheets and scripts under `static/site/` are served straight off
disk, and the HTML templates are re-read whenever their mtime changes
(`corpus/html_view.py:template_text`). Nothing is baked in at startup.

To see your changes locally:

```bash
python3 -m corpus.web.public --open --port 8001      # then http://127.0.0.1:8001
```

`--open` skips the passphrase, which is what you want on your own
machine. Leave it running while you work.

To put them on the live site: commit, push, then on the server pull and
press **Restart the public site** on the dashboard. A pure CSS change
only needs the files on disk, but restarting costs nothing and removes
the question.

## Without a server: the style preview

`style-preview/index.html` opens straight from disk. It holds frozen
copies of real pages, both the public site and the admin tools: a
section with notes, a table, definitions, an Example, dot points, the
contents page, search results, the dashboard, review and its Edit window,
and more.

They link the stylesheets in `static/site/` and `static/admin/` by
relative path, so **edit a stylesheet, save, reload**. The bar in the
bottom corner switches light and dark.

The copies have no scripts, so buttons don't work and nothing loads. The
markup is whatever the pages looked like when the copies were taken.
After changing a page's HTML (not its CSS), take them again:

```bash
python -m corpus.publishing.style_preview
```

That needs Playwright and its Chromium (`pip install playwright &&
playwright install chromium`), or `CHROMIUM=/path/to/chrome` for one
already installed. It works from the committed `data/` in a throwaway
directory and never touches your own database.

## Where things live

| To change | Edit |
|---|---|
| Colours, light **and** dark | `static/site/tokens.css` |
| Fonts | `static/site/tokens.css` — `--sans`, `--reading`, `@font-face` |
| Buttons, the header bar, search box, breadcrumbs, results, footer layout | `static/site/page.css` |
| The reading column — provision text, margins, the contents outline | `static/site/reader.css` |
| Page structure — the site name, header order, what loads | `static/site/page.html` |
| **Footer wording** | `static/site/footer.html` |
| Landing page | `corpus/web/public.py` → `_landing_page_html` |
| "Only part of this document has been reviewed…" | `corpus/web/public.py` → `_partial_notice_html` |
| Where "the authorised text" points | `corpus/web/public.py` → `OFFICIAL_SOURCE_URL` / `_NAME` |
| The admin dashboard's look | `static/admin/dashboard.css` |
| The review GUI's look | `static/admin/review.css` |
| History review, Teaching, Lessons | `static/admin/history.css`, `teaching.css`, `lessons.css` |
| The admin tools' font | `static/admin/fonts.css` |

The first six are the ones you will want most, and none of them is code.

## Colours are tokens, not hex codes

Everything on the site draws its colour from a variable in
`tokens.css` — and so does every admin screen (dashboard, review,
history, lessons, teaching): each `static/admin/*.css` starts by
importing `../site/tokens.css` and only renames tokens for its own use.
Change `--accent` once and every button, link and highlight follows, on
both surfaces and in both themes.

Write a hex code directly into `page.css` and you have made an exception
— it will look right in light mode and wrong in dark, because the dark
theme only overrides the tokens. If you need a new colour, add it to
**both** `:root` blocks in `tokens.css`.

The palette is a faded navy (`--accent`, with `--accent-strong` for
hover and `--accent-soft` for tinted fills) on cool, blue-tinted greys:
`--bg` for the page, `--panel` for quiet surfaces, `--surface` for
raised ones (menus, cards, modals). The dark values are a separate ramp
rather than the light ones inverted; a new accent needs a lighter step
of it for dark.

Contrast is the constraint that doesn't move: body text is 13:1 or
better against every surface, muted text and the accent at least 6:1,
in both themes. Check a new colour against `--bg`, `--panel` and
`--surface` before using it for text — WCAG's formula, not the eye.

## Shape and space

Curves come from `--radius-sm` (6px: chips, inputs, tags) and `--radius`
(10px: panels, notices, menus, modals); round tags are `999px`. Raised
things take `--shadow` (or `--shadow-sm` for a card) instead of a hard
border. Gaps come from the `--space-1`…`--space-6` scale (4–32px).

The legislative text is set at line-height 1.6 with 18px between
provisions: it is read slowly, and the room is what keeps a long
provision followable. Keep the 34em measure — width, not size, is what
readability rests on.

Check both themes before you push — Theme in the header's Aa menu
toggles it. The choice is shared with the review GUI through
`localStorage.reviewTheme`, so the two surfaces stay in step.

## Chips

Every small control — the EM chips, History, Copy section, Earlier and
Later, the Aa menu and its buttons — is one `.chip` (`page.css`), sized
by `--chip-h`, `--chip-font` and `--chip-pad` in `tokens.css`. Change
those to resize them all at once; the header's search box takes the same
height. A new control gets the `chip` class (from Python, through
`html_view._chip`) and only its own differences as extra rules.

## Fonts

Both are self-hosted from `static/site/fonts/` on purpose: reading the
law here should not announce every reader to a font CDN. `--reading` is
the face the legislation itself is set in; `--sans` is everything else.

To swap one, put the `.woff2` in that folder and point its `@font-face`
at it. Keep it local.

## The footer

`static/site/footer.html` is the whole footer. Edit it and reload — no
restart, no Python.

Two things to keep:

- `class="site-footer"` on the wrapper, which is what `page.css` styles.
- the `legislation.vic.gov.au` link, **as a link**. The footer's job is
  to tell a reader this text is not authoritative; the least it can do
  is take them to the text that is.

HTML comments in that file are stripped before the page is served, so
notes to yourself are free.

## Leave these alone unless you mean it

- `[id] { scroll-margin-top: … }` in `page.css` — without it, every
  in-page link lands *underneath* the sticky header, and the link looks
  broken when it isn't.
- `--header-h` — the outline's sticky offset is measured from it.
- The `<script>` block in `page.html`'s `<head>` — it applies the saved
  theme before the first paint. Move it lower and every page flashes
  white before going dark.

## If something looks wrong

```bash
python3 -m pytest -q
```

A few tests assert page *structure* rather than wording — that the
footer exists and still says the text is not official, that breadcrumb
links point at headings that exist. They will tell you if an edit
removed something load-bearing. They will not complain about rewording,
which is the point.
