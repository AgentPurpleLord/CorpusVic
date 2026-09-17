"""
Finding a provision by what it is about, rather than by its words.

The lexical index answers a question asked in the statute's own
vocabulary, and corpus/query.py stretches that a long way: stopwords
dropped, synonyms applied, and the drafting conventions this corpus uses
read directly ("Meaning of X" for "definition of X", "Who may appeal" for
"who can appeal"). Measured over data/search_eval.yaml, that reaches MRR
0.785 on the queries it was built for.

It reaches 0.012 on the rest. "What happens if you breach an intervention
order" is answered by a section headed "Contravention of family violence
intervention order"; "can I call a lawyer after being arrested" by "Right
to communicate with a friend, relative or legal practitioner"; "I was
forced to commit the crime by threats" by one word, "Duress". Five of
those six are nowhere in twenty results. No synonym list scales to that,
because the gap is not between two words -- it is between a question and
what a provision is *about*.

So this module embeds the corpus and compares meanings.

**Sections, not provisions.** 6,173 rather than 50,462: 19 MB of vectors
instead of 155 MB, a build in a couple of minutes rather than half an
hour, and a more coherent unit to embed. A bare subsection -- "(2) In
this section, *notice* means a notice under section 30" -- embeds into
nothing useful on its own; its section does not. The lexical side goes on
doing the precise within-section work, and this side decides which
section is about what was asked.

**Optional, always.** No model on disk and nothing here runs: search is
exactly what it was, every test passes, and no page mentions semantics.
That is what keeps a fresh clone, CI and a small VPS all working, and it
is the reason every entry point below begins by asking whether there is
anything to load.

**Nothing is committed.** The model is a download (see
download_search_model.py) and the vectors are built from it, so both are
gitignored -- like data/search.db, and for the same reason: a derived
file in a repository is a third copy of something two files already say,
and the copy is the one that goes stale.
"""
import json
import os
from pathlib import Path

# Where download_search_model.py puts the model, and where this looks for
# it. Overridable so a test can point at a directory it made.
MODEL_DIRNAME = "models/bge-base-en-v1.5"
VECTORS_FILENAME = "search_vectors.npy"
MANIFEST_FILENAME = "search_vectors.json"

# bge-base-en-v1.5. The dimension is fixed by the model and recorded here
# so a mismatch is caught when the vectors load rather than at the first
# query, when it would look like a relevance problem.
DIMENSIONS = 768
MAX_TOKENS = 512

# What bge-en-v1.5 asks to be prefixed to a short query for retrieval.
# Only the query side: the passages are embedded as they are.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# How many sections the semantic side proposes. Small: this is a
# suggestion to be fused with the lexical ranking, not a result list.
SEMANTIC_DEPTH = 40

# Reciprocal rank fusion's constant, at its usual value. The point of RRF
# is that it never compares a bm25 score with a cosine -- only positions
# -- so there is no scale to calibrate and one side being strange cannot
# swamp the other.
RRF_K = 60


def model_dir(base_dir) -> Path:
    return Path(base_dir) / MODEL_DIRNAME


def vectors_path(base_dir) -> Path:
    return Path(base_dir) / "data" / VECTORS_FILENAME


def manifest_path(base_dir) -> Path:
    return Path(base_dir) / "data" / MANIFEST_FILENAME


def model_present(base_dir) -> bool:
    """Whether there is a model to load, without loading it."""
    directory = model_dir(base_dir)
    return (directory / "model.onnx").exists() and (directory / "tokenizer.json").exists()


def vectors_present(base_dir) -> bool:
    return vectors_path(base_dir).exists() and manifest_path(base_dir).exists()


def status(base_dir) -> dict:
    """What there is, for the dashboard to report and for a person to
    read when search is not doing what they expected."""
    built = None
    if vectors_present(base_dir):
        try:
            built = json.loads(manifest_path(base_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            built = None
    return {
        "model": model_present(base_dir),
        "vectors": bool(built),
        "sections": (built or {}).get("sections"),
        "signature": (built or {}).get("signature"),
        "model_name": (built or {}).get("model"),
    }


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class Model:
    """The ONNX encoder, loaded once.

    Imports numpy, onnxruntime and tokenizers inside the constructor
    rather than at module scope. None of them is needed to run this
    project -- they are what you install if you want the semantic half --
    and an import at the top would make the whole of search depend on
    them being present."""

    def __init__(self, directory, threads: int = 1):
        import numpy as np
        import onnxruntime
        from tokenizers import Tokenizer

        self._np = np
        directory = Path(directory)
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding()

        options = onnxruntime.SessionOptions()
        # One thread by default. This runs on a 2-4 GB VPS beside a web
        # server, where finishing a build slightly sooner is worth less
        # than not making every other request slow while it runs.
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = threads
        self.session = onnxruntime.InferenceSession(
            str(directory / "model.onnx"), sess_options=options,
            providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list, batch: int = 16):
        """Texts to unit vectors, one row each.

        CLS pooling then L2 normalisation, which is what bge-v1.5 is
        trained for -- mean pooling instead would quietly cost accuracy
        in a way nothing here would catch. Normalised, so a cosine is a
        dot product and the whole search is one matrix multiply."""
        np = self._np
        out = []
        for start in range(0, len(texts), batch):
            chunk = texts[start:start + batch]
            encoded = self.tokenizer.encode_batch(chunk)
            feed = {
                "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            }
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in encoded], dtype=np.int64)
            hidden = self.session.run(None, {k: v for k, v in feed.items()
                                             if k in self._inputs})[0]
            cls = hidden[:, 0]
            norms = np.linalg.norm(cls, axis=1, keepdims=True)
            out.append(cls / np.maximum(norms, 1e-12))
        return np.vstack(out).astype("float32") if out else np.zeros((0, DIMENSIONS), "float32")


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def section_texts(conn) -> list:
    """One text per section of the corpus, from the index itself.

    The index already holds every provision with the section page it
    belongs to, so the sections are a grouping of it rather than a second
    pass over the parses -- which also means the vectors cannot disagree
    with the index about what the corpus contains.

    Superseded reprints are left out. They would double the work to
    answer a question about the law as it stands, which is what somebody
    searching almost always means."""
    rows = conn.execute(
        """SELECT d.site_slug, f.page, f.label, f.heading, f.body
           FROM node_fts f JOIN doc d ON d.id = f.doc_id
           WHERE d.is_current = 1
           ORDER BY d.site_slug, f.page, f.rowid""").fetchall()
    sections: dict = {}
    for row in rows:
        key = (row["site_slug"], row["page"])
        if key not in sections:
            # The label leads, because "Section 123 Contravention of
            # family violence intervention order" says what the section
            # is in the words a person would use to ask for it.
            sections[key] = [row["label"] or "", row["heading"] or ""]
        if row["body"]:
            sections[key].append(row["body"])
    return [{"site_slug": slug, "page": page, "text": " ".join(p for p in parts if p)}
            for (slug, page), parts in sections.items()]


def build(base_dir, signature: str = "", threads: int = 1, log=None) -> dict:
    """Embeds every current section and writes the vectors beside the
    index.

    `signature` is the index's own stamp, kept in the manifest so a
    vector file built from a different corpus can be recognised as such
    instead of quietly answering about documents that have changed."""
    import numpy as np

    from . import search

    base = Path(base_dir)
    if not model_present(base):
        raise FileNotFoundError(
            f"No model in {model_dir(base)}. Run download_search_model.py first.")

    conn = search.Index(base).connection()
    sections = section_texts(conn)
    if log:
        log(f"embedding {len(sections)} sections")

    model = Model(model_dir(base), threads=threads)
    vectors = model.encode([s["text"] for s in sections])
    if vectors.shape[1] != DIMENSIONS:
        raise ValueError(f"model produced {vectors.shape[1]} dimensions, expected {DIMENSIONS}")

    # Written beside the index and moved into place, for the same reason
    # the index is: a reader must never open a half-written file.
    target = vectors_path(base)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + ".building")
    # Through a file handle, because np.save adds ".npy" to a path that
    # does not end in it and would leave the scratch file somewhere else.
    with open(scratch, "wb") as handle:
        np.save(handle, vectors)
    os.replace(scratch, target)
    manifest_path(base).write_text(json.dumps({
        "model": MODEL_DIRNAME,
        "dimensions": DIMENSIONS,
        "sections": [{"site_slug": s["site_slug"], "page": s["page"]} for s in sections],
        "signature": signature or search.signature(base),
    }), encoding="utf-8")
    return {"sections": len(sections), "path": str(target)}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class Vectors:
    """The built vectors, memory-mapped, and the model to embed a query
    with.

    At 6,173 x 768 a brute-force dot product is a few milliseconds, so
    there is no vector index here: nothing to tune, nothing to rebuild
    incrementally, and nothing that can be subtly wrong while appearing
    to work. Memory-mapped because 19 MB of float32 does not need to be
    resident on a 2 GB box when most of a query touches it once."""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self._matrix = None
        self._sections = None
        self._model = None
        self._stamp = None

    def _identity(self):
        try:
            st = vectors_path(self.base_dir).stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def available(self) -> bool:
        return self._identity() is not None and model_present(self.base_dir)

    def load(self) -> bool:
        """Opens the vectors, or says there are none. Reloads when the
        file has been replaced -- the same staleness rule the index uses,
        and for the same reason: a rebuild while the site is running must
        not leave it answering from what was there before."""
        stamp = self._identity()
        if stamp is None:
            return False
        if self._matrix is None or stamp != self._stamp:
            try:
                import numpy as np

                manifest = json.loads(manifest_path(self.base_dir).read_text(encoding="utf-8"))
                matrix = np.load(vectors_path(self.base_dir), mmap_mode="r")
            except ImportError:
                # Vectors on disk but the libraries never installed --
                # a half-finished setup, which should read as "no
                # semantic half" rather than as a crash.
                return False
            except (OSError, ValueError):
                return False
            sections = manifest.get("sections") or []
            if matrix.shape[0] != len(sections):
                # A manifest and a matrix that disagree describe a corpus
                # neither of them holds.
                return False
            self._matrix, self._sections, self._stamp = matrix, sections, stamp
        return True

    def model(self):
        if self._model is None:
            self._model = Model(model_dir(self.base_dir))
        return self._model

    def nearest(self, query: str, limit: int = SEMANTIC_DEPTH) -> list:
        """The sections most like the query, best first.

        Returns [] rather than raising when there is nothing to read:
        this is an optional half of search, and a missing model is an
        ordinary state, not a fault."""
        if not query.strip() or not self.load() or not model_present(self.base_dir):
            return []
        import numpy as np

        vector = self.model().encode([QUERY_PREFIX + query])[0]
        scores = np.asarray(self._matrix) @ vector
        top = np.argpartition(-scores, min(limit, len(scores) - 1))[:limit]
        top = top[np.argsort(-scores[top])]
        return [{"site_slug": self._sections[i]["site_slug"],
                 "page": self._sections[i]["page"],
                 "score": float(scores[i])} for i in top]


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def fuse(lexical: list, semantic: list, key) -> list:
    """Reciprocal rank fusion of two orderings of the same rows.

    `1/(k + rank)` summed over the lists a row appears in. Positions
    only, never scores: bm25 and cosine are not on the same scale and
    never will be, and every attempt to weigh one against the other is a
    constant somebody has to tune against a handful of queries. A row in
    neither list keeps its lexical position and nothing else.

    `key` maps a row to what the semantic side knows it by -- a section,
    since that is the unit embedded, so every provision of a section
    shares its semantic rank and the lexical side decides which of them
    is the answer."""
    semantic_rank = {}
    for position, hit in enumerate(semantic, start=1):
        semantic_rank.setdefault((hit["site_slug"], hit["page"]), position)

    scored = []
    for position, row in enumerate(lexical, start=1):
        score = 1.0 / (RRF_K + position)
        rank = semantic_rank.get(key(row))
        if rank is not None:
            score += 1.0 / (RRF_K + rank)
        # position breaks ties in favour of the lexical order, which is
        # the one that was there before any of this.
        scored.append((-score, position, row))
    scored.sort(key=lambda triple: (triple[0], triple[1]))
    return [row for _score, _position, row in scored]


def main():
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Build the semantic vectors for search.")
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--threads", type=int, default=1,
                    help="onnxruntime threads (default 1, to stay polite on a small VPS)")
    args = ap.parse_args()

    started = time.time()
    stats = build(args.base_dir, threads=args.threads, log=print)
    print(f"{stats['sections']} sections in {time.time() - started:.0f}s -> {stats['path']}")


if __name__ == "__main__":
    main()
