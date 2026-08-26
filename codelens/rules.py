"""Deterministic static checks that need no model.

These exist for three reasons. They are free and instant, so they run on every
review. They are reproducible, so they can be unit-tested - unlike an LLM's
output. And they catch the boring, high-frequency issues, which leaves the
model's attention (and your token budget) for the ones that need judgement.

Every rule reports the line number of the offending code so a finding is
actionable rather than advisory.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .parsing import Symbol, parse

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Finding:
    """One reviewable issue, from either a static rule or the model."""

    file: str
    line: int
    severity: str
    category: str
    title: str
    detail: str
    suggestion: str = ""
    source: str = "static"  # static | llm
    confidence: float = 1.0
    symbol: Optional[str] = None

    def key(self) -> tuple:
        return (self.file, self.line, self.category)

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "source": self.source,
            "confidence": round(self.confidence, 2),
            "symbol": self.symbol,
        }


# Patterns that look like committed credentials. Kept deliberately narrow -
# a noisy secret scanner gets muted, and a muted scanner catches nothing.
_SECRET_PATTERNS = [
    (r"(?i)\b(aws_secret_access_key|aws_access_key_id)\b\s*[=:]\s*['\"][^'\"]{8,}", "AWS credential"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[=:]\s*['\"][A-Za-z0-9_\-]{16,}['\"]", "hardcoded credential"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "GitHub token"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI-style API key"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
]

_TODO_PATTERN = re.compile(r"(?i)#\s*(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)")
_LONG_LINE = 120


# A UTF-8 BOM decoded as text leaves U+FEFF at the front of the string, and
# ast.parse rejects it as "invalid non-printable character". Readers use
# utf-8-sig, but rules are also called with source from other places, so strip
# it here too rather than trusting every caller.
BOM = "\ufeff"


def strip_bom(source: str) -> str:
    return source[1:] if source.startswith(BOM) else source


def scan_text(path: str, source: str, only_lines: Optional[List[int]] = None) -> List[Finding]:
    """Language-agnostic line checks."""
    source = strip_bom(source)
    findings: List[Finding] = []
    wanted = set(only_lines) if only_lines else None

    for number, line in enumerate(source.splitlines(), start=1):
        if wanted is not None and number not in wanted:
            continue

        for pattern, label in _SECRET_PATTERNS:
            if re.search(pattern, line):
                findings.append(
                    Finding(
                        file=path,
                        line=number,
                        severity="critical",
                        category="security",
                        title=f"Possible {label} committed to the repository",
                        detail=(
                            "A value on this line matches the shape of a real credential. "
                            "If it is genuine, it must be rotated - removing it from the "
                            "working tree does not remove it from git history."
                        ),
                        suggestion="Move the value to an environment variable and rotate the key.",
                    )
                )
                break

        match = _TODO_PATTERN.search(line)
        if match:
            note = match.group(2).strip()
            findings.append(
                Finding(
                    file=path,
                    line=number,
                    severity="info",
                    category="maintainability",
                    title=f"{match.group(1).upper()} comment",
                    detail=note or "Unresolved marker left in the code.",
                    suggestion="Track it in an issue, or resolve it before merge.",
                )
            )

        if len(line) > _LONG_LINE:
            findings.append(
                Finding(
                    file=path,
                    line=number,
                    severity="low",
                    category="style",
                    title=f"Line is {len(line)} characters",
                    detail=f"Lines over {_LONG_LINE} characters are hard to review side by side.",
                    suggestion="Wrap the line or extract part of the expression.",
                )
            )

    return findings


def scan_python(path: str, source: str, only_lines: Optional[List[int]] = None) -> List[Finding]:
    """AST-based checks. Silent on files that do not parse."""
    source = strip_bom(source)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            Finding(
                file=path,
                line=exc.lineno or 1,
                severity="high",
                category="correctness",
                title="File does not parse",
                detail=f"SyntaxError: {exc.msg}",
                suggestion="Fix the syntax error before review.",
            )
        ]

    wanted = set(only_lines) if only_lines else None
    findings: List[Finding] = []

    def in_scope(node: ast.AST, symbol: Optional[Symbol] = None) -> bool:
        if wanted is None:
            return True
        if symbol is not None:
            return any(symbol.start_line <= n <= symbol.end_line for n in wanted)
        return getattr(node, "lineno", -1) in wanted

    for node in ast.walk(tree):
        # Bare `except:` swallows KeyboardInterrupt and SystemExit too.
        if isinstance(node, ast.ExceptHandler) and node.type is None and in_scope(node):
            findings.append(
                Finding(
                    file=path,
                    line=node.lineno,
                    severity="high",
                    category="correctness",
                    title="Bare except catches everything",
                    detail=(
                        "A bare `except:` also catches KeyboardInterrupt and SystemExit, "
                        "which makes the process hard to stop and hides real failures."
                    ),
                    suggestion="Catch the specific exception, or `except Exception:` at minimum.",
                )
            )

        # `except ...: pass` discards the error with no trace at all.
        if (
            isinstance(node, ast.ExceptHandler)
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass)
            and in_scope(node)
        ):
            findings.append(
                Finding(
                    file=path,
                    line=node.lineno,
                    severity="medium",
                    category="correctness",
                    title="Exception silently swallowed",
                    detail="The handler body is `pass`, so the failure leaves no trace.",
                    suggestion="Log the exception, or add a comment explaining why it is safe to ignore.",
                )
            )

        # Mutable default arguments are shared across every call.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and in_scope(node):
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    findings.append(
                        Finding(
                            file=path,
                            line=default.lineno,
                            severity="high",
                            category="correctness",
                            title=f"Mutable default argument in {node.name}()",
                            detail=(
                                "The default object is created once at definition time and "
                                "shared by every call, so mutations leak between calls."
                            ),
                            suggestion="Default to None and build the container inside the function.",
                            symbol=node.name,
                        )
                    )

        # `== None` / `!= None` instead of `is None`.
        if isinstance(node, ast.Compare) and in_scope(node):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant):
                    if comparator.value is None:
                        findings.append(
                            Finding(
                                file=path,
                                line=node.lineno,
                                severity="low",
                                category="style",
                                title="Comparison to None with == or !=",
                                detail="`is None` / `is not None` is the correct identity test.",
                                suggestion="Use `is None` or `is not None`.",
                            )
                        )

        # `assert` disappears entirely under `python -O`.
        if isinstance(node, ast.Assert) and in_scope(node) and "test" not in path.lower():
            findings.append(
                Finding(
                    file=path,
                    line=node.lineno,
                    severity="medium",
                    category="correctness",
                    title="assert used outside tests",
                    detail=(
                        "Assertions are stripped when Python runs with -O, so an assert "
                        "used for validation silently stops validating in production."
                    ),
                    suggestion="Raise an explicit exception instead.",
                )
            )

        # eval / exec on anything.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and in_scope(node):
            if node.func.id in {"eval", "exec"}:
                findings.append(
                    Finding(
                        file=path,
                        line=node.lineno,
                        severity="critical",
                        category="security",
                        title=f"Use of {node.func.id}()",
                        detail=(
                            f"{node.func.id}() executes arbitrary code. If any part of its "
                            "input reaches a user, this is remote code execution."
                        ),
                        suggestion="Use ast.literal_eval, a parser, or an explicit dispatch table.",
                    )
                )

        # subprocess with shell=True.
        if isinstance(node, ast.Call) and in_scope(node):
            for keyword in node.keywords:
                if (
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    findings.append(
                        Finding(
                            file=path,
                            line=node.lineno,
                            severity="high",
                            category="security",
                            title="subprocess called with shell=True",
                            detail=(
                                "With shell=True the command string is interpreted by the "
                                "shell, so any interpolated value becomes injectable."
                            ),
                            suggestion="Pass the command as a list and drop shell=True.",
                        )
                    )

    # Symbol-level checks: size, complexity, missing docstrings on public API.
    for symbol in parse(source, "python"):
        if not in_scope(tree, symbol):
            continue
        if symbol.kind in {"function", "method"} and symbol.complexity >= 11:
            findings.append(
                Finding(
                    file=path,
                    line=symbol.start_line,
                    severity="medium" if symbol.complexity < 20 else "high",
                    category="maintainability",
                    title=f"{symbol.qualified_name}() has cyclomatic complexity {symbol.complexity}",
                    detail=(
                        "Each independent path needs its own test. Above roughly 10, "
                        "functions become hard to test exhaustively and hard to change safely."
                    ),
                    suggestion="Extract the branches into named helpers.",
                    symbol=symbol.qualified_name,
                )
            )
        # Length limits differ by kind. An 80-line function is a smell; an
        # 80-line class is a normal class, and reporting it trains people to
        # ignore the tool. Classes are only flagged when they are genuinely
        # unmanageable.
        length_limit = 80 if symbol.kind in {"function", "method"} else 400
        if symbol.line_count > length_limit:
            noun = "function" if symbol.kind == "function" else symbol.kind
            findings.append(
                Finding(
                    file=path,
                    line=symbol.start_line,
                    severity="low",
                    category="maintainability",
                    title=(
                        f"{noun} {symbol.qualified_name} is {symbol.line_count} lines long"
                    ),
                    detail=(
                        f"Long {noun}s usually hold more than one responsibility."
                    ),
                    suggestion="Split it along its natural seams.",
                    symbol=symbol.qualified_name,
                )
            )
        if (
            not symbol.docstring
            and not symbol.name.startswith("_")
            and symbol.kind in {"function", "class"}
            and symbol.line_count > 10
        ):
            findings.append(
                Finding(
                    file=path,
                    line=symbol.start_line,
                    severity="info",
                    category="documentation",
                    title=f"Public {symbol.kind} {symbol.name} has no docstring",
                    detail="Public API without a docstring is guesswork for the next reader.",
                    suggestion="Add a one-line docstring describing what it does and returns.",
                    symbol=symbol.qualified_name,
                )
            )

    return findings


def analyze(
    path: str,
    source: str,
    language: str,
    only_lines: Optional[List[int]] = None,
) -> List[Finding]:
    """Run every rule that applies to this file."""
    findings = scan_text(path, source, only_lines=only_lines)
    if language == "python":
        findings.extend(scan_python(path, source, only_lines=only_lines))
    return sort_findings(dedupe(findings))


def dedupe(findings: List[Finding]) -> List[Finding]:
    """Collapse duplicates, keeping the most severe report of each issue."""
    best: dict = {}
    for finding in findings:
        existing = best.get(finding.key())
        if existing is None or SEVERITY_ORDER[finding.severity] < SEVERITY_ORDER[existing.severity]:
            best[finding.key()] = finding
    return list(best.values())


def sort_findings(findings: List[Finding]) -> List[Finding]:
    return sorted(
        findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.file, f.line)
    )
