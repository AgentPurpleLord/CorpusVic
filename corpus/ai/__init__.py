"""The parts of Corpus that actually use a model.

Everything else in `corpus` is deterministic: it reads a PDF's own
geometry and typography and applies rules written down in a profile, and
given the same PDF it gives the same answer every time. None of it asks a
model anything, which is the whole point -- see rule_parser's module
docstring on why that matters for legislation.

What lives here is the exception, and it is always optional and always
advisory: a second opinion a reviewer can ask for on a piece the
diagnostics have already flagged (assist), a whole-document audit pass
run offline (scan), and the local backend both go through (backend).
Nothing in here decides anything; a human does.

This package exists so that "which parts use AI?" has a one-word answer.
It used to be that the whole thing was called `corpus`, which said
the opposite of what is true of nearly all of it.
"""
