"""
Reading what somebody typed.

The gap this closes was measured rather than guessed. Searching this
corpus for "definition of family violence" did not return Section 5 of
the Family Violence Protection Act anywhere in twenty results -- because
that section is headed "Meaning of family violence" and contains the word
"definition" nowhere, and because FTS5 requires every term, so "of" was
mandatory too. "when can police issue a safety notice" returned nothing
at all, for the same second reason.

So three things happen here, in order:

  - **stopwords go**, which is what makes a question answerable at all;
  - **what is left is OR'd**, so a provision matching four words of five
    still appears, ranked below one matching all five -- which is what
    bm25 already does well and was never given the chance to do;
  - **the question's shape is read**, not just its words. "what is X",
    "definition of X" and "meaning of X" are all asking where X is
    *defined*, and this corpus records which nodes are definitions and
    heads its defining sections "Meaning of X". That structure is already
    in the parse and was going unused.

Typos are corrected against the index's own vocabulary -- 5,519 distinct
terms for the whole corpus, so the nearest one is found by looking. The
rule that makes always-on correction safe is that **a word the corpus
contains is never second-guessed**: correction only ever fires on a word
that matches nothing, so a precise query cannot be softened into a vague
one. Both the typed word and its correction are searched, so a correction
can add results and never take any away.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

TERMS_FILE = Path(__file__).resolve().parent / "search_terms.yaml"

_TOKEN_RE = re.compile(r'"[^"]*"?|\S+')
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_OPERATORS = {"AND", "OR", "NOT", "NEAR"}

_terms_cache: "dict | None" = None


def terms() -> dict:
    """The vocabulary file, read once."""
    global _terms_cache
    if _terms_cache is None:
        import yaml

        loaded = yaml.safe_load(TERMS_FILE.read_text(encoding="utf-8")) or {}
        _terms_cache = {
            "stopwords": set(loaded.get("stopwords") or []),
            "synonyms": {k: list(v) for k, v in (loaded.get("synonyms") or {}).items()},
            "definition_intent": list(loaded.get("definition_intent") or []),
            "intent_words": set(loaded.get("intent_words") or []),
            "interrogatives": list(loaded.get("interrogatives") or []),
        }
    return _terms_cache


@dataclass
class Query:
    """What a typed query turned out to mean."""

    raw: str
    match: str = ""
    # The content words, after stopwords and corrections.
    words: list = field(default_factory=list)
    # What the question is *about*, with the words that carry its shape
    # removed -- "family violence" out of "definition of family violence".
    subject: list = field(default_factory=list)
    wants_definition: bool = False
    # The word this question opens with -- "who", "when", "how" -- when
    # it opens with one. Legislation heads provisions the same way, so
    # this is a place to look rather than a word to match.
    asks: str = ""
    # {what was typed: what it was read as}, only for words the corpus
    # does not contain.
    corrections: dict = field(default_factory=dict)
    # A query the searcher wrote in FTS5's own syntax (quoted phrases,
    # NOT, prefixes). Left alone rather than interpreted.
    verbatim: bool = False

    def __bool__(self) -> bool:
        return bool(self.match)


def _correct(word: str, vocabulary: dict) -> "str | None":
    """The nearest word the corpus actually contains, or None.

    Only ever called for a word the corpus does *not* contain, which is
    what keeps always-on correction from softening a precise query. Ties
    go to the more frequent word, since that is the one more likely to
    have been meant."""
    if not vocabulary or len(word) < 4:
        return None
    allowed = 1 if len(word) < 7 else 2
    best, best_score = None, None
    for candidate, count in vocabulary.items():
        if abs(len(candidate) - len(word)) > allowed or candidate[0] != word[0]:
            # A first letter apart is possible but rare in a typo, and
            # checking it first is what keeps this to a few thousand
            # cheap comparisons rather than a full scan of edit
            # distances.
            continue
        distance = _edit_distance(word, candidate, allowed)
        if distance is None:
            continue
        score = (distance, -count)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


def _edit_distance(a: str, b: str, limit: int) -> "int | None":
    """Levenshtein distance, or None once it is past `limit`.

    Bounded because the answer is only interesting while it is small, and
    abandoning a row that cannot come back under the limit is most of the
    saving."""
    if abs(len(a) - len(b)) > limit:
        return None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        if min(current) > limit:
            return None
        previous = current
    return previous[-1] if previous[-1] <= limit else None


def _looks_verbatim(raw: str) -> bool:
    """Whether the searcher is speaking FTS5's own language.

    A quoted phrase, a NOT, a prefix star: somebody who typed one of
    those means it, and rewriting their query would be taking it off
    them."""
    return ('"' in raw) or ("*" in raw) or any(
        word in _OPERATORS for word in raw.split())


def analyse(raw: str, vocabulary: "dict | None" = None) -> Query:
    """What the query says, and what it is asking for."""
    raw = raw or ""
    vocab = vocabulary or {}
    config = terms()

    if _looks_verbatim(raw):
        return Query(raw=raw, match=_verbatim_match(raw), verbatim=True,
                     words=_WORD_RE.findall(raw.lower()))

    typed = _WORD_RE.findall(raw.lower())
    if not typed:
        return Query(raw=raw)

    kept = [w for w in typed if w not in config["stopwords"]] or typed

    corrections = {}
    words = []
    for word in kept:
        if vocab and word not in vocab:
            fixed = _correct(word, vocab)
            if fixed:
                corrections[word] = fixed
        words.append(word)

    low = raw.lower()
    wants_definition = any(phrase in low for phrase in config["definition_intent"])
    asks = typed[0] if typed[0] in config["interrogatives"] else ""
    subject = [w for w in words if w not in config["intent_words"]]

    # What goes to FTS5: every content word, its correction if it has
    # one, and its synonyms. OR'd, so a provision matching most of them
    # still appears and bm25 decides how far down.
    expanded: list[str] = []
    for word in words:
        expanded.append(word)
        if word in corrections:
            expanded.append(corrections[word])
        expanded.extend(config["synonyms"].get(word, []))
    if wants_definition:
        # The words this corpus defines things with, so that a section
        # headed "Meaning of X" can be found by somebody asking for the
        # definition of X.
        expanded.extend(["meaning", "means", "definition"])

    ordered = list(dict.fromkeys(expanded))
    return Query(
        raw=raw,
        match=" OR ".join(f'"{w}"' for w in ordered),
        words=words,
        subject=subject,
        wants_definition=wants_definition,
        asks=asks,
        corrections=corrections,
    )


def _verbatim_match(raw: str) -> str:
    """A query in FTS5's own syntax, made safe without being rewritten.

    Phrases, NOT and prefixes survive; everything else is reduced to
    quoted words, which is what stops `s 3(1)` -- and an unbalanced
    quote -- from being a syntax error."""
    parts = []
    for token in _TOKEN_RE.findall(raw):
        if token.startswith('"'):
            words = _WORD_RE.findall(token.strip('"').strip())
            if words:
                parts.append('"' + " ".join(words) + '"')
            continue
        if token in _OPERATORS:
            if parts and parts[-1] not in _OPERATORS:
                parts.append(token)
            continue
        if token.startswith("-") and len(token) > 1:
            words = _WORD_RE.findall(token[1:])
            if words and parts:
                parts.extend(["NOT", '"' + " ".join(words) + '"'])
            continue
        prefix = token.endswith("*")
        words = _WORD_RE.findall(token)
        if not words:
            continue
        parts.append('"' + " ".join(words) + '"' + ("*" if prefix else ""))
    while parts and parts[-1] in _OPERATORS:
        parts.pop()
    return " ".join(parts)
