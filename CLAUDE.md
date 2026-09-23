# CorpusVic

Victorian legislation parsed from PDFs, reviewed by hand, published at
corpusvic.au. `README.md` is the full tour; this is the short orientation
so a fresh session doesn't have to go looking, plus how to work here.

## What is true of the data

- `data/review/**.jsonl` is the source of truth. `data/legislation.db` is
  derived from it, gitignored, and rebuilt by
  `python -m corpus.review.review_sync import`.
- A parse is a **flat** list of nodes. Hierarchy is derived by
  `corpus/parsing/tree.py::annotate_paths`, never stored.
- Review rows are keyed by `node_id` — a provision's name, not its
  position. See `corpus/parsing/identity.py`.
- What a node type looks like is declared, not coded:
  `corpus/domain/rules/recognition/*.yaml`, matched by
  `corpus/parsing/recognise.py`.
- The Act PDFs are not in git. Tests needing them skip.

## Conventions

- Any project file goes through `corpus.PROJECT_ROOT`. Never count
  `.parent`s — modules move and the count doesn't follow.
- Run other modules as `-m corpus.x.y`, never by filename.
- The pre-commit hook is tracked at `deploy/githooks/pre-commit`. Point
  git at it once: `git config core.hooksPath deploy/githooks`.
- Tests: `python -m pytest -q`. Baseline **1,471 passed, 1 skipped**.

## How to work here

Token budget is a real constraint. Every tool call re-sends the whole
conversation, so the count matters as much as the size.

- **Verify with the suite and targeted checks.** Before a browser drive, a
  full static export, or reverting a fix to prove a test bites: say what
  it would show and ask. Don't do it by default.
- **No task list on this project.**
- **No subagents** unless asked.
- Use the Grep and Read tools, not `grep`/`sed`/`cat` through Bash.
- Batch independent Bash commands into one call.
- Read line ranges, not whole files. Never re-read a file just edited.
- Don't poll background jobs.

## Writing

Comments and docs say **why**, briefly — the reason this code is shaped
this way, or the defect it exists to stop. Not what the code does, not a
record of the conversation that produced it. Short paragraphs. No essays.
