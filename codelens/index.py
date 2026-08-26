"""Code index: chunk a repository by symbol and make it searchable.

Chunking on symbol boundaries rather than fixed line windows matters here.
A function cut in half retrieves badly and reads worse when it is handed to a
model as review context.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .config import Settings, settings as default_settings
from .embeddings import build_embedder, cosine_scores, top_indices
from .history import History
from .parsing import parse
from .repo import SourceFile, summarize, walk

EMBEDDER_STATE_FILE = "code_embedder.pkl"


@dataclass
class CodeChunk:
    file: str
    language: str
    symbol: Optional[str]
    start_line: int
    end_line: int
    text: str

    @property
    def location(self) -> str:
        return f"{self.file}:{self.start_line}-{self.end_line}"


@dataclass
class CodeHit:
    chunk: CodeChunk
    score: float

    def to_dict(self) -> dict:
        return {
            "location": self.chunk.location,
            "file": self.chunk.file,
            "symbol": self.chunk.symbol,
            "start_line": self.chunk.start_line,
            "end_line": self.chunk.end_line,
            "score": round(self.score, 4),
            "text": self.chunk.text,
        }


def repo_id_for(path: Path) -> str:
    return hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:16]


# How many times the symbol name is repeated in the text that gets embedded.
# A function's name is the densest description of it that exists - `find_user`
# says more about what the code does than the twenty lines inside it. Repeating
# it lifts its terms above the body's incidental vocabulary.
#
# Measured with `python -m eval.search_eval --sweep`: weight 0 scores top-1
# 4/10 and top-5 7/10; weight 8 scores 6/10 and 9/10. The gain flattens past 8.
# Ten queries is a small benchmark - treat a one-point difference as noise and
# only trust the shape of the curve.
SYMBOL_WEIGHT = 8


def embedding_text(chunk: "CodeChunk") -> str:
    """The text actually embedded - the chunk body, with its name emphasised.

    Note this is NOT what gets stored or shown; `chunk.text` stays clean. Only
    the vector sees the repetition.
    """
    if not chunk.symbol:
        return chunk.text
    # Split on the qualifier too, so `History.save_review` contributes
    # "history", "save" and "review" rather than one opaque token.
    name = chunk.symbol.replace(".", " ")
    return ((name + " ") * SYMBOL_WEIGHT) + "\n" + chunk.text


def chunk_file(source: SourceFile, chunk_lines: int, overlap: int) -> List[CodeChunk]:
    """Split a file on symbol boundaries, falling back to line windows."""
    lines = source.text.splitlines()
    symbols = parse(source.text, source.language)
    chunks: List[CodeChunk] = []

    if symbols:
        for symbol in symbols:
            if symbol.kind == "class" and any(
                s.parent == symbol.name for s in symbols
            ):
                # Methods are indexed individually; skip the whole-class chunk
                # so the class body is not duplicated in the index.
                continue
            start = max(symbol.start_line - 1, 0)
            end = min(symbol.end_line, len(lines))
            body = "\n".join(lines[start:end]).strip()
            if body:
                chunks.append(
                    CodeChunk(
                        file=source.relpath,
                        language=source.language,
                        symbol=symbol.qualified_name,
                        start_line=symbol.start_line,
                        end_line=max(symbol.end_line, symbol.start_line),
                        text=body,
                    )
                )

    if not chunks:
        step = max(chunk_lines - overlap, 1)
        for start in range(0, len(lines), step):
            body = "\n".join(lines[start : start + chunk_lines]).strip()
            if body:
                chunks.append(
                    CodeChunk(
                        file=source.relpath,
                        language=source.language,
                        symbol=None,
                        start_line=start + 1,
                        end_line=min(start + chunk_lines, len(lines)),
                        text=body,
                    )
                )
    return chunks


class CodeIndex:
    def __init__(
        self,
        history: Optional[History] = None,
        config: Optional[Settings] = None,
    ) -> None:
        self.config = config or default_settings
        self.history = history or History(self.config.db_path)
        self._chunks: List[CodeChunk] = []
        self._matrix: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._embedder = None

    # -- building -------------------------------------------------------
    def build(self, root: Path) -> dict:
        """Index a repository from scratch. Returns a summary."""
        root = Path(root).resolve()
        files = walk(root, max_file_bytes=self.config.max_file_bytes)
        if not files:
            raise ValueError(f"No indexable source files found under {root}")

        repo = repo_id_for(root)
        chunks: List[CodeChunk] = []
        for source in files:
            chunks.extend(
                chunk_file(source, self.config.chunk_lines, self.config.chunk_overlap_lines)
            )
        if not chunks:
            raise ValueError("Files were found but produced no chunks.")

        languages = summarize(files)
        self.history.upsert_repository(
            repo_id=repo,
            path=str(root),
            name=root.name,
            files=len(files),
            lines=sum(f.lines for f in files),
            languages=languages,
        )

        with self.history.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE repo_id = ?", (repo,))
            ids: List[int] = []
            for chunk in chunks:
                cursor = conn.execute(
                    "INSERT INTO chunks(repo_id, file, language, symbol, start_line, end_line, text) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        repo, chunk.file, chunk.language, chunk.symbol,
                        chunk.start_line, chunk.end_line, chunk.text,
                    ),
                )
                ids.append(int(cursor.lastrowid))

        embedder = build_embedder(
            self.config.embedding_backend, self.config.embedding_model, self.config.embedding_dim
        )
        texts = [embedding_text(c) for c in chunks]
        embedder.fit(texts)
        matrix = embedder.encode(texts)

        with self.history.connect() as conn:
            conn.execute("DELETE FROM vectors")
            for chunk_id, row in zip(ids, matrix.astype(np.float32)):
                conn.execute(
                    "INSERT INTO vectors(chunk_id, dim, vector) VALUES(?, ?, ?)",
                    (chunk_id, int(matrix.shape[1]), row.tobytes()),
                )

        self.history.set_meta("embedding_backend", embedder.name)
        self.history.set_meta("indexed_repo", repo)
        embedder.save(Path(self.config.data_dir) / EMBEDDER_STATE_FILE)

        self._chunks, self._matrix, self._embedder = chunks, matrix, embedder
        return {
            "repo_id": repo,
            "root": str(root),
            "files": len(files),
            "chunks": len(chunks),
            "lines": sum(f.lines for f in files),
            "languages": languages,
            "embedding_backend": embedder.name,
        }

    # -- loading --------------------------------------------------------
    def load(self) -> bool:
        """Load a previously built index. False if there is nothing to load."""
        chunks, matrix = self._load_matrix()
        if not chunks:
            return False
        embedder = build_embedder(
            self.history.get_meta("embedding_backend") or self.config.embedding_backend,
            self.config.embedding_model,
            self.config.embedding_dim,
        )
        if embedder.name == "tfidf":
            if not embedder.load(Path(self.config.data_dir) / EMBEDDER_STATE_FILE):
                raise RuntimeError("Index found but embedder state is missing. Re-run `index`.")
        self._chunks, self._matrix, self._embedder = chunks, matrix, embedder
        return True

    def _load_matrix(self) -> Tuple[List[CodeChunk], np.ndarray]:
        with self.history.connect() as conn:
            rows = conn.execute(
                "SELECT c.file, c.language, c.symbol, c.start_line, c.end_line, c.text, v.vector "
                "FROM chunks c JOIN vectors v ON v.chunk_id = c.id ORDER BY c.id"
            ).fetchall()
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        chunks = [
            CodeChunk(
                file=row["file"],
                language=row["language"],
                symbol=row["symbol"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                text=row["text"],
            )
            for row in rows
        ]
        matrix = np.vstack([np.frombuffer(row["vector"], dtype=np.float32) for row in rows])
        return chunks, matrix

    # -- searching ------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self._chunks

    def search(self, query: str, k: int = 5) -> List[CodeHit]:
        if self.is_empty and not self.load():
            return []
        scores = cosine_scores(self._embedder.encode([query]), self._matrix)
        return [
            CodeHit(chunk=self._chunks[i], score=float(scores[i]))
            for i in top_indices(scores, k)
        ]

    def context_for(self, file: str, text: str, k: int = 4) -> List[CodeHit]:
        """Find related code elsewhere in the repository.

        Used to give the reviewer context a plain diff does not carry: the
        callers, the sibling implementations, the test that covers this path.
        The changed file itself is excluded - the diff already contains it.
        """
        hits = self.search(text, k=k * 3)
        return [hit for hit in hits if hit.chunk.file != file][:k]
