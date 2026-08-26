"""FastAPI surface. Run with: uvicorn codelens.api:app --reload"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import settings
from .explain import Explainer
from .history import History
from .index import CodeIndex
from .review import Reviewer

app = FastAPI(
    title="CodeLens",
    description="Repository intelligence and AI-assisted code review.",
    version="0.1.0",
)

_history = History(settings.db_path)


class IndexRequest(BaseModel):
    path: str = Field(..., examples=["."])


class ReviewRequest(BaseModel):
    path: str = Field(..., examples=["."])
    base: str = "HEAD~1"
    head: str = "HEAD"
    working: bool = False
    staged: bool = False


class PatchRequest(BaseModel):
    path: str = Field(..., examples=["."])
    diff: str = Field(..., min_length=1)
    ref: str = "patch"


class FileRequest(BaseModel):
    path: str = Field(..., examples=["."])
    file: str
    symbol: Optional[str] = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "repositories": len(_history.list_repositories()),
        "llm_provider": settings.llm_provider,
        "embedding_backend": _history.get_meta("embedding_backend"),
    }


@app.get("/repositories")
def repositories() -> dict:
    return {"repositories": _history.list_repositories()}


@app.post("/index")
def build_index(request: IndexRequest) -> dict:
    try:
        return CodeIndex(history=_history, config=settings).build(Path(request.path))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/search")
def search(q: str, k: int = 5) -> dict:
    hits = CodeIndex(history=_history, config=settings).search(q, k=k)
    if not hits:
        raise HTTPException(status_code=409, detail="Nothing indexed. POST /index first.")
    return {"query": q, "hits": [hit.to_dict() for hit in hits]}


@app.post("/review")
def review(request: ReviewRequest) -> dict:
    reviewer = Reviewer(Path(request.path), history=_history, config=settings)
    try:
        if request.working or request.staged:
            result = reviewer.review_working_tree(staged=request.staged)
        else:
            result = reviewer.review_ref(base=request.base, head=request.head)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.to_dict()


@app.post("/review/patch")
def review_patch(request: PatchRequest) -> dict:
    """Review a diff supplied directly - how a CI job or webhook would call this."""
    reviewer = Reviewer(Path(request.path), history=_history, config=settings)
    return reviewer.review_diff_text(request.diff, ref=request.ref).to_dict()


@app.get("/reviews")
def list_reviews(limit: int = 20) -> dict:
    return {"reviews": _history.list_reviews(limit=limit)}


@app.get("/reviews/{review_id}")
def get_review(review_id: int) -> dict:
    record = _history.get_review(review_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"No review #{review_id}")
    return record


@app.get("/findings")
def find_findings(
    q: Optional[str] = None,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    file: Optional[str] = None,
    limit: int = 50,
) -> dict:
    return {
        "findings": _history.search_findings(
            query=q, severity=severity, category=category, file_like=file, limit=limit
        )
    }


@app.get("/stats")
def stats(repo_id: Optional[str] = None) -> dict:
    return _history.finding_stats(repo_id=repo_id)


@app.post("/structure")
def structure(request: FileRequest) -> dict:
    try:
        return Explainer(Path(request.path), config=settings).structure(request.file).to_dict()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/explain")
def explain(request: FileRequest) -> dict:
    try:
        return Explainer(Path(request.path), config=settings).explain(
            request.file, symbol_name=request.symbol
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/document")
def document(request: FileRequest) -> dict:
    try:
        return Explainer(Path(request.path), config=settings).document(request.file)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/tests")
def recommend_tests(request: FileRequest) -> dict:
    try:
        return Explainer(Path(request.path), config=settings).recommend_tests(request.file)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
