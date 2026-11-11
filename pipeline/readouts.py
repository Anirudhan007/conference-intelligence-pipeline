"""Agent Readouts: candidate scoring, evidence helpers and verifier for clinical readouts.

No LLM API is available. Extraction is done by the agent reading abstracts and writing
data/stage/readouts_draft.json; this module ranks candidates and verifies the draft:
every quote must be an exact substring of the stored abstract, every number must appear
in its quote, and numeric logic must hold. Readouts that pass every check are
review.status = "auto_verified", others "needs_review" with reasons.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import math
import os
import re
from typing import Any, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DRAFT = os.path.join(ROOT, "data", "stage", "readouts_draft.json")
DEFAULT_PARSED = os.path.join(ROOT, "data", "stage", "parsed.json")
DEFAULT_TAGS = os.path.join(ROOT, "data", "stage", "tags.json")
DEFAULT_OUT = os.path.join(ROOT, "data", "stage", "readouts.json")
DEFAULT_CANDIDATES = os.path.join(ROOT, "data", "stage", "readout_candidates.json")

DESIGNS = {
    "Randomized", "Single-arm", "Early clinical", "Retrospective", "Observational",
    "Translational", "Biomarker analysis", "Registry", "Trial in progress", "Other",
}
PHASES = {"1", "1/2", "2", "2/3", "3", None}
ANALYSES = {"primary", "interim", "final", "updated", "exploratory", None}
UNITS = {"%", "months", "ratio", "weeks", "days", "years", "count", None}
ENDPOINT_TYPES = {"time_to_event", "proportion", "continuous", "count"}
POPULATIONS = {"ITT", "safety", "efficacy_evaluable", "per_protocol", "subgroup", "unknown"}
ROLES = {"drug", "control", "cohort"}

# --------------------------------------------------------------------------------------
# Candidate scoring
# --------------------------------------------------------------------------------------
I = re.IGNORECASE
OUTCOME_SIGNALS: dict[str, tuple[re.Pattern, float]] = {
    "orr": (re.compile(r"\b(ORR|objective response|overall response rate|response rate|best overall response)\b", I), 3.0),
    "response": (re.compile(r"\b(partial response|complete response|stable disease|RECIST|mRECIST|iRECIST|Choi)\b|\b(CR|PR|SD|PD)\b"), 2.0),
    "dcr": (re.compile(r"\b(DCR|disease control|clinical benefit rate|CBR)\b", I), 2.0),
    "pfs": (re.compile(r"\b(m?PFS|progression[- ]free survival)\b", I), 3.0),
    "os": (re.compile(r"\b(m?OS|overall survival)\b"), 3.0),
    "dfs": (re.compile(r"\b(DFS|disease[- ]free survival)\b", I), 2.0),
    "efs": (re.compile(r"\b(EFS|event[- ]free survival)\b", I), 2.0),
    "rfs": (re.compile(r"\b(RFS|MFS|recurrence[- ]free survival|relapse[- ]free survival|metastasis[- ]free survival|local control)\b", I), 2.0),
    "median": (re.compile(r"\bmedian\b", I), 1.0),
    "months": (re.compile(r"\d+(?:\.\d+)?\s*(months|mo)\b", I), 1.5),
    "hr": (re.compile(r"\b(HR|hazard ratio)\b", I), 2.0),
    "ci": (re.compile(r"\b(95\s*%\s*CI|CI)\b|confidence interval", I), 1.5),
    "p_value": (re.compile(r"\bp\s*[<=>≤≥]\s*0?\.\d+", I), 1.0),
    "n_patients": (re.compile(r"\b\d+\s+(?:\w+\s+){0,2}(patients|pts|participants|subjects|children|adults)\b", I), 1.5),
    "grade3": (re.compile(r"grade\s*(≥|>=|3|III)|\bG[≥]?3\b|G3-4|grade 3", I), 1.5),
    "adverse_events": (re.compile(r"adverse events?|\bTRAEs?\b|\bTEAEs?\b|\bAEs?\b|toxicit|dose[- ]limiting", I), 1.0),
    "rate_landmark": (re.compile(r"\b\d+[- ](year|month|yr)\b", I), 1.0),
    "percent": (re.compile(r"\d+(?:\.\d+)?\s*%"), 0.0),  # counted separately
}
DESIGN_SIGNALS: dict[str, tuple[re.Pattern, float]] = {
    "prospective": (re.compile(r"\bprospective|open[- ]label|single[- ]arm|multicent(er|re) (clinical )?(trial|study)|investigator[- ]initiated|enrolled\b", I), 3.0),
    "randomized": (re.compile(r"\brandomi[sz]ed|placebo", I), 3.0),
    "phase23": (re.compile(r"\bphase\s*(I/II|Ib/II|1b/2|1/2|II|2|III|3|IIa|IIb|2a|2b)\b", I), 4.0),
    "phase1": (re.compile(r"\bphase\s*(I|1|Ia|Ib|1a|1b)\b|first[- ]in[- ]human|dose[- ]escalation", I), 2.0),
    "nct": (re.compile(r"\bNCT\d{8}\b"), 3.0),
    "trial_word": (re.compile(r"\b(clinical trial|trial)\b", I), 1.0),
    "retrospective": (re.compile(r"\bretrospective(ly)?\b", I), 0.5),
}
PENALTY_SIGNALS: dict[str, tuple[re.Pattern, float]] = {
    "preclinical": (re.compile(r"\b(mice|murine|xenograft|PDX|cell lines?|in vitro|in vivo|organoids?|zebrafish)\b", I), -4.0),
    "in_progress": (re.compile(r"\b(trial in progress|enrollment (is )?ongoing|is currently enrolling|will be enrolled|will be randomi[sz]ed|accrual is ongoing|planned enrollment)\b", I), -4.0),
    "case_report": (re.compile(r"\bcase report\b|\bwe (report|present|describe) (a|an|the case)\b", I), -3.0),
    "review": (re.compile(r"\b(systematic review|meta-analysis|literature review|narrative review|survey)\b", I), -1.0),
}
NCT_RE = re.compile(r"\bNCT\d{8}\b")


def _presentations(parsed: dict | list) -> list[dict]:
    if isinstance(parsed, dict):
        return parsed.get("presentations") or []
    return list(parsed)


def score_candidates(parsed: dict | list, tags: dict | None = None) -> list[dict]:
    """Rank presentations whose abstract reports clinical outcome numbers.

    Returns a list (score desc, then id) of
    {presentationId, number, title, score, clinical, signals, nct, drugs, tip}.
    Presentations without an abstract or without any outcome signal are omitted.
    """
    tags = tags or {}
    out = []
    for p in _presentations(parsed):
        text = p.get("abstract") or ""
        if not text.strip():
            continue
        signals: dict[str, int] = {}
        score = 0.0
        outcome_hits = 0
        for name, (rx, w) in OUTCOME_SIGNALS.items():
            n = len(rx.findall(text))
            if n:
                signals[name] = n
                if name != "percent":
                    outcome_hits += 1
                    score += w * min(n, 3) ** 0.5
        pct = signals.get("percent", 0)
        score += min(pct, 10) * 0.3
        nums = len(re.findall(r"\d+(?:\.\d+)?", text))
        if outcome_hits == 0:
            continue
        for name, (rx, w) in DESIGN_SIGNALS.items():
            n = len(rx.findall(text))
            if n:
                signals[name] = n
                score += w
        for name, (rx, w) in PENALTY_SIGNALS.items():
            n = len(rx.findall(text))
            if n:
                signals[name] = n
                score += w
        if p.get("tip"):
            signals["tip_flag"] = 1
            score -= 6.0
        drugs = sorted({t.get("label") for t in tags.get(p.get("id"), []) or []
                        if t.get("kind") == "drug" and t.get("label")})
        inv = [t for t in tags.get(p.get("id"), []) or []
               if t.get("kind") == "drug" and t.get("role") in ("investigational", "backbone")]
        if drugs:
            signals["drug_tags"] = len(drugs)
            score += 2.0 if inv else 1.0
        design_hit = any(k in signals for k in ("prospective", "phase23", "phase1", "nct", "randomized"))
        clinical = (outcome_hits >= 2 and (pct >= 1 or "months" in signals)
                    and ("preclinical" not in signals or (outcome_hits >= 4 and design_hit)))
        if signals.get("in_progress") or p.get("tip"):
            clinical = clinical and pct >= 3
        out.append({
            "presentationId": p.get("id"),
            "number": p.get("number"),
            "title": p.get("title"),
            "score": round(score, 2),
            "clinical": bool(clinical),
            "signals": dict(sorted(signals.items())),
            "numbersInAbstract": nums,
            "nct": sorted(set(NCT_RE.findall(text))),
            "drugs": drugs,
            "tip": bool(p.get("tip")),
        })
    out.sort(key=lambda c: (-c["score"], c["presentationId"] or ""))
    for i, c in enumerate(out, 1):
        c["rank"] = i
    return out


# --------------------------------------------------------------------------------------
# Evidence helpers (used by the agent while drafting)
# --------------------------------------------------------------------------------------
def find_all(text: str, quote: str) -> list[int]:
    res, i = [], text.find(quote)
    while i >= 0:
        res.append(i)
        i = text.find(quote, i + 1)
    return res


def make_evidence(text: str, quote: str, near: int | None = None, field: str = "abstract") -> dict:
    """Build an evidence item for an exact quote; raises ValueError if absent."""
    hits = find_all(text, quote)
    if not hits:
        raise ValueError(f"quote not in text: {quote!r}")
    start = hits[0] if near is None else min(hits, key=lambda h: abs(h - near))
    return {"field": field, "charStart": start, "charEnd": start + len(quote), "quote": quote}


def slug(code: str | None) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (code or "").strip()).strip("-").lower()
    return s or "x"


# --------------------------------------------------------------------------------------
# Number matching
# --------------------------------------------------------------------------------------
_NUM_RE = re.compile(r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)?(?:[.·]\d+)?(?![\d])")


def numbers_in(text: str) -> list[float]:
    vals = []
    for m in _NUM_RE.finditer(text or ""):
        s = m.group(0)
        if not s or not re.search(r"\d", s):
            continue
        s = s.replace(",", "").replace("·", ".")
        try:
            vals.append(float(s))
        except ValueError:
            continue
        # also allow "1,234" read as two numbers ("1,2 lines" style lists)
        if "," in m.group(0):
            for part in m.group(0).split(","):
                try:
                    vals.append(float(part))
                except ValueError:
                    pass
    return vals


_UNITS_W = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS_W = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_WORD_RE = re.compile(r"\b(" + "|".join(list(_TENS_W) + list(_UNITS_W)) + r")(?:[- ](" + "|".join(
    [w for w in _UNITS_W if 0 < _UNITS_W[w] < 10]) + r"))?\b", re.IGNORECASE)


def word_numbers_in(text: str) -> list[float]:
    """Spelled-out integers 0..99 (e.g. 'Twenty-six', 'seven')."""
    vals = []
    for m in _WORD_RE.finditer(text or ""):
        a, b = m.group(1).lower(), (m.group(2) or "").lower()
        v = _TENS_W.get(a, _UNITS_W.get(a, 0))
        if b and a in _TENS_W:
            v += _UNITS_W[b]
        vals.append(float(v))
    return vals


def number_in(x: Any, text: str) -> bool:
    if x is None:
        return True
    try:
        xv = float(x)
    except (TypeError, ValueError):
        return False
    return any(math.isclose(xv, v, rel_tol=0, abs_tol=1e-9) for v in numbers_in(text) + word_numbers_in(text))


def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).strip().lstrip("<>=≤≥ "))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Verifier
# --------------------------------------------------------------------------------------
def _now_iso() -> str:
    env = os.environ.get("READOUTS_AT")
    if env:
        return env
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _Ctx:
    def __init__(self, text: str):
        self.text = text
        self.reasons: list[str] = []
        self.fixes: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str):
        if msg not in self.reasons:
            self.reasons.append(msg)

    def check_evidence(self, evs: Any, where: str) -> str:
        """Validate/fix evidence list in place; returns concatenated valid quotes."""
        if not isinstance(evs, list):
            self.fail(f"{where}: evidence is not a list")
            return ""
        quotes = []
        for k, ev in enumerate(evs):
            q = ev.get("quote") if isinstance(ev, dict) else None
            if not q:
                self.fail(f"{where}.evidence[{k}]: empty quote")
                continue
            if ev.get("field", "abstract") != "abstract":
                self.fail(f"{where}.evidence[{k}]: field must be 'abstract'")
            cs, ce = ev.get("charStart"), ev.get("charEnd")
            if isinstance(cs, int) and isinstance(ce, int) and self.text[cs:ce] == q:
                quotes.append(q)
                continue
            hits = find_all(self.text, q)
            if not hits:
                self.fail(f"{where}.evidence[{k}]: quote not an exact substring of abstract: {q[:60]!r}")
                continue
            new = min(hits, key=lambda h: abs(h - cs)) if isinstance(cs, int) else hits[0]
            self.fixes.append(f"{where}.evidence[{k}]: offsets {cs}:{ce} -> {new}:{new + len(q)}")
            ev["charStart"], ev["charEnd"] = new, new + len(q)
            ev["field"] = "abstract"
            quotes.append(q)
        return "\n".join(quotes)

    def check_arm(self, arm: dict, where: str, unit: str | None):
        if arm.get("role") not in ROLES:
            self.fail(f"{where}: role {arm.get('role')!r} not in {sorted(ROLES)}")
        qt = self.check_evidence(arm.get("evidence", []), where)
        has_num = any(arm.get(k) is not None for k in ("value", "n", "count", "denominator")) or arm.get("ci")
        if has_num and not qt:
            self.fail(f"{where}: numbers without valid evidence")
        for k in ("value", "n", "count", "denominator", "ciLevel"):
            v = arm.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                self.fail(f"{where}.{k}: not numeric ({v!r})")
                continue
            if v < 0:
                self.fail(f"{where}.{k}: negative")
            if not number_in(v, qt):
                self.fail(f"{where}.{k}={v} not found in evidence quote")
        ci = arm.get("ci")
        val = _num(arm.get("value"))
        if ci is not None:
            self._check_ci(ci, val, qt, where, unit)
            if arm.get("ciLevel") is None:
                pass  # level not stated is allowed
        if unit == "%" and val is not None and not (0 <= val <= 100):
            self.fail(f"{where}: percent value {val} outside 0..100")
        c, d = _num(arm.get("count")), _num(arm.get("denominator"))
        if c is not None and d is not None:
            if d <= 0:
                self.fail(f"{where}: denominator <= 0")
            elif c > d:
                self.fail(f"{where}: count {c} > denominator {d}")
            elif val is not None and unit == "%" and abs(100.0 * c / d - val) > 1.0:
                self.fail(f"{where}: count/denominator {c}/{d}={100*c/d:.1f}% differs from value {val} by >1 point")
        n = _num(arm.get("n"))
        if n is not None and d is not None and d > n and arm.get("role") != "cohort":
            self.fail(f"{where}: denominator {d} > arm n {n}")

    def _check_ci(self, ci, val, qt, where, unit):
        if not (isinstance(ci, list) and len(ci) == 2):
            self.fail(f"{where}: ci must be [lo, hi]")
            return
        lo, hi = _num(ci[0]), _num(ci[1])
        if ci[0] is not None and not number_in(ci[0], qt):
            self.fail(f"{where}: ci lo {ci[0]} not found in evidence quote")
        if ci[1] is not None and not number_in(ci[1], qt):
            self.fail(f"{where}: ci hi {ci[1]} not found in evidence quote")
        if lo is not None and hi is not None and not lo < hi:
            self.fail(f"{where}: ci lo {lo} not < hi {hi}")
        if val is not None:
            if lo is not None and val < lo:
                self.fail(f"{where}: estimate {val} below ci lo {lo}")
            if hi is not None and val > hi:
                self.fail(f"{where}: estimate {val} above ci hi {hi}")
        if unit == "%":
            for b in (lo, hi):
                if b is not None and not (0 <= b <= 100):
                    self.fail(f"{where}: percent ci bound {b} outside 0..100")


def _verify_one(ro: dict, text: str | None, pres_ids: set, title: str = "") -> tuple[list[str], list[str], list[str]]:
    rid = ro.get("id", "?")
    if text is None:
        reason = "presentation not found in parsed" if ro.get("presentationId") not in pres_ids else "presentation has no abstract"
        return [reason], [], []
    ctx = _Ctx(text)
    for k in ("id", "code", "presentationId", "drug", "design", "metrics", "safety", "evidence", "limitation"):
        if k not in ro:
            ctx.fail(f"missing key {k}")
    if ro.get("design") not in DESIGNS:
        ctx.fail(f"design {ro.get('design')!r} invalid")
    if ro.get("phase") not in PHASES:
        ctx.fail(f"phase {ro.get('phase')!r} invalid")
    if ro.get("analysis") not in ANALYSES:
        ctx.fail(f"analysis {ro.get('analysis')!r} invalid")
    cut = ro.get("cutoff")
    if cut is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(cut)):
        ctx.fail(f"cutoff {cut!r} not YYYY-MM-DD")
    for nct in ro.get("nct") or []:
        if not NCT_RE.fullmatch(nct) or (nct not in text and nct not in (title or "")):
            ctx.fail(f"nct {nct} not found in abstract or title")
    top = ctx.check_evidence(ro.get("evidence", []), "evidence")
    sn = ro.get("studyN")
    if sn is not None:
        if not isinstance(sn, int) or sn <= 0:
            ctx.fail(f"studyN {sn!r} must be a positive int")
        elif not number_in(sn, top):
            ctx.fail(f"studyN={sn} not found in readout evidence quote")
    if cut is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(cut)):
        y = str(cut)[:4]
        if y not in top:
            ctx.fail(f"cutoff year {y} not found in readout evidence quote")
    metric_keys = []
    pk = ro.get("primaryEndpoint")
    for mi, m in enumerate(ro.get("metrics") or []):
        w = f"metrics[{mi}:{m.get('key')}]"
        metric_keys.append(m.get("key"))
        unit = m.get("unit")
        if unit not in UNITS:
            ctx.fail(f"{w}: unit {unit!r} invalid")
        if m.get("endpointType") not in ENDPOINT_TYPES:
            ctx.fail(f"{w}: endpointType {m.get('endpointType')!r} invalid")
        if m.get("population") not in POPULATIONS:
            ctx.fail(f"{w}: population {m.get('population')!r} invalid")
        if not m.get("reportedValue"):
            ctx.fail(f"{w}: reportedValue missing")
        arms = m.get("arms") or []
        if not arms and not m.get("hr"):
            ctx.fail(f"{w}: no arms and no hr")
        all_q = [ctx.check_evidence(m.get("evidence", []), w)] if m.get("evidence") else []
        for ai, a in enumerate(arms):
            ctx.check_arm(a, f"{w}.arms[{ai}]", unit)
            all_q.append("\n".join(e.get("quote", "") for e in a.get("evidence", []) if isinstance(e, dict)))
        # reportedValue is the verbatim source string; multi-arm values may join verbatim pieces with " | "
        if m.get("reportedValue"):
            for piece in str(m["reportedValue"]).split(" | "):
                if piece not in text:
                    ctx.fail(f"{w}: reportedValue piece not verbatim in abstract: {piece[:50]!r}")
        hr = m.get("hr")
        if hr:
            hr_q = ctx.check_evidence(hr.get("evidence", []), w + ".hr") if hr.get("evidence") else ""
            qt = "\n".join(all_q + [hr_q])
            hv = _num(hr.get("value"))
            for nm, v in (("value", hr.get("value")), ("p", hr.get("p")), ("level", hr.get("level"))):
                if v is not None and not number_in(_num(v), qt):
                    ctx.fail(f"{w}.hr.{nm}={v} not found in evidence quote")
            if hv is not None and hv <= 0:
                ctx.fail(f"{w}.hr: value must be > 0")
            if hr.get("ci") is not None:
                ctx._check_ci(hr["ci"], hv, qt, w + ".hr", "ratio")
            p = _num(hr.get("p"))
            if p is not None and not (0 <= p <= 1):
                ctx.fail(f"{w}.hr.p={p} outside 0..1")
        ns = [a.get("n") for a in arms if isinstance(a.get("n"), (int, float)) and a.get("role") != "cohort"]
        if not ns:
            ns = [a.get("n") for a in arms if isinstance(a.get("n"), (int, float))]
        if sn is not None and isinstance(sn, (int, float)) and ns and sum(ns) > sn:
            # cohorts may overlap (e.g. subgroup splits); compare the largest one too
            if max(ns) > sn or any(a.get("role") != "cohort" for a in arms):
                ctx.fail(f"{w}: sum of arm n {sum(ns)} > studyN {sn}")
    if pk is not None and pk not in metric_keys:
        ctx.warnings.append(f"primaryEndpoint {pk!r} has no extracted metric")
    for si, s in enumerate(ro.get("safety") or []):
        w = f"safety[{si}:{s.get('key')}]"
        for ai, a in enumerate(s.get("arms") or []):
            ctx.check_arm(a, f"{w}.arms[{ai}]", s.get("unit", "%"))
            n = _num(a.get("n"))
            if sn is not None and n is not None and n > sn:
                ctx.fail(f"{w}.arms[{ai}]: n {n} > studyN {sn}")
        if not (s.get("arms") or s.get("note")):
            ctx.fail(f"{w}: no arms and no note")
    if not ro.get("metrics") and not ro.get("safety"):
        ctx.fail("no metrics and no safety entries")
    return ctx.reasons, ctx.fixes, ctx.warnings


def verify(readouts: list[dict], parsed: dict | list, at: str | None = None) -> list[dict]:
    """Verify readouts against parsed abstracts; returns new list with review set.

    Offsets are recomputed with str.find when the quote exists but offsets are wrong
    (recorded in review.fixes). review.status is auto_verified only if all checks pass.
    """
    at = at or _now_iso()
    pres = {p.get("id"): p for p in _presentations(parsed)}
    out = []
    seen = set()
    for ro in readouts:
        ro = copy.deepcopy(ro)
        p = pres.get(ro.get("presentationId"))
        text = p.get("abstract") if p else None
        reasons, fixes, warnings = _verify_one(ro, text, set(pres), (p or {}).get("title") or "")
        if ro.get("id") in seen:
            reasons.append("duplicate readout id")
        seen.add(ro.get("id"))
        if p and ro.get("code") not in (None, p.get("number")):
            reasons.append(f"code {ro.get('code')!r} != presentation number {p.get('number')!r}")
        prev = ro.get("review") or {}
        manual = prev.get("manualReasons") or []
        ro["review"] = {
            "status": "auto_verified" if not reasons and not manual else "needs_review",
            "by": "agent:readouts",
            "at": at,
            "reasons": reasons + list(manual),
            "fixes": fixes,
            "warnings": warnings,
        }
        if manual:
            ro["review"]["manualReasons"] = list(manual)
        if p is not None and "embargo" not in ro and p.get("embargo") is not None:
            ro["embargo"] = p.get("embargo")
        out.append(ro)
    out.sort(key=lambda r: r.get("id") or "")
    return out


# --------------------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------------------
def _load(path: str | None, default=None):
    if path and os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return default


def write_candidates(parsed_path: str = DEFAULT_PARSED, tags_path: str = DEFAULT_TAGS,
                     out_path: str = DEFAULT_CANDIDATES) -> dict:
    parsed = _load(parsed_path)
    tags = _load(tags_path, {}) or {}
    cands = score_candidates(parsed, tags)
    doc = {
        "count": len(cands),
        "clinicalCount": sum(c["clinical"] for c in cands),
        "tagsUsed": bool(tags),
        "candidates": cands,
    }
    with open(out_path, "w") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=True)
    return doc


def run(parsed_path: str = DEFAULT_PARSED, tags_path: str = DEFAULT_TAGS, out_path: str = DEFAULT_OUT,
        draft_path: str = DEFAULT_DRAFT, at: str | None = None) -> dict:
    parsed = _load(parsed_path)
    draft = _load(draft_path, []) or []
    verified = verify(draft, parsed, at=at)
    with open(out_path, "w") as fh:
        json.dump(verified, fh, indent=1, ensure_ascii=False, sort_keys=True)
    st = [r["review"]["status"] for r in verified]
    return {"n": len(verified), "auto_verified": st.count("auto_verified"), "needs_review": st.count("needs_review")}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "candidates":
        d = write_candidates()
        print(d["count"], d["clinicalCount"])
    else:
        print(run())
