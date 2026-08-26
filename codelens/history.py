"""Searchable review history.

Every review is stored with its findings, so you can answer questions a single
run cannot: is this file repeatedly flagged? Did the security findings go down
after that refactor? Which rule fires most often across the repository?
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from .rules import Finding

SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    repo_id    TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    name       TEXT NOT NULL,
    files      INTEGER NOT NULL DEFAULT 0,
    lines      INTEGER NOT NULL DEFAULT 0,
    languages  TEXT NOT NULL DEFAULT '{}',
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id       TEXT NOT NULL,
    ref           TEXT NOT NULL,
    subject       TEXT,
    files_changed INTEGER NOT NULL DEFAULT 0,
    added         INTEGER NOT NULL DEFAULT 0,
    removed       INTEGER NOT NULL DEFAULT 0,
    n_findings    INTEGER NOT NULL DEFAULT 0,
    provider      TEXT NOT NULL DEFAULT 'mock',
    summary       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS findings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file       TEXT NOT NULL,
    line       INTEGER NOT NULL,
    severity   TEXT NOT NULL,
    category   TEXT NOT NULL,
    title      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    suggestion TEXT,
    source     TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    symbol     TEXT
);

CREATE INDEX IF NOT EXISTS idx_findings_review   ON findings(review_id);
CREATE INDEX IF NOT EXISTS idx_findings_file     ON findings(file);
CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id   TEXT NOT NULL,
    file      TEXT NOT NULL,
    language  TEXT NOT NULL,
    symbol    TEXT,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    text       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_repo ON chunks(repo_id);

CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class History:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- meta -----------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # -- repositories ---------------------------------------------------
    def upsert_repository(
        self, repo_id: str, path: str, name: str, files: int, lines: int, languages: dict
    ) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM repositories WHERE repo_id = ?", (repo_id,))
            conn.execute(
                "INSERT INTO repositories(repo_id, path, name, files, lines, languages) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (repo_id, path, name, files, lines, json.dumps(languages)),
            )

    def get_repository(self, repo_id: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM repositories WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        record["languages"] = json.loads(record["languages"])
        return record

    def list_repositories(self) -> List[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM repositories ORDER BY indexed_at DESC"
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["languages"] = json.loads(record["languages"])
            out.append(record)
        return out

    # -- reviews --------------------------------------------------------
    def save_review(
        self,
        repo_id: str,
        ref: str,
        subject: Optional[str],
        files_changed: int,
        added: int,
        removed: int,
        provider: str,
        summary: str,
        findings: List[Finding],
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO reviews(repo_id, ref, subject, files_changed, added, removed, "
                "n_findings, provider, summary) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_id, ref, subject, files_changed, added, removed,
                    len(findings), provider, summary,
                ),
            )
            review_id = int(cursor.lastrowid)
            for finding in findings:
                conn.execute(
                    "INSERT INTO findings(review_id, file, line, severity, category, title, "
                    "detail, suggestion, source, confidence, symbol) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        review_id, finding.file, finding.line, finding.severity,
                        finding.category, finding.title, finding.detail, finding.suggestion,
                        finding.source, finding.confidence, finding.symbol,
                    ),
                )
        return review_id

    def get_review(self, review_id: int) -> Optional[dict]:
        with self.connect() as conn:
            review = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            if not review:
                return None
            findings = conn.execute(
                "SELECT * FROM findings WHERE review_id = ? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, file, line",
                (review_id,),
            ).fetchall()
        record = dict(review)
        record["findings"] = [dict(row) for row in findings]
        return record

    def list_reviews(self, repo_id: Optional[str] = None, limit: int = 20) -> List[dict]:
        query = "SELECT * FROM reviews"
        params: list = []
        if repo_id:
            query += " WHERE repo_id = ?"
            params.append(repo_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def search_findings(
        self,
        query: Optional[str] = None,
        severity: Optional[str] = None,
        category: Optional[str] = None,
        file_like: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        """Search across every review ever run."""
        sql = (
            "SELECT f.*, r.ref, r.created_at, r.repo_id FROM findings f "
            "JOIN reviews r ON r.id = f.review_id WHERE 1=1"
        )
        params: list = []
        if query:
            sql += " AND (f.title LIKE ? OR f.detail LIKE ? OR f.symbol LIKE ?)"
            params.extend([f"%{query}%"] * 3)
        if severity:
            sql += " AND f.severity = ?"
            params.append(severity)
        if category:
            sql += " AND f.category = ?"
            params.append(category)
        if file_like:
            sql += " AND f.file LIKE ?"
            params.append(f"%{file_like}%")
        sql += " ORDER BY f.id DESC LIMIT ?"
        params.append(limit)

        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def finding_stats(self, repo_id: Optional[str] = None) -> dict:
        """Aggregate counts - the view a single review cannot give you."""
        where = " WHERE r.repo_id = ?" if repo_id else ""
        params = [repo_id] if repo_id else []
        with self.connect() as conn:
            by_severity = conn.execute(
                "SELECT f.severity, COUNT(*) AS n FROM findings f "
                f"JOIN reviews r ON r.id = f.review_id{where} GROUP BY f.severity",
                params,
            ).fetchall()
            by_category = conn.execute(
                "SELECT f.category, COUNT(*) AS n FROM findings f "
                f"JOIN reviews r ON r.id = f.review_id{where} GROUP BY f.category "
                "ORDER BY n DESC",
                params,
            ).fetchall()
            hotspots = conn.execute(
                "SELECT f.file, COUNT(*) AS n FROM findings f "
                f"JOIN reviews r ON r.id = f.review_id{where} GROUP BY f.file "
                "ORDER BY n DESC LIMIT 10",
                params,
            ).fetchall()
            reviews = conn.execute(
                "SELECT COUNT(*) AS n FROM reviews"
                + (" WHERE repo_id = ?" if repo_id else ""),
                params,
            ).fetchone()
        return {
            "reviews": reviews["n"] if reviews else 0,
            "by_severity": {row["severity"]: row["n"] for row in by_severity},
            "by_category": {row["category"]: row["n"] for row in by_category},
            "hotspots": [{"file": row["file"], "findings": row["n"]} for row in hotspots],
        }
