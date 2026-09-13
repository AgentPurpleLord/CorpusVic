# Junicode

The reading typeface for legislative text on this site. Part of the
page template in `static/site/`, served as `/assets/fonts/` and asked for
by `tokens.css` beside it. Junicode is by
Peter S. Baker, licensed under the SIL Open Font License 1.1 (`OFL.txt`),
from <https://junicode.sourceforge.io/> / <https://github.com/psb1558/Junicode-font>.

`Junicode-Roman.woff2` and `Junicode-Italic.woff2` here are subsets of
that project's own variable webfonts, cut down for this site:

- the `wdth` (width) and `ENLA` (enlarged capitals, for medieval texts)
  axes are pinned at their defaults, since nothing here varies them;
- `wght` is left variable across 300-700, so one file covers every weight;
- the character set is Latin-1 and Latin Extended-A plus the punctuation
  this project's typography needs (curly quotes, real dashes, ellipsis,
  section and paragraph marks, trademark and copyright symbols).

That takes each file from about 1 MB to under 75 KB. Regenerate with
fontTools if the character set ever needs widening -- subset first, then
instance the axes (the reverse order trips a gvar bug in fontTools 4.64).
