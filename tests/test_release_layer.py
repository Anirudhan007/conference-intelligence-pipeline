from __future__ import annotations

import json
from pathlib import Path

import pytest

from capture import capture
from capture.capture import build_user_agent
from conference_pipeline import cli
from conference_pipeline.config import ConferenceConfig, ConfigurationError
from conference_pipeline.runner import QualityGateError, RunResult, run_release, validate_bundle

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "ctos-2026.json"


def minimal_bundle() -> dict:
    return {
        "source": {"platform": "eventscribe", "started": "2026-11-11T00:00:00Z"},
        "listings": {
            "posters": {
                "url": "https://ctos2026.eventscribe.net/PosterTitles.asp",
                "retrievedAt": "2026-11-11T00:00:00Z",
                "bytes": 0,
                "html": "",
            }
        },
        "details": {},
        "errors": [],
    }


def test_validated_ctos_config_and_cli(capsys):
    config = ConferenceConfig.load(CONFIG)
    config.assert_supported()
    assert cli.main(["validate", "--config", str(CONFIG)]) == 0
    assert '"conference": "ctos-2026"' in capsys.readouterr().out


def test_unsupported_config_fails_closed(tmp_path):
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["conference"]["id"] = "other-2026"
    path = tmp_path / "other.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="ships the EventScribe CTOS adapter only"):
        ConferenceConfig.load(path).assert_supported()


def test_bundle_validation_and_release_manifest(tmp_path):
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(minimal_bundle()), encoding="utf-8")
    assert validate_bundle(bundle)["listings"]
    result = run_release(config=ConferenceConfig.load(CONFIG), bundle_path=bundle, run_dir=tmp_path / "run")
    assert result.is_releasable
    assert result.document_path.exists() and result.manifest_path.exists()
    document = json.loads(result.document_path.read_text(encoding="utf-8"))
    assert document["conference"]["name"] == "CTOS 2026 Annual Meeting"
    assert document["embargo"]["rules"][1]["class"] == "public"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["quality"] == {"schemaValid": True, "failingChecks": {}, "pendingReview": []}


def test_invalid_capture_contact_and_quality_state():
    with pytest.raises(ValueError, match="contact-email"):
        build_user_agent("not-an-email")
    assert "contact: research@example.org" in build_user_agent("research@example.org")
    assert not RunResult(Path("data.json"), Path("manifest.json"), {"schema": 1}, ()).is_releasable


def test_capture_fails_closed_when_robots_policy_is_unknown(monkeypatch):
    class UnknownRobotsFetcher:
        robots_known = False
        errors = [["https://example.org/robots.txt", "robots fetch failed"]]

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(capture, "Fetcher", UnknownRobotsFetcher)
    with pytest.raises(RuntimeError, match="cannot establish robots.txt policy"):
        capture.capture_eventscribe("https://example.org", "research@example.org")


def test_release_runner_on_synthetic_eventscribe_bundle_and_quality_gate(tmp_path):
    import re

    fx = json.loads((ROOT / "tests" / "fixtures_synthetic" / "eventscribe_synthetic.json").read_text(encoding="utf-8"))
    draft = ROOT / "tests" / "fixtures_synthetic" / "readouts_draft_synthetic.json"
    details = {}
    for card in fx["poster_cards"]:
        details["PosterInfo:" + re.search(r"PosterID=(\d+)", card["url"]).group(1)] = card
    for card in fx["pres_cards"]:
        details["PresentationInfo:" + re.search(r"PresentationID=(\d+)", card["url"]).group(1)] = card
    details["SessionInfo:920007"] = fx["session_card"]
    bundle_payload = {
        "source": {"platform": "eventscribe", "url": "https://ctos2026.eventscribe.net/index.asp?launcher=1"},
        "listings": {
            "posters": {"url": "https://ctos2026.eventscribe.net/PosterTitles.asp", "retrievedAt": "2026-11-11T00:00:00Z", "bytes": len(fx["poster_listing"]), "html": fx["poster_listing"]},
            "presentations": {"url": "https://ctos2026.eventscribe.net/SearchByPresentation.asp", "retrievedAt": "2026-11-11T00:00:00Z", "bytes": len(fx["title_listing"]), "html": fx["title_listing"]},
            "day_2026_11_20": {"url": "https://ctos2026.eventscribe.net/agenda.asp", "retrievedAt": "2026-11-11T00:00:00Z", "bytes": len(fx["agenda_day"]), "html": fx["agenda_day"]},
            "sponsors": {"url": "https://ctos2026.eventscribe.net/sponsors.asp", "retrievedAt": "2026-11-11T00:00:00Z", "bytes": len(fx["sponsors"]), "html": fx["sponsors"]},
        },
        "details": details,
        "errors": [],
    }
    bundle_path = tmp_path / "synthetic_bundle.json"
    bundle_path.write_text(json.dumps(bundle_payload), encoding="utf-8")

    # 1. Valid synthetic readout passes QA and review gates
    clean_run = run_release(
        config=ConferenceConfig.load(CONFIG),
        bundle_path=bundle_path,
        run_dir=tmp_path / "clean_run",
        readouts_draft=draft,
    )
    assert clean_run.is_releasable
    doc = json.loads(clean_run.document_path.read_text(encoding="utf-8"))
    assert len(doc["readouts"]) == 1 and doc["qa"]["schemaValid"] is True

    # 2. Corrupted CI in readout draft blocks release unless explicitly allowed for triage
    bad_draft_data = json.loads(draft.read_text(encoding="utf-8"))
    bad_draft_data[0]["metrics"][0]["arms"][0]["ci"] = [40.0, 51.7]
    bad_draft = tmp_path / "bad_draft.json"
    bad_draft.write_text(json.dumps(bad_draft_data), encoding="utf-8")
    with pytest.raises(QualityGateError, match="release blocked by quality gate"):
        run_release(
            config=ConferenceConfig.load(CONFIG),
            bundle_path=bundle_path,
            run_dir=tmp_path / "blocked_run",
            readouts_draft=bad_draft,
        )
    triage_run = run_release(
        config=ConferenceConfig.load(CONFIG),
        bundle_path=bundle_path,
        run_dir=tmp_path / "triage_run",
        readouts_draft=bad_draft,
        allow_qa_failures=True,
    )
    assert not triage_run.is_releasable
    assert "readout_ci_contains_estimate" in triage_run.qa_failures

