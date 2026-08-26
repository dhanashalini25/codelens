"""CodeLens - repository intelligence and AI-assisted code review.

Static analysis and an AI review pass over the same change, merged into one
prioritised list of findings, stored so the history is searchable.
"""

__version__ = "0.1.0"

from .config import Settings, settings  # noqa: F401
from .history import History  # noqa: F401
from .index import CodeIndex  # noqa: F401
from .review import ReviewResult, Reviewer  # noqa: F401
from .rules import Finding  # noqa: F401

__all__ = [
    "CodeIndex",
    "Finding",
    "History",
    "ReviewResult",
    "Reviewer",
    "Settings",
    "settings",
    "__version__",
]
