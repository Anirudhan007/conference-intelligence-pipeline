"""Tests for the Assembler (aggregate, qa, export, CLI) on small synthetic inputs only."""
from __future__ import annotations

import copy
import csv
import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run as cli  # noqa: E402
from pipeline import aggregate, export, qa  # noqa: E402

CID = "ctos-2026"


@pytest.fixture
def tmp_path(request):
    """Keep scratch files inside the workspace (BRIEF rule 7), one clean dir per test."""
    import shutil
    d = ROOT / "tests" / ".tmp_assemble" / request.node.name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_external_kb(monkeypatch):
    """Isolate from the Tagger's pipeline/kb dictionaries: tests use only synthetic inputs."""
    monkeypatch.setattr(aggregate, "_kb_targets", lambda: {})
    monkeypatch.setattr(aggregate, "_kb_diseases", lambda: {})
    monkeypatch.setattr(export, "_kb_index", lambda: {})


def ev(field, text, quote):
    i = text.index(quote)
    return [{"field": field, "charStart": i, "charEnd": i + len(quote), "quote": quote}]


def make_pres(i, fmt="poster", title=None, abstract=None, track="Bone Sarcoma", session=None, date="2026-11-19"):
    kind = "PosterInfo" if fmt == "poster" else "PresentationInfo"
    title = title or f"Study {i} of osteosarcoma"
    return {
        "id": f"{CID}:{kind}:{1000 + i}", "number": f"P {i:02d}" if fmt == "poster" else f"Paper {i}",
        "title": title, "titleNormalized": title.lower(), "format": fmt, "tip": False, "track": track,
        "sessionId": session, "sessionTitle": "Session 1: Bone" if session else None, "date": date,
        "startTime": "09:00", "endTime": "10:00", "timezone": "Asia/Seoul", "location": "Hall A",
        "authors": [{"personId": "person:1", "name": "A. Kim", "degrees": "MD", "pronouns": None,
                     "role": "presenter", "position": None, "institution": "Seoul National University Hospital",
                     "city": "Seoul", "region": None, "country": "Korea", "order": 1}],
        "abstract": abstract, "abstractSections": None, "disclosures": [], "slidesAvailable": None,
        "views": None, "url": f"https://x/{i}", "sourceKind": kind, "sourceId": str(1000 + i),
        "contentHash": f"h{i}", "embargo": {"embargoed": abstract is not None,
                                            "liftsAtUtc": "2026-11-18T14:00:00Z"},
    }


ABS = ("Objective: test drug X. Methods: 40 patients received sunitinib. Results: ORR was 25% (10/40; "
       "95% CI 12.7-41.2). Median PFS 6.1 months. HR 0.62 (95% CI 0.41-0.93), p=0.02.")


def synthetic():
    pres = [make_pres(i) for i in range(1, 7)]
    pres.append(make_pres(7, fmt="oral", title="Sunitinib in KIT-mutant GIST", abstract=ABS, track=None,
                          session="ctos-2026:SessionInfo:9", date="2026-11-20"))
    pres.append(make_pres(8, fmt="oral", title="HER2-negative angiosarcoma cohort", track=None,
                          session="ctos-2026:SessionInfo:9", date="2026-11-20"))
    parsed = {
        "conference": {"id": CID, "name": "CTOS 2026"},
        "presentations": pres,
        "sessions": [{"id": "ctos-2026:SessionInfo:9", "title": "Session 1: Bone", "date": "2026-11-20",
                      "startTime": "09:00", "endTime": "10:30", "location": "Hall A", "chairs": [],
                      "moderators": [], "presentationIds": [pres[6]["id"], pres[7]["id"]]}],
        "people": [{"id": "person:1", "name": "A. Kim", "institutions": ["SNUH"], "countries": ["South Korea"]}],
        "sponsors": [{"id": "sponsor:1", "name": "Acme"}], "events": [], "parseReport": {},
    }
    p7, p8 = pres[6], pres[7]
    tags = {p["id"]: [{"kind": "disease", "entityId": "OS", "label": "Osteosarcoma", "role": None,
                       "polarity": None, "evidence": ev("title", p["title"], "osteosarcoma"), "method": "dict",
                       "confidence": 0.9}] for p in pres[:6]}
    tags[p7["id"]] = [
        {"kind": "drug", "entityId": "drug:sunitinib", "label": "sunitinib", "role": "investigational",
         "polarity": None, "evidence": ev("title", p7["title"], "Sunitinib"), "method": "dict", "confidence": 1},
        {"kind": "target", "entityId": "KIT", "label": "KIT", "role": "drug_target", "polarity": None,
         "evidence": ev("title", p7["title"], "KIT"), "method": "kb", "confidence": 1},
        {"kind": "target", "entityId": "KDR", "label": "VEGFR2", "role": "drug_target", "polarity": None,
         "evidence": ev("title", p7["title"], "Sunitinib"), "method": "kb", "confidence": 1},
        {"kind": "target", "entityId": "KIT", "label": "KIT", "role": "selection_biomarker", "polarity": "mutant",
         "evidence": ev("title", p7["title"], "KIT-mutant"), "method": "dict", "confidence": 1},
        {"kind": "modality", "entityId": "small_molecule", "label": "Tyrosine kinase inhibitor", "role": None,
         "polarity": None, "evidence": ev("title", p7["title"], "Sunitinib"), "method": "kb", "confidence": 1},
        {"kind": "disease", "entityId": "GIST", "label": "Gastrointestinal stromal tumor", "role": None,
         "polarity": None, "evidence": ev("title", p7["title"], "GIST"), "method": "dict", "confidence": 1},
    ]
    tags[p8["id"]] = [
        {"kind": "target", "entityId": "ERBB2", "label": "HER2", "role": "biomarker_status", "polarity": "negative",
         "evidence": ev("title", p8["title"], "HER2-negative"), "method": "dict", "confidence": 1},
        {"kind": "disease", "entityId": "ANGS", "label": "Angiosarcoma", "role": None, "polarity": None,
         "evidence": ev("title", p8["title"], "angiosarcoma"), "method": "dict", "confidence": 1},
    ]
    readouts = [{
        "id": "ro:1", "code": "P7-1", "presentationId": p7["id"], "drug": "sunitinib", "trial": None,
        "targets": ["KIT"], "modality": "Small molecule", "disease": "GIST", "setting": "2L", "phase": "2",
        "design": "single-arm", "studyN": 40, "cutoff": "2026-06-30", "followUp": None, "analysis": "primary",
        "primaryEndpoint": "ORR", "readoutType": "efficacy",
        "metrics": [
            {"key": "orr", "label": "ORR", "unit": "%", "endpointType": "primary", "population": "ITT",
             "status": "reported", "hr": None, "reportedValue": "25%",
             "arms": [{"label": "sunitinib", "role": "experimental", "n": 40, "value": 25, "ci": [12.7, 41.2],
                       "ciLevel": 95, "count": 10, "denominator": 40,
                       "evidence": ev("abstract", ABS, "ORR was 25% (10/40; 95% CI 12.7-41.2)")}]},
            {"key": "pfs", "label": "PFS", "unit": "months", "endpointType": "secondary", "population": "ITT",
             "status": "reported", "hr": {"value": 0.62, "ci": [0.41, 0.93], "level": 95, "p": 0.02},
             "reportedValue": None,
             "arms": [{"label": "sunitinib", "role": "experimental", "n": 40, "value": 6.1, "ci": None,
                       "ciLevel": None, "count": None, "denominator": None,
                       "evidence": ev("abstract", ABS, "Median PFS 6.1 months")}]},
        ],
        "safety": [], "limitation": None, "evidence": ev("abstract", ABS, "40 patients received sunitinib"),
        "review": {"status": "auto_verified", "by": "verifier", "at": None},
    }]
    return parsed, tags, readouts


def bundle_for(parsed):
    posters = "".join(f'<li data-posterid="{p["sourceId"]}">x</li>' for p in parsed["presentations"]
                      if p["format"] == "poster")
    talks = "".join(f'<li data-presid="{p["sourceId"]}">x</li>' for p in parsed["presentations"]
                    if p["format"] != "poster")
    home = "<p>Abstracts are under embargo until 9:00 AM EST on November 18, 2026.</p>"
    t = "2026-11-11T10:00:00Z"
    return {"source": {}, "listings": {
        "posters": {"url": "https://x/posters", "retrievedAt": t, "bytes": len(posters), "html": posters},
        "presentations": {"url": "https://x/pres", "retrievedAt": t, "bytes": len(talks), "html": talks},
        "home": {"url": "https://x/home", "retrievedAt": t, "bytes": len(home), "html": home}},
        "details": {}}


def write_inputs(tmp: Path, parsed, tags, readouts, bundle=None):
    stage = tmp / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    paths = {"parsed": stage / "parsed.json", "tags": stage / "tags.json", "readouts": stage / "readouts.json",
             "aggregates": stage / "aggregates.json", "qa": stage / "qa.json", "activities": stage / "activities.json",
             "bundle": tmp / "bundle.json", "out_json": tmp / "out" / "c.json", "out_gz": tmp / "out" / "c.json.gz",
             "out_csv": tmp / "out" / "c.csv", "out_prev": tmp / "out" / "c.prev.json", "report": tmp / "report.md"}
    paths["parsed"].write_text(json.dumps(parsed))
    paths["tags"].write_text(json.dumps(tags))
    if readouts is not None:
        paths["readouts"].write_text(json.dumps(readouts))
    if bundle is not None:
        paths["bundle"].write_text(json.dumps(bundle))
    return paths


def checks_by_id(q):
    return {c["id"]: c for c in q["checks"]}


# ------------------------------------------------------------------------------------------ aggregate
def test_heatmap_uses_only_drug_target_and_unique_presentations():
    parsed, tags, readouts = synthetic()
    res = aggregate.compute(parsed, tags, readouts)
    hm = res["aggregates"]["heatmap"]
    cells = {(c["row"], c["col"]): c["count"] for c in hm["cells"]}
    assert cells == {("kit-pdgfra", "small-molecule"): 1, ("vegf-vegfr", "small-molecule"): 1}
    assert hm["roleFilter"] == "drug_target"
    assert hm["coverage"] == {"presentationsInGrid": 1, "total": 8}
    assert hm["scale"]["max"] == 1 and hm["scale"]["p95"] == 1.0
    # HER2-negative biomarker_status never reaches the grid
    assert not any("her2" in c["row"] for c in hm["cells"])


def test_heatmap_pairs_target_with_its_own_drug_modality():
    parsed, tags, _ = synthetic()
    p = parsed["presentations"][7]
    e = ev("title", p["title"], "HER2")
    tags[p["id"]] = [
        {"kind": "drug", "entityId": "tdxd", "label": "trastuzumab deruxtecan", "role": "investigational",
         "modality": "adc", "evidence": e},
        {"kind": "drug", "entityId": "pembro", "label": "pembrolizumab", "role": "investigational",
         "modality": "monoclonal_antibody", "evidence": e},
        {"kind": "modality", "entityId": "adc", "label": "Antibody-drug conjugate", "fromDrugs": ["tdxd"],
         "evidence": e},
        {"kind": "modality", "entityId": "monoclonal_antibody", "label": "Monoclonal antibody",
         "fromDrugs": ["pembro"], "evidence": e},
        {"kind": "target", "entityId": "ERBB2", "label": "HER2", "role": "drug_target", "fromDrugs": ["tdxd"],
         "members": ["ERBB2"], "evidence": e},
        {"kind": "target", "entityId": "PDCD1", "label": "PD-1", "role": "drug_target", "fromDrugs": ["pembro"],
         "members": ["PDCD1"], "evidence": e},
    ]
    hm = aggregate.compute(parsed, tags, [])["aggregates"]["heatmap"]
    cells = {(c["row"], c["col"]) for c in hm["cells"]}
    assert ("her2", "adc") in cells and ("pd-1-pd-l1", "monoclonal-antibody") in cells
    assert ("her2", "monoclonal-antibody") not in cells and ("pd-1-pd-l1", "adc") not in cells


def test_heatmap_scale_from_data():
    parsed, tags, _ = synthetic()
    base = parsed["presentations"][6]
    for i in range(10, 14):  # four more KIT TKI presentations
        p = copy.deepcopy(base)
        p["id"] = f"{CID}:PresentationInfo:{i}"
        parsed["presentations"].append(p)
        tags[p["id"]] = copy.deepcopy(tags[base["id"]])
    hm = aggregate.compute(parsed, tags, [])["aggregates"]["heatmap"]
    counts = sorted(c["count"] for c in hm["cells"])
    assert counts == [5, 5]
    assert hm["scale"]["max"] == 5 and hm["scale"]["breaks"] == [5.0]


def test_themes_sarcoma_activation_and_folding():
    parsed, tags, readouts = synthetic()
    themes = {t["id"]: t for t in aggregate.compute(parsed, tags, readouts)["aggregates"]["themes"]}
    # six osteosarcoma posters: osteosarcoma (6) survives; bone_tumors (6) survives
    assert themes["osteosarcoma"]["count"] == 6
    assert themes["bone_tumors"]["count"] >= 6
    assert themes["osteosarcoma"]["definition"]["activation"]["active"] is True
    # GIST has 1 presentation -> folded into "other"
    assert "gist" not in themes
    assert "gist" in themes["other"]["definition"]["foldedFrom"]
    assert all(t["count"] >= aggregate.MIN_THEME_SIZE for k, t in themes.items() if k != "other")


def test_themes_child_folds_into_parent():
    parsed, tags, _ = synthetic()
    # make 3 of 6 posters Ewing instead of osteosarcoma: ewing (3) folds into bone_tumors
    for p in parsed["presentations"][:3]:
        p["title"] = p["title"].replace("osteosarcoma", "Ewing sarcoma")
        tags[p["id"]] = [{"kind": "disease", "entityId": "ES", "label": "Ewing sarcoma", "role": None,
                          "polarity": None, "evidence": ev("title", p["title"], "Ewing sarcoma"),
                          "method": "dict", "confidence": 1}]
    themes = {t["id"]: t for t in aggregate.compute(parsed, tags, [])["aggregates"]["themes"]}
    assert "ewing" not in themes and "osteosarcoma" not in themes
    assert {"ewing", "osteosarcoma"} <= set(themes["bone_tumors"]["definition"]["foldedFrom"])
    assert themes["bone_tumors"]["count"] == 8  # 6 posters + 2 talks in session "Session 1: Bone"


def test_kb_disease_hierarchy_drives_themes(monkeypatch):
    parsed, tags, _ = synthetic()
    monkeypatch.setattr(aggregate, "_kb_diseases", lambda: {
        "adamantinoma": {"label": "Adamantinoma", "groups": ["bone"], "ancestors": ["bone_sarcoma", "sarcoma"],
                         "oncotree": None}})
    for p in parsed["presentations"][:6]:
        p["track"] = "General"
        tags[p["id"]] = [{"kind": "disease", "entityId": "adamantinoma", "label": "Adamantinoma", "role": None,
                          "polarity": None, "evidence": ev("title", p["title"], "Study"), "method": "dict",
                          "confidence": 1}]
    themes = {t["id"]: t for t in aggregate.compute(parsed, tags, [])["aggregates"]["themes"]}
    assert themes["bone_tumors"]["count"] >= 6 and themes["ultra_rare"]["count"] == 6


def test_no_sarcoma_set_for_non_sarcoma_meeting():
    parsed, tags, _ = synthetic()
    for pid, tl in tags.items():
        for t in tl:
            if t["kind"] == "disease":
                t["label"], t["entityId"] = "Non-small cell lung cancer", "NSCLC"
    themes = aggregate.compute(parsed, tags, [])["aggregates"]["themes"]
    assert all(t["definition"]["templateSet"] in ("pan_oncology", "derived") for t in themes)


def test_overview_and_facets():
    parsed, tags, readouts = synthetic()
    a = aggregate.compute(parsed, tags, readouts)["aggregates"]
    ov = a["overview"]
    assert ov["talksPerDay"] == [{"date": "2026-11-20", "count": 2, "byFormat": {"oral": 2}}]
    assert ov["postersPerTrack"] == [{"track": "Bone Sarcoma", "count": 6}]
    assert ov["topCountries"][0] == {"name": "South Korea", "count": 8}
    assert ov["diseaseCounts"][0]["label"] == "Osteosarcoma" and ov["diseaseCounts"][0]["count"] == 6
    assert ov["sessionGroups"][0]["group"] == "Scientific sessions"
    assert {r["value"] for r in a["facets"]["format"]} == {"poster", "oral"}


# ------------------------------------------------------------------------------------------ export + qa
def test_export_end_to_end_valid(tmp_path):
    parsed, tags, readouts = synthetic()
    paths = write_inputs(tmp_path, parsed, tags, readouts, bundle_for(parsed))
    doc = export.run(paths)
    c = checks_by_id(doc["qa"])
    assert doc["qa"]["schemaValid"], c["schema_valid"]["examples"]
    failing = {k: v["examples"] for k, v in c.items() if v["failures"]}
    assert failing == {}, failing
    assert doc["embargo"]["rules"][0]["sourceQuote"].startswith("Abstracts are under embargo")
    assert doc["coverage"]["withPoster"] == 6 and doc["coverage"]["withAbstract"] == 1
    p7 = next(p for p in doc["presentations"] if p["id"].endswith("1007"))
    assert p7["drug"] == ["sunitinib"] and p7["targets"] == ["KIT", "VEGFR2"]
    assert p7["rights"]["displayFullText"] is False and p7["readoutIds"] == ["ro:1"]
    assert {e["id"] for e in doc["entities"]["targets"]} == {"KIT", "KDR", "ERBB2"}
    assert json.loads(gzip.open(paths["out_gz"]).read()) == json.loads(paths["out_json"].read_text())
    rows = list(csv.DictReader(paths["out_csv"].open(encoding="utf-8-sig")))
    assert len(rows) == 8 and rows[0]["id"]
    rep = paths["report"].read_text()
    assert "## QA" in rep and "No previous export found." in rep
    # second run: diff against previous
    doc2 = export.run(paths)
    assert doc2["coverage"]["deltaSincePrevious"]["added"] == []
    assert "No previous export found." not in paths["report"].read_text()


def test_export_deterministic(tmp_path):
    parsed, tags, readouts = synthetic()
    paths = write_inputs(tmp_path, parsed, tags, readouts, bundle_for(parsed))
    d1, _ = export.build_document(paths)
    d2, _ = export.build_document(paths)
    assert export.content_hash(d1) == export.content_hash(d2)


def test_missing_readouts_file_is_empty_list(tmp_path):
    parsed, tags, _ = synthetic()
    paths = write_inputs(tmp_path, parsed, tags, None, bundle_for(parsed))
    doc = export.run(paths)
    assert doc["readouts"] == [] and doc["qa"]["schemaValid"]
    assert any("readouts.json absent" in i for i in doc["qa"]["knownIssues"])


def _qa_for(parsed, tags, readouts, tmp_path, bundle=True):
    paths = write_inputs(tmp_path, parsed, tags, readouts, bundle_for(parsed) if bundle else None)
    doc, ctx = export.build_document(paths)
    return checks_by_id(qa.compute(doc, ctx)), doc


def test_numeric_checks_catch_errors(tmp_path):
    parsed, tags, readouts = synthetic()
    r = readouts[0]
    orr, pfs = r["metrics"]
    orr["arms"][0]["ci"] = [30.0, 41.2]           # does not contain 25
    orr["arms"][0]["count"] = 12                  # 12/40 = 30% != 25%
    pfs["hr"] = {"value": -0.5, "ci": [0.9, 0.4], "level": 95, "p": 1.4}
    r["cutoff"] = "2026-12-01"                    # after presentation
    r["studyN"] = 30                              # arms n=40
    r["metrics"].append({"key": "dcr", "label": "DCR", "unit": "%", "arms": [
        {"label": "x", "value": 120, "evidence": ev("abstract", ABS, "ORR was 25%")}]})
    c, _ = _qa_for(parsed, tags, readouts, tmp_path)
    for cid in ("readout_ci_contains_estimate", "readout_ci_order", "readout_hr_positive", "readout_p_range",
                "readout_percent_range", "readout_count_denominator", "readout_arm_n_sum",
                "readout_cutoff_before_presentation"):
        assert c[cid]["failures"] >= 1, cid


def test_evidence_and_integrity_checks(tmp_path):
    parsed, tags, readouts = synthetic()
    p7 = parsed["presentations"][6]
    tags[p7["id"]][0]["evidence"] = [{"field": "title", "charStart": 0, "charEnd": 5, "quote": "Imatinib"}]
    readouts[0]["metrics"][0]["arms"][0]["evidence"] = [{"field": "abstract", "quote": "ORR was 52%"}]
    parsed["presentations"][0]["sessionId"] = "ctos-2026:SessionInfo:404"
    tags["ctos-2026:PosterInfo:999999"] = []
    readouts.append({"id": "ro:orphan", "presentationId": "ctos-2026:PosterInfo:31337"})
    c, _ = _qa_for(parsed, tags, readouts, tmp_path)
    assert c["evidence_tags"]["failures"] == 1
    assert c["evidence_tag_offsets"]["failures"] == 1
    assert c["evidence_readouts"]["failures"] == 1
    assert c["ref_sessions"]["failures"] == 1
    assert c["ref_tags"]["failures"] == 1
    assert c["ref_readouts"]["failures"] == 1
    paths = write_inputs(tmp_path, parsed, tags, readouts, bundle_for(parsed))
    doc, ctx = export.build_document(paths)
    issues = qa.compute(doc, ctx)["knownIssues"]
    assert any(i.startswith("STALE INPUT: tags.json") and "999999" in i for i in issues)
    assert any(i.startswith("STALE INPUT:") and "31337" in i for i in issues)


def test_coverage_embargo_and_zero_checks(tmp_path):
    parsed, tags, readouts = synthetic()
    bundle = bundle_for(parsed)
    bundle["listings"]["posters"]["html"] += '<li data-posterid="5555">x</li>'
    parsed["presentations"][6]["embargo"] = {"embargoed": False, "liftsAtUtc": None}
    readouts[0]["metrics"][0]["arms"][0]["count"] = 0
    readouts[0]["metrics"][0]["arms"][0]["value"] = 0
    readouts[0]["metrics"][0]["arms"][0]["evidence"] = []
    paths = write_inputs(tmp_path, parsed, tags, readouts, bundle)
    doc, ctx = export.build_document(paths)
    c = checks_by_id(qa.compute(doc, ctx))
    assert c["coverage_posters"]["failures"] == 2  # count mismatch + missing id
    assert c["embargo_flag"]["failures"] == 1
    assert c["no_zero_imputation"]["failures"] == 2


def test_tag_rule_checks(tmp_path):
    parsed, tags, readouts = synthetic()
    p8 = parsed["presentations"][7]
    tags[p8["id"]][0]["role"] = "drug_target"  # HER2-negative as drug target
    tags[p8["id"]].append({"kind": "modality", "entityId": "adc", "label": "ADC", "role": None, "polarity": None,
                           "evidence": ev("title", p8["title"], "HER2"), "method": "x", "confidence": 1})
    c, _ = _qa_for(parsed, tags, readouts, tmp_path)
    assert c["tag_negative_not_drug_target"]["failures"] == 1
    assert c["tag_modality_from_drug"]["failures"] == 1


def test_schema_rejects_bad_document(tmp_path):
    parsed, tags, readouts = synthetic()
    parsed["presentations"][0]["format"] = "keynote"
    tags[parsed["presentations"][1]["id"]][0]["polarity"] = "sideways"
    c, _ = _qa_for(parsed, tags, readouts, tmp_path)
    assert c["schema_valid"]["failures"] >= 2


# ------------------------------------------------------------------------------------------ CLI
def test_cli_missing_module_errors_clearly(monkeypatch, capsys):
    monkeypatch.setitem(cli.CONTRACTS, "parse", ("pipeline.no_such_stage", "run", ["bundle_path", "out_path"],
                                                 "Agent Parser"))
    with pytest.raises(cli.StageUnavailable, match="not found"):
        cli.load_stage("parse")


def test_cli_missing_function_errors_clearly(monkeypatch):
    monkeypatch.setitem(cli.CONTRACTS, "tag", ("pipeline.assemble_common", "run", ["parsed_path", "out_path"],
                                               "Agent Tagger"))
    with pytest.raises(cli.StageUnavailable, match="has no function run"):
        cli.load_stage("tag")


def test_cli_aggregate_qa_export(tmp_path, capsys):
    parsed, tags, readouts = synthetic()
    paths = write_inputs(tmp_path, parsed, tags, readouts, bundle_for(parsed))
    common = ["--parsed", str(paths["parsed"]), "--tags", str(paths["tags"]), "--readouts", str(paths["readouts"]),
              "--aggregates", str(paths["aggregates"]), "--qa", str(paths["qa"]), "--bundle", str(paths["bundle"]),
              "--out", str(tmp_path / "out" / "x.json")]
    assert cli.main(["aggregate", "qa"] + common) == 0
    assert paths["aggregates"].exists() and paths["qa"].exists()
    assert cli.main(["parse", "--bundle", str(tmp_path / "missing.json"), "--parsed", str(paths["parsed"])]) == 2
    assert "not found" in capsys.readouterr().err


def test_legacy_cli_blocks_export_with_failed_quality_gate(tmp_path):
    parsed, tags, readouts = synthetic()
    readouts[0]["metrics"][0]["arms"][0]["ci"] = [30.0, 41.2]
    paths = write_inputs(tmp_path, parsed, tags, readouts, bundle_for(parsed))
    common = ["--parsed", str(paths["parsed"]), "--tags", str(paths["tags"]), "--readouts", str(paths["readouts"]),
              "--aggregates", str(paths["aggregates"]), "--qa", str(paths["qa"]), "--bundle", str(paths["bundle"]),
              "--out", str(tmp_path / "out" / "x.json")]
    assert cli.main(["export"] + common) == 3
    assert cli.main(["export", "--allow-qa-failures"] + common) == 0
