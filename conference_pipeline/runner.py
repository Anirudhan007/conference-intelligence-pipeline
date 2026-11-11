"""A release-oriented orchestration layer around the evidence extraction stages."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import aggregate, export, parse, readouts, tag
from pipeline.assemble_common import dump_json

from ._version import __version__
from .config import ConferenceConfig, ConfigurationError


class QualityGateError(RuntimeError):
    """Raised when a document is built but cannot be released."""


@dataclass(frozen=True)
class RunResult:
    document_path: Path
    manifest_path: Path
    qa_failures: dict[str, int]
    pending_review: tuple[str, ...]

    @property
    def is_releasable(self) -> bool:
        return not self.qa_failures and not self.pending_review


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_bundle(path: str | Path) -> dict[str, Any]:
    bundle_path = Path(path)
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"raw bundle not found: {bundle_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"raw bundle is not valid JSON: {bundle_path}: {exc}") from exc
    if not isinstance(bundle, dict) or not isinstance(bundle.get("listings"), dict) or not isinstance(bundle.get("details"), dict):
        raise ConfigurationError("raw bundle must contain object-valued listings and details")
    if not bundle["listings"]:
        raise ConfigurationError("raw bundle contains no listing pages")
    return bundle


def _paths(run_dir: Path, bundle: Path) -> dict[str, Path]:
    stage = run_dir / "stage"
    out = run_dir / "out"
    return {
        "bundle": bundle,
        "parsed": stage / "parsed.json",
        "tags": stage / "tags.json",
        "readouts": stage / "readouts.json",
        "aggregates": stage / "aggregates.json",
        "qa": stage / "qa.json",
        "activities": stage / "activities.json",
        "out_json": out / "ctos-2026.json",
        "out_gz": out / "ctos-2026.json.gz",
        "out_csv": out / "ctos-2026_presentations.csv",
        "out_prev": out / "ctos-2026.previous.json",
        "report": run_dir / "reports" / "run_report.md",
    }


def _apply_conference_config(parsed: dict[str, Any], config: ConferenceConfig) -> None:
    """Make the reviewed run configuration the export source of truth for CTOS-level facts."""
    conference = parsed.setdefault("conference", {})
    source = config.raw["conference"]
    conference.update({
        "id": source["id"],
        "name": source["name"],
        "organization": source.get("organization"),
        "city": source.get("city"),
        "country": source.get("country"),
        "startDate": source["startDate"],
        "endDate": source["endDate"],
        "timezone": source["timezone"],
        "sourceUrl": source["sourceUrl"],
    })
    public_release = next((rule["releaseUtc"] for rule in config.embargo_rules if rule.get("class") == "public"), None)
    if not public_release:
        return
    conference["embargoLiftsAtUtc"] = public_release
    for presentation in parsed.get("presentations", []):
        if presentation.get("abstract"):
            presentation["embargo"] = {"embargoed": True, "liftsAtUtc": public_release}


def _write_manifest(run_dir: Path, config: ConferenceConfig, bundle_path: Path, paths: dict[str, Path], doc: dict) -> Path:
    output_paths = {key: path for key, path in paths.items() if key.startswith("out_") or key == "report"}
    manifest = {
        "schemaVersion": "conference-pipeline/run-manifest/v1",
        "createdAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "packageVersion": __version__,
        "conferenceConfig": {"path": str(config.path), "sha256": _sha256(config.path)},
        "rawBundle": {"path": str(bundle_path.resolve()), "sha256": _sha256(bundle_path)},
        "outputs": {
            key: {"path": str(path.resolve()), "sha256": _sha256(path)}
            for key, path in output_paths.items()
            if path.exists()
        },
        "quality": {
            "schemaValid": doc["qa"]["schemaValid"],
            "failingChecks": {c["id"]: c["failures"] for c in doc["qa"]["checks"] if c["failures"]},
            "pendingReview": doc["qa"]["pendingReview"],
        },
    }
    target = run_dir / "run-manifest.json"
    dump_json(manifest, target, indent=2)
    return target


def run_release(
    *,
    config: ConferenceConfig,
    bundle_path: str | Path,
    run_dir: str | Path,
    readouts_draft: str | Path | None = None,
    allow_qa_failures: bool = False,
) -> RunResult:
    """Execute deterministic local stages and refuse release on failed QA or unreviewed readouts."""
    config.assert_supported()
    bundle = validate_bundle(bundle_path)
    source = Path(bundle_path).resolve()
    destination = Path(run_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    paths = _paths(destination, source)

    # The legacy CTOS parser is intentionally kept behind an explicit adapter check above.
    parsed = parse.build(bundle, "D")
    _apply_conference_config(parsed, config)
    dump_json(parsed, paths["parsed"])
    tags, unknown = tag.run(parsed, "E")
    dump_json(tags, paths["tags"])
    dump_json(unknown, destination / "stage" / "unknown_drug_codes.json")

    draft_path = Path(readouts_draft).resolve() if readouts_draft else None
    if draft_path is not None and not draft_path.exists():
        raise ConfigurationError(f"readouts draft not found: {draft_path}")
    if draft_path is not None:
        readouts.run(
            str(paths["parsed"]), str(paths["tags"]), str(paths["readouts"]), draft_path=str(draft_path)
        )
    else:
        # A missing draft is an intentional empty list, never an implicit production readout extraction.
        dump_json([], paths["readouts"])
    aggregate.run(paths["parsed"], paths["tags"], paths["readouts"], paths["aggregates"])
    paths["conference_config"] = config.path
    doc = export.run(paths)
    dump_json(doc["qa"], paths["qa"])
    manifest = _write_manifest(destination, config, source, paths, doc)

    failures = {check["id"]: check["failures"] for check in doc["qa"]["checks"] if check["failures"]}
    pending = tuple(doc["qa"]["pendingReview"])
    result = RunResult(paths["out_json"], manifest, failures, pending)
    if not allow_qa_failures and not result.is_releasable:
        details = ", ".join([f"{key}={value}" for key, value in failures.items()] + ([f"pending_review={len(pending)}"] if pending else []))
        raise QualityGateError(f"release blocked by quality gate: {details}. Inspect {paths['qa']} and {paths['report']}.")
    return result
