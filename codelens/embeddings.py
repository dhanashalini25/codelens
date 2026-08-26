"""Embeddings for code search, with a zero-download, zero-compile default.

Hashed TF-IDF in pure numpy. No scikit-learn, no scipy - those ship compiled
extension modules, and on a locked-down Windows machine an Application Control
policy blocks the DLL outright. A tool that will not import is worse than a
tool with a simpler algorithm.

Code search benefits from the lexical backend more than prose does: identifiers
are exact tokens, and an exact match on `parse_hunk` is usually what you wanted.
Set CODELENS_EMBEDDING_BACKEND=st for dense embeddings via sentence-transformers.
"""
from __future__ import annotations

import math
import pickle
import re
import zlib
from pathlib import Path
from typing import List, Sequence

import numpy as np

# Split identifiers so `parseUnifiedDiff` and `parse_unified_diff` both index
# as parse/unified/diff. Without this, camelCase code is nearly unsearchable.
_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def tokenize_code(text: str) -> List[str]:
    return [part.lower() for part in _SPLIT.split(text) if part]


def code_features(text: str, ngram_max: int = 2) -> List[str]:
    """Identifier parts plus adjacent pairs, so `open file` beats a lone `file`."""
    words = tokenize_code(text)
    features = list(words)
    for n in range(2, ngram_max + 1):
        features.extend(" ".join(words[i : i + n]) for i in range(len(words) - n + 1))
    return features


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def bucket(term: str, dim: int) -> int:
    """CRC32, not Python's `hash()`.

    String hashing is randomized per process, so an index built in one run
    would be unreadable in the next - a bug that surfaces as silently useless
    search rather than an error.
    """
    return zlib.crc32(term.encode("utf-8")) % dim


class Embedder:
    name = "base"
    dim = 0

    def fit(self, corpus: Sequence[str]) -> None:
        pass

    def encode(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def save(self, path: Path) -> None:
        pass

    def load(self, path: Path) -> bool:
        return True


class TfidfEmbedder(Embedder):
    """Hashed TF-IDF in pure numpy. No compiled dependencies."""

    name = "tfidf"

    # Pivoted length normalization (BM25's `b`). Short chunks concentrating a
    # few terms *look* like they should dominate cosine ranking, so this was
    # set to 0.75 - and measurement said otherwise: on eval/search_eval.py,
    # b=0.75 scored top-1 4/10 and top-5 8/10, against 6/10 and 9/10 for plain
    # cosine. Left at 0 deliberately, with the number recorded, so nobody
    # "fixes" it back on intuition. Symbol weighting (see index.py) was the
    # change that actually helped.
    LENGTH_NORM_B = 0.0

    def __init__(self, dim: int = 8192, ngram_max: int = 2) -> None:
        self.dim = max(int(dim), 256)
        self.ngram_max = ngram_max
        self._idf: np.ndarray | None = None
        self._avg_length: float = 1.0

    def fit(self, corpus: Sequence[str]) -> None:
        corpus = [c for c in corpus if c.strip()]
        if not corpus:
            raise ValueError("Cannot fit on an empty corpus.")

        document_frequency = np.zeros(self.dim, dtype=np.float32)
        lengths: List[int] = []
        for document in corpus:
            features = code_features(document, self.ngram_max)
            lengths.append(max(len(features), 1))
            for index in {bucket(term, self.dim) for term in features}:
                document_frequency[index] += 1.0

        n_documents = len(corpus)
        self._idf = (
            np.log((1.0 + n_documents) / (1.0 + document_frequency)).astype(np.float32) + 1.0
        )
        self._avg_length = float(sum(lengths)) / len(lengths)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if self._idf is None:
            raise RuntimeError("fit() must run before encode().")

        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            features = code_features(text, self.ngram_max)
            counts: dict = {}
            for term in features:
                index = bucket(term, self.dim)
                counts[index] = counts.get(index, 0) + 1
            for index, count in counts.items():
                # Sublinear term frequency matters more in code than in prose:
                # a loop variable can appear fifty times without being the point
                # of the function.
                matrix[row, index] = (1.0 + math.log(count)) * self._idf[index]

            if self.LENGTH_NORM_B:
                b = self.LENGTH_NORM_B
                pivot = (1.0 - b) + b * (max(len(features), 1) / self._avg_length)
                matrix[row] /= pivot

        # b == 0 is the measured default: plain cosine.
        return matrix if self.LENGTH_NORM_B else normalize_rows(matrix)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            pickle.dump(
                {
                    "idf": self._idf,
                    "dim": self.dim,
                    "ngram_max": self.ngram_max,
                    "avg_length": self._avg_length,
                },
                handle,
            )

    def load(self, path: Path) -> bool:
        if not path.exists():
            return False
        with path.open("rb") as handle:
            state = pickle.load(handle)
        self._idf = state["idf"]
        self.dim = state["dim"]
        self.ngram_max = state.get("ngram_max", 2)
        self._avg_length = state.get("avg_length", 1.0)
        return True


class SentenceTransformerEmbedder(Embedder):
    name = "st"

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        vectors = self._model.encode(
            list(texts), batch_size=32, show_progress_bar=False, convert_to_numpy=True
        )
        return normalize_rows(vectors)


def build_embedder(backend: str, model_name: str, dim: int) -> Embedder:
    backend = (backend or "auto").lower()
    if backend in {"auto", "st"}:
        try:
            return SentenceTransformerEmbedder(model_name)
        except Exception as exc:
            if backend == "st":
                raise RuntimeError(f"sentence-transformers unavailable: {exc}") from exc
    return TfidfEmbedder(dim=dim)


def cosine_scores(query_vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return np.zeros(0, dtype=np.float32)
    return matrix @ normalize_rows(query_vector)[0]


def top_indices(scores: np.ndarray, k: int) -> List[int]:
    if scores.size == 0:
        return []
    k = min(k, scores.size)
    candidates = np.argpartition(-scores, k - 1)[:k]
    return [int(i) for i in candidates[np.argsort(-scores[candidates])]]
