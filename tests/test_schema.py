"""Tests for corpus/schema.py -- which structural types a document of
each kind can actually be labelled with.

One flat enum meant an Act's reviewer was asked whether a provision might
be a "clause" (it never is -- that is a Bill's word for the same thing,
see hierarchy.make_ranks) and a Bill's reviewer was offered "section".
review.py adds back anything a node actually carries, so filtering can
never strand a node's own type."""
from corpus.schema import NODE_TYPES, TYPES_BY_DOCUMENT, types_for_document


def test_an_act_is_offered_sections_and_never_clauses():
    types = types_for_document("act")

    assert "section" in types
    assert "clause" not in types


def test_a_bill_is_offered_clauses_and_never_sections():
    types = types_for_document("bill")

    assert "clause" in types
    assert "section" not in types


def test_only_a_consolidated_act_is_offered_repealed():
    # "* * * *" stands in for a provision since repealed. A Bill has not
    # been enacted yet, so nothing in it can have been repealed.
    assert "repealed" in types_for_document("act")
    assert "repealed" not in types_for_document("bill")
    assert "repealed" not in types_for_document("em")


def test_an_em_keeps_the_legacy_type_its_own_older_parses_carry():
    # "em_entry" is no longer emitted (see em_parser.py), but an EM parsed
    # before that change still holds nodes typed with it -- and a type you
    # can see but not name is worse than one nobody picks.
    assert "em_entry" in types_for_document("em")
    assert "em_entry" not in types_for_document("act")


def test_an_em_is_offered_the_types_its_bulleted_lists_parse_to():
    types = types_for_document("em")

    assert {"clause", "paragraph", "subparagraph", "heading_group"} <= set(types)


def test_every_document_type_offers_only_real_schema_types():
    for document_type, types in TYPES_BY_DOCUMENT.items():
        assert set(types) <= set(NODE_TYPES), document_type
        assert len(types) == len(set(types)), document_type


def test_an_unrecognised_document_type_loses_nothing():
    # A parse from before document_type was recorded gets the full enum
    # rather than a guess.
    assert types_for_document(None) == NODE_TYPES
    assert types_for_document("statutory-rule") == NODE_TYPES
