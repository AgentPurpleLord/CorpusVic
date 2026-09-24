"""Teaching the parser from review.

Every decision a reviewer makes about a piece the parser opened -- kept as
it was, retyped, renumbered, merged away, deleted, or a piece the parser
missed -- is an example: the printed line, what the parser saw there, what
the parser said, and what the reviewer said (examples.py). Every parse is
then checked against all of them (score.py), so a correction made once is
never silently undone by a later change to the parser.
"""
