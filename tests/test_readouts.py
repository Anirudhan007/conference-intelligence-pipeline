import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import readouts as R  # noqa: E402

AT = "2026-11-11T00:00:00Z"

TRIAL = (
    "Objective:\nTo evaluate drugximab in advanced leiomyosarcoma.\n"
    "Methods:\nThis open-label, single-arm phase 2 trial (NCT01234567) enrolled 40 patients.\n"
    "Results:\nAs of 2026-03-01, the ORR was 25.0% (10/40; 95% CI, 12.7-41.2). "
    "Median PFS was 5.6 months (95% CI 3.1-7.9). Grade >=3 TRAEs occurred in 8 patients (20%).\n"
    "Conclusion:\nDrugximab showed activity."
)
PRECLIN = (
    "Objective:\nStudy of a compound in cell lines.\nMethods:\nXenograft mice were treated in vivo.\n"
    "Results:\nTumor volume decreased by 50% in mice.\nConclusion:\nPromising."
)
RETRO = (
    "Objective:\nOutcomes in Ewing sarcoma.\nMethods:\nRetrospective review of 120 patients.\n"
    "Results:\n5-year OS was 62% and EFS 51%. Median follow-up 48 months.\nConclusion:\nOK."
)


def parsed():
    return {"presentations": [
        {"id": "ctos-2026:PosterInfo:1", "number": "P 01", "title": "Trial", "abstract": TRIAL, "tip": False},
        {"id": "ctos-2026:PosterInfo:2", "number": "P 02", "title": "Mice", "abstract": PRECLIN, "tip": False},
        {"id": "ctos-2026:PosterInfo:3", "number": "P 03", "title": "Retro", "abstract": RETRO, "tip": False},
        {"id": "ctos-2026:PosterInfo:4", "number": "P 04", "title": "No abstract", "abstract": None},
    ]}


def ev(q, text=TRIAL):
    return R.make_evidence(text, q)


def readout():
    return {
        "id": "ro-p-01", "code": "P 01", "presentationId": "ctos-2026:PosterInfo:1",
        "drug": "drugximab", "trial": None, "nct": ["NCT01234567"], "targets": [], "modality": "monoclonal antibody",
        "disease": "Leiomyosarcoma", "setting": "advanced", "phase": "2", "design": "Single-arm",
        "studyN": 40, "cutoff": "2026-03-01", "followUp": None, "analysis": "primary",
        "primaryEndpoint": "orr", "readoutType": "efficacy",
        "metrics": [
            {"key": "orr", "label": "ORR", "unit": "%", "endpointType": "proportion", "population": "unknown",
             "status": None, "hr": None, "reportedValue": "25.0% (10/40; 95% CI, 12.7-41.2)",
             "arms": [{"label": "drugximab", "role": "drug", "n": 40, "value": 25.0, "ci": [12.7, 41.2],
                       "ciLevel": 95, "count": 10, "denominator": 40,
                       "evidence": [ev("the ORR was 25.0% (10/40; 95% CI, 12.7-41.2)")]}]},
            {"key": "pfs", "label": "Median PFS", "unit": "months", "endpointType": "time_to_event",
             "population": "unknown", "status": None, "hr": None, "reportedValue": "5.6 months (95% CI 3.1-7.9)",
             "arms": [{"label": "drugximab", "role": "drug", "n": None, "value": 5.6, "ci": [3.1, 7.9],
                       "ciLevel": 95, "count": None, "denominator": None,
                       "evidence": [ev("Median PFS was 5.6 months (95% CI 3.1-7.9)")]}]},
        ],
        "safety": [{"key": "g3plus_trae", "label": "Grade >=3 TRAE", "unit": "%", "gradeRange": ">=3", "note": None,
                    "arms": [{"label": "drugximab", "role": "drug", "n": None, "value": 20, "ci": None, "ciLevel": None,
                              "count": 8, "denominator": None,
                              "evidence": [ev("Grade >=3 TRAEs occurred in 8 patients (20%)")]}]}],
        "limitation": "Single arm, small N.",
        "evidence": [ev("open-label, single-arm phase 2 trial (NCT01234567) enrolled 40 patients"),
                     ev("As of 2026-03-01")],
    }


def test_score_candidates_ranks_trial_first():
    c = R.score_candidates(parsed(), {})
    ids = [x["presentationId"] for x in c]
    assert ids[0] == "ctos-2026:PosterInfo:1"
    assert "ctos-2026:PosterInfo:4" not in ids
    top = c[0]
    assert top["clinical"] and top["nct"] == ["NCT01234567"]
    assert {"orr", "pfs", "ci", "phase23"} <= set(top["signals"])
    mice = [x for x in c if x["presentationId"].endswith(":2")]
    assert not mice or not mice[0]["clinical"]
    retro = next(x for x in c if x["presentationId"].endswith(":3"))
    assert retro["clinical"] and retro["score"] < top["score"]


def test_drug_tags_boost():
    tags = {"ctos-2026:PosterInfo:3": [{"kind": "drug", "label": "ifosfamide", "role": "investigational"}]}
    a = next(x for x in R.score_candidates(parsed(), {}) if x["presentationId"].endswith(":3"))
    b = next(x for x in R.score_candidates(parsed(), tags) if x["presentationId"].endswith(":3"))
    assert b["score"] > a["score"] and b["drugs"] == ["ifosfamide"]


def test_valid_readout_auto_verified():
    out = R.verify([readout()], parsed(), at=AT)
    assert out[0]["review"]["status"] == "auto_verified", out[0]["review"]["reasons"]
    assert out[0]["review"]["by"] == "agent:readouts" and out[0]["review"]["at"] == AT


def test_wrong_offsets_fixed():
    ro = readout()
    e = ro["metrics"][0]["arms"][0]["evidence"][0]
    e["charStart"] += 3
    e["charEnd"] += 3
    out = R.verify([ro], parsed(), at=AT)[0]
    assert out["review"]["status"] == "auto_verified"
    assert out["review"]["fixes"]
    e2 = out["metrics"][0]["arms"][0]["evidence"][0]
    assert TRIAL[e2["charStart"]:e2["charEnd"]] == e2["quote"]


def test_quote_not_substring_fails():
    ro = readout()
    ro["metrics"][0]["arms"][0]["evidence"][0]["quote"] = "the ORR was 26.0%"
    out = R.verify([ro], parsed(), at=AT)[0]
    assert out["review"]["status"] == "needs_review"
    assert any("not an exact substring" in r for r in out["review"]["reasons"])


def test_number_not_in_quote_fails():
    ro = readout()
    ro["metrics"][1]["arms"][0]["value"] = 5.9
    out = R.verify([ro], parsed(), at=AT)[0]
    assert any("value=5.9 not found" in r for r in out["review"]["reasons"])
    # 5.9 also violates nothing else, but must flag
    assert out["review"]["status"] == "needs_review"


def test_ci_must_contain_estimate_and_lo_lt_hi():
    ro = readout()
    ro["metrics"][1]["arms"][0]["ci"] = [7.9, 3.1]
    out = R.verify([ro], parsed(), at=AT)[0]
    rs = " ".join(out["review"]["reasons"])
    assert "not < hi" in rs


def test_count_denominator_mismatch():
    ro = readout()
    ro["metrics"][0]["arms"][0]["count"] = 40
    ro["metrics"][0]["arms"][0]["denominator"] = 40
    out = R.verify([ro], parsed(), at=AT)[0]
    assert any("differs from value" in r for r in out["review"]["reasons"])


def test_sum_arm_n_exceeds_study_n():
    ro = readout()
    arm2 = copy.deepcopy(ro["metrics"][0]["arms"][0])
    arm2["label"] = "other"
    ro["metrics"][0]["arms"].append(arm2)
    out = R.verify([ro], parsed(), at=AT)[0]
    assert any("sum of arm n" in r for r in out["review"]["reasons"])


def test_p_value_and_percent_bounds():
    ro = readout()
    ro["metrics"][0]["arms"][0]["value"] = 125.0
    ro["metrics"][1]["hr"] = {"value": 0.5, "ci": [0.3, 0.8], "level": 95, "p": 1.5}
    out = R.verify([ro], parsed(), at=AT)[0]
    rs = " ".join(out["review"]["reasons"])
    assert "outside 0..100" in rs and "outside 0..1" in rs


def test_missing_presentation_and_no_abstract():
    ro = readout()
    ro["presentationId"] = "ctos-2026:PosterInfo:4"
    out = R.verify([ro], parsed(), at=AT)[0]
    assert out["review"]["status"] == "needs_review"
    assert "no abstract" in out["review"]["reasons"][0]


def test_studyn_must_be_in_evidence():
    ro = readout()
    ro["studyN"] = 41
    out = R.verify([ro], parsed(), at=AT)[0]
    assert any("studyN=41" in r for r in out["review"]["reasons"])


def test_null_is_not_zero():
    # null values are allowed and never checked against text; zero must appear in text
    ro = readout()
    ro["metrics"][1]["arms"][0]["n"] = 0
    out = R.verify([ro], parsed(), at=AT)[0]
    assert any("n=0 not found" in r for r in out["review"]["reasons"])


def test_numbers_in_parses_formats():
    assert R.number_in(12.7, "95% CI, 12.7-41.2")
    assert R.number_in(1234, "n=1,234 patients")
    assert R.number_in(0.03, "p=.03")
    assert R.number_in(0.5, "HR 0·50")
    assert not R.number_in(12, "12.7")


def test_run_writes_output(tmp_path):
    pp = tmp_path / "parsed.json"
    pp.write_text(json.dumps(parsed()))
    dp = tmp_path / "draft.json"
    dp.write_text(json.dumps([readout()]))
    op = tmp_path / "out.json"
    s = R.run(str(pp), str(tmp_path / "none.json"), str(op), draft_path=str(dp), at=AT)
    assert s == {"n": 1, "auto_verified": 1, "needs_review": 0}
    assert json.loads(op.read_text())[0]["review"]["status"] == "auto_verified"


def test_spelled_numbers_and_reported_value_pieces():
    assert R.number_in(26, "Twenty-six patients have been enrolled")
    ro = readout()
    ro["metrics"][0]["reportedValue"] = "25.0% (10/40; 95% CI, 12.7-41.2) | 5.6 months"
    assert R.verify([ro], parsed(), at=AT)[0]["review"]["status"] == "auto_verified"
    ro["metrics"][0]["reportedValue"] = "25.0% | 99.9 months"
    out = R.verify([ro], parsed(), at=AT)[0]
    assert any("reportedValue piece" in r for r in out["review"]["reasons"])


def test_manual_reason_forces_needs_review_and_primary_endpoint_warning():
    ro = readout()
    ro["primaryEndpoint"] = "os"
    out = R.verify([ro], parsed(), at=AT)[0]
    assert out["review"]["status"] == "auto_verified" and out["review"]["warnings"]
    ro["review"] = {"manualReasons": ["source inconsistency"]}
    out = R.verify([ro], parsed(), at=AT)[0]
    assert out["review"]["status"] == "needs_review"


def test_deterministic_output():
    a = R.verify([readout()], parsed(), at=AT)
    b = R.verify([readout()], parsed(), at=AT)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
