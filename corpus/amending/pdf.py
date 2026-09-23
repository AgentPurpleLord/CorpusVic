"""An amending Act's PDF as the nodes instructions.read_act reads.

Not through rule_parser: its profiles are tuned to consolidated reprints,
and an amending Act is simpler and set differently. What matters here is
where each section and subsection starts, what "the Principal Act" is in
each Part, and nothing of the page furniture -- the running header
(the Part, the Act's title, "No. 1 of 2026"), the "Authorised by" footer
and the page number all come out of the PDF inside the text, and one
caught between "for" and its quoted words would stop the sentence being
read.

Set, as the Office of the Chief Parliamentary Counsel sets them: body in
12pt, Part and Division headings larger and bold, section headings 12pt
bold with the number first, the furniture in smaller type or outside the
body's band of the page.
"""
import re

import fitz

_BODY_TOP, _BODY_BOTTOM = 140.0, 715.0
_SECTION = re.compile(r"^(?P<number>\d+[A-Z]*)\s+(?P<heading>\S.*)$")
_SUBSECTION = re.compile(r"^\((?P<number>\d+[A-Z]*)\)\s+")
# In a Schedule of consequential amendments: "2 Criminal Procedure Act
# 2009", then its items "2.1 In item 22A of Schedule 3, for ...".
_SCHEDULE_ACT = re.compile(r"^(?P<number>\d+)\s+(?P<act>\S.* Act \d{4})$")
_ITEM = re.compile(r"^(?P<number>\d+\.\d+[A-Z]*)\s+(?P<text>\S.*)$")
_ENACTED_BY = re.compile(r"^Sections? (?P<number>\d+[A-Z]*)")
_START = "The Parliament of Victoria enacts"


def _lines(doc) -> list[dict]:
    out = []
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = "".join(s["text"] for s in line["spans"]).strip()
                out.append({
                    "text": text, "x": line["bbox"][0], "y": line["bbox"][1],
                    "size": round(spans[0]["size"], 1),
                    "bold": all(s["flags"] & 16 for s in spans),
                    "starts_bold": bool(spans[0]["flags"] & 16),
                })
    return out


# Text being inserted is given in quotes, and carries its own "(1)" and
# "464 Heading" lines that are not this Act's. It opens on a line starting
# with a quote and closes on one ending with the quote and the sentence's
# own full stop or semicolon. Counting quote marks instead lost count at
# the first quoted term inside an inserted section, and took the next
# sixty sections with it.
_OPENS = re.compile(r'^["“]')
_CLOSES = re.compile(r'["”]\s*[.;](?:\s*(?:and|or))?\s*$')


def read_pdf(path) -> list[dict]:
    """[{"type": "part"|"division"|"section"|"subsection"|"schedule"|
    "schedule_act"|"item", "number", "heading", "text"}] in order, from the
    enacting words to the Endnotes. A schedule also carries "section",
    the one that enacts it."""
    lines = _lines(fitz.open(path))
    start = next((i for i, l in enumerate(lines) if l["text"].startswith(_START)), 0)
    nodes: list[dict] = []
    heading_open = in_block = False
    for line in lines[start + 1:]:
        text = line["text"]
        if text.startswith("═") or text == "Endnotes":
            break
        # A Schedule says, in small type beside its heading, which section
        # enacts it -- and a margin note cites its items through that
        # section: "13/2025 s. 96(Sch. 3 item 2.1)".
        enacted = _ENACTED_BY.match(text)
        if enacted and nodes and nodes[-1]["type"] == "schedule" and _BODY_TOP <= line["y"]:
            nodes[-1]["section"] = enacted.group("number")
            continue
        if not (_BODY_TOP <= line["y"] <= _BODY_BOTTOM) or line["size"] < 11.5:
            continue
        if nodes and nodes[-1]["type"] in ("section", "subsection", "item") and (in_block or _OPENS.match(text)):
            nodes[-1]["text"] = f"{nodes[-1]['text']} {text}".strip()
            in_block = not _CLOSES.search(text)
            heading_open = False
            continue
        if line["size"] >= 13.5 and line["bold"]:
            kind = ("part" if text.startswith("Part") else "division" if text.startswith("Division")
                    else "schedule" if text.startswith("Schedule") else None)
            if kind or (nodes and nodes[-1]["type"] in ("part", "division", "schedule") and heading_open):
                if kind == "schedule":
                    number = re.match(r"^Schedule (\d+[A-Z]*)", text)
                    nodes.append({"type": kind, "number": number.group(1) if number else None,
                                  "heading": text, "text": "", "section": None})
                elif kind:
                    nodes.append({"type": kind, "number": None, "heading": text, "text": ""})
                else:
                    nodes[-1]["heading"] += " " + text
                heading_open = True
                continue
        # Smaller than a Division's, so caught by what it says rather than
        # its size; left alone it ran on into the section above it.
        if line["bold"] and re.match(r"^Subdivision \d+[A-Z]*—", text):
            nodes.append({"type": "division", "number": None, "heading": text, "text": ""})
            heading_open = True
            continue
        in_schedule = any(n["type"] == "schedule" for n in nodes)
        if in_schedule and line["x"] < 200:
            act, item = _SCHEDULE_ACT.match(text), _ITEM.match(text)
            if act and line["bold"]:
                nodes.append({"type": "schedule_act", "number": act.group("number"), "heading": act.group("act"),
                              "text": ""})
                heading_open = False
                continue
            if item:
                nodes.append({"type": "item", "number": item.group("number"), "heading": None,
                              "text": item.group("text")})
                heading_open = False
                continue
        section = _SECTION.match(text)
        if section and line["starts_bold"] and line["x"] < 180:
            nodes.append({"type": "section", "number": section.group("number"),
                          "heading": section.group("heading"), "text": ""})
            heading_open = True
            continue
        if heading_open and nodes and nodes[-1]["type"] == "section" and line["bold"] and line["x"] < 205:
            nodes[-1]["heading"] += " " + text
            continue
        heading_open = False
        if not nodes or nodes[-1]["type"] in ("part", "division", "schedule", "schedule_act"):
            continue
        sub = _SUBSECTION.match(text)
        if sub and line["x"] < 205:
            nodes.append({"type": "subsection", "number": sub.group("number"), "heading": None,
                          "text": text[sub.end():]})
            continue
        nodes[-1]["text"] = f"{nodes[-1]['text']} {text}".strip()
    return nodes
