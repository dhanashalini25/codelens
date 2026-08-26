"""Unified diff parsing and git plumbing.

A review is only as good as its understanding of what changed. This module
turns a unified diff into structured hunks with real line numbers, so a finding
can point at `src/auth.py:142` rather than "somewhere in this patch".
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class Hunk:
    old_start: int
    new_start: int
    header: str
    lines: List[str] = field(default_factory=list)
    added_lines: List[tuple] = field(default_factory=list)   # (line_number, text)
    removed_lines: List[tuple] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class FileDiff:
    path: str
    old_path: Optional[str] = None
    status: str = "modified"  # added | modified | deleted | renamed
    hunks: List[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def added(self) -> int:
        return sum(len(hunk.added_lines) for hunk in self.hunks)

    @property
    def removed(self) -> int:
        return sum(len(hunk.removed_lines) for hunk in self.hunks)

    @property
    def changed_line_numbers(self) -> List[int]:
        """New-file line numbers that were added - where review should look."""
        return [number for hunk in self.hunks for number, _ in hunk.added_lines]

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "added": self.added,
            "removed": self.removed,
            "hunks": len(self.hunks),
            "binary": self.is_binary,
        }

    def render(self, max_chars: Optional[int] = None) -> str:
        body = "\n".join(
            f"@@ {hunk.header}".rstrip() + "\n" + hunk.text for hunk in self.hunks
        )
        text = f"--- {self.old_path or self.path}\n+++ {self.path}\n{body}"
        if max_chars and len(text) > max_chars:
            return text[:max_chars] + "\n... (diff truncated)"
        return text


def parse_unified_diff(diff_text: str) -> List[FileDiff]:
    """Parse `git diff` output into per-file hunks with accurate line numbers."""
    files: List[FileDiff] = []
    current: Optional[FileDiff] = None
    hunk: Optional[Hunk] = None
    old_line = new_line = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            path = parts[-1].strip() if len(parts) > 1 else line.split()[-1]
            current = FileDiff(path=path)
            files.append(current)
            hunk = None
            continue

        if current is None:
            continue

        if line.startswith("--- "):
            source = line[4:].strip()
            current.old_path = None if source == "/dev/null" else source[2:] if source.startswith("a/") else source
            if source == "/dev/null":
                current.status = "added"
            continue

        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current.status = "deleted"
            elif target.startswith("b/"):
                current.path = target[2:]
            continue

        if line.startswith("rename from"):
            current.status = "renamed"
            current.old_path = line[len("rename from") :].strip()
            continue

        if line.startswith("Binary files"):
            current.is_binary = True
            continue

        match = _HUNK_HEADER.match(line)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            hunk = Hunk(
                old_start=old_line,
                new_start=new_line,
                header=line[2:].strip() if line.startswith("@@") else line,
            )
            hunk.header = line
            current.hunks.append(hunk)
            continue

        if hunk is None:
            continue

        hunk.lines.append(line)
        if line.startswith("+"):
            hunk.added_lines.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-"):
            hunk.removed_lines.append((old_line, line[1:]))
            old_line += 1
        else:
            old_line += 1
            new_line += 1

    return files


def git_diff(repo: Path, base: str = "HEAD~1", head: str = "HEAD") -> str:
    """Diff between two refs. Falls back to the working tree for a fresh repo."""
    repo = Path(repo)
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-color", f"{base}...{head}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # A repository with a single commit has no HEAD~1; show that commit.
        fallback = subprocess.run(
            ["git", "-C", str(repo), "show", "--no-color", "--format=", head],
            capture_output=True,
            text=True,
        )
        if fallback.returncode != 0:
            raise RuntimeError(
                f"git diff failed: {result.stderr.strip() or fallback.stderr.strip()}"
            )
        return fallback.stdout
    return result.stdout


def git_working_diff(repo: Path, staged: bool = False) -> str:
    """Uncommitted changes - what you review before you commit."""
    command = ["git", "-C", str(repo), "diff", "--no-color"]
    if staged:
        command.append("--cached")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def git_commit_info(repo: Path, ref: str = "HEAD") -> dict:
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%H%n%an%n%ad%n%s", ref],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    parts = result.stdout.strip().split("\n")
    keys = ["sha", "author", "date", "subject"]
    return dict(zip(keys, parts))
