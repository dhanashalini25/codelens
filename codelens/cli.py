"""Command line interface. Run with: python -m codelens.cli --help"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .config import settings
from .explain import Explainer
from .history import History
from .index import CodeIndex
from .review import Reviewer
from .rules import SEVERITY_ORDER

app = typer.Typer(add_completion=False, help="CodeLens - repository intelligence and AI code review.")

SEVERITY_COLOUR = {
    "critical": typer.colors.BRIGHT_RED,
    "high": typer.colors.RED,
    "medium": typer.colors.YELLOW,
    "low": typer.colors.CYAN,
    "info": typer.colors.BRIGHT_BLACK,
}


def _history() -> History:
    return History(settings.db_path)


@app.command()
def index(
    path: Path = typer.Argument(Path("."), help="Repository root"),
) -> None:
    """Index a repository for code search and review context."""
    summary = CodeIndex(history=_history(), config=settings).build(path)
    typer.secho(
        f"Indexed {summary['files']} files, {summary['chunks']} chunks, "
        f"{summary['lines']} lines.",
        fg=typer.colors.GREEN,
    )
    for language, counts in summary["languages"].items():
        typer.echo(f"    {language:<12} {counts['files']:>4} files  {counts['lines']:>7} lines")
    typer.echo(f"  embedding backend: {summary['embedding_backend']}")


@app.command()
def review(
    path: Path = typer.Argument(Path("."), help="Repository root"),
    base: str = typer.Option("HEAD~1", "--base", "-b"),
    head: str = typer.Option("HEAD", "--head", "-h"),
    working: bool = typer.Option(False, "--working", help="Review uncommitted changes"),
    staged: bool = typer.Option(False, "--staged", help="Review staged changes"),
    as_json: bool = typer.Option(False, "--json"),
    fail_on: Optional[str] = typer.Option(
        None, "--fail-on", help="Exit non-zero if a finding at or above this severity exists"
    ),
) -> None:
    """Review a change: static rules plus an AI pass."""
    reviewer = Reviewer(path, history=_history(), config=settings)

    if working or staged:
        result = reviewer.review_working_tree(staged=staged)
    else:
        result = reviewer.review_ref(base=base, head=head)

    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        _print_review(result)

    if fail_on:
        threshold = SEVERITY_ORDER.get(fail_on.lower(), 99)
        blocking = [f for f in result.findings if SEVERITY_ORDER.get(f.severity, 9) <= threshold]
        if blocking:
            typer.secho(
                f"\n{len(blocking)} finding(s) at or above '{fail_on}'.", fg=typer.colors.RED
            )
            raise typer.Exit(code=1)


def _print_review(result) -> None:
    typer.echo()
    if result.commit.get("subject"):
        typer.secho(f"  {result.commit['subject']}", bold=True)
        typer.echo(f"  {result.commit.get('sha', '')[:10]}  {result.commit.get('author', '')}")
        typer.echo()

    typer.echo(f"  {result.summary}")
    typer.echo(f"  provider: {result.provider}")
    if result.provider == "mock":
        typer.secho(
            "  (mock provider: static analysis only - set CODELENS_LLM_PROVIDER for the AI pass)",
            fg=typer.colors.BRIGHT_BLACK,
        )
    typer.echo()

    if not result.findings:
        typer.secho("  No findings.", fg=typer.colors.GREEN)
        return

    for finding in result.findings:
        colour = SEVERITY_COLOUR.get(finding.severity, typer.colors.WHITE)
        tag = f"[{finding.source}]" if finding.source == "llm" else ""
        typer.secho(
            f"  {finding.severity.upper():<9} {finding.file}:{finding.line}  "
            f"{finding.title} {tag}",
            fg=colour,
        )
        if finding.detail:
            typer.echo(f"            {finding.detail}")
        if finding.suggestion:
            typer.secho(f"            fix: {finding.suggestion}", fg=typer.colors.BRIGHT_BLACK)
        typer.echo()

    if result.review_id:
        typer.secho(
            f"  saved as review #{result.review_id} - "
            f"see `python -m codelens.cli show {result.review_id}`",
            fg=typer.colors.BRIGHT_BLACK,
        )


@app.command()
def search(
    query: str = typer.Argument(..., help="What to look for"),
    k: int = typer.Option(5, "--top-k", "-k"),
) -> None:
    """Semantic search across the indexed repository."""
    code_index = CodeIndex(history=_history(), config=settings)
    hits = code_index.search(query, k=k)
    if not hits:
        typer.secho("No results. Have you run `index`?", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    for hit in hits:
        typer.secho(f"\n  {hit.chunk.location}  score={hit.score:.3f}", fg=typer.colors.CYAN)
        if hit.chunk.symbol:
            typer.echo(f"    {hit.chunk.symbol}")
        preview = "\n".join(hit.chunk.text.splitlines()[:4])
        typer.echo("    " + preview.replace("\n", "\n    "))


@app.command()
def structure(
    file: str = typer.Argument(..., help="File path relative to the repository root"),
    path: Path = typer.Option(Path("."), "--repo", help="Repository root"),
) -> None:
    """Show a file's symbols, sizes and complexity - no model needed."""
    typer.echo(Explainer(path, config=settings).structure(file).render())


@app.command()
def explain(
    file: str = typer.Argument(...),
    symbol: Optional[str] = typer.Option(None, "--symbol", "-s"),
    path: Path = typer.Option(Path("."), "--repo"),
) -> None:
    """Explain a file or a single symbol."""
    explainer = Explainer(path, config=settings)
    result = explainer.explain(file, symbol_name=symbol)
    typer.echo()
    typer.echo(explainer.structure(file).render())
    typer.secho(f"\n{result['explanation']}\n", fg=typer.colors.GREEN)


@app.command()
def document(
    file: str = typer.Argument(...),
    path: Path = typer.Option(Path("."), "--repo"),
) -> None:
    """Propose docstrings for undocumented public symbols."""
    result = Explainer(path, config=settings).document(file)
    if not result["docstrings"]:
        typer.echo(f"  {result.get('note') or 'Nothing to document.'}")
        if result.get("targets"):
            typer.echo(f"  undocumented: {', '.join(result['targets'])}")
        return
    for entry in result["docstrings"]:
        typer.secho(f"\n  {entry.get('symbol')} (line {entry.get('line')})", fg=typer.colors.CYAN)
        typer.echo(f"    {entry.get('docstring', '')}")


@app.command("tests")
def recommend_tests(
    file: str = typer.Argument(...),
    path: Path = typer.Option(Path("."), "--repo"),
) -> None:
    """Recommend tests, prioritised by cyclomatic complexity."""
    result = Explainer(path, config=settings).recommend_tests(file)
    typer.echo("\n  priority by complexity:")
    for entry in result["priority_symbols"]:
        typer.echo(f"    {entry['name']:<40} complexity {entry['complexity']}")
    if not result["tests"]:
        typer.echo(f"\n  {result.get('note') or 'No recommendations returned.'}")
        return
    for test in result["tests"]:
        typer.secho(f"\n  {test.get('name')}  [{test.get('priority', '?')}]", fg=typer.colors.CYAN)
        typer.echo(f"    target:   {test.get('target')}")
        typer.echo(f"    scenario: {test.get('scenario')}")
        typer.echo(f"    catches:  {test.get('why')}")


@app.command("reviews")
def list_reviews(limit: int = typer.Option(10, "--limit", "-n")) -> None:
    """List past reviews."""
    rows = _history().list_reviews(limit=limit)
    if not rows:
        typer.echo("No reviews yet.")
        return
    for row in rows:
        typer.echo(
            f"  #{row['id']:<4} {row['created_at']}  {row['ref']:<20} "
            f"{row['n_findings']} findings  {row['summary']}"
        )


@app.command()
def show(review_id: int = typer.Argument(...)) -> None:
    """Show a stored review in full."""
    record = _history().get_review(review_id)
    if not record:
        typer.secho(f"No review #{review_id}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"\n  review #{record['id']}  {record['ref']}  {record['created_at']}")
    typer.echo(f"  {record['summary']}\n")
    for finding in record["findings"]:
        colour = SEVERITY_COLOUR.get(finding["severity"], typer.colors.WHITE)
        typer.secho(
            f"  {finding['severity'].upper():<9} {finding['file']}:{finding['line']}  "
            f"{finding['title']}",
            fg=colour,
        )


@app.command("find")
def find_findings(
    query: Optional[str] = typer.Argument(None, help="Text to match in title or detail"),
    severity: Optional[str] = typer.Option(None, "--severity"),
    category: Optional[str] = typer.Option(None, "--category"),
    file: Optional[str] = typer.Option(None, "--file"),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Search findings across every review ever run."""
    rows = _history().search_findings(
        query=query, severity=severity, category=category, file_like=file, limit=limit
    )
    if not rows:
        typer.echo("No matching findings.")
        return
    for row in rows:
        colour = SEVERITY_COLOUR.get(row["severity"], typer.colors.WHITE)
        typer.secho(
            f"  {row['severity'].upper():<9} {row['file']}:{row['line']}  {row['title']}  "
            f"(review {row['review_id']}, {row['created_at']})",
            fg=colour,
        )


@app.command()
def stats() -> None:
    """Aggregate findings across all reviews - hotspots and category counts."""
    summary = _history().finding_stats()
    typer.echo(f"\n  reviews stored: {summary['reviews']}")
    if summary["by_severity"]:
        typer.echo("\n  by severity:")
        for severity, count in sorted(
            summary["by_severity"].items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9)
        ):
            typer.echo(f"    {severity:<10} {count}")
    if summary["by_category"]:
        typer.echo("\n  by category:")
        for category, count in summary["by_category"].items():
            typer.echo(f"    {category:<16} {count}")
    if summary["hotspots"]:
        typer.echo("\n  hotspots:")
        for entry in summary["hotspots"]:
            typer.echo(f"    {entry['findings']:>3}  {entry['file']}")
    typer.echo()


if __name__ == "__main__":
    app()
