"""One Hansard record -- a speech or a Statement of Compatibility -- as
its paragraphs.

Kept as a small subset of HTML rather than as text, because the
paragraphing is the point: a second reading speech read as one block is
unreadable, and its emphasis and lists are the Minister's own. What is
kept is safe to serve as it stands -- no attributes, no scripts, nothing
that loads anything.
"""
import html
import re
import urllib.request
from html.parser import HTMLParser

_AGENT = "CorpusVic Hansard fetcher (+https://corpusvic.au)"

# What survives, and what each becomes. b/i are kept as written: Hansard
# uses them for a Bill's name and for quoted provisions.
_KEEP = {"p": "p", "h1": "h3", "h2": "h3", "h3": "h3", "h4": "h4", "h5": "h4", "h6": "h4",
         "em": "em", "i": "i", "strong": "strong", "b": "b", "br": "br",
         "ul": "ul", "ol": "ol", "li": "li", "blockquote": "blockquote"}
_VOID = {"br"}
# Never content: their text is dropped along with them.
_DROP = {"script", "style", "noscript", "template", "head", "nav", "header", "footer", "form",
         "button", "select", "svg", "iframe", "object"}
_BLOCK = {"p", "h3", "h4", "ul", "ol", "li", "blockquote"}


class _Sanitiser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.dropping = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self.dropping += 1
            return
        if self.dropping or tag not in _KEEP:
            if tag in ("div", "section", "article", "td", "tr") and not self.dropping:
                self._close_inline_block()
            return
        kept = _KEEP[tag]
        if kept in _VOID:
            self.out.append("<br>")
            return
        if kept == "p" and "p" in self.open:
            self._close("p")   # an unclosed <p> before the next, as old HTML does
        self.out.append(f"<{kept}>")
        self.open.append(kept)

    def handle_startendtag(self, tag, attrs):
        if not self.dropping and _KEEP.get(tag) == "br":
            self.out.append("<br>")

    def handle_endtag(self, tag):
        if tag in _DROP:
            self.dropping = max(0, self.dropping - 1)
            return
        if self.dropping or tag not in _KEEP or _KEEP[tag] in _VOID:
            return
        self._close(_KEEP[tag])

    def handle_data(self, data):
        if not self.dropping:
            self.out.append(html.escape(data, quote=False))

    def _close(self, kept):
        if kept not in self.open:
            return
        while self.open:
            top = self.open.pop()
            self.out.append(f"</{top}>")
            if top == kept:
                break

    def _close_inline_block(self):
        if "p" in self.open:
            self._close("p")

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def sanitise(fragment: str) -> str:
    """`fragment` reduced to the kept subset, with text outside any block
    gathered into paragraphs and empty blocks dropped."""
    parser = _Sanitiser()
    parser.feed(fragment)
    parser.close()
    raw = parser.result()
    # Loose text between blocks -- a page that separates paragraphs with
    # <br><br> rather than <p> -- becomes paragraphs of its own.
    parts = re.split(r"(</?(?:%s)>)" % "|".join(sorted(_BLOCK)), raw)
    out, depth, loose = [], 0, []

    def flush():
        text = "".join(loose)
        for chunk in re.split(r"(?:\s*<br>\s*){2,}", text):
            chunk = re.sub(r"^(?:\s|<br>)+|(?:\s|<br>)+$", "", chunk)
            if re.sub(r"<[^>]+>", "", chunk).strip():
                out.append(f"<p>{chunk}</p>")
        loose.clear()

    for part in parts:
        m = re.fullmatch(r"<(/?)(\w+)>", part)
        if m and m.group(2) in _BLOCK:
            if not m.group(1):
                if depth == 0:
                    flush()
                depth += 1
            else:
                depth = max(0, depth - 1)
            out.append(part)
        elif depth == 0:
            loose.append(part)
        else:
            out.append(part)
    flush()
    cleaned = "".join(out)
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Blocks left with nothing in them: wrappers around dropped content.
    for _ in range(3):
        cleaned = re.sub(r"<(p|h3|h4|li|blockquote|ul|ol)>(?:\s|<br>)*</\1>", "", cleaned)
    cleaned = re.sub(r"\s*(</?(?:p|h3|h4|ul|ol|li|blockquote)>)\s*", r"\1", cleaned)
    return cleaned.replace("</p>", "</p>\n").replace("</h3>", "</h3>\n").replace("</h4>", "</h4>\n").strip()


def paragraph_count(kept: str) -> int:
    return len(re.findall(r"<(?:p|li)>", kept))


class _Blocks(HTMLParser):
    """The text each element holds directly in its <p> children, by
    element -- to find where on a page the record's own paragraphs are."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[list] = []   # [tag, start offset, paragraph chars]
        self.best = (0, None, None)
        self.in_p = 0
        self.dropping = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self.dropping += 1
        if tag in _VOID or tag in ("img", "meta", "link", "input", "hr"):
            return
        if tag == "p":
            if self.stack and self.stack[-1][0] == "p":
                self.handle_endtag("p")   # old HTML leaves <p> unclosed
            self.in_p += 1
        self.stack.append([tag, self.getpos(), 0])

    def handle_endtag(self, tag):
        if tag in _DROP:
            self.dropping = max(0, self.dropping - 1)
        if tag == "p":
            self.in_p = max(0, self.in_p - 1)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                closed = self.stack[i:]
                del self.stack[i:]
                element = closed[0]
                # Not passed up to the parent: <body> holds every
                # paragraph on the page, the speech's container only its own.
                if element[0] != "p" and element[2] > self.best[0]:
                    self.best = (element[2], element[1], self.getpos())
                return

    def handle_data(self, data):
        if self.in_p and not self.dropping and self.stack:
            # Credited to the element holding the <p>, so the winner is
            # the one container whose own paragraphs carry the most text.
            for frame in reversed(self.stack):
                if frame[0] != "p":
                    frame[2] += len(data.strip())
                    break


class Refused(Exception):
    """Hansard's firewall answered instead of Hansard."""


# What its firewall (F5) sends in place of the page: a JavaScript
# challenge, or a rejection naming a support ID. Both come back as 200,
# and read as text, the rejection would be stored as the speech.
_REFUSAL_RE = re.compile(r"<title>\s*Request Rejected\s*</title>|/TSPD/|Your support ID is", re.IGNORECASE)


def main_text(page: str) -> str:
    """The part of a whole page holding the record: the element whose
    paragraphs carry the most text. Navigation, search forms and footers
    are short paragraphs or none, and a speech is many long ones. A page
    with no paragraphs gives nothing, rather than all its text."""
    if _REFUSAL_RE.search(page):
        raise Refused("Hansard's firewall refused the request")
    finder = _Blocks()
    finder.feed(page)
    _chars, start, end = finder.best
    if start is None:
        return ""
    lines = page.splitlines(keepends=True)
    offset = lambda pos: sum(len(l) for l in lines[:pos[0] - 1]) + pos[1]
    return sanitise(page[offset(start):offset(end)])


def fetch(url: str, get=None) -> str:
    """The kept paragraphs of the record at `url`."""
    raw = (get or _get)(url)
    return main_text(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _AGENT, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()
