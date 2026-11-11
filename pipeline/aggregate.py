"""Aggregates for the Conference Atlas data layer v2. Owned by Agent Assembler.

    aggregate.run(parsed_path, tags_path, readouts_path, out_path)

Inputs are the canonical stage files (BRIEF.md). Output (data/stage/aggregates.json):
    {"aggregates": {heatmap, designs, themes, targetMap, facets, overview},
     "watchlist": [...], "contextUpdates": [...]}

Everything is computed from the data: no constants in the heatmap scale, no hand-set theme sizes.
Themes are saved queries over tags (and derived target groups / modality families) plus track and
session titles. A theme with fewer than MIN_THEME_SIZE presentations folds into its parent.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from pipeline.assemble_common import (counter_rows, dump_json, load_json, norm_space, presentations_of,
                                      quantile, readouts_list, slug, sorted_unique, tags_index)
from pipeline.readout_norm import normalize_readout

MIN_THEME_SIZE = 5
SARCOMA_ACTIVATION_SHARE = 0.5  # sarcoma set activates when >= half of disease-tagged presentations are sarcoma
TOP_N = 25

# ----------------------------------------------------------------------------- target display groups
# HGNC symbol (or common label) -> display group. Curated; unknown targets display under their own label.
_TARGET_GROUPS = {
    "KIT": "KIT/PDGFRA", "PDGFRA": "KIT/PDGFRA", "PDGFRB": "PDGFR",
    "CDK4": "CDK4/6", "CDK6": "CDK4/6", "CDK2": "CDK2", "MDM2": "MDM2",
    "KDR": "VEGF/VEGFR", "FLT1": "VEGF/VEGFR", "FLT4": "VEGF/VEGFR", "VEGFA": "VEGF/VEGFR",
    "VEGFR": "VEGF/VEGFR", "VEGFR2": "VEGF/VEGFR", "VEGF": "VEGF/VEGFR",
    "PDCD1": "PD-1/PD-L1", "CD274": "PD-1/PD-L1", "PDCD1LG2": "PD-1/PD-L1", "PD-1": "PD-1/PD-L1",
    "PD-L1": "PD-1/PD-L1", "CTLA4": "CTLA-4", "CTLA-4": "CTLA-4", "LAG3": "LAG-3", "TIGIT": "TIGIT",
    "CSF1R": "CSF1/CSF1R", "CSF1": "CSF1/CSF1R",
    "NTRK1": "TRK", "NTRK2": "TRK", "NTRK3": "TRK", "TRK": "TRK",
    "ALK": "ALK", "ROS1": "ROS1", "RET": "RET", "MET": "MET", "EGFR": "EGFR", "ERBB2": "HER2", "HER2": "HER2",
    "FGFR1": "FGFR", "FGFR2": "FGFR", "FGFR3": "FGFR", "FGFR4": "FGFR",
    "MTOR": "mTOR", "PIK3CA": "PI3K/AKT", "AKT1": "PI3K/AKT", "PIK3CB": "PI3K/AKT",
    "BRAF": "BRAF/MEK", "MAP2K1": "BRAF/MEK", "MAP2K2": "BRAF/MEK", "MEK": "BRAF/MEK",
    "EZH2": "EZH2", "XPO1": "XPO1", "PARP1": "PARP", "PARP2": "PARP", "PARP": "PARP",
    "ATR": "DNA damage response", "WEE1": "DNA damage response", "CHEK1": "DNA damage response",
    "IGF1R": "IGF-1R", "CD276": "B7-H3", "B7-H3": "B7-H3", "GD2": "GD2",
    "TOP1": "Topoisomerase I", "TOP2A": "Topoisomerase II", "TUBB": "Tubulin",
    "CTAG1B": "Cancer-testis antigens", "NY-ESO-1": "Cancer-testis antigens", "MAGEA4": "Cancer-testis antigens",
    "PRAME": "Cancer-testis antigens", "SSTR2": "SSTR2", "FAP": "FAP", "CD47": "CD47",
    "NOTCH1": "Gamma-secretase/Notch", "PSEN1": "Gamma-secretase/Notch", "GAMMA-SECRETASE": "Gamma-secretase/Notch",
    "AURKA": "Aurora kinase", "HDAC1": "HDAC", "HDAC": "HDAC", "DNMT1": "DNMT", "IDH1": "IDH1/2", "IDH2": "IDH1/2",
    "SMO": "Hedgehog", "AR": "Androgen receptor", "ESR1": "Estrogen receptor", "TEAD": "YAP/TEAD",
    "MTAP": "PRMT5/MTAP", "PRMT5": "PRMT5/MTAP", "EWSR1-FLI1": "EWS-FLI1", "EWS-FLI1": "EWS-FLI1",
    "GPC3": "GPC3", "HER3": "HER3", "ERBB3": "HER3", "TROP2": "TROP2", "TACSTD2": "TROP2",
    "CDK4/6": "CDK4/6", "FGFR": "FGFR", "NTRK": "TRK", "AKT": "PI3K/AKT", "PI3K": "PI3K/AKT", "IDH": "IDH1/2",
    "MCL1": "BCL-2 family", "BCL2": "BCL-2 family", "RANKL": "RANKL", "TNFSF11": "RANKL",
}


def _clean_id(x: Any) -> str:
    s = str(x or "").strip()
    if ":" in s and not s.upper().startswith(("EWS", "NY-")):
        s = s.split(":", 1)[1]
    return s.upper()


_KB_DIR = Path(__file__).resolve().parent / "kb"


@lru_cache(maxsize=1)
def _kb_targets() -> dict[str, str]:
    """Tagger KB (pipeline/kb/targets.json): symbol -> group label. Optional."""
    try:
        kb = json.loads((_KB_DIR / "targets.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    labels = {g.get("id"): g.get("label") or g.get("id") for g in kb.get("groups") or []}
    out = {}
    for g in kb.get("groups") or []:
        for m in g.get("members") or []:
            out[str(m).upper()] = labels[g.get("id")]
    for t in kb.get("targets") or []:
        sym = str(t.get("symbol") or "").upper()
        if sym and sym not in out:
            grp = t.get("group")
            out[sym] = labels.get(grp) or (t.get("label") if grp == t.get("symbol") else grp) or t.get("label") or sym
    return out


@lru_cache(maxsize=1)
def _kb_diseases() -> dict[str, dict]:
    """Tagger KB (pipeline/kb/diseases.json): id -> {parent, groups, label, ancestors}. Optional."""
    try:
        kb = json.loads((_KB_DIR / "diseases.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = {d["id"]: d for d in kb.get("diseases") or [] if d.get("id")}
    out = {}
    for did, d in rows.items():
        anc, cur, seen = [], d.get("parent"), set()
        while cur and cur in rows and cur not in seen:
            anc.append(cur)
            seen.add(cur)
            cur = rows[cur].get("parent")
        out[did] = {"label": d.get("label"), "groups": list(d.get("groups") or []), "ancestors": anc,
                    "oncotree": d.get("oncotree")}
    return out


def target_group(tag: dict) -> str:
    if tag.get("displayGroup"):
        return str(tag["displayGroup"])
    cands = (_clean_id(tag.get("entityId")), str(tag.get("label") or "").strip().upper())
    if tag.get("members"):  # Tagger emits drug_target tags at group level; keep its label unless curated
        return _TARGET_GROUPS.get(cands[0]) or _TARGET_GROUPS.get(cands[1]) or str(tag.get("label") or cands[0])
    for cand in cands:
        if cand in _TARGET_GROUPS:
            return _TARGET_GROUPS[cand]
    kb = _kb_targets()
    for cand in cands:
        if cand in kb:
            return str(kb[cand])
    return str(tag.get("group") or tag.get("label") or tag.get("entityId") or "Unknown")


# ----------------------------------------------------------------------------- modality families
_MODALITY_FAMILIES = [  # order matters: first match wins
    ("ADC", r"antibody[- ]drug conjugate|\badcs?\b|immunoconjugate"),
    ("Radiopharmaceutical", r"radioligand|radiopharm|radionuclide|radioconjugate|radiolabel"),
    ("Bispecific / engager", r"bispecific|engager|\bbite\b|tri-?specific"),
    ("Cell therapy", r"car[- ]?t\b|car-?nk|\btcr\b|tcr-?t|\btil\b|cell therap|adoptive|nk cell|t-cell receptor"),
    ("Vaccine", r"vaccin"),
    ("Oncolytic virus", r"oncolytic"),
    ("Nucleic acid / gene therapy", r"gene therap|antisense|sirna|oligonucleotide|mrna|aso\b"),
    ("Degrader", r"degrader|protac|molecular glue"),
    ("Monoclonal antibody", r"monoclonal|antibod|\bmab\b|checkpoint inhibitor|fc[- ]fusion"),
    ("Protein / cytokine", r"cytokine|fusion protein|interleukin|interferon|enzyme"),
    ("Cytotoxic chemotherapy", r"chemo|cytotoxic|alkylat|anthracycl|taxane|antimetabol|platinum|topoisomerase|"
                               r"vinca|antimicrotub|trabectedin|eribulin"),
    ("Small molecule", r"small[- ]molecule|kinase inhibitor|\btki\b|inhibitor|antagonist|agonist|modulator"),
    ("Hormonal", r"hormon|endocrine|anti-?estrogen|aromatase"),
]


_MODALITY_ID_FAMILY = {  # Tagger KB modality ids (pipeline/kb/drugs.json meta.modalities)
    "small_molecule_kinase_inhibitor": "Small molecule", "small_molecule_targeted": "Small molecule",
    "chemotherapy": "Cytotoxic chemotherapy", "monoclonal_antibody": "Monoclonal antibody",
    "bispecific_antibody": "Bispecific / engager", "adc": "ADC", "cell_therapy": "Cell therapy",
    "radiopharmaceutical": "Radiopharmaceutical", "vaccine": "Vaccine", "oncolytic_virus": "Oncolytic virus",
    "fusion_protein": "Protein / cytokine", "hormonal_therapy": "Hormonal", "other": "Other",
}


def modality_family(tag: dict) -> str:
    if tag.get("family"):
        return str(tag["family"])
    if str(tag.get("entityId") or "") in _MODALITY_ID_FAMILY:
        return _MODALITY_ID_FAMILY[str(tag["entityId"])]
    text = f"{tag.get('label') or ''} {tag.get('entityId') or ''}".lower()
    for fam, pat in _MODALITY_FAMILIES:
        if re.search(pat, text):
            return fam
    return str(tag.get("label") or "Other")


# ----------------------------------------------------------------------------- disease helpers
_SARCOMA_RE = re.compile(
    r"sarcom|\bgist\b|gastrointestinal stromal|desmoid|fibromatosis|tenosynovial|\btgct\b|chordoma|ewing|"
    r"rhabdo|liposarc|leiomyosarc|osteosarc|chondrosarc|synovial|mpnst|nerve sheath|angiosarc|hemangioendo|"
    r"\behe\b|dsrct|desmoplastic small round|alveolar soft|pecoma|solitary fibrous|connective tissue|"
    r"soft tissue|bone tumou?r|giant cell tumou?r|myxofibro|fibrosarc|\bups\b|undifferentiated pleomorphic",
    re.I)
_SARCOMA_CODES = {"GIST", "OS", "ES", "CHS", "CHDM", "DES", "LIPO", "DDLS", "WDLS", "MRLS", "PLLS", "LMS", "ULMS",
                  "RMS", "ERMS", "ARMS", "SYNS", "MPNST", "ANGS", "EHE", "DSRCT", "ASPS", "CCS", "ESS", "UPS", "MFS",
                  "PECOMA", "SFT", "STS", "SARCOMA", "SOFT_TISSUE", "BONE", "EPIS", "GCTB"}

DISEASE_KINDS = ("disease",)  # histology tags (grade, morphology words) are not diseases


def is_sarcoma(tag: dict) -> bool:
    eid = str(tag.get("entityId") or "")
    kb = _kb_diseases()
    if eid == "sarcoma" or "sarcoma" in (tag.get("rollup") or []) or (eid in kb and "sarcoma" in kb[eid]["ancestors"]):
        return True
    if _clean_id(eid) in _SARCOMA_CODES:
        return True
    return bool(_SARCOMA_RE.search(f"{tag.get('label') or ''} {tag.get('entityId') or ''}"))


def _drug_links(tag: dict) -> set[str]:
    """Drug ids a target/modality tag was derived from (Tagger E: `fromDrugs`)."""
    for key in ("fromDrugs", "drugIds", "drugId", "drugEntityId", "linkedDrug"):
        v = tag.get(key)
        if isinstance(v, str) and v:
            return {v}
        if isinstance(v, list) and v:
            return {str(x) for x in v if x}
    return set()


def drug_target_tags(tl: list[dict]) -> list[dict]:
    return [t for t in tl if t.get("kind") == "target" and t.get("role") == "drug_target"]


# ----------------------------------------------------------------------------- heatmap
def build_heatmap(pres: list[dict], tix: dict[str, list[dict]]) -> dict:
    cells: dict[tuple[str, str], set] = defaultdict(set)
    for p in pres:
        tl = tix.get(p["id"], [])
        targets = drug_target_tags(tl)
        mods = [t for t in tl if t.get("kind") == "modality"]
        if not targets or not mods:
            continue
        mods_by_drug: dict[str, set] = defaultdict(set)
        for m in mods:
            for d in _drug_links(m):
                mods_by_drug[d].add(modality_family(m))
        for t in tl:  # drug tags may carry their own modality id
            if t.get("kind") == "drug" and t.get("modality") and t.get("entityId"):
                mods_by_drug[t["entityId"]].add(modality_family({"entityId": t["modality"], "label": t["modality"]}))
        all_mods = {modality_family(m) for m in mods}
        for t in targets:
            links = _drug_links(t)
            linked = set().union(*(mods_by_drug.get(d, set()) for d in links)) if links else set()
            fams = linked or all_mods
            for fam in fams:
                cells[(target_group(t), fam)].add(p["id"])
    row_ids: dict[str, set] = defaultdict(set)
    col_ids: dict[str, set] = defaultdict(set)
    for (r, c), ids in cells.items():
        row_ids[r] |= ids
        col_ids[c] |= ids
    rows = [{"key": slug(r), "label": r, "count": len(ids)} for r, ids in row_ids.items()]
    cols = [{"key": slug(c), "label": c, "count": len(ids)} for c, ids in col_ids.items()]
    rows.sort(key=lambda x: (-x["count"], x["label"]))
    cols.sort(key=lambda x: (-x["count"], x["label"]))
    out_cells = [{"row": slug(r), "col": slug(c), "count": len(ids), "presentationIds": sorted(ids)}
                 for (r, c), ids in cells.items()]
    out_cells.sort(key=lambda x: (x["row"], x["col"]))
    counts = sorted(c["count"] for c in out_cells)
    breaks = sorted({quantile(counts, q) for q in (0.2, 0.4, 0.6, 0.8)} - {None}) if counts else []
    in_grid = set().union(*cells.values()) if cells else set()
    return {
        "rows": rows, "cols": cols, "cells": out_cells,
        "scale": {"max": counts[-1] if counts else None, "p95": quantile(counts, 0.95), "breaks": breaks},
        "roleFilter": "drug_target",
        "coverage": {"presentationsInGrid": len(in_grid), "total": len(pres)},
    }


# ----------------------------------------------------------------------------- themes
TEXT_FIELDS = ["track", "sessionTitle"]
# Titles are queried only with narrow, high-precision terms (ctDNA, AI) where neither tags nor tracks carry the concept.
TITLE_FIELD = ["title"]

PAN_ONCOLOGY = [
    {"id": "adc", "label": "Antibody-drug conjugates", "parent": None,
     "query": {"any": [{"modalityFamily": ["ADC"]},
                       {"text": {"fields": TEXT_FIELDS, "pattern": r"antibody[- ]drug conjugate|\bADCs?\b"}}]}},
    {"id": "immunotherapy", "label": "Checkpoint and immunotherapy", "parent": None,
     "query": {"any": [{"targetGroup": ["PD-1/PD-L1", "CTLA-4", "LAG-3", "TIGIT", "CD47"]},
                       {"modalityFamily": ["Vaccine", "Oncolytic virus", "Protein / cytokine"]},
                       {"tag": {"kind": ["modality"], "pattern": r"checkpoint|immun"}},
                       {"text": {"fields": TEXT_FIELDS, "pattern": r"immun|checkpoint"}}]}},
    {"id": "targeted_therapy", "label": "Targeted therapy", "parent": None,
     "query": {"any": [{"modalityFamily": ["Small molecule", "Degrader"]},
                       {"text": {"fields": TEXT_FIELDS, "pattern": r"targeted therap|kinase inhibitor|precision"}}]}},
    {"id": "cell_therapy_engagers", "label": "Cell therapy and engagers", "parent": "immunotherapy",
     "query": {"any": [{"modalityFamily": ["Cell therapy", "Bispecific / engager"]},
                       {"text": {"fields": TEXT_FIELDS,
                                 "pattern": r"CAR[- ]?T|cell(ular)? therap|\bTCR|engager|bispecific|adoptive"}}]}},
    {"id": "radiotherapy_local", "label": "Radiotherapy and local therapy", "parent": None,
     "query": {"any": [{"modalityFamily": ["Radiopharmaceutical"]},
                       {"text": {"fields": TEXT_FIELDS,
                                 "pattern": r"radiother|radiation|proton|carbon[- ]ion|SBRT|brachy|ablat|"
                                            r"isolated limb|local therap|HIPEC|cryo|radiology|imaging"}}]}},
    {"id": "surgery", "label": "Surgery", "parent": "radiotherapy_local",
     "query": {"any": [{"text": {"fields": TEXT_FIELDS,
                                 "pattern": r"surg|resect|amputat|margin|limb[- ]salvage|reconstruct|orthop"}}]}},
    {"id": "ctdna_biomarkers", "label": "ctDNA and biomarkers", "parent": None,
     "query": {"any": [{"tag": {"kind": ["target"], "roles": ["selection_biomarker", "biomarker_status"]}},
                       {"text": {"fields": TEXT_FIELDS,
                                 "pattern": r"ctDNA|liquid biopsy|biomarker|genomic|sequencing|molecular|"
                                            r"circulating tumou?r"}},
                       {"text": {"fields": TITLE_FIELD,
                                 "pattern": r"\bctDNA|cell[- ]free DNA|\bcfDNA|liquid biops|circulating tumou?r"}}]}},
    {"id": "supportive_care", "label": "Supportive care and survivorship", "parent": None,
     "query": {"any": [{"text": {"fields": TEXT_FIELDS,
                                 "pattern": r"supportive|survivorship|quality of life|\bQoL\b|patient[- ]reported|"
                                            r"palliat|psychosoc|rehabil|late effect|fertility|\bpain\b|nutrition|"
                                            r"patient[- ]cent|advocacy|caregiver"}}]}},
]


def _dz(pattern: str, under: list[str] | None = None, groups: list[str] | None = None) -> dict:
    """Disease theme: KB hierarchy (if the Tagger KB is present) OR disease-tag label OR track/session text."""
    anyc: list[dict] = []
    if under or groups:
        c: dict = {}
        if under:
            c["diseaseUnder"] = under
        if groups:
            c["diseaseGroup"] = groups
        anyc.append(c)
    anyc += [{"tag": {"kind": list(DISEASE_KINDS), "pattern": pattern}},
             {"text": {"fields": TEXT_FIELDS, "pattern": pattern}}]
    return {"any": anyc}


SARCOMA = [
    {"id": "gist", "label": "GIST", "parent": None, "query": _dz(r"\bGIST|gastrointestinal stromal", ["gist"])},
    {"id": "bone_tumors", "label": "Bone tumors", "parent": None,
     "query": _dz(r"osteosarc|ewing|chondrosarc|chordoma|\bbone\b|giant cell tumou?r of bone|GCTB", ["bone_sarcoma"], ["bone"])},
    {"id": "osteosarcoma", "label": "Osteosarcoma", "parent": "bone_tumors", "query": _dz(r"osteosarc", ["osteosarcoma"])},
    {"id": "ewing", "label": "Ewing sarcoma", "parent": "bone_tumors", "query": _dz(r"ewing", ["ewing"])},
    {"id": "chondrosarcoma", "label": "Chondrosarcoma", "parent": "bone_tumors", "query": _dz(r"chondrosarc", ["chondrosarcoma"])},
    {"id": "chordoma", "label": "Chordoma", "parent": "bone_tumors", "query": _dz(r"chordoma", ["chordoma"])},
    {"id": "desmoid_tgct", "label": "Desmoid and TGCT", "parent": None,
     "query": _dz(r"desmoid|aggressive fibromatosis|tenosynovial|\bTGCT|PVNS|pigmented villonodular", ["desmoid", "tgct"])},
    {"id": "retroperitoneal", "label": "Retroperitoneal sarcoma", "parent": None,
     "query": _dz(r"retroperiton|\bRPS\b", ["rps"])},
    {"id": "pediatric_rms", "label": "Pediatric and rhabdomyosarcoma", "parent": None,
     "query": _dz(r"rhabdomyosarc|\bRMS\b|pediatric|paediatric|child|adolescent|\bAYA\b|young adult|infant",
                  ["rms", "sarcoma_pediatric"])},
    {"id": "liposarcoma", "label": "Liposarcoma", "parent": None,
     "query": _dz(r"liposarc|DDLPS|WDLPS|DDLS|WDLS|myxoid lipo|round cell lipo", ["lps"])},
    {"id": "lms_gyn", "label": "Leiomyosarcoma and gynecologic sarcomas", "parent": None,
     "query": _dz(r"leiomyosarc|\bLMS\b|uterine|gyn|endometrial stromal", ["lms", "ess", "uterine_sarcoma"])},
    {"id": "ultra_rare", "label": "Ultra-rare sarcomas", "parent": None,
     "query": _dz(r"ultra[- ]rare|alveolar soft part|\bASPS|epithelioid sarcoma|PEComa|clear cell sarcoma|"
                  r"desmoplastic small round|DSRCT|\bEHE\b|epithelioid hemangioendothelioma|solitary fibrous|"
                  r"\bSFT\b|CIC[- ]|BCOR|intimal sarcoma|myoepithelial|inflammatory myofibroblastic",
                  ["asps", "epithelioid_sarcoma", "pecoma", "clear_cell_sarcoma", "dsrct", "ehe", "sft",
                   "cic_sarcoma", "bcor_sarcoma", "ewsr1_non_ets_sarcoma", "intimal_sarcoma", "imt", "emc",
                   "lgfms", "sef", "ntrk_sarcoma", "mrt", "adamantinoma", "infantile_fibrosarcoma"])},
    {"id": "ai_digital", "label": "AI and digital", "parent": None,
     "query": {"any": [{"text": {"fields": TEXT_FIELDS,
                                 "pattern": r"artificial intelligence|\bAI\b|machine learning|deep learning|digital|"
                                            r"radiomic|computational|algorithm|natural language|\bLLM|neural network|"
                                            r"informatic|real[- ]world data"}},
                       {"text": {"fields": TITLE_FIELD,
                                 "pattern": r"artificial intelligence|machine learning|deep learning|radiomic|"
                                            r"large language model|\bLLMs?\b|neural network|computer vision"}}]}},
]


def _clause_matches(clause: dict, p: dict, tl: list[dict]) -> bool:
    if "modalityFamily" in clause:
        fams = set(clause["modalityFamily"])
        return any(t.get("kind") == "modality" and modality_family(t) in fams for t in tl)
    if "targetGroup" in clause:
        groups = set(clause["targetGroup"])
        return any(target_group(t) in groups for t in drug_target_tags(tl))
    if "diseaseUnder" in clause or "diseaseGroup" in clause:
        kb = _kb_diseases()
        under, groups = set(clause.get("diseaseUnder") or []), set(clause.get("diseaseGroup") or [])
        for t in tl:
            if t.get("kind") not in DISEASE_KINDS or t.get("polarity") == "negative":
                continue
            eid = str(t.get("entityId") or "")
            node = kb.get(eid) or {}
            ancestors = set(node.get("ancestors") or []) | set(t.get("rollup") or [])
            tgroups = set(node.get("groups") or []) | set(t.get("groups") or [])
            if eid in under or under & ancestors or groups & tgroups:
                return True
        return False
    if "tag" in clause:
        c = clause["tag"]
        rx = re.compile(c["pattern"], re.I) if c.get("pattern") else None
        for t in tl:
            if c.get("kind") and t.get("kind") not in c["kind"]:
                continue
            if c.get("roles") and t.get("role") not in c["roles"]:
                continue
            if rx and not rx.search(f"{t.get('label') or ''} {t.get('entityId') or ''}"):
                continue
            return True
        return False
    if "text" in clause:
        c = clause["text"]
        rx = re.compile(c["pattern"], re.I)
        return any(rx.search(str(p.get(f) or "")) for f in c["fields"])
    raise ValueError(f"unknown clause {clause}")


def query_matches(query: dict, p: dict, tl: list[dict]) -> bool:
    if any(_clause_matches(c, p, tl) for c in query.get("none", [])):
        return False
    return any(_clause_matches(c, p, tl) for c in query.get("any", []))


def sarcoma_share(pres: list[dict], tix: dict[str, list[dict]]) -> tuple[float | None, int, int]:
    with_dz = sarc = 0
    for p in pres:
        dz = [t for t in tix.get(p["id"], []) if t.get("kind") in DISEASE_KINDS]
        if not dz:
            continue
        with_dz += 1
        if any(is_sarcoma(t) for t in dz):
            sarc += 1
    return (round(sarc / with_dz, 4) if with_dz else None), sarc, with_dz


def _describe(query: dict) -> str:
    parts = []
    for c in query.get("any", []):
        if "diseaseUnder" in c or "diseaseGroup" in c:
            bits = []
            if c.get("diseaseUnder"):
                bits.append("disease tag at or under " + ", ".join(c["diseaseUnder"]))
            if c.get("diseaseGroup"):
                bits.append("disease tag in KB group " + ", ".join(c["diseaseGroup"]))
            parts.append(" or ".join(bits))
        elif "modalityFamily" in c:
            parts.append("modality family in " + ", ".join(c["modalityFamily"]))
        elif "targetGroup" in c:
            parts.append("drug_target group in " + ", ".join(c["targetGroup"]))
        elif "tag" in c:
            t = c["tag"]
            s = "tag kind " + "/".join(t.get("kind") or ["any"])
            if t.get("roles"):
                s += " role " + "/".join(t["roles"])
            if t.get("pattern"):
                s += f" matching /{t['pattern']}/"
            parts.append(s)
        elif "text" in c:
            parts.append(f"{'/'.join(c['text']['fields'])} matching /{c['text']['pattern']}/")
    return " OR ".join(parts)


def build_themes(pres: list[dict], tix: dict[str, list[dict]], readouts: list[dict]) -> list[dict]:
    share, sarc, with_dz = sarcoma_share(pres, tix)
    sarcoma_active = share is not None and share >= SARCOMA_ACTIVATION_SHARE
    templates = [dict(t, templateSet="pan_oncology") for t in PAN_ONCOLOGY]
    if sarcoma_active:
        templates += [dict(t, templateSet="sarcoma") for t in SARCOMA]
    ro_by_pres: dict[str, set] = defaultdict(set)
    for r in readouts:
        if r.get("presentationId"):
            ro_by_pres[r["presentationId"]].add(r["id"])

    members: dict[str, set] = {}
    for t in templates:
        members[t["id"]] = {p["id"] for p in pres if query_matches(t["query"], p, tix.get(p["id"], []))}
    by_id = {t["id"]: t for t in templates}
    folded: dict[str, list[str]] = defaultdict(list)

    def depth(tid: str) -> int:
        d, cur = 0, by_id[tid].get("parent")
        while cur:
            d, cur = d + 1, by_id[cur].get("parent")
        return d

    other: set = set()
    alive = set(by_id)
    for tid in sorted(by_id, key=lambda x: (-depth(x), x)):  # leaves first, parents see folded children
        if len(members[tid]) >= MIN_THEME_SIZE:
            continue
        parent = by_id[tid].get("parent")
        target = parent if parent in alive else None
        if target:
            members[target] |= members[tid]
            folded[target] += [tid] + folded.get(tid, [])
        else:
            other |= members[tid]
            folded["other"] += [tid] + folded.get(tid, [])
        alive.discard(tid)

    activation = {"rule": f"sarcoma share of disease-tagged presentations >= {SARCOMA_ACTIVATION_SHARE}",
                  "share": share, "sarcomaPresentations": sarc, "diseaseTaggedPresentations": with_dz,
                  "active": sarcoma_active}
    out = []
    for t in templates:
        if t["id"] not in alive:
            continue
        ids = members[t["id"]]
        defn = {"templateSet": t["templateSet"], "parent": t.get("parent"), "query": t["query"],
                "text": _describe(t["query"]), "minSize": MIN_THEME_SIZE,
                "foldedFrom": sorted(folded.get(t["id"], []))}
        if t["templateSet"] == "sarcoma":
            defn["activation"] = activation
        out.append({"id": t["id"], "label": t["label"], "definition": defn, "presentationIds": sorted(ids),
                    "readoutIds": sorted(set().union(*(ro_by_pres[i] for i in ids)) if ids else set()),
                    "count": len(ids)})
    if other:
        out.append({"id": "other", "label": "Other (folded small themes)",
                    "definition": {"templateSet": "derived", "parent": None, "query": {"any": []},
                                   "text": "Union of themes below the minimum size that have no surviving parent",
                                   "minSize": None, "foldedFrom": sorted(folded["other"])},
                    "presentationIds": sorted(other),
                    "readoutIds": sorted(set().union(*(ro_by_pres[i] for i in other))),
                    "count": len(other)})
    return out


# ----------------------------------------------------------------------------- designs / target map
def build_designs(readouts: list[dict]) -> list[dict]:
    groups: dict[tuple, list[str]] = defaultdict(list)
    for r in readouts:
        groups[(r.get("design"), r.get("phase"), r.get("readoutType"))].append(r["id"])
    rows = [{"design": d, "phase": ph, "readoutType": rt, "count": len(ids), "readoutIds": sorted(ids)}
            for (d, ph, rt), ids in groups.items()]
    rows.sort(key=lambda x: (-x["count"], str(x["design"]), str(x["phase"]), str(x["readoutType"])))
    return rows


def build_target_map(pres: list[dict], tix: dict[str, list[dict]], readouts: list[dict]) -> list[dict]:
    acc: dict[str, dict] = {}
    ro_by_pres: dict[str, set] = defaultdict(set)
    for r in readouts:
        ro_by_pres[r.get("presentationId")].add(r["id"])
    for p in pres:
        tl = tix.get(p["id"], [])
        drugs = {t.get("entityId") or t.get("label"): t for t in tl if t.get("kind") == "drug"
                 and t.get("role") != "mentioned"}
        mods = [t for t in tl if t.get("kind") == "modality"]
        for t in drug_target_tags(tl):
            key = t.get("entityId") or t.get("label")
            a = acc.setdefault(key, {"targetId": key, "label": t.get("label") or key, "group": target_group(t),
                                     "drugs": {}, "modalityFamilies": set(), "presentationIds": set(),
                                     "readoutIds": set()})
            links = _drug_links(t)
            linked = {d: drugs[d] for d in links if d in drugs} or ({} if links else drugs)
            for did, d in linked.items():
                a["drugs"][did] = d.get("label") or did
            fams = {modality_family(m) for m in mods if not links or not _drug_links(m) or links & _drug_links(m)}
            a["modalityFamilies"] |= fams
            a["presentationIds"].add(p["id"])
            a["readoutIds"] |= ro_by_pres.get(p["id"], set())
    out = []
    for a in acc.values():
        out.append({"targetId": a["targetId"], "label": a["label"], "group": a["group"],
                    "drugs": [{"entityId": k, "label": v} for k, v in sorted(a["drugs"].items())],
                    "modalityFamilies": sorted(a["modalityFamilies"]),
                    "presentationIds": sorted(a["presentationIds"]), "readoutIds": sorted(a["readoutIds"]),
                    "count": len(a["presentationIds"])})
    out.sort(key=lambda x: (-x["count"], x["label"]))
    return out


# ----------------------------------------------------------------------------- facets / overview
_COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "u.s.a.": "United States", "united states of america":
    "United States", "united states": "United States", "uk": "United Kingdom", "u.k.": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "great britain": "United Kingdom",
    "korea": "South Korea", "republic of korea": "South Korea", "korea, republic of": "South Korea",
    "korea (republic of)": "South Korea", "south korea": "South Korea", "the netherlands": "Netherlands",
    "people's republic of china": "China", "prc": "China", "pr china": "China",
    "china (people's republic)": "China", "taiwan, republic of china": "Taiwan", "republic of china": "Taiwan",
    "russian federation": "Russia", "türkiye": "Turkey", "turkiye": "Turkey", "viet nam": "Vietnam",
}


def norm_country(c: Any) -> str | None:
    s = norm_space(c).strip(" .,")
    if not s:
        return None
    return _COUNTRY_ALIASES.get(s.lower(), s)


def inst_key(name: Any) -> str | None:
    s = norm_space(name)
    if not s:
        return None
    k = re.sub(r"^the\s+", "", s.lower())
    k = k.replace("&", " and ")
    k = re.sub(r"[^\w]+", " ", k)
    return norm_space(k) or None


def _institution_rows(pres: list[dict]) -> tuple[dict[str, set], dict[str, str]]:
    """Group institution strings by a light key (case, leading 'The', punctuation); label = most common variant."""
    ids: dict[str, set] = defaultdict(set)
    variants: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in pres:
        for a in _authors(p):
            k = inst_key(a.get("institution"))
            if k:
                ids[k].add(p["id"])
                variants[k][norm_space(a.get("institution"))] += 1
    labels = {k: sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, v in variants.items()}
    return ids, labels


def _authors(p: dict) -> list[dict]:
    return [a for a in (p.get("authors") or []) if isinstance(a, dict)]


def build_facets(pres: list[dict], tix: dict[str, list[dict]]) -> dict:
    f: dict[str, dict[str, set]] = {k: defaultdict(set) for k in
                                    ("format", "track", "session", "date", "disease", "modality", "target",
                                     "country", "institution")}
    labels: dict[str, dict] = defaultdict(dict)
    for p in pres:
        pid = p["id"]
        for key, val in (("format", p.get("format")), ("track", p.get("track")), ("date", p.get("date"))):
            if val:
                f[key][val].add(pid)
        if p.get("sessionId") or p.get("sessionTitle"):
            k = p.get("sessionId") or p.get("sessionTitle")
            f["session"][k].add(pid)
            labels["session"][k] = p.get("sessionTitle") or k
        tl = tix.get(pid, [])
        for t in tl:
            if t.get("kind") in DISEASE_KINDS and t.get("polarity") not in ("negative",):
                k = t.get("entityId") or t.get("label")
                f["disease"][k].add(pid)
                labels["disease"][k] = t.get("label") or k
            elif t.get("kind") == "modality":
                f["modality"][modality_family(t)].add(pid)
        for t in drug_target_tags(tl):
            f["target"][target_group(t)].add(pid)
        for a in _authors(p):
            if norm_country(a.get("country")):
                f["country"][norm_country(a.get("country"))].add(pid)
    inst_ids, inst_labels = _institution_rows(pres)
    f["institution"] = inst_ids
    labels["institution"] = inst_labels
    out = {}
    for key, counts in f.items():
        rows = counter_rows(counts, "value", label_map=labels.get(key, {}))
        if key == "date":
            rows.sort(key=lambda r: r["value"])
        out[key] = rows
    return out


_SESSION_GROUPS = [
    ("Poster sessions", r"poster"),
    ("Plenary, award and named lectures", r"plenary|presidential|keynote|lecture|award|of the year|honou?r"),
    ("Industry and satellite symposia", r"industry|sponsor|satellite|symposi|supported by"),
    ("Networking, meals and breaks", r"network|reception|lunch|break|breakfast|dinner|coffee|welcome|social|gala"),
    ("Business and logistics", r"registration|opening|closing|business|meeting|committee|board|orientation"),
    ("Workshops and education", r"workshop|educat|tutorial|course|masterclass|career|mentor"),
    ("Scientific sessions", r"^\s*(session|paper|abstract|oral|scientific)\b|session \d+"),
]


def session_group(title: str) -> str:
    for label, pat in _SESSION_GROUPS:
        if re.search(pat, title or "", re.I):
            return label
    return "Other sessions"


def build_overview(parsed: dict, pres: list[dict], tix: dict[str, list[dict]]) -> dict:
    talks: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in pres:
        if p.get("format") in ("oral", "lecture") and p.get("date"):
            talks[p["date"]][p["format"]] += 1
    talks_per_day = [{"date": d, "count": sum(v.values()), "byFormat": dict(sorted(v.items()))}
                     for d, v in sorted(talks.items())]
    ppt: dict[str, set] = defaultdict(set)
    for p in pres:
        if p.get("format") == "poster":
            ppt[p.get("track") or "(no track)"].add(p["id"])
    posters_per_track = counter_rows(ppt, "track")
    groups: dict[str, list[str]] = defaultdict(list)
    for s in (parsed.get("sessions") or []):
        groups[session_group(s.get("title") or "")].append(s["id"])
    session_groups = [{"group": g, "count": len(ids), "sessionIds": sorted(ids)} for g, ids in groups.items()]
    session_groups.sort(key=lambda x: (-x["count"], x["group"]))
    dz: dict[str, set] = defaultdict(set)
    dz_label: dict[str, str] = {}
    for p in pres:
        for t in tix.get(p["id"], []):
            if t.get("kind") in DISEASE_KINDS and t.get("polarity") != "negative":
                k = t.get("entityId") or t.get("label")
                dz[k].add(p["id"])
                dz_label.setdefault(k, t.get("label") or k)
    disease_counts = [{"entityId": k, "label": dz_label[k], "count": len(v), "sarcoma": is_sarcoma(
        {"entityId": k, "label": dz_label[k]})} for k, v in dz.items()]
    disease_counts.sort(key=lambda x: (-x["count"], x["label"]))
    inst, inst_labels = _institution_rows(pres)
    ctry: dict[str, set] = defaultdict(set)
    for p in pres:
        for a in _authors(p):
            if norm_country(a.get("country")):
                ctry[norm_country(a.get("country"))].add(p["id"])
    return {
        "talksPerDay": talks_per_day,
        "postersPerTrack": posters_per_track,
        "sessionGroups": session_groups,
        "diseaseCounts": disease_counts,
        "topInstitutions": [{"name": inst_labels[r["value"]], "count": r["count"]}
                            for r in counter_rows(inst, limit=TOP_N)],
        "topCountries": [{"name": r["value"], "count": r["count"]} for r in counter_rows(ctry, limit=TOP_N)],
    }


# ----------------------------------------------------------------------------- watchlist / updates
def build_watchlist(pres: list[dict], tix: dict[str, list[dict]], readouts: list[dict]) -> list[dict]:
    """Investigational drugs with a structured readout, or in >= 2 presentations."""
    by_drug: dict[str, dict] = {}
    for p in pres:
        for t in tix.get(p["id"], []):
            if t.get("kind") == "drug" and t.get("role") == "investigational" and t.get("entityId"):
                d = by_drug.setdefault(t["entityId"], {"label": t.get("label") or t["entityId"], "p": set(),
                                                       "r": set()})
                d["p"].add(p["id"])
    pres_drugs: dict[str, set] = defaultdict(set)
    for p in pres:
        for t in tix.get(p["id"], []):
            if t.get("kind") == "drug" and t.get("entityId") in by_drug:
                pres_drugs[p["id"]].add(t["entityId"])
    for r in readouts:
        for did in pres_drugs.get(r.get("presentationId"), set()):
            by_drug[did]["r"].add(r["id"])
    out = []
    for did, d in by_drug.items():
        reasons = []
        if d["r"]:
            reasons.append(f"{len(d['r'])} structured readout(s)")
        if len(d["p"]) >= 2:
            reasons.append(f"investigational in {len(d['p'])} presentations")
        if not reasons:
            continue
        out.append({"id": f"watch:drug:{did}", "kind": "drug", "entityId": did, "label": d["label"],
                    "reason": "; ".join(reasons), "presentationIds": sorted(d["p"]), "readoutIds": sorted(d["r"])})
    out.sort(key=lambda x: (-len(x["readoutIds"]), -len(x["presentationIds"]), x["label"]))
    return out


def _fmt_num(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def build_context_updates(pres: list[dict], readouts: list[dict]) -> list[dict]:
    """One machine-written line per readout, built only from extracted fields (no free text invented)."""
    pmap = {p["id"]: p for p in pres}
    out = []
    for r in readouts:
        p = pmap.get(r.get("presentationId"), {})
        bits = []
        for m in r.get("metrics") or []:
            vals = [f"{a.get('label') or a.get('role') or 'arm'} {_fmt_num(a['value'])}{m.get('unit') or ''}"
                    for a in m.get("arms") or [] if a.get("value") is not None]
            hr = m.get("hr") or {}
            if hr.get("value") is not None:
                vals.append(f"HR {_fmt_num(hr['value'])}")
            if vals:
                bits.append(f"{m.get('label') or m.get('key')}: " + ", ".join(vals))
        head = " ".join(x for x in [r.get("drug"), "in" if r.get("drug") and r.get("disease") else None,
                                    r.get("disease")] if x)
        n = f" (n={r['studyN']})" if r.get("studyN") is not None else ""
        summary = (head or r.get("code") or r["id"]) + n + (": " + "; ".join(bits[:3]) if bits else "")
        out.append({"id": f"update:{r['id']}", "kind": "readout", "entityId": r.get("drug"), "summary": summary,
                    "presentationId": r.get("presentationId"), "readoutId": r["id"], "date": p.get("date"),
                    "reviewStatus": (r.get("review") or {}).get("status")})
    out.sort(key=lambda x: (str(x["date"]), x["id"]))
    return out


# ----------------------------------------------------------------------------- entry points
def compute(parsed: dict, tags: Any, readouts: Any) -> dict:
    pres = sorted(presentations_of(parsed), key=lambda p: p["id"])
    tix = tags_index(tags)
    ros = sorted((normalize_readout(r, i) for i, r in enumerate(readouts_list(readouts))), key=lambda r: r["id"])
    return {
        "aggregates": {
            "heatmap": build_heatmap(pres, tix),
            "designs": build_designs(ros),
            "themes": build_themes(pres, tix, ros),
            "targetMap": build_target_map(pres, tix, ros),
            "facets": build_facets(pres, tix),
            "overview": build_overview(parsed or {}, pres, tix),
        },
        "watchlist": build_watchlist(pres, tix, ros),
        "contextUpdates": build_context_updates(pres, ros),
    }


def run(parsed_path, tags_path, readouts_path, out_path) -> dict:
    parsed = load_json(parsed_path, {}) or {}
    tags = load_json(tags_path, {}) or {}
    readouts = load_json(readouts_path, []) or []
    result = compute(parsed, tags, readouts)
    dump_json(result, out_path)
    return result


if __name__ == "__main__":  # pragma: no cover
    import sys
    from pipeline.assemble_common import DEFAULT_PATHS as D
    run(*(sys.argv[1:5] or [D["parsed"], D["tags"], D["readouts"], D["aggregates"]]))
