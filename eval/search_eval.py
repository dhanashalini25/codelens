"""Code search benchmark.

Ten natural-language questions with a known correct symbol, run against
CodeLens's own source. Small, but enough to settle arguments with a number
instead of an opinion - which is exactly what it was built for.

Two configuration choices in this repository came from this file:

  * Symbol weighting (index.SYMBOL_WEIGHT) - top-1 4/10 -> 7/10
  * Pivoted length normalization (embeddings.LENGTH_NORM_B) - looked obviously
    right, measured worse (top-5 8/10 -> 6/10), left off.

Usage:
    python -m eval.search_eval                  # score the current settings
    python -m eval.search_eval --sweep          # compare configurations
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Tuple

from codelens.embeddings import TfidfEmbedder, cosine_scores, top_indices
from codelens.index import chunk_file, embedding_text
from codelens.repo import walk

# (question, the symbol that should be returned)
QUERIES: List[Tuple[str, str]] = [
    ("parse a unified diff into hunks", "parse_unified_diff"),
    ("compute cyclomatic complexity", "_cyclomatic_complexity"),
    ("walk a repository and skip vendored directories", "walk"),
    ("shallow clone a git repository", "clone"),
    ("extract json from a model response", "extract_json"),
    ("split a file into chunks on symbol boundaries", "chunk_file"),
    ("search findings across every review", "History.search_findings"),
    ("recommend tests prioritised by complexity", "Explainer.recommend_tests"),
    ("detect a bare except handler", "scan_python"),
    ("post findings and save a review to the database", "History.save_review"),
]


def load_chunks(root: Path):
    chunks = []
    for source in walk(root):
        chunks.extend(chunk_file(source, 60, 10))
    if not chunks:
        raise SystemExit(f"No indexable code found under {root}")
    return chunks


def score(chunks, dim: int, ngram: int, b: float, symbol_weight: int) -> Tuple[int, int]:
    embedder = TfidfEmbedder(dim=dim, ngram_max=ngram)
    embedder.LENGTH_NORM_B = b

    if symbol_weight is None:
        texts = [embedding_text(c) for c in chunks]          # current settings
    else:
        texts = [
            (((c.symbol or "").replace(".", " ") + " ") * symbol_weight) + "\n" + c.text
            for c in chunks
        ]

    embedder.fit(texts)
    matrix = embedder.encode(texts)

    top1 = top5 = 0
    for question, expected in QUERIES:
        scores = cosine_scores(embedder.encode([question]), matrix)
        ranked = [chunks[i].symbol or "" for i in top_indices(scores, 5)]
        if ranked and ranked[0] == expected:
            top1 += 1
        if expected in ranked:
            top5 += 1
    return top1, top5


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark CodeLens code search.")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--sweep", action="store_true", help="compare configurations")
    args = parser.parse_args()

    chunks = load_chunks(args.repo)
    total = len(QUERIES)
    print(f"\n  {len(chunks)} chunks indexed from {args.repo.resolve().name}\n")

    if not args.sweep:
        top1, top5 = score(chunks, 8192, 2, 0.0, None)
        print(f"  current settings     top-1 {top1}/{total}   top-5 {top5}/{total}\n")
        for question, expected in QUERIES:
            print(f"    - {question}  ->  {expected}")
        print()
        return

    print("  symbol weighting (dim=8192, bigrams, b=0):")
    for weight in (0, 1, 3, 6, 8, 10):
        top1, top5 = score(chunks, 8192, 2, 0.0, weight)
        print(f"    weight {weight:<3}          top-1 {top1}/{total}   top-5 {top5}/{total}")

    print("\n  length normalization (dim=8192, bigrams, symbol weight 8):")
    for b in (0.0, 0.4, 0.75, 1.0):
        top1, top5 = score(chunks, 8192, 2, b, 8)
        print(f"    b={b:<16} top-1 {top1}/{total}   top-5 {top5}/{total}")

    print("\n  hash dimension (bigrams, symbol weight 8, b=0):")
    for dim in (2048, 8192, 32768):
        top1, top5 = score(chunks, dim, 2, 0.0, 8)
        print(f"    dim={dim:<14} top-1 {top1}/{total}   top-5 {top5}/{total}")
    print()


if __name__ == "__main__":
    main()
