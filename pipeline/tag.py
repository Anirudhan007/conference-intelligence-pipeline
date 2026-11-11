"""Tagger for the CTOS 2026 conference intelligence pipeline (Agent Tagger).

Two methods behind a flag:

  --method K   Baseline that replicates Conference Atlas: case-insensitive literal
               keyword (substring) matching on titles only, closed vocabulary of
               30 targets, modality from any keyword in the title.
  --method E   Evidence-based tagger: title + abstract + track (+ session title for
               diseases), dictionary matching with word boundaries and synonym
               normalisation, modality and drug_target derived only from matched
               drugs, biomarker polarity/role (ConText-style), per-component
               tagging of combinations, disease tagging at the deepest taxonomy
               node with roll-up, companies from drug owners (dated lineage) and
               explicit mentions, registry ids, histology, unknown drug detector.

Every tag carries evidence spans whose quote is an exact substring of the field.

Usage:
  python -m pipeline.tag --method E --in data/stage/parsed.json --out data/stage/tags.json
  python -m pipeline.tag --method K --in data/stage/parsed.json --out data/stage/tags_keyword.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb")
REFERENCE_DATE = "2026-11-11"

HYPHENS = "-‐‑‒–—"
HCLS = r"\-" + HYPHENS[1:]               # same set, safe inside a regex character class
SEP = r"[\s" + HCLS + r"]?"              # flexible separator between tokens
LB = r"(?<![A-Za-z0-9])"                   # left boundary
RB = r"(?![A-Za-z0-9])"                    # right boundary
FIELDS_E = ("title", "abstract", "track")
FIELD_ORDER = {"title": 0, "abstract": 1, "track": 2, "sessionTitle": 3}


# --------------------------------------------------------------------------- utils
def _load(name: str) -> dict:
    with open(os.path.join(KB_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def norm_key(s: str, lower: bool = True) -> str:
    s = re.sub(r"[\s" + HCLS + r"'’]", "", s)
    return s.lower() if lower else s


def term_regex(term: str) -> str:
    """Regex for a dictionary term: escaped, flexible hyphen/space between tokens."""
    out = []
    for ch in term:
        if ch.isspace() or ch in HYPHENS:
            if out and out[-1] == SEP:
                continue
            out.append(SEP)
        elif ch in "'’":
            out.append("['’]?")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def is_all_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) > 0.9


_CONTRAST = re.compile(r"(?i)\b(unlike|rather than|instead of|in contrast to|compared with|future|may|might|could|"
                       r"potential(?:ly)?|warrant|explore|prior|previous(?:ly)?|history of|after failure of|refractory to)\b")


def _contrast_or_speculative(text: str, start: int) -> bool:
    """True when the words just before a class mention mark it as contrast, speculation or prior therapy."""
    return bool(_CONTRAST.search(text[max(0, start - 60): start]))


_AE_CONTEXT = re.compile(r"(?i)advers|toxicit|\bAEs?\b|\bTRAEs?\b|\bTEAEs?\b|CTCAE|reaction|events?\b|complication|"
                         r"Clavien|symptom|neutropeni|anemi|anaemi|thrombocytop|nause|fatigue|diarrh|mucositis|"
                         r"dermatitis|pneumonitis|infection|pain|toxic|side effect|glioma|lymphoma|carcinoma")
_TUMOUR_GRADE = re.compile(r"(?i)FNCLCC|tumou?r grade|histolog|sarcoma|differentiat|grade (?:1|2|3|I{1,3}) (?:tumou?r|sarcoma|disease)")


def _grade_is_not_tumour(text: str, m) -> bool:
    """True when a grade mention describes an adverse event or another cancer, not the sarcoma's histologic grade."""
    tok = m.group(0).lower()
    win = text[max(0, m.start() - 70): m.end() + 70]
    if tok.startswith("grade"):  # bare "grade 1/2/3": needs tumour context and no AE context
        return bool(_AE_CONTEXT.search(win)) or not _TUMOUR_GRADE.search(win)
    after = text[m.end(): m.end() + 30]
    return bool(re.match(r"(?i)\s*(glioma|lymphoma|carcinoma|dysplasia|neoplasia)", after)) or bool(
        re.search(r"(?i)(advers|toxicit|CTCAE|Clavien|complication)", win))



@dataclass

class Match:
    kind: str           # disease|drug|target|company|histology|regimen|trial
    ids: tuple          # entity ids
    field: str
    start: int
    end: int
    text: str
    via: str            # name|abbr|code|symbol|defined|class
    confidence: float
    extra: dict = field(default_factory=dict)


class TermIndex:
    """Many terms -> one combined regex; lookup by normalised key."""

    def __init__(self, ignore_case: bool, plural: bool = False):
        self.ignore_case = ignore_case
        self.plural = plural
        self.map: dict[str, list] = defaultdict(list)
        self.terms: set[str] = set()
        self.rx = None

    def add(self, term: str, payload) -> None:
        if not term:
            return
        k = norm_key(term, self.ignore_case)
        if payload not in self.map[k]:
            self.map[k].append(payload)
        self.terms.add(term)

    def compile(self) -> None:
        parts = sorted({term_regex(t) for t in self.terms}, key=lambda p: (-len(p), p))
        if not parts:
            self.rx = None
            return
        suffix = r"(?:e?s)?" if self.plural else ""
        pat = LB + "(?:" + "|".join(parts) + ")" + suffix + RB
        self.rx = re.compile(pat, re.IGNORECASE if self.ignore_case else 0)

    def lookup(self, text: str):
        k = norm_key(text, self.ignore_case)
        if k in self.map:
            return self.map[k]
        if self.plural:
            for cut in ("es", "s"):
                if k.endswith(cut) and k[: -len(cut)] in self.map:
                    return self.map[k[: -len(cut)]]
        return []

    def finditer(self, text: str, flags_override: bool = False):
        if self.rx is None:
            return
        rx = self.rx
        if flags_override and not self.ignore_case:
            rx = re.compile(self.rx.pattern, re.IGNORECASE)
        for m in rx.finditer(text):
            payloads = self.lookup(m.group(0))
            if payloads:
                yield m, payloads


# --------------------------------------------------------------------------- KB
class KB:
    def __init__(self):
        self.diseases_raw = _load("diseases.json")
        self.drugs_raw = _load("drugs.json")
        self.targets_raw = _load("targets.json")
        self.companies_raw = _load("companies.json")
        self.pol = _load("polarity_patterns.json")
        self._build()

    # ---- diseases
    def _build(self) -> None:
        self.disease = {d["id"]: d for d in self.diseases_raw["diseases"]}
        self.dis_names = TermIndex(ignore_case=True, plural=True)
        self.dis_abbr_safe = TermIndex(ignore_case=False)
        self.dis_abbr_unsafe = TermIndex(ignore_case=False)
        for d in self.diseases_raw["diseases"]:
            for n in d["names"]:
                self.dis_names.add(n, (d["id"],))
            for a in d.get("abbreviations", []):
                (self.dis_abbr_safe if a["safe"] else self.dis_abbr_unsafe).add(a["abbr"], (d["id"],))
        for c in self.diseases_raw.get("compounds", []):
            for n in c["names"]:
                self.dis_names.add(n, tuple(c["ids"]))
        for ix in (self.dis_names, self.dis_abbr_safe, self.dis_abbr_unsafe):
            ix.compile()
        self.histology = TermIndex(ignore_case=True)
        self.histology_label = {}
        for h in self.diseases_raw.get("histology", []):
            self.histology_label[h["id"]] = h["label"]
            for n in h["names"]:
                self.histology.add(n, h["id"])
        self.histology.compile()

        # ---- targets
        self.target = {}
        self.group = {g["id"]: g for g in self.targets_raw["groups"]}
        self.tgt_names = TermIndex(ignore_case=True)
        self.tgt_sym_caps_safe = TermIndex(ignore_case=False)
        self.tgt_sym_caps_unsafe = TermIndex(ignore_case=False)
        self.status_only: set[str] = set()
        for t in self.targets_raw["targets"]:
            self.target[t["symbol"]] = t
            for n in t.get("names", []):
                self.tgt_names.add(n, t["symbol"])
            for s in t.get("symbols", []):
                (self.tgt_sym_caps_safe if t.get("capsSafe", True) else self.tgt_sym_caps_unsafe).add(s, t["symbol"])
            for s in t.get("statusOnly", []):
                self.status_only.add(s)
        for ix in (self.tgt_names, self.tgt_sym_caps_safe, self.tgt_sym_caps_unsafe):
            ix.compile()

        # ---- drugs
        self.drug = {d["id"]: d for d in self.drugs_raw["drugs"]}
        self.modality_label = self.drugs_raw["meta"]["modalities"]
        self.drug_ci = TermIndex(ignore_case=True)
        self.drug_cs = TermIndex(ignore_case=False)
        self.known_codes: set[str] = set()
        for d in self.drugs_raw["drugs"]:
            via = "class" if d.get("isClass") else "name"
            for s in d.get("synonyms", []):
                self.drug_ci.add(s, (d["id"], via))
            for s in d.get("codes", []):
                self.drug_ci.add(s, (d["id"], "code"))
                self.known_codes.add(norm_key(s))
            for s in d.get("caseSensitive", []):
                self.drug_cs.add(s, (d["id"], "class" if d.get("isClass") else "abbr"))
        self.drug_ci.compile()
        self.drug_cs.compile()
        self.regimen = {r["id"]: r for r in self.drugs_raw.get("regimens", [])}
        self.reg_ci = TermIndex(ignore_case=True)
        self.reg_cs = TermIndex(ignore_case=False)
        self.reg_ambiguous: set[str] = set()
        for r in self.drugs_raw.get("regimens", []):
            for n in r.get("names", []):
                self.reg_ci.add(n, r["id"])
            for n in r.get("caseSensitive", []):
                self.reg_cs.add(n, r["id"])
            for n in r.get("ambiguous", []):
                self.reg_ambiguous.add(n)
        self.reg_ci.compile()
        self.reg_cs.compile()
        self.trials = self.drugs_raw.get("trials", [])
        self.trial_ix_ci = TermIndex(ignore_case=True)
        self.trial_ix_cs = TermIndex(ignore_case=False)
        for t in self.trials:
            for n in t["names"]:
                (self.trial_ix_cs if t.get("caseSensitive") else self.trial_ix_ci).add(n, t["id"])
        self.trial_ix_ci.compile()
        self.trial_ix_cs.compile()
        self.trial_by_id = {t["id"]: t for t in self.trials}

        # ---- companies
        self.company = {c["id"]: c for c in self.companies_raw["companies"]}
        self.co_ci = TermIndex(ignore_case=True)
        self.co_cs = TermIndex(ignore_case=False)
        for c in self.companies_raw["companies"]:
            for a in c.get("aliases", []):
                self.co_ci.add(a, c["id"])
            for a in c.get("caseSensitiveAliases", []):
                self.co_cs.add(a, c["id"])
        self.co_ci.compile()
        self.co_cs.compile()

        # ---- polarity
        self.post_rules = [dict(r, rx=re.compile(r["regex"], 0 if r.get("caseSensitive") else re.IGNORECASE))
                           for r in self.pol["postfix"]]
        self.pre_rules = [dict(r, rx=re.compile(r["regex"], re.IGNORECASE)) for r in self.pol["prefix"]]
        neg = self.pol["negation"]
        self.neg_pre = re.compile(neg["preTriggers"], re.IGNORECASE)
        self.neg_pseudo = re.compile(neg["pseudoTriggers"], re.IGNORECASE)
        self.neg_term = re.compile(neg["terminators"], re.IGNORECASE)
        self.neg_post = re.compile(neg["postNegation"], re.IGNORECASE)
        self.neg_flip = neg["negationFlip"]
        self.neg_max_tokens = neg["maxTokensBefore"]
        self.sel_before = re.compile(self.pol["selectionCues"]["before"], re.IGNORECASE)
        self.sel_after = re.compile(self.pol["selectionCues"]["after"], re.IGNORECASE)
        self.fusion_pairs = {frozenset(p) for p in self.pol["fusionPairs"]}
        self.fusion_conn = re.compile(self.pol["fusionConnector"])

    # ---- helpers
    def ancestors(self, did: str) -> list[str]:
        out, cur = [], self.disease[did].get("parent")
        while cur:
            out.append(cur)
            cur = self.disease[cur].get("parent")
        return out

    def target_group(self, symbol: str) -> str:
        t = self.target.get(symbol)
        return t["group"] if t else symbol

    def group_label(self, gid: str) -> str:
        if gid in self.group:
            return self.group[gid]["label"]
        t = self.target.get(gid)
        return t["label"] if t else gid

    def group_members(self, gid: str) -> list[str]:
        if gid in self.group:
            return list(self.group[gid]["members"])
        return [gid]

    def current_owner(self, cid: str, ref: str = REFERENCE_DATE) -> tuple[str, list]:
        chain, cur, seen = [], cid, set()
        while cur and cur not in seen:
            seen.add(cur)
            nxt = None
            for ev in self.company[cur].get("lineage", []):
                if ev["event"] in ("acquired_by", "asset_sold_to") and ev["date"] <= ref:
                    nxt = ev["by"]
                    chain.append({"from": cur, "to": ev["by"], "event": ev["event"], "date": ev["date"]})
            if nxt is None:
                break
            cur = nxt
        return cur, chain


_KB: KB | None = None


def get_kb() -> KB:
    global _KB
    if _KB is None:
        _KB = KB()
    return _KB


# --------------------------------------------------------------------------- text helpers
SECTION_RX = re.compile(
    r"(?:(?<=\n)|^)\s*(Objectives?|Background|Purpose|Introduction|Aims?|Methods?|Materials and Methods|Patients and Methods|"
    r"Design|Results?|Conclusions?|Discussion|Case (?:Report|Presentation|Description|Summary)s?|Summary)\s*:",
    re.IGNORECASE)


def sections(text: str) -> list[tuple[int, str]]:
    out = [(0, "objective")]
    for m in SECTION_RX.finditer(text or ""):
        lab = m.group(1).lower()
        if lab.startswith(("objective", "background", "purpose", "introduction", "aim")):
            lab = "objective"
        elif lab.startswith(("method", "materials", "patients", "design")):
            lab = "methods"
        elif lab.startswith("result"):
            lab = "results"
        elif lab.startswith(("conclusion", "discussion", "summary")):
            lab = "conclusion"
        else:
            lab = "case"
        out.append((m.start(), lab))
    return out


def section_at(secs, pos: int) -> str:
    lab = "objective"
    for s, l in secs:
        if s <= pos:
            lab = l
    return lab


SENT_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(\[])|\n+")


def sentence_bounds(text: str, pos: int) -> tuple[int, int]:
    start = 0
    for m in SENT_END.finditer(text, 0, pos):
        start = m.end()
    m = SENT_END.search(text, pos)
    end = m.start() if m else len(text)
    return start, end


# --------------------------------------------------------------------------- Method E
STUDY_CUE = re.compile(
    r"\b(?:we|our|this (?:study|trial|analysis|phase)|the (?:study|trial)|phase\s*(?:I{1,3}|1|2|3|Ib|1b|II[ab]?|2[ab]?)\b|single[- ]arm|"
    r"randomi[sz]ed|enrolled|received|receiving|treated (?:with|by)|treatment with|administered|dose|doses|dosing|mg\b|mg/m2|mg/kg|"
    r"cohorts?|arms?\b|evaluate[sd]?|investigate[sd]?|assess(?:ed)?|patients (?:were|who)|combin\w+|plus|monotherapy|regimen|"
    r"exposed|incubated|in vitro|in vivo|xenograft|cell lines?|sensitivity|resistance to|response to|efficacy|safety)",
    re.IGNORECASE)
PRIOR_CUE = re.compile(
    r"(?:prior|previous|previously|after (?:failure of|progression on)|failure of|refractory to|resistant to|resistance to|progressed on|"
    r"progression on|pretreated with|pre-treated with|intolerant (?:of|to)|following|after)\s+(?:[\w\-/]+\s+){0,3}$",
    re.IGNORECASE)
BACKGROUND_CUE = re.compile(
    r"\b(?:has shown|have shown|has been|have been|is approved|was approved|approved|standard(?: of care)?|is the|remains|historically|"
    r"current(?:ly)?|frontline|first-line|guidelines?|recommended)\b", re.IGNORECASE)
CONTROL_BEFORE = re.compile(
    r"(?:versus|vs\.?|compared (?:with|to)|comparator|control arm(?: of)?|control group(?: received)?|or placebo|placebo or|"
    r"physician'?s choice of|investigator'?s choice of)\s+(?:[\w\-]+\s+){0,3}$", re.IGNORECASE)
CONTROL_AFTER = re.compile(r"^\s*(?:\(control\)|as (?:the )?(?:control|comparator)|control arm|in the control arm)", re.IGNORECASE)
COMBO_GAP = re.compile(r"^\s*(?:\+|plus|with|combined with|in combination with|and|/|,|-|–|followed by|or)\s*$", re.IGNORECASE)
COMBO_CONTEXT = re.compile(r"combin|plus|\+|concurrent|together with|with", re.IGNORECASE)

REGISTRY_PATTERNS = [
    ("NCT", re.compile(r"\bNCT\s?\d{8}\b")),
    ("EudraCT", re.compile(r"\b(?:EudraCT\s*(?:No\.?|number|#)?\s*:?\s*)?(?P<id>(?:19|20)\d{2}-\d{6}-\d{2})\b")),
    ("EU CT", re.compile(r"\b(?P<id>20\d{2}-5\d{5}-\d{2}-\d{2})\b")),
    ("ChiCTR", re.compile(r"\bChiCTR[-\s]?(?:[A-Z]{2,4}-?)?\d{6,12}\b")),
    ("ISRCTN", re.compile(r"\bISRCTN\s?\d{8}\b")),
    ("jRCT", re.compile(r"\bjRCT[a-z]?\d{9,10}\b")),
    ("UMIN", re.compile(r"\bUMIN\d{9}\b")),
    ("ACTRN", re.compile(r"\bACTRN\d{14}\b")),
    ("CTRI", re.compile(r"\bCTRI/\d{4}/\d{2,3}/\d{6}\b")),
    ("KCT", re.compile(r"\bKCT\d{7}\b")),
    ("DRKS", re.compile(r"\bDRKS\d{8}\b")),
    ("COG", re.compile(r"\b(?:ARST|AEWS|AOST|ANBL|AREN|ACNS|ADVL|APEC)\d{4}\b")),
    ("EORTC", re.compile(r"\bEORTC[\s-]?\d{4,5}\b")),
    ("SARC", re.compile(r"\bSARC\d{3}\b")),
    ("Alliance", re.compile(r"\bA0\d{5}\b")),
]

# unknown drug detector -----------------------------------------------------
CODE_RX = re.compile(r"(?<![\w\-/.])(?P<code>[A-Z][A-Za-z]{0,5}(?:[- ]?)\d{2,7}[A-Za-z]{0,2}\d{0,2})(?![\w\-/])")
INN_SUFFIX_RX = re.compile(
    r"\b(?P<w>[A-Za-z]{3,}(?:tinib|rafenib|ciclib|parib|lisib|sertib|metinib|gestat|cestat|madlin|utlin|degib|zomib|"
    r"mab|tug|bart|mig|cept|leucel|ectedin|tecan|rubicin|platin|taxel|citabine))\b"
    r"(?P<adc>\s+(?:vedotin|deruxtecan|govitecan|emtansine|mafodotin|soravtansine|tirumotecan|duocarmazine|rezetecan|ravtansine|"
    r"mertansine|ozogamicin|tesirine|pelidotin|ledadotin|adizutecan|rilsodotin|sudocotin))?", re.IGNORECASE)
HYPHEN_CEL_RX = re.compile(r"\b(?P<w>[a-z]{3,7}-cel)\b")
NOT_INN_WORDS = {"concept", "except", "accept", "intercept", "percept", "precept", "transcept", "glyphosate"}
CODE_EXCLUDE_RX = re.compile(
    r"^(?:CD\d+[A-Za-z]?|IL[- ]?\d+|HLA|COVID[- ]?19|SARS|NCT\d+|CTOS|ASCO|ESMO|AJCC|FIGO|WHO|ECOG|RECIST|CTCAE|ICD[- ]?\d+|"
    r"FNCLCC|EORTC|COG|SSG|ISG|GEIS|FSG|JCOG|KCSG|NRG|SWOG|ANZSA|FDA|EMA|PD[- ]?L?\d|Q\d+[WDH]?|T\d+[a-z]?|N\d|M\d|G\d+|R\d|"
    r"H\d+K\d+|K\d+M|IC\d+|EC\d+|ED\d+|AUC\d*|BMI|KPS|EQ[- ]?5D|QLQ|FACT|MRI|CT\d*|PET|SUV\w*|Gy|cGy|IMRT|SBRT|VMAT|"
    r"ESR\d?|CRP|LDH|ALP|ALT|AST|ANC|WBC|ECG|LVEF|NGS|WES|WGS|RNA|DNA|qPCR|PCR|FISH|IHC|TMA|COX|ROC|SHAP|GPT[- ]?\d*|LLM|BERT|"
    r"ResNet\d*|YOLO\w*|ISO\d*|TNM|BRCA\d|ATP|ADP|GDP|ROS\d?|GD\d|GM\d|MSI|TMB|HER\d|ERBB\d|CDK\d+|FGFR\d|VEGFR?\d?|NTRK\d|"
    r"SSX\d|PAX\d|WT\d|STAT\d|SDH\w|IDH\d|TSC\d|MEK\d|AKT\d|ATF\d|NF\d|RB\d|TP\d+|MAGE\w*|NY|ESO|U\d+OS|U2OS|HT[- ]?1080|"
    r"SK[- ]?\w+|MG[- ]?63|A\d{3}|TC[- ]?\d+|SW\d+|LPS\d+|T\d+|GIST[- ]?\w*|RD\d*|RH\d+|HEK\d+|HeLa|MCF\d*|HCT\d+|"
    r"C\d+|P\d+|S\d+|F\d+|E\d+|V\d+|D\d+|R\d+|K\d+|Y\d+|W\d+|L\d+|I\d+|H\d+|G\d+|A\d+|B\d+|X\d+|Z\d+|UMIN\d+|ISRCTN\d+|"
    r"KCT\d+|DRKS\d+|ACTRN\d+|ChiCTR\w+|ARST\d+|AEWS\d+|AOST\d+|ANBL\d+|SARC\d+|EORTC\d+|NSG|NOD|Ki[- ]?67|MIB[- ]?1|"
    r"COL\d\w*|ALDH\d\w*|CXCL\d+|CCL\d+|CXCR\d+|CCR\d+|TLR\d+|TGF\w*|TNF\w*|IFN\w*|MMP\d+|KRT\d+|SOX\d+|GATA\d|NKX\d\w*|"
    r"HOX\w+|TERT|ATRX|DAXX|ARID\d\w*|SMARC\w+|MYOD\d|MYOG|PRAME|CIC|BCOR|DUX\d|MDM\d|CDKN\d\w*|EWSR\d|FLI\d|FUS|DDIT\d|"
    r"NR\d\w+|ETV\d|CAMTA\d|YAP\d|WWTR\d|TFE\d|ASPSCR\d|NAB\d|HMGA\d|ALK|RET|MET|KIT|PDGFR\w*|CSF\d\w*|IGF\d\w*|"
    r"CTLA\d|LAG\d|TIM\d|PDCD\d|FOXO\d|FOXP\d|GPC\d|EZH\d|XPO\d|PARP\d|ATR|WEE\d|PIK\d\w*|PTEN|MTOR|H3F\d\w*|H3|"
    r"MYC\w*|NOTCH\d|HES\d|JAG\d|DLL\d|SSTR\d|FAP|MKI\d+|LSD\d|KDM\d\w*|HDAC\d+|BRD\d|BET|EP\d+|CBP|HIF\d\w*|"
    r"Stage|Grade|Phase|Cohort|Arm|Figure|Fig|Table|Version|Day|Days|Week|Weeks|Cycle|Cycles|Group|Level|Type|Class|Zone|"
    r"Exon|Codon|Chr\w*|Chapter|Vol|No|Nos|Patient|Pt|Case|Study|Trial|Year|Years|Age|Aged|Score|OS\d*|PFS\d*|EFS\d*|"
    r"DFS\d*|MFS\d*|LRFS\d*|LC\d*|DSS\d*|ORR\d*|DCR\d*|CBR\d*|HR\d*|OR\d*|CI\d*|IQR\d*|SD\d*|CR\d*|PR\d*|PD\d*|"
    r"SF[- ]?\d+|PROMIS\d*|MSTS\d*|TESS\d*|ISOLS\d*|QOL\d*|PROMs?\d*|ERAS\d*|ICU\d*|BSA\d*|AE\d*|SAE\d*|TEAE\d*|DLT\d*|"
    r"RP2D\d*|MTD\d*|NTRK|TRK\w*|SBRT\d*|IORT\d*|EBRT\d*|PBT\d*|CIRT\d*|RT\d*|Gy\d*|WBC\d*|NLR\d*|PLR\d*|LMR\d*|"
    r"PNI\d*|GPS\d*|mGPS\d*|CTC\d*|ctDNA\d*|cfDNA\d*|EV\d*|miR[- ]?\d+\w*|lnc\w+|circ\w+|LINC\d+)$", re.IGNORECASE)
NONDRUG_FOLLOW = re.compile(r"^\s*(?:cells?|cell lines?|cell[- ]line|xenografts?|PDXs?|models?|mice|organoids?|spheroids?|"
                            r"patients?|cases?|tumou?rs?|samples?|specimens?|lesions?|genes?|proteins?|mutations?|"
                            r"fusions?|antigen|years?|months?|days?|weeks?|cGy|Gy|kDa|nm\b|µM|uM|mM|%|percent|guidelines?|"
                            r"staging|criteria|classification|edition)", re.IGNORECASE)
DRUG_CONTEXT = re.compile(r"\b(?:mg|mg/kg|mg/m2|doses?|dosing|inhibitors?|antagonists?|agonists?|antibod(?:y|ies)|conjugates?|ADCs?|"
                          r"compounds?|agents?|drugs?|therap(?:y|ies)|treated|treatment|administered|oral(?:ly)?|intravenous(?:ly)?|"
                          r"daily|BID|QD|Q3W|Q2W|Q4W|phase|plus|combination|monotherapy|investigational|small[- ]molecule|"
                          r"degraders?|PROTACs?|bispecifics?|CAR|TCR|vaccines?|radioligands?|blockade|targeting|selective)\b",
                          re.IGNORECASE)
ABBR_VETO = {  # survival-endpoint readings of ambiguous disease abbreviations
    "OS": (re.compile(r"(?:median|overall|\d+[- ]?(?:year|yr)s?|and|improved|longer|shorter|prolonged|PFS|EFS|DFS|MFS|,)\s*$", re.I),
           re.compile(r"^\s*(?:rates?|was|were|is|of\s+\d|at\s+\d|\(\s*HR|HR|benefit|probabilit|months?|and\s+(?:PFS|EFS|DFS)|"
                      r"\(\d|:\s*\d|=|\d)", re.I)),
}
GENE_LIST_TAIL = re.compile(r"(?:\(?\s*[A-Z][A-Z0-9\-]{1,9}\s*(?:,|/|\)|\band\b|\bor\b)\s*){1,8}(?:\)\s*)?(?:,\s*)?(?:(?:and|or)\s+)?$")
AUTOLEUCEL_RX = re.compile(r"\b[A-Za-z]{4,}\s+autoleucel\b", re.IGNORECASE)
STOPWORDS = {"from", "of", "the", "and", "in", "at", "to", "for", "with", "on", "by", "or", "as", "is", "was", "were", "are",
             "after", "over", "under", "than", "per", "vs", "day", "days", "month", "months", "year", "years", "week", "weeks",
             "cycle", "cycles", "dose", "group", "arm", "level", "grade", "stage", "phase", "part", "visit", "n", "age",
             "ci", "hr", "or", "orr", "dcr", "iqr", "os", "pfs", "efs", "dfs", "rt", "sd", "pr", "cr", "pd", "ae", "sae",
             "july", "june", "may", "march", "april", "august", "january", "february", "september", "october",
             "november", "december", "cancer", "version", "table", "figure", "fig", "top", "only", "all", "up", "about"}
CODE_EXCLUDE2_RX = re.compile(
    r"^(?:HSP\d+|RAD\d+|MUC\d+|LRRC\d+|NKp\d+|OX[- ]?\d+|TRXR\d|PTK\d|ROR\d|CHK\d|IL[- ]?\d+R?\w?|Lu[- ]?177|"
    r"Ac[- ]?225|Ga[- ]?68|F[- ]?18|Y[- ]?90|I[- ]?131|Visium\w*|EA\d{4}|NF\d+|STRIVE[- ]?\d+|SAR[- ]?\d+|SARC[- ]?\d+|"
    r"MCC\d+|ESHO[- ]?\d+|EpSSG\w*|CWS\w*|LMS\d+|RIS\d+|MCA\d+|KMR\d+|SBKP\w*|DES\d+|CHLA\w*|SJ\w+|TTC\d+|"
    r"JR\d+|SYO\d*|HS[- ]?SY\w*|Aska\w*|Yamato\w*|MNNG\w*|HOS\w*|SaOS\w*|LM\d+|SCS\d*|CME\d*|NCI\w*|PDX\w*|"
    r"CDX\w*|QLQ\w*|C\d{2}|CAR\d*|IR\d+|CD\d+\w*|HLA\w*|DSM\w*|ICD\w*|ASA\d*|BI\d?|UCSF\w*|MDACC\w*|MSK\w*|"
    r"FFPE\w*|GSE\d+|TCGA\w*|GTEx\w*|ENSG\d+|rs\d+|GPT\w*|ViT\w*|Llama\w*|Qwen\w*|DINO\w*|UNI\d*|CONCH\w*)$",
    re.IGNORECASE)
MUTATION_TOKEN = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]\d{2,4}[ACDEFGHIKLMNPQRSTVWY]?$")


class Doc:
    def __init__(self, pres: dict, fields: Iterable[str]):
        self.pres = pres
        self.fields = {f: (pres.get(f) or "") for f in fields}
        self.caps = {f: is_all_caps(t) for f, t in self.fields.items()}
        self.secs = sections(self.fields.get("abstract", ""))


def _ev(m: Match) -> dict:
    return {"field": m.field, "charStart": m.start, "charEnd": m.end, "quote": m.text}


def _sort_ev(evs: list[dict]) -> list[dict]:
    uniq = {(e["field"], e["charStart"], e["charEnd"]): e for e in evs}
    return [uniq[k] for k in sorted(uniq, key=lambda k: (FIELD_ORDER.get(k[0], 9), k[1], k[2]))]


def _resolve_overlaps(matches: list[Match]) -> list[Match]:
    """Longest span wins inside the same kind; ties keep the first (deterministic)."""
    out = []
    by_key = defaultdict(list)
    for m in matches:
        by_key[(m.kind, m.field)].append(m)
    for key, ms in by_key.items():
        ms.sort(key=lambda m: (m.start, -(m.end - m.start)))
        chosen: list[Match] = []
        for m in sorted(ms, key=lambda m: (-(m.end - m.start), m.start)):
            if any(not (m.end <= c.start or m.start >= c.end) for c in chosen):
                continue
            chosen.append(m)
        out.extend(chosen)
    out.sort(key=lambda m: (FIELD_ORDER.get(m.field, 9), m.start, m.kind))
    return out


class TaggerE:
    method = "E"

    def __init__(self, kb: KB | None = None):
        self.kb = kb or get_kb()

    # ---------------------------------------------------------------- matching
    def _match_diseases(self, doc: Doc) -> list[Match]:
        kb, out = self.kb, []
        fields = list(FIELDS_E) + ["sessionTitle"]
        texts = {f: (doc.pres.get(f) or "") for f in fields}
        # abbreviations defined locally: "long form (ABBR)"
        defined = defaultdict(set)
        for f, t in texts.items():
            for m, payloads in kb.dis_names.finditer(t):
                tail = t[m.end(): m.end() + 14]
                dm = re.match(r"\s*\(\s*([A-Za-z][A-Za-z0-9\-/]{1,9})\s*[),;]", tail)
                if dm:
                    for p in payloads:
                        for did in p:
                            defined[dm.group(1)].add(did)
        # an abbreviation defined for something that is not a disease long form is conflicted ("overall survival (OS)")
        conflicted = set()
        for ab in defined:
            for f, t in texts.items():
                for dm in re.finditer(r"\(\s*" + re.escape(ab) + r"\s*[),;]", t):
                    pre = t[max(0, dm.start() - 60): dm.start()].rstrip()
                    if not any(t[:dm.start()].rstrip().endswith(x.group(0)) for x in kb.dis_names.rx.finditer(pre)):
                        conflicted.add(ab)
        for f, t in texts.items():
            if not t:
                continue
            caps = is_all_caps(t)
            conf_base = {"title": 0.95, "abstract": 0.9, "track": 0.6, "sessionTitle": 0.6}[f]
            for m, payloads in kb.dis_names.finditer(t):
                for p in payloads:
                    out.append(Match("disease", tuple(p), f, m.start(), m.end(), m.group(0), "name", conf_base))
            for m, payloads in kb.dis_abbr_safe.finditer(t, flags_override=False):
                if caps and m.group(0) in ("ARMS",):
                    continue
                for p in payloads:
                    out.append(Match("disease", tuple(p), f, m.start(), m.end(), m.group(0), "abbr", conf_base - 0.07))
            for m, payloads in kb.dis_abbr_unsafe.finditer(t):
                ab = m.group(0)
                if ab in conflicted or caps:
                    continue
                veto = ABBR_VETO.get(ab)
                if veto and (veto[0].search(t[max(0, m.start() - 25): m.start()]) or veto[1].match(t[m.end(): m.end() + 25])):
                    continue
                for p in payloads:
                    ids = tuple(i for i in p if i in defined.get(ab, set()))
                    if ids:
                        out.append(Match("disease", ids, f, m.start(), m.end(), ab, "defined", conf_base - 0.07))
        return out

    def _match_targets(self, doc: Doc) -> list[Match]:
        kb, out = self.kb, []
        for f, t in doc.fields.items():
            if not t or f == "track":
                continue
            caps = doc.caps[f]
            conf = 0.9 if f == "title" else 0.85
            for m, payloads in kb.tgt_names.finditer(t):
                for s in payloads:
                    out.append(Match("target", (s,), f, m.start(), m.end(), m.group(0), "name", conf))
            for m, payloads in kb.tgt_sym_caps_safe.finditer(t, flags_override=False):
                for s in payloads:
                    out.append(Match("target", (s,), f, m.start(), m.end(), m.group(0), "symbol", conf))
            if not caps:
                for m, payloads in kb.tgt_sym_caps_unsafe.finditer(t):
                    for s in payloads:
                        out.append(Match("target", (s,), f, m.start(), m.end(), m.group(0), "symbol", conf - 0.05))
        return out

    def _match_drugs(self, doc: Doc) -> list[Match]:
        kb, out = self.kb, []
        local_abbr: dict[str, str] = {}
        for f, t in doc.fields.items():
            if not t or f == "track":
                continue
            caps = doc.caps[f]
            conf = 0.95 if f == "title" else 0.9
            for m, payloads in kb.drug_ci.finditer(t):
                for did, via in payloads:
                    c = conf - (0.25 if via == "class" else 0.0)
                    out.append(Match("drug", (did,), f, m.start(), m.end(), m.group(0), via, c))
            alltext = " ".join(x for x in doc.fields.values() if x)
            veto = kb.drugs_raw["meta"].get("abbrVeto", {})
            for m, payloads in kb.drug_cs.finditer(t, flags_override=caps):
                vx = veto.get(m.group(0).upper() if caps else m.group(0))
                if vx and re.search(vx, alltext, re.IGNORECASE):
                    continue
                for did, via in payloads:
                    c = conf - (0.25 if via == "class" else 0.05)
                    out.append(Match("drug", (did,), f, m.start(), m.end(), m.group(0), via, c))
            for d in kb.drugs_raw["drugs"]:
                for cs_ in d.get("contextSynonyms", []):
                    alltext = " ".join(x for x in doc.fields.values() if x)
                    if not re.search(cs_["context"], alltext, re.IGNORECASE):
                        continue
                    for term in cs_["terms"]:
                        for m in re.finditer(LB + term_regex(term) + RB, t, re.IGNORECASE):
                            out.append(Match("drug", (d["id"],), f, m.start(), m.end(), m.group(0), "context", conf - 0.15))
            # local abbreviation definitions: "bezuclastinib (BEZU)" -> BEZU means bezuclastinib in this presentation
            for m0 in [x for x in out if x.field == f and x.kind == "drug" and x.via in ("name", "code")]:
                dm = re.match(r"\s*\(\s*([A-Za-z][A-Za-z0-9\-]{1,7})\s*[),;]", t[m0.end: m0.end + 14])
                if dm and dm.group(1).lower() not in STOPWORDS and not kb.drug_ci.lookup(dm.group(1)):
                    local_abbr.setdefault(dm.group(1), m0.ids[0])
            # regimens
            for ix, cs in ((kb.reg_ci, False), (kb.reg_cs, True)):
                for m, payloads in ix.finditer(t):
                    txt = m.group(0)
                    if cs and txt in kb.reg_ambiguous:
                        # only accept an ambiguous acronym defined in parentheses after its components
                        if caps or not (m.start() > 0 and t[m.start() - 1] == "(" and t[m.end(): m.end() + 1] == ")"):
                            continue
                    for rid in payloads:
                        out.append(Match("regimen", (rid,), f, m.start(), m.end(), txt, "regimen", conf - 0.1))
        for ab, did in sorted(local_abbr.items()):
            rx = re.compile(LB + re.escape(ab) + RB)
            for f, t in doc.fields.items():
                if not t or f == "track":
                    continue
                for m in rx.finditer(t):
                    out.append(Match("drug", (did,), f, m.start(), m.end(), m.group(0), "defined", 0.8))
        return out

    def _match_companies(self, doc: Doc) -> list[Match]:
        kb, out = self.kb, []
        for f, t in doc.fields.items():
            if not t or f == "track":
                continue
            for m, payloads in kb.co_ci.finditer(t):
                for cid in payloads:
                    out.append(Match("company", (cid,), f, m.start(), m.end(), m.group(0), "name", 0.8))
            if not doc.caps[f]:
                for m, payloads in kb.co_cs.finditer(t):
                    for cid in payloads:
                        out.append(Match("company", (cid,), f, m.start(), m.end(), m.group(0), "abbr", 0.7))
        return out

    def _match_histology(self, doc: Doc) -> list[Match]:
        out = []
        for f, t in doc.fields.items():
            if not t or f == "track":
                continue
            for m, payloads in self.kb.histology.finditer(t):
                if _grade_is_not_tumour(t, m):
                    continue
                for hid in payloads:
                    out.append(Match("histology", (hid,), f, m.start(), m.end(), m.group(0), "name", 0.7))
        return out

    def _match_trials(self, doc: Doc) -> list[Match]:
        out = []
        for f, t in doc.fields.items():
            if not t or f == "track":
                continue
            for reg, rx in REGISTRY_PATTERNS:
                for m in rx.finditer(t):
                    g = m.groupdict().get("id") or m.group(0)
                    s = m.start("id") if m.groupdict().get("id") else m.start()
                    e = s + len(g)
                    if reg == "EudraCT" and not re.search(r"EudraCT|EU\s?CT", t[max(0, s - 30): s], re.IGNORECASE):
                        continue
                    if reg == "EU CT" and not re.search(r"EU\s?CT|CTIS", t[max(0, s - 30): s], re.IGNORECASE):
                        continue
                    if reg == "Alliance" and not re.search(r"Alliance", t[max(0, s - 40): s]):
                        continue
                    tid = re.sub(r"\s", "", g)
                    out.append(Match("trial", (f"{reg}:{tid}",), f, s, e, t[s:e], "registry", 0.99, {"registry": reg}))
            if not doc.caps[f]:
                for ix in (self.kb.trial_ix_ci, self.kb.trial_ix_cs):
                    for m, payloads in ix.finditer(t):
                        for tid in payloads:
                            tr = self.kb.trial_by_id[tid]
                            if tr.get("contextRequired") and not re.search(r"\b(?:trial|study|phase)\b", t[max(0, m.start() - 60): m.end() + 60], re.IGNORECASE):
                                continue
                            out.append(Match("trial", (f"acronym:{tid}",), f, m.start(), m.end(), m.group(0), "acronym", 0.7,
                                             {"registry": "acronym", "drug": tr.get("drug")}))
        return out

    # ---------------------------------------------------------------- polarity
    def _polarity(self, doc: Doc, m: Match, symbol: str, mentions: list[Match]) -> tuple[str, str | None, str | None, dict]:
        """Return (polarity, statusDetail, roleHint, extra)."""
        kb = self.kb
        t = doc.fields[m.field]
        after = t[m.end: m.end + 80]
        before = t[max(0, m.start - 80): m.start]
        pol, detail, role_hint, rule = "unknown", None, None, None
        post_end = m.end
        for r in kb.post_rules:
            if r.get("genes") and symbol not in r["genes"]:
                continue
            mm = r["rx"].match(after)
            if mm:
                pol, rule = r["polarity"], r["id"]
                post_end = m.end + mm.end()
                detail = (mm.groupdict().get("detail") or "").strip() or None
                role_hint = r.get("role")
                break
        # skip a trailing list of other gene symbols so list members inherit the list cue
        before = GENE_LIST_TAIL.sub("", before) if GENE_LIST_TAIL.search(before) else before
        if rule is None or pol == "unknown":
            for r in kb.pre_rules:
                if r.get("genes") and symbol not in r["genes"]:
                    continue
                mm = r["rx"].search(before)
                if mm and (rule is None or r["polarity"] != "unknown"):
                    pol, rule, role_hint = r["polarity"], r["id"], r.get("role") or role_hint
                    break
        # negation (ConText): trigger within N tokens before, same clause, not pseudo
        negated = False
        s0, _ = sentence_bounds(t, m.start)
        clause = t[s0: m.start]
        clause = GENE_LIST_TAIL.sub("", clause)
        cut = 0
        for tm in kb.neg_term.finditer(clause):
            cut = tm.end()
        clause = clause[cut:]
        toks = clause.split()
        window = " ".join(toks[-(kb.neg_max_tokens + 1):]) if toks else ""
        if window and kb.neg_pre.search(window) and not kb.neg_pseudo.search(window):
            negated = True
        if kb.neg_post.match(t[m.end: m.end + 60]) and rule is not None:
            negated = True
        if negated and (rule is not None):
            pol = kb.neg_flip.get(pol, pol)
        return pol, detail, role_hint, {"rule": rule, "negated": negated, "postEnd": post_end}

    # ---------------------------------------------------------------- main
    def tag(self, pres: dict) -> list[dict]:
        kb = self.kb
        doc = Doc(pres, FIELDS_E)
        raw = (self._match_diseases(doc) + self._match_targets(doc) + self._match_drugs(doc) +
               self._match_companies(doc) + self._match_histology(doc) + self._match_trials(doc))
        # drug and regimen compete for the same spans
        for m in raw:
            if m.kind == "regimen":
                m.kind = "drug"
                m.extra["regimen"] = m.ids[0]
        matches = _resolve_overlaps(raw)
        drug_spans = [(m.field, m.start, m.end) for m in matches if m.kind == "drug"]

        def inside_drug(m: Match) -> bool:
            return any(f == m.field and s <= m.start and m.end <= e for f, s, e in drug_spans)

        tags: list[dict] = []
        # ---------------- diseases
        dis_ev = defaultdict(list)
        dis_conf = defaultdict(float)
        for m in matches:
            if m.kind != "disease":
                continue
            for did in m.ids:
                dis_ev[did].append(_ev(m))
                dis_conf[did] = max(dis_conf[did], m.confidence)
        main_fields = {"title", "abstract"}
        primary = {d for d, evs in dis_ev.items() if any(e["field"] in main_fields for e in evs)}
        present = set(primary)
        prim_anc = {a for p in primary for a in kb.ancestors(p)}
        prim_min = primary - prim_anc
        for d in set(dis_ev) - primary:   # track/session-only diseases must be compatible with the text
            if not prim_min or any(d in kb.ancestors(p) or p in kb.ancestors(d) for p in prim_min):
                present.add(d)
        anc_of_present = set()
        for d in present:
            anc_of_present.update(kb.ancestors(d))
            for g in kb.disease[d].get("groups", []):
                for b in present:
                    if b != d and kb.disease[b].get("kind") == "bucket" and kb.disease[b]["group"] == g \
                            and b not in kb.ancestors(d) and kb.disease[d].get("kind") != "bucket":
                        anc_of_present.add(b)
        for did in sorted(present - anc_of_present):
            d = kb.disease[did]
            tags.append({"kind": "disease", "entityId": did, "label": d["label"], "role": None, "polarity": None,
                         "evidence": _sort_ev(dis_ev[did]), "method": "E", "confidence": round(dis_conf[did], 2),
                         "oncotree": d.get("oncotree"), "group": d["group"], "groups": d.get("groups", []),
                         "rollup": kb.ancestors(did), "source": "curated"})

        # ---------------- drugs (incl. regimen expansion)
        drug_mentions: dict[str, list[Match]] = defaultdict(list)
        for m in matches:
            if m.kind != "drug":
                continue
            if "regimen" in m.extra:
                for comp in kb.regimen[m.extra["regimen"]]["components"]:
                    mm = Match("drug", (comp,), m.field, m.start, m.end, m.text, "regimen", m.confidence,
                               {"regimen": m.extra["regimen"]})
                    drug_mentions[comp].append(mm)
            else:
                drug_mentions[m.ids[0]].append(m)
        # parent suppression (liposomal irinotecan suppresses irinotecan etc.)
        # 'parent' (formulation family) is informational: both are tagged when both are mentioned
        # unknown drug detector
        unknown = self.detect_unknown(doc, matches)
        for u in unknown:
            if u.get("known"):
                drug_mentions[u["known"]].append(u["match"])
            else:
                drug_mentions[u["entityId"]].append(u["match"])
        unknown = [u for u in unknown if not u.get("known")]

        # combinations: adjacency of drug spans
        combos = defaultdict(set)
        flat = sorted([(m.field, m.start, m.end, did) for did, ms in drug_mentions.items() for m in ms])
        for i in range(len(flat) - 1):
            f1, s1, e1, d1 = flat[i]
            f2, s2, e2, d2 = flat[i + 1]
            if f1 != f2 or d1 == d2:
                continue
            gap = doc.fields[f1][e1:s2]
            if len(gap) <= 30 and COMBO_GAP.match(gap):
                ss, se = sentence_bounds(doc.fields[f1], s1)
                sent = doc.fields[f1][ss:se]
                g = gap.strip().lower()
                if g in ("and", ",", "or") and not re.search(r"combin|plus|\bwith\b|concurrent", sent, re.IGNORECASE):
                    continue
                if g == "or":
                    continue
                combos[d1].add(d2)
                combos[d2].add(d1)
        # same regimen components are combined
        for did, ms in drug_mentions.items():
            for m in ms:
                if "regimen" in m.extra:
                    for comp in kb.regimen[m.extra["regimen"]]["components"]:
                        if comp != did:
                            combos[did].add(comp)

        roles = {}
        for did, ms in drug_mentions.items():
            roles[did] = self._drug_role(doc, did, ms)
        # backbone: chemo combined with an investigational non-chemo drug
        for did, ms in drug_mentions.items():
            info = kb.drug.get(did)
            if not info or info.get("modality") != "chemotherapy" or roles[did] != "investigational":
                continue
            partners = [p for p in combos.get(did, ()) if p in roles and roles[p] == "investigational"
                        and (kb.drug.get(p) or {}).get("modality") not in ("chemotherapy", None)]
            if partners:
                roles[did] = "backbone"

        modality_ev = defaultdict(list)
        modality_from = defaultdict(set)
        modality_conf = defaultdict(float)
        target_ev = defaultdict(list)
        target_from = defaultdict(set)
        target_conf = defaultdict(float)
        company_ev = defaultdict(list)
        company_from = defaultdict(set)
        company_chain = {}
        for did in sorted(drug_mentions):
            ms = drug_mentions[did]
            evs = _sort_ev([_ev(m) for m in ms])
            conf = round(max(m.confidence for m in ms), 2)
            if did.startswith("unknown:"):
                u = next(u for u in unknown if u["entityId"] == did)
                tag = {"kind": "drug", "entityId": did, "label": u["label"], "role": "mentioned", "polarity": None,
                       "evidence": evs, "method": "E", "confidence": conf, "modality": u.get("modality"),
                       "targets": [], "unknown": True, "detector": u["detector"], "needsReview": True}
                if combos.get(did):
                    tag["combinationWith"] = sorted(combos[did])
                tags.append(tag)
                if u.get("modality"):
                    modality_ev[u["modality"]].extend(evs)
                    modality_from[u["modality"]].add(did)
                    modality_conf[u["modality"]] = max(modality_conf[u["modality"]], 0.3)
                continue
            info = kb.drug[did]
            eff_mod = info.get("modality")
            # An ADC named only by its antibody (e.g. "samrotamab" used as an imaging probe) is a naked antibody here.
            ab_names = {c["name"].lower() for c in info.get("components") or [] if c.get("role") == "antibody"}
            if eff_mod == "adc" and ab_names and all(e["quote"].strip().lower() in ab_names for e in evs):
                eff_mod = "monoclonal_antibody"
            # A drug class that is only mentioned (contrast, background, future work) says nothing about the study.
            class_mention_only = bool(info.get("isClass")) and roles[did] == "mentioned" and all(
                _contrast_or_speculative(doc.fields.get(e["field"]) or "", e["charStart"]) for e in evs)
            tag = {"kind": "drug", "entityId": did, "label": info["label"], "role": roles[did], "polarity": None,
                   "evidence": evs, "method": "E", "confidence": conf, "modality": eff_mod,
                   "mechanism": info.get("mechanism"), "targets": sorted({kb.target_group(s) for s in info.get("targets", [])}),
                   "isClass": bool(info.get("isClass")), "source": "curated"}
            if info.get("components"):
                tag["components"] = info["components"]
            if combos.get(did):
                tag["combinationWith"] = sorted(combos[did])
            regs = sorted({m.extra["regimen"] for m in ms if "regimen" in m.extra})
            if regs:
                tag["regimens"] = regs
            if info.get("company"):
                owner, chain = kb.current_owner(info["company"])
                tag["company"] = {"originator": info["company"], "currentOwner": owner}
                company_ev[owner].extend(evs)
                company_from[owner].add(did)
                company_chain[owner] = chain or company_chain.get(owner, [])
            tags.append(tag)
            if class_mention_only:
                continue
            if eff_mod:
                mod = eff_mod
                modality_ev[mod].extend(evs)
                modality_from[mod].add(did)
                modality_conf[mod] = max(modality_conf[mod], conf)
            for s in info.get("targets", []):
                g = kb.target_group(s)
                target_ev[g].extend(evs)
                target_from[g].add(did)
                target_conf[g] = max(target_conf[g], conf)

        for mod in sorted(modality_ev):
            tags.append({"kind": "modality", "entityId": mod, "label": kb.modality_label.get(mod, mod), "role": None,
                         "polarity": None, "evidence": _sort_ev(modality_ev[mod]), "method": "E",
                         "confidence": round(modality_conf[mod], 2), "fromDrugs": sorted(modality_from[mod])})
        for g in sorted(target_ev):
            tags.append({"kind": "target", "entityId": g, "label": kb.group_label(g), "role": "drug_target",
                         "polarity": None, "evidence": _sort_ev(target_ev[g]), "method": "E",
                         "confidence": round(target_conf[g], 2), "group": g, "members": kb.group_members(g),
                         "fromDrugs": sorted(target_from[g])})

        # ---------------- biomarker mentions
        tmentions = [m for m in matches if m.kind == "target" and not inside_drug(m)]
        pol_by_mention = {}
        for m in tmentions:
            pol_by_mention[id(m)] = self._polarity(doc, m, m.ids[0], tmentions)
        # coordination inheritance + fusion partners
        partners = defaultdict(set)
        ordered = sorted(tmentions, key=lambda m: (m.field, m.start))
        for i in range(len(ordered) - 1):
            a, b = ordered[i], ordered[i + 1]
            if a.field != b.field:
                continue
            gap = doc.fields[a.field][a.end: b.start]
            sa, sb = a.ids[0], b.ids[0]
            if kb.fusion_conn.match(gap) and gap.strip() in ("::", "-", "‐", "‑", "–", "/", ":", ""):
                is_pair = frozenset((sa, sb)) in kb.fusion_pairs or gap.strip() == "::" or \
                    pol_by_mention[id(b)][0] == "fusion" or pol_by_mention[id(a)][0] == "fusion"
                if gap.strip() == "/" and not (frozenset((sa, sb)) in kb.fusion_pairs or pol_by_mention[id(b)][0] == "fusion"):
                    is_pair = False
                if is_pair and sa != sb:
                    partners[id(a)].add(sb)
                    partners[id(b)].add(sa)
                    for x in (a, b):
                        p = pol_by_mention[id(x)]
                        pol_by_mention[id(x)] = ("fusion", p[1], p[2], dict(p[3], rule=(p[3].get("rule") or "fusion_pair")))
                    continue
            if re.fullmatch(r"\s*(?:[-‐‑–]\s*)?(?:,|/|or|and|and/or)\s*", gap) and pol_by_mention[id(a)][0] == "unknown" \
                    and pol_by_mention[id(a)][3].get("rule") is None and pol_by_mention[id(b)][0] != "unknown":
                pb = pol_by_mention[id(b)]
                pol_by_mention[id(a)] = (pb[0], pb[1], pb[2], dict(pb[3], rule="coordination"))
        bio = defaultdict(lambda: {"ev": [], "conf": 0.0, "details": set(), "partners": set(), "selection": False,
                                   "status": False})
        for m in tmentions:
            sym = m.ids[0]
            pol, detail, role_hint, extra = pol_by_mention[id(m)]
            if m.text in kb.status_only and extra.get("rule") in (None, "status", "pre_expression_of"):
                continue
            if m.text in kb.status_only and pol == "unknown":
                continue
            t = doc.fields[m.field]
            selection = False
            if pol not in ("unknown",):
                s0, _ = sentence_bounds(t, m.start)
                if kb.sel_before.search(t[s0: m.start]) or m.field == "title":
                    selection = True
                pe = extra.get("postEnd", m.end)
                tail = t[pe: pe + 60]
                if kb.sel_after.match(re.sub(r"^\s*[-‐‑–]?\s*", "", tail)) or kb.sel_after.match(tail):
                    selection = True
            key = (sym, pol)
            b = bio[key]
            b["ev"].append(_ev(m))
            b["conf"] = max(b["conf"], m.confidence - (0.15 if pol == "unknown" else 0.05))
            if detail:
                b["details"].add(detail)
            b["partners"].update(partners.get(id(m), set()))
            b["selection"] |= selection
            b["status"] |= bool(role_hint == "biomarker_status" or pol != "unknown")
        for (sym, pol) in sorted(bio):
            b = bio[(sym, pol)]
            if pol != "unknown":
                role = "selection_biomarker" if b["selection"] else "biomarker_status"
            else:
                role = "biomarker_status" if b["status"] else "mentioned"
            g = kb.target_group(sym)
            tag = {"kind": "target", "entityId": sym, "label": kb.target[sym]["label"] if sym in kb.target else sym,
                   "role": role, "polarity": pol, "evidence": _sort_ev(b["ev"]), "method": "E",
                   "confidence": round(b["conf"], 2), "group": g, "hgnc": kb.target.get(sym, {}).get("hgnc", True)}
            if b["details"]:
                tag["statusDetail"] = sorted(b["details"])
            if b["partners"]:
                tag["fusionPartners"] = sorted(b["partners"])
            tags.append(tag)

        # ---------------- companies
        explicit = defaultdict(list)
        for m in matches:
            if m.kind == "company":
                explicit[m.ids[0]].append(_ev(m))
        for cid in sorted(set(company_ev) | set(explicit)):
            if cid in company_ev:
                c = kb.company[cid]
                tag = {"kind": "company", "entityId": cid, "label": c["name"], "role": "drug_owner", "polarity": None,
                       "evidence": _sort_ev(company_ev[cid] + explicit.get(cid, [])), "method": "E",
                       "confidence": 0.85 if cid not in explicit else 0.95, "fromDrugs": sorted(company_from[cid]),
                       "lineage": company_chain.get(cid, [])}
                tags.append(tag)
            else:
                owner, chain = kb.current_owner(cid)
                tags.append({"kind": "company", "entityId": cid, "label": kb.company[cid]["name"], "role": "mentioned",
                             "polarity": None, "evidence": _sort_ev(explicit[cid]), "method": "E", "confidence": 0.8,
                             "currentOwner": owner, "lineage": chain})

        # ---------------- trials / registry ids
        tr = defaultdict(list)
        tr_extra = {}
        for m in matches:
            if m.kind == "trial":
                tr[m.ids[0]].append(_ev(m))
                tr_extra[m.ids[0]] = m.extra
        for tid in sorted(tr):
            ex = tr_extra[tid]
            tag = {"kind": "trial", "entityId": tid, "label": tid.split(":", 1)[1], "role": None, "polarity": None,
                   "evidence": _sort_ev(tr[tid]), "method": "E", "confidence": 0.99 if ex["registry"] != "acronym" else 0.7,
                   "registry": ex["registry"]}
            if ex.get("drug"):
                tag["drug"] = ex["drug"]
            tags.append(tag)

        # ---------------- histology
        hs = defaultdict(list)
        for m in matches:
            if m.kind == "histology":
                hs[m.ids[0]].append(_ev(m))
        for hid in sorted(hs):
            tags.append({"kind": "histology", "entityId": hid, "label": kb.histology_label[hid], "role": None,
                         "polarity": None, "evidence": _sort_ev(hs[hid]), "method": "E", "confidence": 0.7})
        return sort_tags(tags)

    # ---------------------------------------------------------------- drug role
    def _drug_role(self, doc: Doc, did: str, ms: list[Match]) -> str:
        if did.startswith("unknown:"):
            return "mentioned"
        control = invest = False
        for m in ms:
            t = doc.fields[m.field]
            before = t[max(0, m.start - 60): m.start]
            after = t[m.end: m.end + 40]
            if CONTROL_BEFORE.search(before) or CONTROL_AFTER.match(after):
                control = True
                continue
            if m.field == "title":
                invest = True
                continue
            if m.field == "abstract":
                if PRIOR_CUE.search(t[max(0, m.start - 60): m.start]):
                    continue
                s0, s1 = sentence_bounds(t, m.start)
                sent = t[s0:s1]
                sec = section_at(doc.secs, m.start)
                if sec in ("methods", "case") or (STUDY_CUE.search(sent) and not BACKGROUND_CUE.search(sent)):
                    invest = True
                elif sec in ("results",) and STUDY_CUE.search(sent):
                    invest = True
        if (self.kb.drug.get(did) or {}).get("isClass") and not any(m.field == "title" for m in ms):
            return "control" if control else "mentioned"   # a named class in the abstract usually describes, not doses
        if control and not any(m.field == "title" and not CONTROL_BEFORE.search(doc.fields["title"][max(0, m.start - 60): m.start]) for m in ms):
            return "control"
        if invest:
            return "investigational"
        return "mentioned"

    # ---------------------------------------------------------------- unknown drugs
    def detect_unknown(self, doc: Doc, matches: list[Match]) -> list[dict]:
        """Unknown drug detector: INN-suffix words and code names (ABC-123) with drug context.

        Returns dicts with entityId 'unknown:<key>' (role mentioned, low confidence) or, for spelling variants
        of a known INN (difflib ratio >= 0.9), a fuzzy hit mapped to the known drug id.
        """
        kb = self.kb
        taken = [(m.field, m.start, m.end) for m in matches if m.kind in ("drug", "target", "disease", "trial", "company")]
        found = {}

        def overlaps(f, s, e):
            return any(f == ff and not (e <= ss or s >= ee) for ff, ss, ee in taken)

        def add(key, label, mod, det, f, s, e, txt, conf=0.3, known=None):
            if key not in found:
                found[key] = {"entityId": key, "label": label, "modality": mod, "detector": det, "known": known,
                              "match": Match("drug", (known or key,), f, s, e, txt, "fuzzy" if known else "unknown", conf)}

        for f, t in doc.fields.items():
            if not t or f == "track":
                continue
            for m in INN_SUFFIX_RX.finditer(t):
                w = m.group("w")
                if w.lower() in NOT_INN_WORDS or overlaps(f, m.start(), m.end()) or kb.drug_ci.lookup(w):
                    continue
                full = m.group(0)
                hit = self._fuzzy(full) or self._fuzzy(w)
                if hit:
                    add("fuzzy:" + hit + ":" + norm_key(full), full, None, "fuzzy_inn", f, m.start(), m.end(), full, 0.6, hit)
                    continue
                low = w.lower()
                mod = "adc" if m.group("adc") else None
                if mod is None:
                    for suf, mo in sorted(kb.drugs_raw["meta"]["innSuffixModality"].items(), key=lambda x: -len(x[0])):
                        if low.endswith(suf):
                            mod = mo
                            break
                add("unknown:" + norm_key(full), full.lower(), mod, "inn_suffix", f, m.start(), m.end(), full)
            for m in AUTOLEUCEL_RX.finditer(t):
                if overlaps(f, m.start(), m.end()):
                    continue
                full = m.group(0)
                hit = self._fuzzy(full)
                if hit:
                    add("fuzzy:" + hit + ":" + norm_key(full), full, None, "fuzzy_inn", f, m.start(), m.end(), full, 0.6, hit)
                else:
                    add("unknown:" + norm_key(full), full.lower(), "cell_therapy", "inn_suffix", f, m.start(), m.end(), full)
            for m in HYPHEN_CEL_RX.finditer(t):
                if overlaps(f, m.start(), m.end()) or kb.drug_ci.lookup(m.group("w")):
                    continue
                add("unknown:" + norm_key(m.group("w")), m.group("w"), "cell_therapy", "inn_suffix", f, m.start(), m.end(),
                    m.group("w"))
            for m in CODE_RX.finditer(t):
                code = m.group("code")
                s, e = m.start("code"), m.end("code")
                if overlaps(f, s, e) or not self._code_ok(code, t, s, e, doc.caps[f]):
                    continue
                add("unknown:" + norm_key(code), code, None, "code_pattern", f, s, e, code)
        return [found[k] for k in sorted(found)]

    def _fuzzy(self, word: str):
        import difflib
        w = word.lower()
        if len(w) < 8:
            return None
        if not hasattr(self, "_fz"):
            self._fz = {}
            for d in self.kb.drugs_raw["drugs"]:
                if d.get("isClass"):
                    continue
                for syn in d.get("synonyms", []):
                    if len(syn) >= 8:
                        self._fz[syn.lower()] = d["id"]
            self._fz_keys = sorted(self._fz)
        best = difflib.get_close_matches(w, self._fz_keys, n=1, cutoff=0.9)
        return self._fz[best[0]] if best else None

    def _code_ok(self, code: str, t: str, s: int, e: int, caps: bool) -> bool:
        kb = self.kb
        base = code[:-1] if code.endswith("s") and code[-2:-1].isdigit() else code
        if norm_key(code) in kb.known_codes or CODE_EXCLUDE_RX.match(base) or MUTATION_TOKEN.match(base):
            return False
        if CODE_EXCLUDE2_RX.match(base):
            return False
        if not re.search(r"\d", code) or not re.search(r"[A-Z]", code):
            return False
        letters = re.match(r"[A-Za-z]+", code).group(0)
        digits = re.search(r"\d+", code).group(0)
        if len(letters) < 2:
            return False
        if " " in code:   # 'BI 907828' style: needs >=3 digits and an uppercase non-word prefix
            if len(digits) < 3 or not letters.isupper() or letters.lower() in STOPWORDS:
                return False
        if letters.lower() in STOPWORDS:
            return False
        if re.fullmatch(r"[A-Z][a-z]+\s?\d+", code):          # 'Figure 2', 'Version 5'
            return False
        if re.search(r"(?:19|20)\d{2}$", code) and len(digits) == 4:   # JCO2026, PRISMA 2020, RMS2005
            return False
        if t[e: e + 1] == ":" or re.match(r"^\s*(?:trial|study|protocol|cohort)\b", t[e: e + 20], re.I):
            return False
        if re.search(r"(?:trial|study|protocol|group)\s*\(?$", t[max(0, s - 20): s], re.I):
            return False
        if NONDRUG_FOLLOW.match(t[e: e + 25]):
            return False
        if kb.tgt_sym_caps_safe.lookup(code) or kb.tgt_sym_caps_unsafe.lookup(code):
            return False
        if kb.dis_abbr_safe.lookup(letters) or kb.dis_abbr_unsafe.lookup(letters):   # LPS224, UPS186, STS22 models
            return False
        window = t[max(0, s - 50): e + 50]
        return bool(DRUG_CONTEXT.search(window))


def sort_tags(tags: list[dict]) -> list[dict]:
    order = {"disease": 0, "drug": 1, "modality": 2, "target": 3, "company": 4, "trial": 5, "histology": 6}
    return sorted(tags, key=lambda t: (order.get(t["kind"], 9), t["entityId"], t.get("role") or "", t.get("polarity") or ""))


# --------------------------------------------------------------------------- Method K
# Reconstruction of the Conference Atlas keyword tagger (titles only, substring, closed vocab).
K_TARGETS = [  # 30-target closed vocabulary (no KIT/PDGFRA, MDM2, CTLA-4, PARP, AR, ALK, RET, FGFR, PI3K)
    ("HER2", ["her2", "erbb2"]), ("HER3", ["her3"]), ("EGFR", ["egfr"]), ("KRAS", ["kras"]), ("BRAF", ["braf"]),
    ("MEK", ["mek"]), ("PD-1", ["pd-1", "pd1"]), ("PD-L1", ["pd-l1", "pdl1"]), ("LAG-3", ["lag-3", "lag3"]),
    ("TIGIT", ["tigit"]), ("VEGF", ["vegf"]), ("VEGFR", ["vegfr"]), ("CDK4/6", ["cdk4/6"]), ("CDK4", ["cdk4"]),
    ("CDK2", ["cdk2"]), ("TROP2", ["trop2", "trop-2"]), ("B7-H3", ["b7-h3", "b7h3"]), ("CLDN18.2", ["cldn18"]),
    ("NECTIN4", ["nectin"]), ("FOLR1", ["folr1", "folate receptor"]), ("MET", ["met "]), ("ROS1", ["ros1"]),
    ("NTRK", ["ntrk", "trk"]), ("mTOR", ["mtor"]), ("ESR1", ["esr1", "estrogen receptor"]), ("BCL2", ["bcl2", "bcl-2"]),
    ("DLL3", ["dll3"]), ("CD47", ["cd47"]), ("TP53", ["tp53", "p53"]), ("EZH2", ["ezh2"]),
]
K_TARGET_TO_GROUP = {"HER2": "ERBB2", "HER3": "ERBB3", "EGFR": "EGFR", "KRAS": "KRAS", "BRAF": "BRAF", "MEK": "MEK",
                     "PD-1": "PDCD1", "PD-L1": "CD274", "LAG-3": "LAG3", "TIGIT": "TIGIT", "VEGF": "VEGFA", "VEGFR": "VEGFR",
                     "CDK4/6": "CDK4/6", "CDK4": "CDK4", "CDK2": "CDK2", "TROP2": "TACSTD2", "B7-H3": "CD276",
                     "CLDN18.2": "CLDN18", "NECTIN4": "NECTIN4", "FOLR1": "FOLR1", "MET": "MET", "ROS1": "ROS1", "NTRK": "NTRK",
                     "mTOR": "MTOR", "ESR1": "ESR1", "BCL2": "BCL2", "DLL3": "DLL3", "CD47": "CD47", "TP53": "TP53", "EZH2": "EZH2"}
K_DRUGS = {  # name -> targets in the K vocabulary (CA style: unknown '-tinib' defaults to EGFR TKI bucket)
    "pembrolizumab": ["PD-1"], "nivolumab": ["PD-1"], "ipilimumab": [], "atezolizumab": ["PD-L1"], "durvalumab": ["PD-L1"],
    "toripalimab": ["PD-1"], "sintilimab": ["PD-1"], "camrelizumab": ["PD-1"], "trastuzumab deruxtecan": ["HER2"],
    "trastuzumab": ["HER2"], "sacituzumab govitecan": ["TROP2"], "doxorubicin": [], "ifosfamide": [], "gemcitabine": [],
    "docetaxel": [], "trabectedin": [], "eribulin": [], "dacarbazine": [], "temozolomide": [], "irinotecan": [],
    "cyclophosphamide": [], "vincristine": [], "etoposide": [], "cisplatin": [], "methotrexate": [],
    "imatinib": [], "sunitinib": ["VEGFR"], "regorafenib": ["VEGFR"], "ripretinib": [], "avapritinib": [],
    "pazopanib": ["VEGFR"], "cabozantinib": ["EGFR"], "lenvatinib": ["EGFR"], "anlotinib": ["EGFR"], "apatinib": ["EGFR"],
    "palbociclib": ["CDK4/6"], "abemaciclib": ["CDK4/6"], "olaparib": [], "tazemetostat": ["EZH2"], "selinexor": [],
    "nirogacestat": [], "pexidartinib": [], "larotrectinib": ["NTRK"], "entrectinib": ["NTRK"], "everolimus": ["mTOR"],
    "denosumab": [], "bevacizumab": ["VEGF"],
}
K_MODALITY_KEYWORDS = [  # modality from ANY keyword in the title
    ("adc", ["antibody-drug conjugate", "antibody drug conjugate", " adc", "vedotin", "deruxtecan", "govitecan"]),
    ("monoclonal_antibody", ["mab", "antibody", "antibodies"]),
    ("small_molecule_kinase_inhibitor", ["tinib", "nib ", "inhibitor", "tecan", "small molecule", "ciclib", "parib", "rafenib"]),
    ("chemotherapy", ["chemotherapy", "doxorubicin", "ifosfamide", "gemcitabine", "docetaxel", "trabectedin", "eribulin",
                      "cyclophosphamide", "vincristine", "etoposide", "cisplatin", "methotrexate", "dacarbazine",
                      "temozolomide", "platin", "taxel"]),
    ("cell_therapy", ["car-t", "car t", "cell therapy", "-cel", "til "]),
    ("vaccine", ["vaccine"]),
    ("radiopharmaceutical", ["radioligand", "lutetium", "177lu", "radium"]),
    ("bispecific_antibody", ["bispecific"]),
]
K_DISEASES = [  # unnormalised literal keywords (substring)
    ("sarcoma", "sarcoma"), ("soft tissue sarcoma", "sts"), ("liposarcoma", "lps"), ("leiomyosarcoma", "lms"),
    ("osteosarcoma", "osteosarcoma"), ("ewing", "ewing"), ("rhabdomyosarcoma", "rms"), ("synovial sarcoma", "synovial_sarcoma"),
    ("gist", "gist"), ("gastrointestinal stromal", "gist"), ("chondrosarcoma", "chondrosarcoma"), ("chordoma", "chordoma"),
    ("desmoid", "desmoid"), ("angiosarcoma", "angiosarcoma"), ("ups", "ups"), ("undifferentiated pleomorphic", "ups"),
    ("mpnst", "mpnst"), ("bone", "bone_sarcoma"), ("tenosynovial", "tgct"), ("tgct", "tgct"),
]


class TaggerK:
    method = "K"

    def __init__(self, kb: KB | None = None):
        self.kb = kb or get_kb()

    @staticmethod
    def _find_all(text_l: str, kw: str):
        i = text_l.find(kw)
        while i >= 0:
            yield i
            i = text_l.find(kw, i + 1)

    def tag(self, pres: dict) -> list[dict]:
        title = pres.get("title") or ""
        tl = title.lower() + " "
        tags = []

        def ev(i, n):
            j = min(i + n, len(title))
            return [{"field": "title", "charStart": i, "charEnd": j, "quote": title[i:j]}]

        seen = set()
        for kw, did in K_DISEASES:
            for i in self._find_all(tl, kw):
                if i + len(kw) > len(title):
                    continue
                key = ("disease", did)
                if key in seen:
                    break
                seen.add(key)
                tags.append({"kind": "disease", "entityId": did, "label": title[i:i + len(kw)], "role": None, "polarity": None,
                             "evidence": ev(i, len(kw)), "method": "K", "confidence": 1.0})
                break
        drug_hits = []
        for name in sorted(K_DRUGS, key=len, reverse=True):
            for i in self._find_all(tl, name):
                if any(s <= i < e for s, e, _ in drug_hits):
                    continue
                drug_hits.append((i, i + len(name), name))
                break
        for s, e, name in sorted(drug_hits):
            did = name.replace(" ", "_")
            did = {"trastuzumab_deruxtecan": "t_dxd", "sacituzumab_govitecan": "sacituzumab_govitecan"}.get(did, did)
            tags.append({"kind": "drug", "entityId": did, "label": name, "role": "mentioned", "polarity": None,
                         "evidence": ev(s, e - s), "method": "K", "confidence": 1.0})
            for tg in K_DRUGS[name]:
                if ("target", tg) not in seen:
                    seen.add(("target", tg))
                    tags.append({"kind": "target", "entityId": K_TARGET_TO_GROUP[tg], "label": tg, "role": "drug_target",
                                 "polarity": None, "evidence": ev(s, e - s), "method": "K", "confidence": 1.0})
        # generic -tinib drug not in table -> EGFR TKI bucket
        for m in re.finditer(r"[a-z]+tinib", tl):
            if not any(s <= m.start() < e for s, e, _ in drug_hits) and ("target", "EGFR") not in seen:
                seen.add(("target", "EGFR"))
                tags.append({"kind": "target", "entityId": "EGFR", "label": "EGFR", "role": "drug_target", "polarity": None,
                             "evidence": ev(m.start(), m.end() - m.start()), "method": "K", "confidence": 1.0})
        for tg, kws in K_TARGETS:
            for kw in kws:
                hit = next(iter(self._find_all(tl, kw)), None)
                if hit is not None and hit + len(kw.strip()) <= len(title):
                    if ("target", tg) not in seen:
                        seen.add(("target", tg))
                        tags.append({"kind": "target", "entityId": K_TARGET_TO_GROUP[tg], "label": tg, "role": "drug_target",
                                     "polarity": None, "evidence": ev(hit, len(kw.strip())), "method": "K", "confidence": 1.0})
                    break
        for mod, kws in K_MODALITY_KEYWORDS:
            for kw in kws:
                hit = next(iter(self._find_all(tl, kw)), None)
                if hit is not None:
                    st = hit + (len(kw) - len(kw.lstrip()))
                    ln = len(kw.strip())
                    if st + ln > len(title):
                        continue
                    tags.append({"kind": "modality", "entityId": mod, "label": mod, "role": None, "polarity": None,
                                 "evidence": ev(st, ln), "method": "K", "confidence": 1.0})
                    break
        return sort_tags(tags)


# --------------------------------------------------------------------------- driver
def verify_evidence(pres: dict, tags: list[dict]) -> list[str]:
    errs = []
    for t in tags:
        if not t.get("evidence"):
            errs.append(f"{pres['id']}: {t['kind']}:{t['entityId']} has no evidence")
        for e in t.get("evidence", []):
            src = pres.get(e["field"]) or ""
            if src[e["charStart"]: e["charEnd"]] != e["quote"]:
                errs.append(f"{pres['id']}: bad quote {e}")
    return errs


def run(parsed: dict, method: str) -> tuple[dict, dict]:
    tagger = TaggerE() if method == "E" else TaggerK()
    out, errs, unknown = {}, [], defaultdict(lambda: {"count": 0, "presentations": [], "examples": []})
    for p in sorted(parsed["presentations"], key=lambda p: p["id"]):
        tags = tagger.tag(p)
        errs.extend(verify_evidence(p, tags))
        out[p["id"]] = tags
        for t in tags:
            if t.get("unknown"):
                u = unknown[t["entityId"]]
                u["label"] = t["label"]
                u["detector"] = t["detector"]
                u["modalityGuess"] = t.get("modality")
                u["count"] += 1
                u["presentations"].append(p["id"])
                e = t["evidence"][0]
                src = p.get(e["field"]) or ""
                if len(u["examples"]) < 2:
                    u["examples"].append(src[max(0, e["charStart"] - 60): e["charEnd"] + 60])
    if errs:
        raise SystemExit("evidence verification failed:\n" + "\n".join(errs[:20]))
    return out, dict(sorted(unknown.items()))


def run_files(parsed_path, out_path, unknown_out="data/stage/unknown_drug_codes.json"):
    """Stage contract: parsed.json in, tags.json out (method E, evidence based)."""
    with open(parsed_path, encoding="utf-8") as fh:
        parsed = json.load(fh)
    tags, unknown = run(parsed, "E")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(tags, fh, ensure_ascii=False, sort_keys=True, indent=1)
    if unknown_out:
        with open(unknown_out, "w", encoding="utf-8") as fh:
            json.dump(unknown, fh, ensure_ascii=False, sort_keys=True, indent=1)
    return {"presentations": len(tags), "tags": sum(len(v) for v in tags.values())}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--method", choices=["E", "K"], default="E")
    ap.add_argument("--in", dest="inp", default="data/stage/parsed.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--unknown-out", default="data/stage/unknown_drug_codes.json")
    a = ap.parse_args(argv)
    with open(a.inp, encoding="utf-8") as fh:
        parsed = json.load(fh)
    out_path = a.out or ("data/stage/tags.json" if a.method == "E" else "data/stage/tags_keyword.json")
    tags, unknown = run(parsed, a.method)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(tags, fh, ensure_ascii=False, sort_keys=True, indent=1)
    if a.method == "E" and a.unknown_out:
        with open(a.unknown_out, "w", encoding="utf-8") as fh:
            json.dump(unknown, fh, ensure_ascii=False, sort_keys=True, indent=1)
    n = sum(len(v) for v in tags.values())
    print(f"method {a.method}: {len(tags)} presentations, {n} tags -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
