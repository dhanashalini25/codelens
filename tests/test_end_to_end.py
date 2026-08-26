"""End-to-end: build a throwaway git repository, change it, review the change."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codelens.config import Settings
from codelens.history import History
from codelens.index import CodeIndex
from codelens.review import Reviewer

CLEAN_FILE = '''"""A small module."""


def add(a, b):
    """Add two numbers."""
    return a + b


def describe(value):
    """Describe a value."""
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"
'''

BAD_CHANGE = '''"""A small module."""

import subprocess


def add(a, b):
    """Add two numbers."""
    return a + b


def describe(value):
    """Describe a value."""
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"


def run(command, cache=[]):
    cache.append(command)
    try:
        return subprocess.run(command, shell=True)
    except:
        pass
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def repository(tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo-repo")
    (repo / "src").mkdir()
    (repo / "src" / "util.py").write_text(CLEAN_FILE, encoding="utf-8")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "Initial commit")

    (repo / "src" / "util.py").write_text(BAD_CHANGE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "Add run helper")
    return repo


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("codelens-data")
    settings = Settings()
    settings.data_dir = data_dir
    settings.db_path = data_dir / "codelens.db"
    settings.llm_provider = "mock"
    settings.embedding_backend = "tfidf"
    return settings


def test_index_builds(repository, config):
    summary = CodeIndex(history=History(config.db_path), config=config).build(repository)
    assert summary["files"] >= 1
    assert summary["chunks"] >= 1


def test_code_search_finds_a_symbol(repository, config):
    code_index = CodeIndex(history=History(config.db_path), config=config)
    code_index.build(repository)
    hits = code_index.search("describe a value positive negative", k=3)
    assert hits
    assert any("describe" in (hit.chunk.symbol or "") for hit in hits)


def test_review_flags_the_real_problems(repository, config):
    history = History(config.db_path)
    reviewer = Reviewer(repository, history=history, config=config)
    result = reviewer.review_ref(base="HEAD~1", head="HEAD")

    assert result.files
    titles = " | ".join(f.title for f in result.findings)
    assert "Mutable default" in titles
    assert "shell=True" in titles
    assert "Bare except" in titles


def test_review_ignores_untouched_code(repository, config):
    """`add` and `describe` are unchanged, so nothing should be reported on them."""
    reviewer = Reviewer(repository, history=History(config.db_path), config=config)
    result = reviewer.review_ref(base="HEAD~1", head="HEAD")
    assert all(f.symbol != "add" for f in result.findings)


def test_findings_are_sorted_by_severity(repository, config):
    from codelens.rules import SEVERITY_ORDER

    reviewer = Reviewer(repository, history=History(config.db_path), config=config)
    result = reviewer.review_ref(base="HEAD~1", head="HEAD")
    order = [SEVERITY_ORDER[f.severity] for f in result.findings]
    assert order == sorted(order)


def test_review_is_persisted_and_searchable(repository, config):
    history = History(config.db_path)
    reviewer = Reviewer(repository, history=history, config=config)
    result = reviewer.review_ref(base="HEAD~1", head="HEAD")

    assert result.review_id is not None
    stored = history.get_review(result.review_id)
    assert stored is not None
    assert stored["n_findings"] == len(result.findings)

    matches = history.search_findings(query="shell")
    assert matches
    assert matches[0]["severity"] == "high"


def test_stats_aggregate_across_reviews(repository, config):
    history = History(config.db_path)
    summary = history.finding_stats()
    assert summary["reviews"] >= 1
    assert summary["by_severity"]
    assert summary["hotspots"]


def test_mock_provider_adds_no_llm_findings(repository, config):
    """Offline, the review must report static findings only - never invent AI output."""
    reviewer = Reviewer(repository, history=History(config.db_path), config=config)
    result = reviewer.review_ref(base="HEAD~1", head="HEAD", save=False)
    assert all(f.source == "static" for f in result.findings)


def test_llm_error_detail_is_truncated(repository, config):
    """A model that returns prose must not dump it whole into a PR comment."""
    from codelens.llm import LLMClient, LLMError
    from codelens.review import Reviewer as R

    class ExplodingClient(LLMClient):
        @property
        def is_mock(self):
            return False

        def complete_json(self, messages, temperature=0.0):
            raise LLMError("x" * 5000)

    reviewer = R(
        repository,
        history=History(config.db_path),
        client=ExplodingClient(config),
        config=config,
    )
    result = reviewer.review_ref(base="HEAD~1", head="HEAD", save=False)
    failures = [f for f in result.findings if f.title == "AI review pass failed"]
    assert failures
    assert len(failures[0].detail) <= 220
    # The static findings must survive the AI pass failing.
    assert any(f.source == "static" for f in result.findings)


def test_bom_file_is_read_without_a_syntax_error(tmp_path):
    """repo.walk must strip a UTF-8 BOM, or every BOM file reports as broken."""
    from codelens.repo import walk

    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbfdef f():\n    return 1\n")
    files = walk(tmp_path)
    assert len(files) == 1
    assert not files[0].text.startswith("﻿")

    from codelens.parsing import parse_python

    assert [s.name for s in parse_python(files[0].text)] == ["f"]


def test_json_mode_400_falls_back_instead_of_failing():
    """Groq returned a 400 with an empty body; the fallback must not read it."""
    import httpx

    from codelens.config import Settings
    from codelens.llm import LLMClient, Message

    calls = []

    class FakeClient(LLMClient):
        def _post(self, payload):
            calls.append(payload)

    client = LLMClient(Settings())
    client.provider = "groq"
    client.config.llm_api_key = "test"

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        if "response_format" in json:
            request = httpx.Request("POST", url)
            return httpx.Response(400, text="", request=request)
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"findings": []}'}}]},
            request=request,
        )

    original = httpx.post
    httpx.post = fake_post
    try:
        result = client.complete_json([Message("user", "review this")])
    finally:
        httpx.post = original

    assert result == {"findings": []}
    assert len(calls) == 2                      # first with JSON mode, then without
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
