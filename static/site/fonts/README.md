# The two faces

Both are served from here rather than from a font CDN. A public register of
the law shouldn't hand every reader's IP address to a third party in order
to have its own text drawn -- and once that is the rule for the face the
legislation is set in, it has to be the rule for the one the rest of the
page is set in too.

Each is subset and instanced for this site; regenerate either with
fontTools if the character set ever needs widening, and **subset first,
then instance the axes** (the reverse order trips a gvar bug in fontTools
4.64). The two carry the same characters as each other on purpose: Latin-1
and Latin Extended-A, plus the punctuation this project's typography and
its renderers actually emit -- curly quotes, real dashes, the ellipsis, the
arrows in the next/previous bar, minus, euro, numero and trademark.

```
RANGE='U+0020-00FF,U+0100-017F,U+2010-2027,U+2030-2033,U+2039-203A,U+2044,U+20AC,U+2116,U+2122,U+2190-2193,U+2212'
pyftsubset InterVariable.ttf --unicodes="$RANGE" \
  --layout-features='kern,liga,clig,calt,ccmp,locl,mark,mkmk,tnum' \
  --output-file=sub.ttf --drop-tables+=DSIG
fonttools varLib.instancer sub.ttf opsz=14 wght=400:700 -o inst.ttf
fonttools ttLib.woff2 compress inst.ttf -o Inter.woff2
```

## Inter

The UI face: headings, notes, chips, the outline, the reading controls --
everything on the page that isn't the legislative text itself. By Rasmus
Andersson, licensed under the SIL Open Font License 1.1 (`Inter-OFL.txt`),
from <https://rsms.me/inter/> (v4.1). `opsz` is pinned at 14, the size most
of this site's chrome is set at; `wght` is left variable across 400-700, so
one file per style covers every weight used here. 880 KB down to 31 KB.

## Junicode

The reading face, used for the legislative text itself. By
Peter S. Baker, licensed under the SIL Open Font License 1.1
(`Junicode-OFL.txt`),
from <https://junicode.sourceforge.io/> / <https://github.com/psb1558/Junicode-font>.

`Junicode-Roman.woff2` and `Junicode-Italic.woff2` here are subsets of
that project's own variable webfonts, cut down for this site:

- the `wdth` (width) and `ENLA` (enlarged capitals, for medieval texts)
  axes are pinned at their defaults, since nothing here varies them;
- `wght` is left variable across 300-700, so one file covers every weight;
- the character set is Latin-1 and Latin Extended-A plus the punctuation
  this project's typography needs (curly quotes, real dashes, ellipsis,
  section and paragraph marks, trademark and copyright symbols).

That takes each file from about 1 MB to under 75 KB.
