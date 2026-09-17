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
python3 public.py --open --port 8001      # then http://127.0.0.1:8001
```

`--open` skips the passphrase, which is what you want on your own
machine. Leave it running while you work.

To put them on the live site: commit, push, then on the server pull and
press **Restart the public site** on the dashboard. A pure CSS change
only needs the files on disk, but restarting costs nothing and removes
the question.

## Where things live

| To change | Edit |
|---|---|
| Colours, light **and** dark | `static/site/tokens.css` |
| Fonts | `static/site/tokens.css` — `--sans`, `--reading`, `@font-face` |
| Buttons, the header bar, search box, breadcrumbs, results, footer layout | `static/site/page.css` |
| The reading column — provision text, margins, the outline | `static/site/reader.css` |
| Page structure — the site name, header order, what loads | `static/site/page.html` |
| **Footer wording** | `static/site/footer.html` |
| Landing page | `export_static_site.py` → `_landing_page_html` |
| "Only part of this document has been reviewed…" | `export_static_site.py` → `_partial_notice_html` |
| "This provision has not been checked by a human." | `export_static_site.py` → `_unverified_notice_html` |
| Where "the authorised text" points | `export_static_site.py` → `OFFICIAL_SOURCE_URL` / `_NAME` |
| The admin dashboard's look | `static/dashboard.html` (self-contained `<style>`) |
| The review GUI's look | `static/review.html` (same) |

The first six are the ones you will want most, and none of them is code.

## Colours are tokens, not hex codes

Everything on the site draws its colour from a variable in
`tokens.css`. Change `--accent` once and every button, link and
highlight follows, in both themes.

Write a hex code directly into `page.css` and you have made an exception
— it will look right in light mode and wrong in dark, because the dark
theme only overrides the tokens. If you need a new colour, add it to
**both** `:root` blocks in `tokens.css`.

The dark values are a separate ramp rather than the light ones inverted
(accent hue ~31.5°, neutral ~17.3°). If you choose a new accent, pick a
lighter step of it for dark so it stays legible on the dark ground.

Check both themes before you push — the moon button in the header
toggles it. The choice is shared with the review GUI through
`localStorage.reviewTheme`, so the two surfaces stay in step.

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
