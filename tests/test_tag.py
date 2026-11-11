"""Tests for pipeline/tag.py covering the known Conference Atlas error cases."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.tag import TaggerE, TaggerK, verify_evidence  # noqa: E402


def pres(title="", abstract=None, track=None, pid="ctos-2026:test:1"):
    return {"id": pid, "title": title, "abstract": abstract, "track": track, "sessionTitle": None}


@pytest.fixture(scope="module")
def E():
    return TaggerE()


@pytest.fixture(scope="module")
def K():
    return TaggerK()


def tags_of(tags, kind, role=None):
    return [t for t in tags if t["kind"] == kind and (role is None or t["role"] == role)]


def ids(tags, kind, role=None):
    return {t["entityId"] for t in tags_of(tags, kind, role)}


# ---------------------------------------------------------------- HER2-negative
def test_her2_negative_is_not_drug_target(E):
    p = pres("DOXORUBICIN IN HER2-NEGATIVE SOFT TISSUE SARCOMA",
             "Objective: Patients with HER2-negative tumors received doxorubicin.")
    t = E.tag(p)
    assert "ERBB2" not in ids(t, "target", "drug_target")
    her2 = [x for x in tags_of(t, "target") if x["entityId"] == "ERBB2"]
    assert her2 and all(x["polarity"] == "negative" for x in her2)
    assert all(x["role"] in ("selection_biomarker", "biomarker_status") for x in her2)
    assert not verify_evidence(p, t)


def test_her2_negative_baseline_error_reproduced(K):
    t = K.tag(pres("DOXORUBICIN IN HER2-NEGATIVE SOFT TISSUE SARCOMA"))
    assert "ERBB2" in ids(t, "target", "drug_target")  # the known Conference Atlas error


@pytest.mark.parametrize("text,pol", [
    ("HER2 0 tumors", "negative"), ("HER2-low disease", "low"), ("HER2 IHC 1+ tumors", "low"),
    ("HER2-positive tumors", "positive"), ("EGFR wild-type tumors", "wild_type"), ("KRAS WT tumors", "wild_type"),
    ("KIT exon 11 mutant GIST", "mutant"), ("PDGFRA D842V GIST", "mutant"), ("MDM2 amplified tumors", "amplified"),
    ("NTRK fusion-positive tumors", "fusion"), ("PD-L1 <1% tumors", "negative"), ("no KRAS mutation was seen", "wild_type"),
    ("loss of INI1 expression", "negative"), ("SMARCB1-deficient tumors", "negative"),
])
def test_polarity_patterns(E, text, pol):
    t = E.tag(pres("", "Results: " + text + "."))
    pols = {x["polarity"] for x in tags_of(t, "target") if x["role"] != "drug_target"}
    assert pol in pols, (text, pols)


def test_fusion_partners(E):
    t = E.tag(pres("", "Methods: Ewing sarcoma with EWSR1-FLI1 fusion and synovial sarcoma with SS18::SSX1 fusion."))
    fus = {x["entityId"]: x for x in tags_of(t, "target") if x["polarity"] == "fusion"}
    assert {"EWSR1", "FLI1", "SS18", "SSX1"} <= set(fus)
    assert "FLI1" in fus["EWSR1"]["fusionPartners"]
    assert "ewing" in ids(t, "disease") and "synovial_sarcoma" in ids(t, "disease")


# ---------------------------------------------------------------- ADC modality
def test_tdxd_is_adc_with_payload_component(E):
    t = E.tag(pres("TRASTUZUMAB DERUXTECAN (T-DXD) IN HER2-EXPRESSING SARCOMA",
                   "Methods: Patients received T-DXd 5.4 mg/kg every 3 weeks."))
    drug = [x for x in tags_of(t, "drug") if x["entityId"] == "t_dxd"]
    assert drug and drug[0]["modality"] == "adc"
    comps = {c["role"]: c for c in drug[0]["components"]}
    assert comps["payload"]["name"].startswith("deruxtecan")
    assert comps["antibody"]["target"] == "ERBB2"
    assert ids(t, "modality") == {"adc"}            # payload never adds a small molecule / chemo modality
    assert "ERBB2" in ids(t, "target", "drug_target")


def test_baseline_tags_adc_as_small_molecule(K):
    t = K.tag(pres("TRASTUZUMAB DERUXTECAN IN SARCOMA"))
    assert "small_molecule_kinase_inhibitor" in ids(t, "modality")   # known baseline error


# ---------------------------------------------------------------- combinations
def test_pembrolizumab_plus_lenvatinib_two_components(E):
    p = pres("PEMBROLIZUMAB PLUS LENVATINIB IN ADVANCED LEIOMYOSARCOMA",
             "Methods: Patients received pembrolizumab 200 mg every 3 weeks plus lenvatinib 20 mg daily.")
    t = E.tag(p)
    drugs = {x["entityId"]: x for x in tags_of(t, "drug")}
    assert {"pembrolizumab", "lenvatinib"} <= set(drugs)
    assert drugs["pembrolizumab"]["modality"] == "monoclonal_antibody"
    assert drugs["lenvatinib"]["modality"] == "small_molecule_kinase_inhibitor"
    assert "lenvatinib" in drugs["pembrolizumab"]["combinationWith"]
    mods = {x["entityId"]: x for x in tags_of(t, "modality")}
    assert mods["monoclonal_antibody"]["fromDrugs"] == ["pembrolizumab"]
    assert mods["small_molecule_kinase_inhibitor"]["fromDrugs"] == ["lenvatinib"]
    assert {"PDCD1", "VEGFR", "FGFR"} <= ids(t, "target", "drug_target")
    assert not verify_evidence(p, t)


def test_regimen_expands_to_components(E):
    t = E.tag(pres("", "Methods: Patients with Ewing sarcoma received VDC/IE chemotherapy."))
    assert {"vincristine", "doxorubicin", "cyclophosphamide", "ifosfamide", "etoposide"} <= ids(t, "drug")


# ---------------------------------------------------------------- lenvatinib / cabozantinib never EGFR
@pytest.mark.parametrize("drug", ["LENVATINIB", "CABOZANTINIB", "ANLOTINIB", "APATINIB"])
def test_multikinase_never_egfr(E, drug):
    t = E.tag(pres(f"{drug} IN OSTEOSARCOMA", f"Methods: Patients received {drug.lower()} daily."))
    assert "EGFR" not in ids(t, "target")


def test_baseline_lenvatinib_egfr_error_reproduced(K):
    assert "EGFR" in ids(K.tag(pres("LENVATINIB IN OSTEOSARCOMA")), "target", "drug_target")


# ---------------------------------------------------------------- CDK4/6 counted once
def test_cdk4_and_cdk46_counted_once(E):
    t = E.tag(pres("PALBOCICLIB, A CDK4/6 INHIBITOR, IN CDK4-AMPLIFIED DEDIFFERENTIATED LIPOSARCOMA",
                   "Methods: Abemaciclib or palbociclib (CDK4 inhibitor) was given."))
    dt = [x for x in tags_of(t, "target", "drug_target")]
    groups = [x["entityId"] for x in dt]
    assert groups.count("CDK4/6") == 1
    assert "CDK4" not in groups and "CDK6" not in groups
    cdk4 = [x for x in tags_of(t, "target") if x["entityId"] == "CDK4"]
    assert cdk4 and cdk4[0]["polarity"] == "amplified" and cdk4[0]["role"] != "drug_target"
    assert "ddlps" in ids(t, "disease")


def test_baseline_cdk_double_count(K):
    t = K.tag(pres("PALBOCICLIB, A CDK4/6 INHIBITOR, IN CDK4-AMPLIFIED LIPOSARCOMA"))
    got = ids(t, "target")
    assert "CDK4/6" in got and "CDK4" in got


# ---------------------------------------------------------------- vocabulary and diseases
def test_new_target_vocabulary(E):
    t = E.tag(pres("", "Methods: Patients received ripretinib, brigimadlin, ipilimumab, olaparib and selpercatinib."))
    assert {"KIT", "PDGFRA", "MDM2", "CTLA4", "PARP1", "RET"} <= ids(t, "target", "drug_target")


def test_disease_deepest_node_and_rollup(E):
    t = E.tag(pres("", "Objective: Liposarcoma is common. We studied dedifferentiated liposarcoma (DDLPS) and soft tissue sarcoma."))
    d = {x["entityId"]: x for x in tags_of(t, "disease")}
    assert "ddlps" in d and "lps" not in d and "sts" not in d
    assert d["ddlps"]["rollup"] == ["lps", "sts", "sarcoma"]
    assert d["ddlps"]["oncotree"] == "DDLS"


def test_os_abbreviation_not_osteosarcoma_when_survival(E):
    t = E.tag(pres("", "Results: Median OS was 14 months in leiomyosarcoma. Overall survival (OS) improved."))
    assert "osteosarcoma" not in ids(t, "disease")


def test_os_defined_as_osteosarcoma(E):
    t = E.tag(pres("", "Objective: Osteosarcoma (OS) is rare. OS patients were enrolled."))
    d = [x for x in tags_of(t, "disease") if x["entityId"] == "osteosarcoma"]
    assert d and len(d[0]["evidence"]) >= 2


# ---------------------------------------------------------------- companies, trials, unknown codes
def test_company_lineage(E):
    t = E.tag(pres("AVAPRITINIB AND RIPRETINIB IN GIST", "Methods: nirogacestat; Merck KGaA and MSD."))
    c = {x["entityId"]: x for x in tags_of(t, "company")}
    assert "sanofi" in c and "ono" in c and "merck_kgaa" in c and "merck_co" in c
    assert c["sanofi"]["role"] == "drug_owner"


def test_registry_ids(E):
    t = E.tag(pres("", "Methods: The trial is registered (NCT01234567; EudraCT 2019-001234-12)."))
    assert {"NCT:NCT01234567", "EudraCT:2019-001234-12"} <= ids(t, "trial")


def test_unknown_drug_codes(E):
    t = E.tag(pres("", "Methods: Patients received ABC-123 at 50 mg daily and novelatinib orally; HT-1080 cells were used."))
    unk = {x["entityId"]: x for x in tags_of(t, "drug") if x.get("unknown")}
    assert "unknown:abc123" in unk and "unknown:novelatinib" in unk
    assert all(x["role"] == "mentioned" and x["confidence"] <= 0.3 for x in unk.values())
    assert not any("1080" in k for k in unk)


def test_deterministic(E):
    p = pres("PEMBROLIZUMAB PLUS LENVATINIB", "Methods: KIT exon 11 mutant GIST; MDM2 amplified DDLPS.")
    assert E.tag(p) == E.tag(p)


def test_synthetic_gold_evaluation(E):
    import json
    from pathlib import Path

    from pipeline import parse, tag_eval

    root = Path(__file__).resolve().parent.parent
    fx = json.loads((root / "tests" / "fixtures_synthetic" / "eventscribe_synthetic.json").read_text(encoding="utf-8"))
    gold = json.loads((root / "tests" / "fixtures_synthetic" / "tags_gold_synthetic.json").read_text(encoding="utf-8"))
    details = {
        "PosterInfo:900101": fx["poster_cards"][0],
        "PosterInfo:900102": fx["poster_cards"][1],
    }
    parsed = parse.build({"listings": {"posters": {"html": fx["poster_listing"]}}, "details": details}, "D")
    tags = {p["id"]: E.tag(p) for p in parsed["presentations"] if p["id"] in gold["items"]}
    scores, _ = tag_eval.score(tag_eval.get_kb(), gold, tags, sorted(gold["items"]))
    assert scores["disease"]["f1"] == 1.0
    assert scores["drug"]["f1"] == 1.0
    assert scores["modality"]["f1"] == 1.0
    acc, count = tag_eval.role_accuracy(gold, tags, sorted(gold["items"]))
    assert acc == 1.0 and count == 2

