"""Structural understanding of source files.

Python gets a real parse through the standard library `ast` module: exact
symbol boundaries, signatures, docstring presence, decorators. Everything else
gets a regex pass that finds top-level declarations well enough to chunk on.

Deliberately no tree-sitter. It would give better multi-language parsing at the
cost of a native dependency and a grammar build step, and the review quality
gain is small when the model reads the source text anyway. That trade is worth
being able to defend in an interview - it is a real engineering decision, not
an omission.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Symbol:
    """A named, addressable piece of code."""

    name: str
    kind: str  # function | method | class | unknown
    start_line: int
    end_line: int
    signature: str = ""
    docstring: bool = False
    parent: Optional[str] = None
    complexity: int = 1
    decorators: List[str] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name

    @property
    def line_count(self) -> int:
        return max(self.end_line - self.start_line + 1, 1)

    def to_dict(self) -> dict:
        return {
            "name": self.qualified_name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "lines": self.line_count,
            "signature": self.signature,
            "docstring": self.docstring,
            "complexity": self.complexity,
            "decorators": self.decorators,
        }


# Branch nodes each add one independent path through a function.
_BRANCH_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.With, ast.AsyncWith, ast.Assert, ast.IfExp,
)


def _cyclomatic_complexity(node: ast.AST) -> int:
    """Approximate cyclomatic complexity: one plus the number of branches.

    Not a substitute for a real static analyser - it is a cheap triage signal
    that tells the reviewer which functions deserve attention first.
    """
    score = 1
    for child in ast.walk(node):
        if isinstance(child, _BRANCH_NODES):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(len(child.values) - 1, 0)
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
        elif isinstance(child, ast.Match):  # py3.10+
            score += max(len(child.cases) - 1, 0)
    return score


def _signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = [a.arg for a in node.args.args]
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        args.extend(a.arg for a in node.args.kwonlyargs)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        bases = [getattr(base, "id", getattr(base, "attr", "?")) for base in node.bases]
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    return ""


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return "?"


def parse_python(source: str) -> List[Symbol]:
    """Extract symbols from Python source. Returns [] on a syntax error."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    symbols: List[Symbol] = []

    def visit(node: ast.AST, parent: Optional[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    Symbol(
                        name=child.name,
                        kind="method" if parent else "function",
                        start_line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                        signature=_signature(child),
                        docstring=ast.get_docstring(child) is not None,
                        parent=parent,
                        complexity=_cyclomatic_complexity(child),
                        decorators=[_decorator_name(d) for d in child.decorator_list],
                    )
                )
                visit(child, parent)  # nested defs keep the outer parent
            elif isinstance(child, ast.ClassDef):
                symbols.append(
                    Symbol(
                        name=child.name,
                        kind="class",
                        start_line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                        signature=_signature(child),
                        docstring=ast.get_docstring(child) is not None,
                        parent=parent,
                        complexity=1,
                        decorators=[_decorator_name(d) for d in child.decorator_list],
                    )
                )
                visit(child, child.name)

    visit(tree, None)
    return sorted(symbols, key=lambda s: s.start_line)


_GENERIC_PATTERNS: Dict[str, List[tuple]] = {
    "javascript": [
        (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
        (r"^\s*(?:export\s+)?class\s+(\w+)", "class"),
        (r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(", "function"),
    ],
    "go": [
        (r"^func\s+(?:\([^)]*\)\s*)?(\w+)", "function"),
        (r"^type\s+(\w+)\s+struct", "class"),
    ],
    "rust": [
        (r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", "function"),
        (r"^\s*(?:pub\s+)?struct\s+(\w+)", "class"),
        (r"^\s*impl\s+(?:\w+\s+for\s+)?(\w+)", "class"),
    ],
    "java": [
        (r"^\s*(?:public|private|protected).*?\s(\w+)\s*\([^;]*\)\s*\{", "method"),
        (r"^\s*(?:public\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
    ],
    "ruby": [(r"^\s*def\s+(\w+)", "function"), (r"^\s*class\s+(\w+)", "class")],
    "php": [(r"^\s*function\s+(\w+)", "function"), (r"^\s*class\s+(\w+)", "class")],
    "csharp": [
        (r"^\s*(?:public|private|protected|internal).*?\s(\w+)\s*\(", "method"),
        (r"^\s*(?:public\s+)?class\s+(\w+)", "class"),
    ],
}
_GENERIC_PATTERNS["typescript"] = _GENERIC_PATTERNS["javascript"]


def parse_generic(source: str, language: str) -> List[Symbol]:
    """Regex-based declaration finder for non-Python languages."""
    patterns = _GENERIC_PATTERNS.get(language)
    if not patterns:
        return []

    lines = source.splitlines()
    symbols: List[Symbol] = []
    for number, line in enumerate(lines, start=1):
        for pattern, kind in patterns:
            match = re.match(pattern, line)
            if match:
                symbols.append(
                    Symbol(
                        name=match.group(1),
                        kind=kind,
                        start_line=number,
                        end_line=number,
                        signature=line.strip()[:120],
                    )
                )
                break

    # Each declaration runs until the next one starts.
    for index, symbol in enumerate(symbols):
        next_start = symbols[index + 1].start_line if index + 1 < len(symbols) else len(lines)
        symbol.end_line = max(symbol.start_line, next_start - 1)
    return symbols


def parse(source: str, language: str) -> List[Symbol]:
    """Parse with the best strategy available for this language."""
    if language == "python":
        return parse_python(source)
    return parse_generic(source, language)


def find_symbol_at_line(symbols: List[Symbol], line: int) -> Optional[Symbol]:
    """Which symbol contains this line? Innermost (smallest) wins."""
    containing = [s for s in symbols if s.start_line <= line <= s.end_line]
    if not containing:
        return None
    return min(containing, key=lambda s: s.line_count)
