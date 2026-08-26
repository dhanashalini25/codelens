"""Repository ingestion: walk a working tree and decide what is worth reading.

Most of the value here is in what gets *excluded*. A naive walker indexes
node_modules, minified bundles and lockfiles, then buries every real result
under vendored code.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

LANGUAGE_BY_SUFFIX: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
}

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", "out", ".next", ".nuxt", ".svelte-kit",
    "coverage", ".idea", ".vscode", "site-packages", ".terraform",
}

EXCLUDED_SUFFIXES = {
    ".min.js", ".min.css", ".map", ".lock", ".pyc", ".pyo", ".so", ".dll",
    ".dylib", ".class", ".jar", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".pdf", ".zip", ".gz", ".tar", ".woff", ".woff2", ".ttf",
}

EXCLUDED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "go.sum", "composer.lock",
}


@dataclass
class SourceFile:
    path: Path
    relpath: str
    language: str
    lines: int
    bytes: int
    text: str


def detect_language(path: Path) -> Optional[str]:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def is_excluded(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    if any(part in EXCLUDED_DIRS for part in relative.parts):
        return True
    if path.name in EXCLUDED_NAMES:
        return True
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def looks_binary(raw: bytes) -> bool:
    """A NUL byte in the first block is the classic, reliable binary signal."""
    return b"\x00" in raw[:4096]


def walk(root: Path, max_file_bytes: int = 400_000) -> List[SourceFile]:
    """Collect readable source files from a working tree."""
    root = Path(root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"No such repository path: {root}")

    files: List[SourceFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or is_excluded(path, root):
            continue
        language = detect_language(path)
        if language is None:
            continue
        try:
            size = path.stat().st_size
            if size > max_file_bytes:
                continue
            raw = path.read_bytes()
            if looks_binary(raw):
                continue
            # utf-8-sig, not utf-8: Windows editors and PowerShell write a
            # UTF-8 BOM, and a leading U+FEFF makes Python's own parser
            # reject the file with "invalid non-printable character".
            text = raw.decode("utf-8-sig", errors="replace")
        except OSError:
            continue

        files.append(
            SourceFile(
                path=path,
                relpath=str(path.relative_to(root)),
                language=language,
                lines=text.count("\n") + 1,
                bytes=size,
                text=text,
            )
        )
    return files


def summarize(files: Iterable[SourceFile]) -> Dict[str, Dict[str, int]]:
    """Per-language file and line counts - the repository at a glance."""
    summary: Dict[str, Dict[str, int]] = {}
    for source in files:
        entry = summary.setdefault(source.language, {"files": 0, "lines": 0})
        entry["files"] += 1
        entry["lines"] += source.lines
    return dict(sorted(summary.items(), key=lambda kv: -kv[1]["lines"]))


def clone(url: str, destination: Path, depth: int = 1) -> Path:
    """Shallow-clone a repository. Depth 1 is enough to review a snapshot."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"{destination} already exists")
    result = subprocess.run(
        ["git", "clone", "--depth", str(depth), url, str(destination)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
    return destination


def is_git_repo(root: Path) -> bool:
    return (Path(root) / ".git").exists()
