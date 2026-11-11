"""Normalize Readout objects from data/stage/readouts.json into the v2 export shape. Owned by Agent Assembler.

The Readouts agent owns extraction; this module only maps field names, fills absent keys with null
(never zero), and coerces obvious encodings (CI as dict, numbers as numeric strings). It never
invents a value.
"""
from __future__ import annotations

import re
from typing import Any

from pipeline.assemble_common import is_number

READOUT_KEYS = ["id", "code", "presentationId", "drug", "trial", "targets", "modality", "disease", "setting",
                "phase", "design", "studyN", "cutoff", "followUp", "analysis", "primaryEndpoint", "readoutType",
                "metrics", "safety", "limitation", "evidence", "review"]
_ALIASES = {
    "presentation": "presentationId", "presentation_id": "presentationId", "n": "studyN", "N": "studyN",
    "sampleSize": "studyN", "dataCutoff": "cutoff", "data_cutoff": "cutoff", "medianFollowUp": "followUp",
    "follow_up": "followUp", "endpoints": "metrics", "efficacy": "metrics", "adverseEvents": "safety",
    "limitations": "limitation", "type": "readoutType",
}
_NUM_RE = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")


def _num(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and _NUM_RE.match(v):
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    return v


def _int_or_none(v: Any) -> int | None:
    v = _num(v)
    if is_number(v) and float(v).is_integer():
        return int(v)
    return None


def _str_or_none(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, list):
        parts = [_label(x) for x in v]
        parts = [p for p in parts if p]
        return " + ".join(parts) if parts else None
    if isinstance(v, dict):
        return _label(v)
    return str(v)


def _label(v: Any) -> str | None:
    if isinstance(v, dict):
        return v.get("label") or v.get("name") or v.get("entityId") or v.get("id")
    return None if v in (None, "") else str(v)


def norm_ci(ci: Any) -> list | None:
    if ci is None:
        return None
    if isinstance(ci, dict):
        lo = ci.get("lower", ci.get("lo", ci.get("low")))
        hi = ci.get("upper", ci.get("hi", ci.get("high")))
        ci = [lo, hi]
    if isinstance(ci, (list, tuple)) and len(ci) == 2:
        lo, hi = _num(ci[0]), _num(ci[1])
        if lo is None and hi is None:
            return None
        return [lo, hi]
    return None


def norm_evidence(ev: Any) -> list[dict]:
    if ev is None:
        return []
    if isinstance(ev, (str, dict)):
        ev = [ev]
    out = []
    for e in ev:
        if isinstance(e, str):
            if e:
                out.append({"field": None, "charStart": None, "charEnd": None, "quote": e})
        elif isinstance(e, dict) and (e.get("quote") or e.get("text")):
            out.append({"field": e.get("field") or e.get("source"), "charStart": _int_or_none(e.get("charStart")),
                        "charEnd": _int_or_none(e.get("charEnd")), "quote": e.get("quote") or e.get("text")})
    return out


def norm_arm(a: dict) -> dict:
    a = a or {}
    return {
        "label": _str_or_none(a.get("label") or a.get("name") or a.get("arm")),
        "role": _str_or_none(a.get("role")),
        "n": _int_or_none(a.get("n", a.get("N"))),
        "value": _num(a.get("value", a.get("estimate"))),
        "ci": norm_ci(a.get("ci")),
        "ciLevel": _num(a.get("ciLevel")),
        "count": _int_or_none(a.get("count", a.get("events"))),
        "denominator": _int_or_none(a.get("denominator")),
        "evidence": norm_evidence(a.get("evidence", a.get("quote"))),
    }


def norm_hr(hr: Any) -> dict | None:
    if not hr:
        return None
    if is_number(hr):
        return {"value": hr, "ci": None, "level": None, "p": None}
    if not isinstance(hr, dict):
        return None
    p = _num(hr.get("p", hr.get("pValue")))
    if isinstance(p, str):
        p = p.strip().replace(" ", "")
    return {"value": _num(hr.get("value", hr.get("estimate"))), "ci": norm_ci(hr.get("ci")),
            "level": _num(hr.get("level", hr.get("ciLevel"))), "p": p}


def norm_metric(m: dict) -> dict:
    m = m or {}
    return {
        "key": _str_or_none(m.get("key") or m.get("id") or m.get("endpoint")) or "unknown",
        "label": _str_or_none(m.get("label") or m.get("name") or m.get("endpoint")),
        "unit": _str_or_none(m.get("unit")),
        "endpointType": _str_or_none(m.get("endpointType")),
        "population": _str_or_none(m.get("population")),
        "status": _str_or_none(m.get("status")),
        "hr": norm_hr(m.get("hr")),
        "reportedValue": _str_or_none(m.get("reportedValue")),
        "arms": [norm_arm(a) for a in (m.get("arms") or [])],
    }


def norm_safety(s: dict) -> dict:
    s = s or {}
    return {
        "key": _str_or_none(s.get("key") or s.get("id") or s.get("term")) or "unknown",
        "label": _str_or_none(s.get("label") or s.get("term")),
        "unit": _str_or_none(s.get("unit")),
        "gradeRange": _str_or_none(s.get("gradeRange") or s.get("grade")),
        "arms": [norm_arm(a) for a in (s.get("arms") or [])],
        "note": _str_or_none(s.get("note")),
    }


def norm_review(rv: Any) -> dict:
    if isinstance(rv, str):
        return {"status": rv, "by": None, "at": None}
    rv = rv or {}
    return {"status": _str_or_none(rv.get("status")) or "pending", "by": _str_or_none(rv.get("by")),
            "at": _str_or_none(rv.get("at"))}


def normalize_readout(r: dict, idx: int = 0) -> dict:
    src = dict(r or {})
    for old, new in _ALIASES.items():
        if old in src and new not in src:
            src[new] = src.pop(old)
    targets = src.get("targets") or []
    if isinstance(targets, str):
        targets = [targets]
    rid = src.get("id") or f"readout:{src.get('presentationId') or 'unknown'}:{idx}"
    return {
        "id": str(rid),
        "code": _str_or_none(src.get("code")),
        "presentationId": _str_or_none(src.get("presentationId")),
        "drug": _str_or_none(src.get("drug")),
        "trial": _str_or_none(src.get("trial")) or _str_or_none(src.get("nct")),
        "targets": sorted({t for t in (_label(x) for x in targets) if t}),
        "modality": _str_or_none(src.get("modality")),
        "disease": _str_or_none(src.get("disease")),
        "setting": _str_or_none(src.get("setting")),
        "phase": _str_or_none(src.get("phase")),
        "design": _str_or_none(src.get("design")),
        "studyN": _int_or_none(src.get("studyN")),
        "cutoff": _str_or_none(src.get("cutoff")),
        "followUp": _str_or_none(src.get("followUp")),
        "analysis": _str_or_none(src.get("analysis")),
        "primaryEndpoint": _str_or_none(src.get("primaryEndpoint")),
        "readoutType": _str_or_none(src.get("readoutType")),
        "metrics": [norm_metric(m) for m in (src.get("metrics") or [])],
        "safety": [norm_safety(s) for s in (src.get("safety") or [])],
        "limitation": _str_or_none(src.get("limitation")),
        "evidence": norm_evidence(src.get("evidence")),
        "review": norm_review(src.get("review")),
    }
