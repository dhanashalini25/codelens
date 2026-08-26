"""Explanation, documentation generation, and test recommendations.

Each of these is a small service over the same two inputs: the code itself and
the structural facts extracted locally. The structure is always computed - even
with no model available - so these commands stay useful offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import Settings, settings as default_settings
from .index import CodeIndex
from .llm import LLMClient, Message
from .parsing import Symbol, parse
from .repo import detect_language


@dataclass
class FileStructure:
    """What can be known about a file without any model."""

    path: str
    language: str
    lines: int
    symbols: List[Symbol] = field(default_factory=list)

    @property
    def undocumented(self) -> List[Symbol]:
        return [
            s for s in self.symbols
            if not s.docstring and not s.name.startswith("_") and s.line_count > 5
        ]

    @property
    def most_complex(self) -> List[Symbol]:
        ranked = sorted(self.symbols, key=lambda s: -s.complexity)
        return [s for s in ranked if s.complexity > 1][:5]

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "language": self.language,
            "lines": self.lines,
            "symbols": [s.to_dict() for s in self.symbols],
            "undocumented": [s.qualified_name for s in self.undocumented],
            "most_complex": [
                {"name": s.qualified_name, "complexity": s.complexity} for s in self.most_complex
            ],
        }

    def render(self) -> str:
        lines = [f"{self.path} ({self.language}, {self.lines} lines)"]
        for symbol in self.symbols:
            marker = "" if symbol.docstring else "  [no docstring]"
            lines.append(
                f"  L{symbol.start_line:<5} {symbol.kind:<8} {symbol.qualified_name} "
                f"(complexity {symbol.complexity}, {symbol.line_count} lines){marker}"
            )
        return "\n".join(lines)


class Explainer:
    def __init__(
        self,
        repo_path: Path,
        client: Optional[LLMClient] = None,
        index: Optional[CodeIndex] = None,
        config: Optional[Settings] = None,
    ) -> None:
        self.config = config or default_settings
        self.repo_path = Path(repo_path).resolve()
        self.client = client or LLMClient(self.config)
        self.index = index or CodeIndex(config=self.config)

    # -- local structure ------------------------------------------------
    def structure(self, relative_path: str) -> FileStructure:
        path = self._resolve(relative_path)
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        language = detect_language(path) or "unknown"
        return FileStructure(
            path=relative_path,
            language=language,
            lines=source.count("\n") + 1,
            symbols=parse(source, language),
        )

    # -- model-backed services -------------------------------------------
    def explain(self, relative_path: str, symbol_name: Optional[str] = None) -> dict:
        """Explain a file, or one symbol within it."""
        path = self._resolve(relative_path)
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        structure = self.structure(relative_path)

        target_source = source
        target_label = relative_path
        if symbol_name:
            symbol = next(
                (s for s in structure.symbols if s.qualified_name == symbol_name
                 or s.name == symbol_name),
                None,
            )
            if symbol is None:
                raise ValueError(
                    f"No symbol named '{symbol_name}' in {relative_path}. "
                    f"Available: {', '.join(s.qualified_name for s in structure.symbols) or 'none'}"
                )
            lines = source.splitlines()
            target_source = "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
            target_label = f"{relative_path}::{symbol.qualified_name}"

        related = ""
        if self.index.load():
            hits = self.index.context_for(relative_path, target_source, k=3)
            related = "\n\n".join(f"# {h.chunk.location}\n{h.chunk.text}" for h in hits)

        prompt = (
            "TASK: explain\n\n"
            f"Structure computed locally:\n{structure.render()}\n\n"
            f"Related code elsewhere in the repository:\n{related or '(none)'}\n\n"
            f"Code to explain ({target_label}):\n```{structure.language}\n"
            f"{target_source[:12000]}\n```\n\n"
            "Explain what this does, how it works, and anything a new maintainer "
            "would get wrong. Be concise and concrete. Plain prose, no headings."
        )
        explanation = self.client.complete(
            [Message("system", "You explain code precisely and briefly."), Message("user", prompt)],
            temperature=0.2,
        )
        return {
            "path": relative_path,
            "symbol": symbol_name,
            "structure": structure.to_dict(),
            "explanation": explanation,
            "provider": self.client.provider,
        }

    def document(self, relative_path: str) -> dict:
        """Propose docstrings for undocumented public symbols."""
        path = self._resolve(relative_path)
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        structure = self.structure(relative_path)
        targets = structure.undocumented

        if not targets:
            return {
                "path": relative_path,
                "docstrings": [],
                "note": "Every public symbol already has a docstring.",
                "provider": self.client.provider,
            }

        lines = source.splitlines()
        blocks = []
        for symbol in targets[:10]:
            body = "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
            blocks.append(f"### {symbol.qualified_name} (line {symbol.start_line})\n{body[:2500]}")

        prompt = (
            "TASK: docstring\n\n"
            f"File: {relative_path} ({structure.language})\n\n"
            "Write a docstring for each symbol below. Describe what it does, its "
            "parameters, what it returns, and what it raises. Do not restate the "
            "code line by line.\n\n"
            + "\n\n".join(blocks)
            + "\n\nReturn ONLY JSON: "
            '{"docstrings": [{"symbol": "name", "line": 42, "docstring": "..."}]}'
        )
        payload = self.client.complete_json(
            [Message("system", "You write precise docstrings."), Message("user", prompt)]
        )
        return {
            "path": relative_path,
            "targets": [s.qualified_name for s in targets],
            "docstrings": payload.get("docstrings", []) if isinstance(payload, dict) else [],
            "note": payload.get("note") if isinstance(payload, dict) else None,
            "provider": self.client.provider,
        }

    def recommend_tests(self, relative_path: str) -> dict:
        """Suggest the tests this file is missing, prioritised by complexity."""
        path = self._resolve(relative_path)
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        structure = self.structure(relative_path)

        # Complexity is the priority signal: each branch is an untested path
        # until proven otherwise.
        priority = sorted(
            [s for s in structure.symbols if s.kind in {"function", "method"}],
            key=lambda s: -s.complexity,
        )[:8]

        prompt = (
            "TASK: tests\n\n"
            f"File: {relative_path} ({structure.language})\n\n"
            "Functions ranked by cyclomatic complexity (each branch is an "
            "independent path that needs coverage):\n"
            + "\n".join(f"  {s.qualified_name} - complexity {s.complexity}" for s in priority)
            + f"\n\nSource:\n```{structure.language}\n{source[:12000]}\n```\n\n"
            "Recommend the tests that would catch real bugs here. Prioritise edge "
            "cases and error paths over happy paths. Return ONLY JSON: "
            '{"tests": [{"target": "function name", "name": "test_...", '
            '"scenario": "what it exercises", "why": "the bug it would catch", '
            '"priority": "high|medium|low"}]}'
        )
        payload = self.client.complete_json(
            [Message("system", "You design tests that find bugs."), Message("user", prompt)]
        )
        return {
            "path": relative_path,
            "priority_symbols": [
                {"name": s.qualified_name, "complexity": s.complexity} for s in priority
            ],
            "tests": payload.get("tests", []) if isinstance(payload, dict) else [],
            "note": payload.get("note") if isinstance(payload, dict) else None,
            "provider": self.client.provider,
        }

    def _resolve(self, relative_path: str) -> Path:
        path = (self.repo_path / relative_path).resolve()
        # Never read outside the repository, even if the caller asks nicely.
        if self.repo_path not in path.parents and path != self.repo_path:
            raise ValueError(f"Path escapes the repository: {relative_path}")
        if not path.is_file():
            raise FileNotFoundError(f"No such file in the repository: {relative_path}")
        return path
