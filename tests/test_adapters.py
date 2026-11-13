"""Contract tests for the adapter registry, the Crossref supplement adapter and the generic EventScribe adapter.

All inputs are synthetic (tests/fixtures_synthetic/crossref_synthetic.json); no network access.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conference_pipeline import cli
from conference_pipeline.adapters import ADAPTERS, get_adapter
from conference_pipeline.adapters import crossref as cr
from conference_pipeline.adapters.http import CaptureBlocked, build_user_agent
from conference_pipeline.config import ConferenceConfig, ConfigurationError
from conference_pipeline.runner import run_release
from pipeline import parse

ROOT = Path(__file__).resolve().parents[1]
FIX = json.loads((ROOT / "tests" / "fixtures_synthetic" / "crossref_synthetic.json").read_text(encoding="utf-8"))


def crossref_config(tmp_path: Path, **source_overrides) -> Path:
    source = {
        "filter": "issn:9999-9999,from-pub-date:2030-05-01,until-pub-date:2030-06-30",
        "match": {"doi": r"^10\.9999/synth\.2030\.1_suppl\."},
        "number": {"from": "doi", "regex": r"_suppl\.([a-z]*\d+)$", "upper": True},
        "formatRules": [["^LBA", "oral"], ["^TPS", "poster", True], ["^E\\d", "other"]],
    }
    source.update(source_overrides)
    cfg = {
        "schemaVersion": "conference-pipeline/config/v1",
        "conference": {"id": "synth-2030", "name": "Synthetic Oncology Meeting 2030", "organization": "Synthetic Society",
                       "sourceUrl": "https://api.crossref.org/works?filter=issn:9999-9999", "platform": "crossref",
                       "timezone": "America/Chicago", "startDate": "2030-05-30", "endDate": "2030-06-03",
                       "city": "Springfield", "country": "United States"},
        "runner": {"adapter": "crossref-supplement-v1", "requiresHumanReadoutReview": True},
        "source": source,
        "embargo": {"rules": [{"class": "public", "releaseUtc": "2030-05-28T00:00:00Z",
                               "sourceQuote": '"published-online": {"date-parts": [[2030, 5, 28]]}',
                               "sourceUrl": "https://api.crossref.org/works/10.9999/synth.2030.1_suppl.tps101",
                               "quoteType": "verbatim"}]},
    }
    path = tmp_path / "synth-2030.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


class FakeHttp:
    """Stands in for PoliteHttp: serves the synthetic cursor pages in order."""

    def __init__(self, *args, **kwargs):
        self.user_agent, self.delay, self.n429, self.requests, self.errors = "test-agent", 1.0, 0, 0, []
        self.pages = list(FIX["pages"])

    def get(self, url, params=None):
        self.requests += 1
        body = self.pages.pop(0)
        return SimpleNamespace(json=lambda: body, url=f"{url}?cursor={params['cursor']}", content=b"{}")


@pytest.fixture()
def synthetic_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "PoliteHttp", FakeHttp)
    config = ConferenceConfig.load(crossref_config(tmp_path))
    bundle = config.adapter().capture(config)
    return config, bundle


# ------------------------------------------------------------------------------------- registry
def test_registry_lists_all_adapters_and_fails_closed():
    assert {"eventscribe-v1", "eventscribe-ctos-v1", "crossref-supplement-v1"} <= set(ADAPTERS)
    with pytest.raises(ConfigurationError, match="unknown adapter"):
        get_adapter("guess-the-site-v0")


def test_ctos_legacy_adapter_bound_to_ctos_and_eventscribe_requires_eventscribe_host(tmp_path):
    cfg = json.loads((ROOT / "configs" / "ctos-2026.json").read_text(encoding="utf-8"))
    cfg["runner"]["adapter"] = "eventscribe-v1"
    cfg["conference"]["sourceUrl"] = "https://example.org/programme"
    path = tmp_path / "x.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="eventscribe.net"):
        ConferenceConfig.load(path).assert_supported()


def test_every_shipped_config_validates_against_its_adapter():
    configs = sorted((ROOT / "configs").glob("*.json"))
    assert len(configs) >= 12
    for path in configs:
        ConferenceConfig.load(path).assert_supported()


def test_cli_lists_adapters(capsys):
    assert cli.main(["adapters"]) == 0
    assert "crossref-supplement-v1" in capsys.readouterr().out


def test_user_agent_contact_is_optional_and_never_invented():
    assert "contact" not in build_user_agent(None)
    assert build_user_agent("team@example.org").endswith("contact: team@example.org)")
    with pytest.raises(ValueError):
        build_user_agent("bad contact")


# ------------------------------------------------------------------------------------- text helpers
def test_jats_abstract_sections_and_double_escaped_entities():
    text, sections = cr.jats_abstract(FIX["pages"][0]["message"]["items"][0]["abstract"])
    assert text.splitlines()[0].startswith("Background: Zentuximab is a synthetic antibody.")
    assert "HER2-negative disease & KRAS G12C" in text
    assert list(sections) == ["background", "methods", "results", "conclusion"]
    inline, inline_sections = cr.jats_abstract(FIX["pages"][0]["message"]["items"][1]["abstract"])
    assert "FLT3 > 1 isoform." in inline_sections["background"]
    assert inline_sections["clinical_trial_information"] == "NCT99999999"
    assert cr.jats_abstract(None) == (None, None)


@pytest.mark.parametrize("raw,expected", [
    ("1Fictional Cancer Institute, Springfield, IL", ("Fictional Cancer Institute, Springfield, IL", "Springfield",
                                                      "United States")),
    ("2Imaginary University Hospital, Lyon, FRANCE,", ("Imaginary University Hospital, Lyon, FRANCE", "Lyon", "France")),
    ("Somewhere Lab", ("Somewhere Lab", None, None)),
    (None, (None, None, None)),
])
def test_affiliation_split(raw, expected):
    assert cr._split_affiliation(raw) == expected


# ------------------------------------------------------------------------------------- capture + parse
def test_capture_pages_cursor_and_filters_to_meeting_records(synthetic_bundle):
    _, bundle = synthetic_bundle
    assert bundle["inventory"]["records"] == [
        "10.9999/synth.2030.1_suppl.e2001", "10.9999/synth.2030.1_suppl.lba7", "10.9999/synth.2030.1_suppl.tps101"]
    assert len(bundle["listings"]) == 3 and bundle["source"]["scanned"] == 4
    assert all(k.startswith("crossref:") for k in bundle["details"])


def test_parse_maps_records_to_canonical_presentations(synthetic_bundle):
    config, bundle = synthetic_bundle
    parsed = config.adapter().parse(bundle, config)
    by_number = {p["number"]: p for p in parsed["presentations"]}
    assert set(by_number) == {"LBA7", "TPS101", "E2001"}
    lba = by_number["LBA7"]
    assert lba["id"] == "synth-2030:crossref:10.9999/synth.2030.1_suppl.lba7"
    assert lba["format"] == "oral" and lba["tip"] is False and lba["timezone"] == "America/Chicago"
    assert [a["role"] for a in lba["authors"]] == ["author", "coauthor"]
    assert lba["authors"][1]["country"] == "France"
    assert lba["url"] == "https://doi.org/10.9999/synth.2030.1_suppl.lba7"
    assert by_number["TPS101"]["format"] == "poster" and by_number["TPS101"]["tip"] is True
    online = by_number["E2001"]
    assert online["format"] == "other" and online["abstract"] is None and online["abstractSections"] is None
    assert online["authors"][0]["name"] == "SYNTH Consortium"
    # missing means null: no zero/empty placeholders
    assert online["date"] is None and online["sessionId"] is None and online["views"] is None
    # the same person at the same institution is one Person
    assert sum(1 for p in parsed["people"] if p["name"] == "Ada Example") == 1
    assert parsed["parseReport"]["captured"] == parsed["parseReport"]["discovered"] == 3


def test_parse_is_deterministic(synthetic_bundle):
    config, bundle = synthetic_bundle
    adapter = config.adapter()
    first = json.dumps(adapter.parse(bundle, config), sort_keys=True)
    assert first == json.dumps(adapter.parse(json.loads(json.dumps(bundle)), config), sort_keys=True)


def test_strip_title_prefix_and_title_number(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "PoliteHttp", FakeHttp)
    path = crossref_config(tmp_path, number={"from": "title", "regex": r"^([A-Z][a-z]+)"},
                           stripTitlePrefix=r"^Zentuximab plus\s+")
    config = ConferenceConfig.load(path)
    parsed = config.adapter().parse(config.adapter().capture(config), config)
    lba = next(p for p in parsed["presentations"] if p["sourceId"].endswith("lba7"))
    assert lba["number"] == "Zentuximab" and lba["title"].startswith("carboplatin versus carboplatin")


def test_capture_stops_on_robots_block(tmp_path, monkeypatch):
    class BlockedHttp(FakeHttp):
        def get(self, url, params=None):
            raise CaptureBlocked("robots.txt disallows")

    monkeypatch.setattr(cr, "PoliteHttp", BlockedHttp)
    config = ConferenceConfig.load(crossref_config(tmp_path))
    bundle = config.adapter().capture(config)
    assert bundle["details"] == {} and "robots.txt disallows" in bundle["errors"][-1][1]


def test_bad_source_regex_rejected(tmp_path):
    with pytest.raises(ConfigurationError, match="not a valid regex"):
        ConferenceConfig.load(crossref_config(tmp_path, match={"doi": "("})).assert_supported()


def test_crossref_end_to_end_release_passes_quality_gate(synthetic_bundle, tmp_path):
    config, bundle = synthetic_bundle
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    result = run_release(config=config, bundle_path=bundle_path, run_dir=tmp_path / "run")
    assert result.is_releasable, result.qa_failures
    doc = json.loads(result.document_path.read_text(encoding="utf-8"))
    assert result.document_path.name == "synth-2030.json"
    assert doc["conference"]["platform"] == "crossref" and doc["coverage"]["captured"] == 3
    lba = next(p for p in doc["presentations"] if p["number"] == "LBA7")
    # synthetic records are "published" in 2030, after the capture time, so they are still embargoed
    assert lba["embargo"] == {"embargoed": True, "liftsAtUtc": "2030-06-01T00:00:00Z"}
    disease_ids = {t["entityId"] for t in lba["tags"] if t["kind"] == "disease"}
    assert "nsclc" in disease_ids
    assert not any(t["kind"] == "target" and t["label"] == "HER2" and t["role"] == "drug_target" for t in lba["tags"])
    check_ids = {c["id"] for c in doc["qa"]["checks"]}
    assert "coverage_records" in check_ids


# ------------------------------------------------------------------------------------- eventscribe generalisation
def test_eventscribe_conference_context_switches_and_restores_constants():
    before = (parse.CONF_ID, parse.TIMEZONE, parse.BASE_URL)
    with parse.conference_context("sno-2023", "America/Vancouver", "https://sno2023.eventscribe.net", None,
                                  {"name": "SNO 2023"}):
        p = parse._empty_presentation("PosterInfo", "1")
        assert p["id"] == "sno-2023:PosterInfo:1" and p["timezone"] == "America/Vancouver"
        assert parse.abs_url("/x.asp") == "https://sno2023.eventscribe.net/x.asp"
    assert (parse.CONF_ID, parse.TIMEZONE, parse.BASE_URL) == before


def test_pan_oncology_disease_layer_present():
    kb = json.loads((ROOT / "pipeline" / "kb" / "diseases.json").read_text(encoding="utf-8"))
    ids = {d["id"] for d in kb["diseases"]}
    assert {"nsclc", "aml", "mm", "breast_cancer", "gbm", "sarcoma", "ddlps"} <= ids
    parents = {d["parent"] for d in kb["diseases"] if d.get("parent")}
    assert parents <= ids
