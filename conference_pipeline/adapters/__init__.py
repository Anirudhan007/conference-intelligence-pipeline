"""Registry of tested conference-platform adapters.

Add a platform by subclassing :class:`Adapter`, registering it here, and adding synthetic contract tests
(see docs/adapter-guide.md). Unknown adapter names fail closed.
"""
from __future__ import annotations

from .base import Adapter
from .crossref import CrossrefSupplementAdapter
from .eventscribe import EventScribeAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "eventscribe-v1": EventScribeAdapter,
    "eventscribe-ctos-v1": EventScribeAdapter,  # original CTOS 2026 identifier, kept for compatibility
    "crossref-supplement-v1": CrossrefSupplementAdapter,
}


def get_adapter(name: str | None) -> Adapter:
    from ..config import ConfigurationError

    if name not in ADAPTERS:
        raise ConfigurationError(
            f"unknown adapter {name!r}; registered adapters: {', '.join(sorted(ADAPTERS))}. "
            "Add and test a platform-specific adapter before running another website."
        )
    return ADAPTERS[name]()


__all__ = ["ADAPTERS", "Adapter", "get_adapter"]
