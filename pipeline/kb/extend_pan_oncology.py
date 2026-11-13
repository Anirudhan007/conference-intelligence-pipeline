"""Add a curated pan-oncology + haematology disease layer to diseases.json (idempotent).

The original dictionary is sarcoma-only (CTOS). Pan-tumour meetings (ASCO, AACR, ESMO, ASH, WCLC, SABCS,
ASTRO, SITC, SNO) need the major solid and haematological diseases. Entries follow the existing format:
names are matched case-insensitively with word boundaries; abbreviations are case-sensitive and
``safe=False`` ones are only accepted when the text defines them next to a long form.
OncoTree codes are given only where confident, otherwise null. Source: curated.

Run: python -m pipeline.kb.extend_pan_oncology
"""
from __future__ import annotations

import json
from pathlib import Path

KB = Path(__file__).with_name("diseases.json")

GROUPS = {
    "solid": "Solid tumours (unspecified)", "thoracic": "Thoracic", "breast": "Breast", "gi": "Gastrointestinal",
    "gu": "Genitourinary", "gyn": "Gynaecological", "head_neck": "Head and neck", "skin": "Skin",
    "cns": "Central nervous system", "pediatric_solid": "Paediatric solid (non-sarcoma)",
    "heme": "Haematological malignancy", "benign_heme": "Non-malignant haematology",
}

# id, label, oncotree, parent, group, kind, names, [(abbr, safe)]
S, U = True, False
SPEC = [
    ("solid_tumor", "Solid tumors (unspecified)", None, None, "solid", "bucket",
     ["solid tumor", "solid tumour", "advanced solid tumor", "advanced solid tumour", "solid malignancy",
      "solid malignancies", "solid cancer"], []),
    # thoracic
    ("lung_cancer", "Lung cancer", None, None, "thoracic", "organ",
     ["lung cancer", "lung carcinoma", "lung tumor", "lung tumour", "thoracic cancer"], []),
    ("nsclc", "Non-small cell lung cancer", "NSCLC", "lung_cancer", "thoracic", "histology",
     ["non-small cell lung cancer", "non-small-cell lung cancer", "non small cell lung cancer",
      "non-small cell lung carcinoma", "non-small-cell lung carcinoma"], [("NSCLC", S), ("mNSCLC", S), ("aNSCLC", S)]),
    ("luad", "Lung adenocarcinoma", "LUAD", "nsclc", "thoracic", "histology",
     ["lung adenocarcinoma", "adenocarcinoma of the lung", "pulmonary adenocarcinoma"], [("LUAD", S)]),
    ("lusc", "Lung squamous cell carcinoma", "LUSC", "nsclc", "thoracic", "histology",
     ["lung squamous cell carcinoma", "squamous cell lung cancer", "squamous non-small cell lung cancer",
      "squamous NSCLC", "squamous cell carcinoma of the lung"], [("LUSC", S)]),
    ("sclc", "Small cell lung cancer", "SCLC", "lung_cancer", "thoracic", "histology",
     ["small cell lung cancer", "small-cell lung cancer", "small cell lung carcinoma",
      "extensive-stage small cell lung cancer", "limited-stage small cell lung cancer"],
     [("SCLC", S), ("ES-SCLC", S), ("LS-SCLC", S)]),
    ("mesothelioma", "Mesothelioma", "MESO", None, "thoracic", "histology",
     ["mesothelioma", "pleural mesothelioma", "malignant pleural mesothelioma", "peritoneal mesothelioma"],
     [("MPM", U)]),
    ("thymic", "Thymic epithelial tumor", None, None, "thoracic", "histology",
     ["thymoma", "thymic carcinoma", "thymic epithelial tumor", "thymic epithelial tumour"], []),
    # breast
    ("breast_cancer", "Breast cancer", "BREAST", None, "breast", "organ",
     ["breast cancer", "breast carcinoma", "mammary carcinoma", "metastatic breast cancer", "early breast cancer",
      "advanced breast cancer"], [("mBC", S), ("eBC", S), ("BC", U)]),
    ("tnbc", "Triple-negative breast cancer", None, "breast_cancer", "breast", "subtype",
     ["triple-negative breast cancer", "triple negative breast cancer", "triple-negative breast carcinoma"],
     [("TNBC", S), ("mTNBC", S)]),
    ("idc", "Invasive ductal carcinoma (breast)", "IDC", "breast_cancer", "breast", "histology",
     ["invasive ductal carcinoma", "invasive breast carcinoma of no special type"], [("IDC", U)]),
    ("ilc", "Invasive lobular carcinoma (breast)", "ILC", "breast_cancer", "breast", "histology",
     ["invasive lobular carcinoma", "lobular breast cancer"], [("ILC", U)]),
    ("dcis", "Ductal carcinoma in situ", "DCIS", "breast_cancer", "breast", "histology",
     ["ductal carcinoma in situ"], [("DCIS", S)]),
    # gastrointestinal
    ("crc", "Colorectal cancer", "COADREAD", None, "gi", "organ",
     ["colorectal cancer", "colorectal carcinoma", "colorectal adenocarcinoma", "colon cancer", "rectal cancer",
      "colon adenocarcinoma", "rectal adenocarcinoma", "bowel cancer"], [("CRC", S), ("mCRC", S), ("LARC", S)]),
    ("gastric", "Gastric cancer", "STOMACH", None, "gi", "organ",
     ["gastric cancer", "gastric carcinoma", "gastric adenocarcinoma", "stomach cancer"], [("GC", U)]),
    ("gej", "Gastroesophageal junction adenocarcinoma", None, None, "gi", "organ",
     ["gastroesophageal junction adenocarcinoma", "gastroesophageal junction cancer",
      "gastro-oesophageal junction adenocarcinoma", "gastro-oesophageal junction cancer",
      "gastroesophageal adenocarcinoma", "gastroesophageal cancer"], [("GEJ", U), ("GEA", U)]),
    ("esophageal", "Esophageal cancer", "ESOPH", None, "gi", "organ",
     ["esophageal cancer", "oesophageal cancer", "esophageal carcinoma", "oesophageal carcinoma"], []),
    ("escc", "Esophageal squamous cell carcinoma", "ESCC", "esophageal", "gi", "histology",
     ["esophageal squamous cell carcinoma", "oesophageal squamous cell carcinoma"], [("ESCC", S)]),
    ("pancreatic", "Pancreatic cancer", "PANCREAS", None, "gi", "organ",
     ["pancreatic cancer", "pancreatic carcinoma", "pancreatic adenocarcinoma", "pancreatic ductal adenocarcinoma",
      "metastatic pancreatic cancer"], [("PDAC", S), ("mPDAC", S)]),
    ("hcc", "Hepatocellular carcinoma", "HCC", None, "gi", "histology",
     ["hepatocellular carcinoma", "liver cancer", "hepatocellular cancer"], [("HCC", S), ("uHCC", S)]),
    ("btc", "Biliary tract cancer", None, None, "gi", "organ",
     ["biliary tract cancer", "biliary tract cancers", "biliary cancer", "cholangiocarcinoma",
      "intrahepatic cholangiocarcinoma", "extrahepatic cholangiocarcinoma", "gallbladder cancer",
      "gallbladder carcinoma"], [("BTC", U), ("iCCA", S), ("CCA", U)]),
    ("anal", "Anal cancer", None, None, "gi", "organ",
     ["anal cancer", "anal carcinoma", "anal squamous cell carcinoma", "squamous cell carcinoma of the anal canal"], []),
    ("net", "Neuroendocrine neoplasm", None, None, "gi", "histology",
     ["neuroendocrine tumor", "neuroendocrine tumour", "neuroendocrine neoplasm", "neuroendocrine carcinoma",
      "gastroenteropancreatic neuroendocrine tumor", "pancreatic neuroendocrine tumor"],
     [("GEP-NET", S), ("GEP-NETs", S), ("pNET", S), ("NEC", U), ("NET", U)]),
    # genitourinary
    ("prostate", "Prostate cancer", "PROSTATE", None, "gu", "organ",
     ["prostate cancer", "prostate adenocarcinoma", "prostatic adenocarcinoma", "prostate carcinoma",
      "metastatic hormone-sensitive prostate cancer", "metastatic castration-sensitive prostate cancer"],
     [("mHSPC", S), ("mCSPC", S), ("PCa", S)]),
    ("crpc", "Castration-resistant prostate cancer", None, "prostate", "gu", "subtype",
     ["castration-resistant prostate cancer", "castration resistant prostate cancer",
      "metastatic castration-resistant prostate cancer"], [("CRPC", S), ("mCRPC", S), ("nmCRPC", S)]),
    ("urothelial", "Urothelial carcinoma", "BLADDER", None, "gu", "organ",
     ["urothelial carcinoma", "urothelial cancer", "bladder cancer", "bladder carcinoma",
      "muscle-invasive bladder cancer", "non-muscle-invasive bladder cancer", "upper tract urothelial carcinoma"],
     [("mUC", S), ("la/mUC", S), ("MIBC", S), ("NMIBC", S), ("UTUC", S), ("UC", U)]),
    ("rcc", "Renal cell carcinoma", "RCC", None, "gu", "histology",
     ["renal cell carcinoma", "kidney cancer", "renal cancer", "renal cell cancer"], [("RCC", S), ("mRCC", S), ("aRCC", S)]),
    ("ccrcc", "Clear cell renal cell carcinoma", "CCRCC", "rcc", "gu", "histology",
     ["clear cell renal cell carcinoma", "clear-cell renal cell carcinoma"], [("ccRCC", S)]),
    ("gct_testis", "Testicular germ cell tumor", None, None, "gu", "histology",
     ["testicular cancer", "testicular germ cell tumor", "testicular germ cell tumour", "germ cell tumor",
      "germ cell tumour"], [("TGCT", U)]),
    # gynaecological
    ("ovarian", "Ovarian cancer", "OVARY", None, "gyn", "organ",
     ["ovarian cancer", "ovarian carcinoma", "epithelial ovarian cancer", "high-grade serous ovarian cancer",
      "high-grade serous ovarian carcinoma", "platinum-resistant ovarian cancer"],
     [("HGSOC", S), ("PROC", S), ("EOC", U), ("OC", U)]),
    ("endometrial", "Endometrial cancer", "UTERUS", None, "gyn", "organ",
     ["endometrial cancer", "endometrial carcinoma", "endometrial adenocarcinoma"], [("EC", U)]),
    ("cervical", "Cervical cancer", "CERVIX", None, "gyn", "organ",
     ["cervical cancer", "cervical carcinoma", "carcinoma of the cervix", "cancer of the cervix"], []),
    # head and neck
    ("hnscc", "Head and neck squamous cell carcinoma", "HNSC", None, "head_neck", "histology",
     ["head and neck squamous cell carcinoma", "squamous cell carcinoma of the head and neck", "head and neck cancer",
      "head and neck cancers", "oropharyngeal cancer", "oropharyngeal squamous cell carcinoma", "oral cavity cancer",
      "laryngeal cancer", "oral squamous cell carcinoma"], [("HNSCC", S), ("SCCHN", S), ("OPSCC", S), ("OSCC", U), ("HNC", U)]),
    ("npc", "Nasopharyngeal carcinoma", "NPC", None, "head_neck", "histology",
     ["nasopharyngeal carcinoma", "nasopharyngeal cancer"], [("NPC", S)]),
    ("thyroid", "Thyroid cancer", "THYROID", None, "head_neck", "organ",
     ["thyroid cancer", "thyroid carcinoma", "papillary thyroid carcinoma", "papillary thyroid cancer",
      "anaplastic thyroid carcinoma", "anaplastic thyroid cancer", "medullary thyroid carcinoma",
      "differentiated thyroid cancer"], [("PTC", U), ("ATC", U), ("MTC", U), ("DTC", U)]),
    # skin
    ("melanoma", "Melanoma", "MEL", None, "skin", "histology",
     ["melanoma", "cutaneous melanoma", "metastatic melanoma", "advanced melanoma", "acral melanoma",
      "mucosal melanoma"], []),
    ("uveal_melanoma", "Uveal melanoma", "UM", "melanoma", "skin", "histology",
     ["uveal melanoma", "ocular melanoma"], [("UM", U), ("mUM", S)]),
    ("cscc", "Cutaneous squamous cell carcinoma", "CSCC", None, "skin", "histology",
     ["cutaneous squamous cell carcinoma"], [("cSCC", S), ("CSCC", S)]),
    ("merkel", "Merkel cell carcinoma", "MCC", None, "skin", "histology",
     ["merkel cell carcinoma"], [("MCC", U)]),
    ("bcc", "Basal cell carcinoma", "BCC", None, "skin", "histology",
     ["basal cell carcinoma"], [("BCC", U)]),
    # central nervous system
    ("glioma", "Glioma", "GLIOMA", None, "cns", "histology",
     ["glioma", "diffuse glioma", "low-grade glioma", "high-grade glioma", "astrocytoma", "oligodendroglioma",
      "IDH-mutant glioma"], [("LGG", U), ("HGG", U)]),
    ("gbm", "Glioblastoma", "GB", "glioma", "cns", "histology",
     ["glioblastoma", "glioblastoma multiforme", "glioblastoma, IDH-wildtype"], [("GBM", S), ("rGBM", S), ("ndGBM", S)]),
    ("dmg", "Diffuse midline glioma", "DIPG", "glioma", "cns", "histology",
     ["diffuse midline glioma", "diffuse intrinsic pontine glioma"], [("DIPG", S), ("DMG", U)]),
    ("meningioma", "Meningioma", "MNG", None, "cns", "histology", ["meningioma"], []),
    ("medulloblastoma", "Medulloblastoma", "MBL", None, "cns", "histology", ["medulloblastoma"], []),
    ("ependymoma", "Ependymoma", "EPM", None, "cns", "histology", ["ependymoma"], []),
    ("pcnsl", "Primary CNS lymphoma", "PCNSL", None, "cns", "histology",
     ["primary central nervous system lymphoma", "primary CNS lymphoma"], [("PCNSL", S)]),
    ("brain_metastases", "Brain metastases", None, None, "cns", "site",
     ["brain metastases", "brain metastasis", "intracranial metastases", "leptomeningeal metastases",
      "leptomeningeal disease"], []),
    # paediatric solid (non-sarcoma)
    ("neuroblastoma", "Neuroblastoma", "NBL", None, "pediatric_solid", "histology", ["neuroblastoma"], []),
    ("wilms", "Wilms tumor", "WT", None, "pediatric_solid", "histology",
     ["Wilms tumor", "Wilms tumour", "Wilms' tumor", "nephroblastoma"], []),
    ("retinoblastoma", "Retinoblastoma", "RBL", None, "pediatric_solid", "histology", ["retinoblastoma"], []),
    # haematological malignancies
    ("heme_malignancy", "Hematologic malignancy (unspecified)", None, None, "heme", "bucket",
     ["hematologic malignancy", "hematologic malignancies", "hematological malignancy", "hematological malignancies",
      "haematological malignancy", "haematological malignancies", "blood cancer"], []),
    ("leukemia", "Leukemia (unspecified)", None, "heme_malignancy", "heme", "bucket",
     ["leukemia", "leukaemia", "acute leukemia", "acute leukaemia"], []),
    ("aml", "Acute myeloid leukemia", "AML", "leukemia", "heme", "histology",
     ["acute myeloid leukemia", "acute myeloid leukaemia", "acute myelogenous leukemia",
      "acute myelogenous leukaemia"], [("AML", S), ("R/R AML", S), ("ND AML", S)]),
    ("apl", "Acute promyelocytic leukemia", "APL", "aml", "heme", "histology",
     ["acute promyelocytic leukemia", "acute promyelocytic leukaemia"], [("APL", U)]),
    ("all", "Acute lymphoblastic leukemia", "ALL", "leukemia", "heme", "histology",
     ["acute lymphoblastic leukemia", "acute lymphoblastic leukaemia", "acute lymphocytic leukemia",
      "B-cell acute lymphoblastic leukemia", "T-cell acute lymphoblastic leukemia"],
     [("B-ALL", S), ("T-ALL", S), ("Ph+ ALL", S), ("ALL", U)]),
    ("cll", "Chronic lymphocytic leukemia", "CLL", "leukemia", "heme", "histology",
     ["chronic lymphocytic leukemia", "chronic lymphocytic leukaemia", "small lymphocytic lymphoma"],
     [("CLL", S), ("SLL", U), ("CLL/SLL", S)]),
    ("cml", "Chronic myeloid leukemia", "CML", "leukemia", "heme", "histology",
     ["chronic myeloid leukemia", "chronic myeloid leukaemia", "chronic myelogenous leukemia"], [("CML", S), ("CML-CP", S)]),
    ("mds", "Myelodysplastic syndromes", "MDS", "heme_malignancy", "heme", "histology",
     ["myelodysplastic syndrome", "myelodysplastic syndromes", "myelodysplastic neoplasm", "myelodysplastic neoplasms"],
     [("MDS", S), ("HR-MDS", S), ("LR-MDS", S), ("CMML", S)]),
    ("mpn", "Myeloproliferative neoplasm", "MPN", "heme_malignancy", "heme", "histology",
     ["myeloproliferative neoplasm", "myeloproliferative neoplasms"], [("MPN", S), ("MPNs", S)]),
    ("myelofibrosis", "Myelofibrosis", "PMF", "mpn", "heme", "histology",
     ["myelofibrosis", "primary myelofibrosis"], [("PMF", U)]),
    ("pv", "Polycythemia vera", "PV", "mpn", "heme", "histology",
     ["polycythemia vera", "polycythaemia vera"], [("PV", U)]),
    ("et", "Essential thrombocythemia", "ET", "mpn", "heme", "histology",
     ["essential thrombocythemia", "essential thrombocythaemia"], [("ET", U)]),
    ("mm", "Multiple myeloma", "PCM", "heme_malignancy", "heme", "histology",
     ["multiple myeloma", "plasma cell myeloma", "relapsed/refractory multiple myeloma",
      "newly diagnosed multiple myeloma", "smoldering multiple myeloma", "smouldering myeloma", "myeloma"],
     [("RRMM", S), ("NDMM", S), ("SMM", U), ("MM", U)]),
    ("al_amyloidosis", "AL amyloidosis", None, "heme_malignancy", "heme", "histology",
     ["AL amyloidosis", "light chain amyloidosis", "light-chain amyloidosis"], []),
    ("lymphoma", "Lymphoma (unspecified)", None, "heme_malignancy", "heme", "bucket",
     ["lymphoma", "non-Hodgkin lymphoma", "non-Hodgkin's lymphoma", "B-cell lymphoma", "B-cell non-Hodgkin lymphoma"],
     [("NHL", S), ("B-NHL", S)]),
    ("dlbcl", "Diffuse large B-cell lymphoma", "DLBCLNOS", "lymphoma", "heme", "histology",
     ["diffuse large B-cell lymphoma", "diffuse large B cell lymphoma", "large B-cell lymphoma",
      "large B cell lymphoma"], [("DLBCL", S), ("LBCL", S), ("R/R DLBCL", S)]),
    ("fl", "Follicular lymphoma", "FL", "lymphoma", "heme", "histology", ["follicular lymphoma"], [("FL", U)]),
    ("mcl", "Mantle cell lymphoma", "MCL", "lymphoma", "heme", "histology", ["mantle cell lymphoma"], [("MCL", U)]),
    ("mzl", "Marginal zone lymphoma", "MZL", "lymphoma", "heme", "histology",
     ["marginal zone lymphoma", "MALT lymphoma"], [("MZL", S)]),
    ("hodgkin", "Hodgkin lymphoma", "CHL", "lymphoma", "heme", "histology",
     ["Hodgkin lymphoma", "Hodgkin's lymphoma", "Hodgkin disease", "classical Hodgkin lymphoma",
      "classic Hodgkin lymphoma"], [("cHL", S), ("HL", U)]),
    ("tcl", "T-cell lymphoma", None, "lymphoma", "heme", "histology",
     ["T-cell lymphoma", "peripheral T-cell lymphoma", "cutaneous T-cell lymphoma", "mycosis fungoides",
      "anaplastic large cell lymphoma"], [("PTCL", S), ("CTCL", S), ("ALCL", S)]),
    ("waldenstrom", "Waldenstrom macroglobulinemia", "WM", "lymphoma", "heme", "histology",
     ["Waldenström macroglobulinemia", "Waldenstrom macroglobulinemia", "Waldenström's macroglobulinemia",
      "lymphoplasmacytic lymphoma"], [("WM", U)]),
    # non-malignant haematology (ASH, EHA)
    ("sickle_cell", "Sickle cell disease", None, None, "benign_heme", "condition",
     ["sickle cell disease", "sickle cell anemia", "sickle cell anaemia"], [("SCD", U)]),
    ("thalassemia", "Thalassemia", None, None, "benign_heme", "condition",
     ["thalassemia", "thalassaemia", "beta-thalassemia", "β-thalassemia", "transfusion-dependent thalassemia"], [("TDT", U)]),
    ("hemophilia", "Hemophilia", None, None, "benign_heme", "condition",
     ["hemophilia", "haemophilia", "hemophilia A", "hemophilia B", "haemophilia A", "haemophilia B"], []),
    ("itp", "Immune thrombocytopenia", None, None, "benign_heme", "condition",
     ["immune thrombocytopenia", "immune thrombocytopenic purpura", "idiopathic thrombocytopenic purpura"], [("ITP", U)]),
    ("aplastic_anemia", "Aplastic anemia", None, None, "benign_heme", "condition",
     ["aplastic anemia", "aplastic anaemia", "severe aplastic anemia"], [("SAA", U)]),
    ("pnh", "Paroxysmal nocturnal hemoglobinuria", None, None, "benign_heme", "condition",
     ["paroxysmal nocturnal hemoglobinuria", "paroxysmal nocturnal haemoglobinuria"], [("PNH", S)]),
    ("vte", "Venous thromboembolism", None, None, "benign_heme", "condition",
     ["venous thromboembolism", "deep vein thrombosis", "pulmonary embolism"], [("VTE", S), ("DVT", S)]),
    ("gvhd", "Graft-versus-host disease", None, None, "benign_heme", "condition",
     ["graft-versus-host disease", "graft versus host disease", "chronic graft-versus-host disease",
      "acute graft-versus-host disease"], [("GVHD", S), ("cGVHD", S), ("aGVHD", S)]),
]


def entries() -> list[dict]:
    out = []
    for did, label, onco, parent, group, kind, names, abbrs in SPEC:
        out.append({"id": did, "label": label, "oncotree": onco, "parent": parent, "group": group,
                    "groups": [group], "kind": kind, "names": names,
                    "abbreviations": [{"abbr": a, "safe": s} for a, s in abbrs], "source": "curated",
                    "layer": "pan_oncology"})
    return out


def main() -> None:
    data = json.loads(KB.read_text(encoding="utf-8"))
    base = [d for d in data["diseases"] if d.get("layer") != "pan_oncology"]
    ids = {d["id"] for d in base}
    new = entries()
    clash = ids & {d["id"] for d in new}
    if clash:
        raise SystemExit(f"id clash with sarcoma layer: {sorted(clash)}")
    known = ids | {d["id"] for d in new}
    for d in new:
        if d["parent"] and d["parent"] not in known:
            raise SystemExit(f"{d['id']}: unknown parent {d['parent']}")
    data["diseases"] = base + new
    data["meta"]["groups"].update(GROUPS)
    data["meta"]["notes"] = data["meta"]["notes"].replace("Sarcoma-focused taxonomy.", "Sarcoma taxonomy plus a "
                                                          "pan-oncology/haematology layer (layer=pan_oncology).")
    KB.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"diseases: {len(base)} sarcoma + {len(new)} pan-oncology = {len(data['diseases'])}")


if __name__ == "__main__":
    main()
