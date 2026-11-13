"""Adapter protocol: platform-specific capture + parse, emitting the canonical parsed.json contract."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover
    from ..config import ConferenceConfig


class Adapter:
    """Every adapter owns capture (raw bundle) and parse (canonical ParsedConference).

    Downstream stages (tag, readouts, aggregate, qa, export) are platform-agnostic and never change per site.
    """

    name: ClassVar[str] = ""
    platform: ClassVar[str] = ""
    #: one-line statement of the access basis checked before this adapter was written
    access_basis: ClassVar[str] = ""

    def validate_config(self, config: ConferenceConfig) -> None:
        """Raise ConfigurationError if the config lacks fields this adapter needs."""

    def capture(self, config: ConferenceConfig, *, contact: str | None = None, delay: float = 1.5,
                limit: int | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def parse(self, bundle: dict[str, Any], config: ConferenceConfig) -> dict[str, Any]:
        raise NotImplementedError

    def inventory(self, bundle: dict[str, Any]) -> dict[str, list[str]]:
        """Source ids the capture says exist, for the QA coverage gate (kind -> sorted ids)."""
        return {k: sorted(v) for k, v in (bundle.get("inventory") or {}).items()}
