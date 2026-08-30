"""System prompt for the structuring step, including dynamically injected
few-shot examples harvested from human review corrections."""

BASE_INSTRUCTIONS = """
You are structuring the text of a Victorian (Australia) Act of Parliament into
its hierarchical components. The source text was extracted directly from the
Act's PDF text layer (not OCR), with running headers/footers and the
right-hand margin amendment-history notes already stripped out, and page
boundaries marked inline as "<<<PAGE N>>>".

Victorian Acts use these structural levels, in this nesting order:
  chapter       e.g. "Chapter 2—Commencing a criminal proceeding" -- the
                optional top level a few Acts group their Parts under
  part          e.g. "Part I—Offences" or "Part 3—Sentencing"
  division      e.g. "Division 1—Offences against the person"
  subdivision   a bracketed-number sub-grouping heading inside a Division,
                e.g. "(1) Homicide" -- distinct from a numbered subsection
  heading_group a bare topical heading with no number, e.g. "Fraud and blackmail"
  section       the primary numbered unit, e.g. "3 Punishment for murder" or
                "34AB Definitions" -- numbers commonly carry letter suffixes
  subsection    numbered "(1)", "(2)" ... inside a section
  paragraph     lettered "(a)", "(b)" ... inside a subsection or section
  subparagraph  roman-numeral "(i)", "(ii)" ... inside a paragraph
  definition    one defined term plus its meaning inside a "Definitions"
                section, e.g. "medical procedure means ..."
  note          an explicit "Note" callout, or a row of "* * * * *" marking
                repealed text

Rules:
- Re-flow wrapped lines into natural prose within each node's "text" field --
  the PDF's line breaks are a layout artifact, not sentence structure.
  Preserve list structure: (a)/(b)/(i)/(ii) items are their own nodes, not
  flattened into the parent's text.
- Emit nodes as a flat list in document order. Do not nest JSON -- the
  calling code reconstructs the hierarchy from node type + document order.
- Set page_start/page_end from the nearest "<<<PAGE N>>>" markers surrounding
  each node.
- This chunk of text may start or end mid-section. If your first node
  continues text cut off at the end of the previous chunk, set
  continued_from_previous=true. If your last node will be cut off and
  continues into the next chunk, set continues_in_next=true. Otherwise both
  are false.
- If a Table of Provisions / contents listing appears in this chunk, skip it
  entirely -- only structure the substantive body text.
""".strip()


def _shorten(text: str, n: int = 300) -> str:
    text = " ".join(text.strip().split())
    return text[:n] + ("..." if len(text) > n else "")


def build_system_prompt(examples: list[dict]) -> str:
    parts = [BASE_INSTRUCTIONS]
    if examples:
        parts.append("\nPast human corrections to learn from:")
        for ex in examples:
            passage = _shorten(ex["human_output"]["text"])
            if ex["changed"]:
                ai = ex["ai_output"]
                human = ex["human_output"]
                parts.append(
                    f"- Passage: \"{passage}\"\n"
                    f"  You previously mislabeled this as type={ai['type']!r} "
                    f"number={ai['number']!r} heading={ai['heading']!r}.\n"
                    f"  Correct: type={human['type']!r} number={human['number']!r} "
                    f"heading={human['heading']!r}."
                )
            else:
                human = ex["human_output"]
                parts.append(
                    f"- Passage: \"{passage}\"\n"
                    f"  Correctly labeled as type={human['type']!r} "
                    f"number={human['number']!r} heading={human['heading']!r}."
                )
    return "\n".join(parts)
