"""Validated, reviewable configuration for one conference extraction run."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised before a run when a conference configuration is incomplete or unsupported."""


@dataclass(frozen=True)
class ConferenceConfig:
    """The minimum immutable facts that make a run reproducible and auditable."""

    path: Path
    conference_id: str
    name: str
    source_url: str
    platform: str
    timezone: str
    start_date: str
    end_date: str
    embargo_rules: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> ConferenceConfig:
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"configuration not found: {source}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"configuration is not valid JSON: {source}: {exc}") from exc

        if raw.get("schemaVersion") != "conference-pipeline/config/v1":
            raise ConfigurationError("schemaVersion must be 'conference-pipeline/config/v1'")
        conference = raw.get("conference")
        if not isinstance(conference, dict):
            raise ConfigurationError("conference must be an object")

        required = ("id", "name", "sourceUrl", "platform", "timezone", "startDate", "endDate")
        missing = [key for key in required if not isinstance(conference.get(key), str) or not conference[key].strip()]
        if missing:
            raise ConfigurationError("conference is missing non-empty fields: " + ", ".join(missing))
        if not conference["sourceUrl"].startswith(("https://", "http://")):
            raise ConfigurationError("conference.sourceUrl must be an absolute HTTP(S) URL")
        embargo = raw.get("embargo", {})
        rules = embargo.get("rules", []) if isinstance(embargo, dict) else []
        if not isinstance(rules, list):
            raise ConfigurationError("embargo.rules must be a list")
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict) or not all(rule.get(k) for k in ("class", "releaseUtc", "sourceQuote", "sourceUrl")):
                raise ConfigurationError(
                    f"embargo.rules[{index}] must include class, releaseUtc, sourceQuote and sourceUrl"
                )
        return cls(
            path=source.resolve(),
            conference_id=conference["id"],
            name=conference["name"],
            source_url=conference["sourceUrl"],
            platform=conference["platform"],
            timezone=conference["timezone"],
            start_date=conference["startDate"],
            end_date=conference["endDate"],
            embargo_rules=tuple(rules),
            raw=raw,
        )

    def assert_supported(self) -> None:
        """Fail closed instead of silently applying CTOS parser assumptions to another website."""
        runner = self.raw.get("runner", {})
        adapter = runner.get("adapter") if isinstance(runner, dict) else None
        if adapter != "eventscribe-ctos-v1" or self.conference_id != "ctos-2026":
            raise ConfigurationError(
                "This release ships the EventScribe CTOS adapter only. Add and test a platform-specific adapter "
                "before running another conference."
            )
