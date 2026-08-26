"""Configuration for CodeLens, loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    """Everything has a working default, so a fresh clone runs with no setup."""

    data_dir: Path = field(default_factory=lambda: Path(_env("CODELENS_DATA_DIR", ".codelens")))
    db_path: Path = field(init=False)

    # --- indexing ------------------------------------------------------
    max_file_bytes: int = field(default_factory=lambda: _env_int("CODELENS_MAX_FILE_BYTES", 400_000))
    chunk_lines: int = field(default_factory=lambda: _env_int("CODELENS_CHUNK_LINES", 60))
    chunk_overlap_lines: int = field(
        default_factory=lambda: _env_int("CODELENS_CHUNK_OVERLAP_LINES", 10)
    )
    embedding_backend: str = field(
        default_factory=lambda: _env("CODELENS_EMBEDDING_BACKEND", "auto")
    )
    embedding_model: str = field(
        default_factory=lambda: _env("CODELENS_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    )
    embedding_dim: int = field(default_factory=lambda: _env_int("CODELENS_EMBEDDING_DIM", 2048))
    context_k: int = field(default_factory=lambda: _env_int("CODELENS_CONTEXT_K", 4))

    # --- review --------------------------------------------------------
    max_diff_chars: int = field(default_factory=lambda: _env_int("CODELENS_MAX_DIFF_CHARS", 14000))
    max_context_chars: int = field(
        default_factory=lambda: _env_int("CODELENS_MAX_CONTEXT_CHARS", 6000)
    )
    min_confidence: float = field(
        default_factory=lambda: _env_float("CODELENS_MIN_CONFIDENCE", 0.5)
    )

    # --- llm -----------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: _env("CODELENS_LLM_PROVIDER", "mock"))
    llm_model: str = field(
        default_factory=lambda: _env("CODELENS_LLM_MODEL", "openai/gpt-oss-120b")
    )
    llm_api_key: str = field(default_factory=lambda: _env("CODELENS_LLM_API_KEY", ""))
    llm_base_url: str = field(
        default_factory=lambda: _env("CODELENS_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    )
    llm_timeout: float = field(default_factory=lambda: _env_float("CODELENS_LLM_TIMEOUT", 90.0))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "codelens.db"


settings = Settings()
