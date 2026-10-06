# The face

Inter is served from here rather than from a font CDN. A public register of
the law shouldn't hand every reader's IP address to a third party in order
to have its own text drawn.

It is subset and instanced for this site; regenerate it with
fontTools if the character set ever needs widening, and **subset first,
then instance the axes** (the reverse order trips a gvar bug in fontTools
4.64). It carries Latin-1 and Latin Extended-A, plus the punctuation this project's typography and
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

Everything on the page, the legislative text included: it replaced the
serif (Junicode) the law was set in, as easier to read on a screen. By Rasmus
Andersson, licensed under the SIL Open Font License 1.1 (`Inter-OFL.txt`),
from <https://rsms.me/inter/> (v4.1). `opsz` is pinned at 14, the size most
of this site's chrome is set at; `wght` is left variable across 400-700, so
one file per style covers every weight used here. 880 KB down to 31 KB.
