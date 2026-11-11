"""QA for the Conference Atlas v2 export. Owned by Agent Assembler.

    qa.compute(doc, ctx) -> qa block
    qa.run(paths=None, out_path=None) -> qa block   (builds the document via export.build_document, writes qa.json)

Checks: JSON Schema validity; numeric logic of readouts; evidence quotes are exact substrings of the
referenced source field; referential integrity; coverage against the capture listings; embargo flags;
and a no-zero-imputation scan (zeros must be backed by evidence that shows a zero).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any, Iterable

from pipeline.assemble_common import EMBARGO_LIFT_UTC, SCHEMA_PATH, dump_json, is_number, resolve_field

MAX_EXAMPLES = 10
REVIEWED = {"approved", "verified", "reviewed", "accepted", "human_verified", "auto_verified"}
PCT_UNITS = {"%", "percent", "pct", "percentage"}
FALLBACK_FIELDS = ("title", "abstract")


class Check:
    def __init__(self, cid: str, description: str):
        self.id, self.description, self.failures, self.examples = cid, description, 0, []

    def fail(self, example: dict) -> None:
        self.failures += 1
        if len(self.examples) < MAX_EXAMPLES:
            self.examples.append(example)

    def as_dict(self) -> dict:
        return {"id": self.id, "description": self.description, "failures": self.failures,
                "examples": self.examples}


@lru_cache(maxsize=1)
def _validator():
    import jsonschema
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def schema_errors(doc: dict) -> list[dict]:
    errs = []
    for e in sorted(_validator().iter_errors(doc), key=lambda e: [str(x) for x in e.absolute_path]):
        errs.append({"path": "/".join(str(x) for x in e.absolute_path), "message": e.message[:300]})
    return errs


# ------------------------------------------------------------------------------------------ numbers
def _f(x: Any) -> float | None:
    if is_number(x):
        return float(x)
    return None


def _p_numeric(p: Any) -> float | None:
    if is_number(p):
        return float(p)
    if isinstance(p, str):
        m = re.search(r"\d*\.?\d+(?:[eE]-?\d+)?", p)
        return float(m.group(0)) if m else None
    return None


def _decimals(v: float | int) -> int:
    s = repr(v)
    return len(s.split(".")[1]) if "." in s and not s.endswith(".0") else 0


def _parse_date(s: Any) -> tuple[dt.date | None, str]:
    """Return (latest possible date for the string, precision)."""
    if not isinstance(s, str):
        return None, "none"
    s = s.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3])), "day"
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        y, mo = int(m[1]), int(m[2])
        nxt = dt.date(y + (mo == 12), mo % 12 + 1, 1)
        return nxt - dt.timedelta(days=1), "month"
    for fmt in ("%d %B %Y", "%B %d, %Y", "%B %d %Y", "%d %b %Y", "%b %d, %Y", "%b %d %Y", "%B %Y", "%b %Y"):
        try:
            d = dt.datetime.strptime(s.replace(".", ""), fmt).date()
            if "%d" not in fmt:
                nxt = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
                return nxt - dt.timedelta(days=1), "month"
            return d, "day"
        except ValueError:
            continue
    m = re.match(r"^(\d{4})$", s)
    if m:
        return dt.date(int(m[1]), 12, 31), "year"
    return None, "unparsed"


def _all_arms(r: dict) -> Iterable[tuple[str, dict, dict]]:
    for m in r.get("metrics") or []:
        for a in m.get("arms") or []:
            yield "metrics", m, a
    for s in r.get("safety") or []:
        for a in s.get("arms") or []:
            yield "safety", s, a


def numeric_checks(doc: dict) -> list[Check]:
    c_ci = Check("readout_ci_contains_estimate", "Arm and HR confidence intervals contain the point estimate")
    c_order = Check("readout_ci_order", "Confidence interval lower bound < upper bound")
    c_hr = Check("readout_hr_positive", "Hazard ratio > 0")
    c_p = Check("readout_p_range", "0 <= p <= 1")
    c_pct = Check("readout_percent_range", "Percent values and CI bounds lie in [0, 100]")
    c_cd = Check("readout_count_denominator", "count <= denominator and count/denominator matches percent within "
                                              "rounding")
    c_n = Check("readout_arm_n_sum", "Randomized designs: sum of arm n within a metric <= studyN. All designs: any single arm n or denominator <= studyN. Non randomized arms may overlap (subsets, sequential lines), so their sum is not checked")
    c_cut = Check("readout_cutoff_before_presentation", "Data cutoff precedes the presentation date")
    c_val = Check("readout_value_type", "Arm values are numeric or an explicit not-reached/not-estimable token")
    pdate = {p["id"]: p.get("date") for p in doc.get("presentations", [])}

    def ci_checks(rid, where, est, ci):
        if not ci:
            return
        lo, hi = _f(ci[0]), _f(ci[1])
        if lo is not None and hi is not None and not lo < hi:
            c_order.fail({"readoutId": rid, "where": where, "ci": ci})
        if est is not None and lo is not None and hi is not None and not (lo <= est <= hi):
            c_ci.fail({"readoutId": rid, "where": where, "estimate": est, "ci": ci})

    for r in doc.get("readouts", []):
        rid = r["id"]
        study_n = r.get("studyN")
        for m in r.get("metrics") or []:
            where = f"metrics.{m.get('key')}"
            hr = m.get("hr") or {}
            if hr:
                v = _f(hr.get("value"))
                if v is not None and v <= 0:
                    c_hr.fail({"readoutId": rid, "where": where, "hr": hr.get("value")})
                ci_checks(rid, where + ".hr", v, hr.get("ci"))
                pv = _p_numeric(hr.get("p"))
                if hr.get("p") is not None and (pv is None or not 0 <= pv <= 1):
                    c_p.fail({"readoutId": rid, "where": where, "p": hr.get("p")})
            n_sum = [a.get("n") for a in m.get("arms") or []]
            if (r.get("design") or "").lower().startswith("randomized") and study_n is not None and n_sum and all(isinstance(x, int) for x in n_sum) and sum(n_sum) > study_n:
                c_n.fail({"readoutId": rid, "where": where, "armN": n_sum, "studyN": study_n})
        for block, m, a in _all_arms(r):
            where = f"{block}.{m.get('key')}.{a.get('label')}"
            val = a.get("value")
            est = _f(val)
            if isinstance(val, str) and not re.fullmatch(r"(?i)\s*(NR|NE|not reached|not estimable|n/?a)\s*", val):
                c_val.fail({"readoutId": rid, "where": where, "value": val})
            ci_checks(rid, where, est, a.get("ci"))
            unit = (m.get("unit") or "").strip().lower()
            if unit in PCT_UNITS:
                for label, x in (("value", est), ("ciLow", _f((a.get("ci") or [None, None])[0])),
                                 ("ciHigh", _f((a.get("ci") or [None, None])[1]))):
                    if x is not None and not 0 <= x <= 100:
                        c_pct.fail({"readoutId": rid, "where": where, label: x})
            cnt, den = a.get("count"), a.get("denominator")
            if isinstance(cnt, int) and isinstance(den, int):
                if den <= 0 or cnt > den:
                    c_cd.fail({"readoutId": rid, "where": where, "count": cnt, "denominator": den})
                elif unit in PCT_UNITS and est is not None:
                    tol = max(10 ** -_decimals(est), 0.05) + 1e-9
                    if abs(cnt / den * 100 - est) > tol:
                        c_cd.fail({"readoutId": rid, "where": where, "count": cnt, "denominator": den,
                                   "value": est, "computed": round(cnt / den * 100, 2)})
            if study_n is not None:
                for k in ("n", "denominator"):
                    if isinstance(a.get(k), int) and a[k] > study_n:
                        c_n.fail({"readoutId": rid, "where": where, k: a[k], "studyN": study_n})
        cut, _prec = _parse_date(r.get("cutoff"))
        pd_, _ = _parse_date(pdate.get(r.get("presentationId")))
        if cut and pd_ and not cut < pd_:
            c_cut.fail({"readoutId": rid, "cutoff": r.get("cutoff"), "presentationDate": pdate.get(r.get(
                "presentationId"))})
    return [c_ci, c_order, c_hr, c_p, c_pct, c_cd, c_n, c_cut, c_val]


# ------------------------------------------------------------------------------------------ evidence
def _field_text(p: dict, field: str | None) -> str | None:
    v = resolve_field(p, field) if field else None
    return v if isinstance(v, str) else None


def _verify_quote(p: dict, ev: dict) -> str | None:
    """Return None if the evidence is exact, else a failure reason."""
    q = ev.get("quote")
    if not isinstance(q, str) or not q:
        return "empty quote"
    field = ev.get("field")
    if field:
        text = _field_text(p, field)
        if text is None:
            return f"field '{field}' missing on presentation"
        if q not in text:
            return f"quote not a substring of {field}"
        return None
    if any(q in (_field_text(p, f) or "") for f in FALLBACK_FIELDS):
        return None
    return "quote not found in title or abstract (no field given)"


def _verify_offsets(p: dict, ev: dict) -> str | None:
    s, e = ev.get("charStart"), ev.get("charEnd")
    if s is None and e is None:
        return None
    text = _field_text(p, ev.get("field"))
    if text is None or not isinstance(s, int) or not isinstance(e, int):
        return "offsets without a resolvable field"
    if text[s:e] != ev.get("quote"):
        return f"text[{s}:{e}] != quote"
    return None


def evidence_checks(doc: dict) -> list[Check]:
    c_tag = Check("evidence_tags", "Every tag evidence quote is an exact substring of the referenced field")
    c_off = Check("evidence_tag_offsets", "Tag evidence charStart/charEnd slice equals the quote")
    c_none = Check("evidence_tags_missing", "Every tag carries at least one evidence quote")
    c_ro = Check("evidence_readouts", "Every readout and arm evidence quote is an exact substring of the "
                                      "referenced field of its presentation")
    pmap = {p["id"]: p for p in doc.get("presentations", [])}
    for p in doc.get("presentations", []):
        for t in p.get("tags") or []:
            evs = t.get("evidence") or []
            if not evs:
                c_none.fail({"presentationId": p["id"], "kind": t.get("kind"), "label": t.get("label")})
            for ev in evs:
                why = _verify_quote(p, ev)
                if why:
                    c_tag.fail({"presentationId": p["id"], "label": t.get("label"), "field": ev.get("field"),
                                "quote": str(ev.get("quote"))[:120], "reason": why})
                why = _verify_offsets(p, ev)
                if why:
                    c_off.fail({"presentationId": p["id"], "label": t.get("label"), "field": ev.get("field"),
                                "reason": why})
    for r in doc.get("readouts", []):
        p = pmap.get(r.get("presentationId"))
        evs = [("readout", e) for e in r.get("evidence") or []]
        evs += [(f"{b}.{m.get('key')}.{a.get('label')}", e) for b, m, a in _all_arms(r) for e in a.get("evidence") or []]
        for where, ev in evs:
            why = "presentation not found" if p is None else _verify_quote(p, ev)
            if why:
                c_ro.fail({"readoutId": r["id"], "where": where, "quote": str(ev.get("quote"))[:120], "reason": why})
    return [c_tag, c_off, c_none, c_ro]


# ------------------------------------------------------------------------------------------ integrity
def integrity_checks(doc: dict, ctx: dict) -> list[Check]:
    c_dup = Check("ref_unique_ids", "Presentation, session, person and readout ids are unique")
    c_sess = Check("ref_sessions", "presentation.sessionId and session.presentationIds resolve")
    c_ppl = Check("ref_people", "author.personId resolves to people[]")
    c_tags = Check("ref_tags", "Every tags.json key is a presentation id")
    c_ro = Check("ref_readouts", "readout.presentationId resolves; presentation.readoutIds resolve")
    c_ent = Check("ref_entities", "Every tag entityId of kind drug/target/disease/company/trial/histology is in "
                                  "entities")
    c_agg = Check("ref_aggregates", "Ids in aggregates, watchlist and contextUpdates resolve")
    pres = doc.get("presentations", [])
    pids = {p["id"] for p in pres}
    sids = {s.get("id") for s in doc.get("sessions", [])}
    people = {x.get("id") for x in doc.get("people", [])}
    rids = {r["id"] for r in doc.get("readouts", [])}
    for name, items in (("presentations", pres), ("sessions", doc.get("sessions", [])),
                        ("people", doc.get("people", [])), ("readouts", doc.get("readouts", []))):
        seen = defaultdict(int)
        for x in items:
            seen[x.get("id")] += 1
        for k, v in seen.items():
            if v > 1:
                c_dup.fail({"block": name, "id": k, "count": v})
    for p in pres:
        if p.get("sessionId") and p["sessionId"] not in sids:
            c_sess.fail({"presentationId": p["id"], "sessionId": p["sessionId"]})
        for a in p.get("authors") or []:
            if a.get("personId") and a["personId"] not in people:
                c_ppl.fail({"presentationId": p["id"], "personId": a["personId"]})
            elif not a.get("personId"):
                c_ppl.fail({"presentationId": p["id"], "author": a.get("name"), "reason": "personId missing"})
        for rid in p.get("readoutIds") or []:
            if rid not in rids:
                c_ro.fail({"presentationId": p["id"], "readoutId": rid})
    for s in doc.get("sessions", []):
        for pid in s.get("presentationIds") or []:
            if pid not in pids:
                c_sess.fail({"sessionId": s.get("id"), "presentationId": pid})
    for k in ctx.get("orphanTagKeys") or []:
        c_tags.fail({"tagsKey": k})
    for r in doc.get("readouts", []):
        if r.get("presentationId") not in pids:
            c_ro.fail({"readoutId": r["id"], "presentationId": r.get("presentationId")})
    ent_ids = {e["id"] for b in doc.get("entities", {}).values() for e in b}
    for p in pres:
        for t in p.get("tags") or []:
            if t.get("kind") in ("drug", "target", "disease", "company", "trial", "histology") and t.get("entityId") \
                    and t["entityId"] not in ent_ids:
                c_ent.fail({"presentationId": p["id"], "entityId": t["entityId"]})

    def chk(where: str, ids: Iterable, universe: set):
        for i in ids or []:
            if i not in universe:
                c_agg.fail({"where": where, "id": i})

    ag = doc.get("aggregates", {})
    hm = ag.get("heatmap", {})
    rows = {r["key"] for r in hm.get("rows", [])}
    cols = {c["key"] for c in hm.get("cols", [])}
    for cell in hm.get("cells", []):
        chk("heatmap.cells", cell.get("presentationIds"), pids)
        chk("heatmap.row", [cell.get("row")], rows)
        chk("heatmap.col", [cell.get("col")], cols)
        if cell.get("count") != len(cell.get("presentationIds") or []):
            c_agg.fail({"where": "heatmap.cells.count", "cell": [cell.get("row"), cell.get("col")]})
    for t in ag.get("themes", []):
        chk(f"themes.{t['id']}", t.get("presentationIds"), pids)
        chk(f"themes.{t['id']}.readouts", t.get("readoutIds"), rids)
    for t in ag.get("targetMap", []):
        chk("targetMap", t.get("presentationIds"), pids)
        chk("targetMap.readouts", t.get("readoutIds"), rids)
    for d in ag.get("designs", []):
        chk("designs", d.get("readoutIds"), rids)
    for w in doc.get("watchlist", []):
        chk("watchlist", w.get("presentationIds"), pids)
        chk("watchlist.readouts", w.get("readoutIds"), rids)
    for u in doc.get("contextUpdates", []):
        chk("contextUpdates", [u.get("presentationId")] if u.get("presentationId") else [], pids)
        chk("contextUpdates.readout", [u.get("readoutId")] if u.get("readoutId") else [], rids)
    for b in doc.get("entities", {}).values():
        for e in b:
            chk(f"entities.{e['id']}", e.get("presentationIds"), pids)
    return [c_dup, c_sess, c_ppl, c_tags, c_ro, c_ent, c_agg]


# ------------------------------------------------------------------------------------------ coverage
def coverage_checks(doc: dict, ctx: dict) -> list[Check]:
    c_post = Check("coverage_posters", "Parsed poster count equals the poster listing count; no listed poster "
                                       "missing")
    c_talk = Check("coverage_presentations", "Every item on the presentation listing is parsed as a presentation, "
                                             "session or event")
    c_emb = Check("embargo_flag", "Every presentation with an abstract has an embargo flag with liftsAtUtc; "
                                  "embargoed is true before the lift time")
    c_rule = Check("embargo_rule_quote", "Embargo rule cites a source quote")
    pres = doc.get("presentations", [])
    ids = ctx.get("listingIds") or {}
    posters = [p for p in pres if p.get("format") == "poster"]
    if "posters" in ids:
        listed = set(ids["posters"])
        have = {str(p.get("sourceId")) for p in pres if p.get("sourceKind") == "PosterInfo"} | \
               {str(p.get("sourceId")) for p in posters}
        if len(posters) != len(listed):
            c_post.fail({"parsedPosters": len(posters), "listingCount": len(listed)})
        for sid in sorted(listed - have):
            c_post.fail({"missingPosterId": sid})
    else:
        c_post.fail({"reason": "no capture listing available to compare", "parsedPosters": len(posters)})
    if "presentations" in ids:
        have = {str(x.get("sourceId") or str(x.get("id", "")).rsplit(":", 1)[-1])
                for x in list(pres) + list(doc.get("sessions") or []) + list(doc.get("events") or [])}
        for sid in sorted(set(ids["presentations"]) - have):
            c_talk.fail({"missingPresentationId": sid})
    snapshot = (doc.get("datasetVersion") or {}).get("snapshot")
    for p in pres:
        if p.get("abstract") is None:
            continue
        emb = p.get("embargo") or {}
        if not isinstance(emb.get("embargoed"), bool) or not emb.get("liftsAtUtc"):
            c_emb.fail({"presentationId": p["id"], "embargo": emb})
        elif snapshot and str(snapshot) < EMBARGO_LIFT_UTC and emb["liftsAtUtc"] > str(snapshot) and \
                not emb["embargoed"]:
            c_emb.fail({"presentationId": p["id"], "reason": "abstract captured before lift but embargoed=false"})
    for rule in (doc.get("embargo") or {}).get("rules", []):
        if not rule.get("sourceQuote"):
            c_rule.fail({"class": rule.get("class"), "reason": "no embargo statement found in captured pages; "
                                                                "release time taken from BRIEF.md"})
    return [c_post, c_talk, c_emb, c_rule]


# ------------------------------------------------------------------------------------------ tag rules
def tag_rule_checks(doc: dict) -> list[Check]:
    c_pol = Check("tag_biomarker_polarity", "selection_biomarker and biomarker_status target tags carry a polarity")
    c_neg = Check("tag_negative_not_drug_target", "A target with negative polarity is never a drug_target")
    c_mod = Check("tag_modality_from_drug", "Modality tags appear only where a drug is tagged")
    for p in doc.get("presentations", []):
        tl = p.get("tags") or []
        for t in tl:
            if t.get("kind") == "target" and t.get("role") in ("selection_biomarker", "biomarker_status") \
                    and not t.get("polarity"):
                c_pol.fail({"presentationId": p["id"], "label": t.get("label")})
            if t.get("kind") == "target" and t.get("role") == "drug_target" and t.get("polarity") == "negative":
                c_neg.fail({"presentationId": p["id"], "label": t.get("label")})
        if any(t.get("kind") == "modality" for t in tl) and not any(t.get("kind") == "drug" for t in tl):
            c_mod.fail({"presentationId": p["id"],
                        "modalities": sorted({t.get("label") for t in tl if t.get("kind") == "modality"})})
    return [c_pol, c_neg, c_mod]


# ------------------------------------------------------------------------------------------ zeros
def zero_checks(doc: dict) -> list[Check]:
    c = Check("no_zero_imputation", "Every zero in a readout is backed by an evidence quote that shows a zero")
    c_agg = Check("no_zero_cells", "Aggregates never emit zero-count cells or rows")
    zero_rx = re.compile(r"(?<![\d.])0(?:\.0+)?(?![\d.])|\bnone\b|\bno\b|\bzero\b", re.I)
    for r in doc.get("readouts", []):
        if r.get("studyN") == 0:
            c.fail({"readoutId": r["id"], "field": "studyN"})
        for m in r.get("metrics") or []:
            hr = m.get("hr") or {}
            if hr.get("value") == 0:
                c.fail({"readoutId": r["id"], "field": f"metrics.{m.get('key')}.hr.value"})
        for b, m, a in _all_arms(r):
            quotes = " | ".join(e.get("quote") or "" for e in a.get("evidence") or [])
            for k in ("n", "value", "count", "denominator"):
                if a.get(k) == 0 and not isinstance(a.get(k), bool) and not zero_rx.search(quotes):
                    c.fail({"readoutId": r["id"], "field": f"{b}.{m.get('key')}.{a.get('label')}.{k}",
                            "evidence": quotes[:120] or None})
    hm = (doc.get("aggregates") or {}).get("heatmap") or {}
    for cell in hm.get("cells", []):
        if not cell.get("count"):
            c_agg.fail({"cell": [cell.get("row"), cell.get("col")]})
    for t in (doc.get("aggregates") or {}).get("themes", []):
        if not t.get("count"):
            c_agg.fail({"theme": t["id"]})
    return [c, c_agg]


# ------------------------------------------------------------------------------------------ assembly
def unresolved_entities(doc: dict) -> list[dict]:
    out = []
    for p in doc.get("presentations", []):
        for t in p.get("tags") or []:
            eid = t.get("entityId")
            if not eid or str(eid).lower().startswith(("unresolved", "unknown")) or t.get("method") == "unresolved" \
                    or t.get("unknown") is True:
                reason = ("no entityId" if not eid else
                          f"not in curated KB (detector: {t.get('detector')})" if t.get("unknown") else
                          "unresolved entityId")
                out.append({"presentationId": p["id"], "kind": t.get("kind") or "unknown", "label": t.get("label"),
                            "reason": reason})
    out.sort(key=lambda x: (x["presentationId"], x["kind"], str(x["label"])))
    return out


def known_issues(doc: dict, ctx: dict, checks: list[dict] | None = None) -> list[str]:
    notes = ctx.get("notes") or {}
    out = []
    if not ctx.get("hasBundle"):
        out.append("Raw bundle not available to the assembler: provenance.sources, discovered/captured counts and "
                   "listing comparisons are empty.")
    if not ctx.get("readoutsPresent"):
        out.append("data/stage/readouts.json absent at assembly time: readouts treated as an empty list.")
    if notes.get("embargoFilled"):
        out.append(f"Embargo flag missing from parsed.json for {notes['embargoFilled']} presentation(s); assembler "
                   f"filled embargoed=(abstract present), liftsAtUtc={EMBARGO_LIFT_UTC}.")
    dropped = notes.get("droppedFields") or {}
    if dropped:
        out.append("Non-canonical presentation fields dropped by the export projection: " +
                   ", ".join(f"{k} ({v})" for k, v in sorted(dropped.items())) + ".")
    orphan, missing = ctx.get("orphanTagKeys") or [], ctx.get("missingTagKeys") or []
    ro_orphan = ctx.get("orphanReadoutPresentations") or []
    offset_fail = next((c for c in (checks or []) if c["id"] == "evidence_tag_offsets"), {}).get("failures", 0)
    if orphan or missing or offset_fail:
        out.append(f"STALE INPUT: tags.json does not match parsed.json ({len(orphan)} tag keys for unknown "
                   f"presentation ids, e.g. {orphan[:3]}; {len(missing)} presentations without a tags key; "
                   f"{offset_fail} evidence offsets that no longer slice to their quote). Rerun the Tagger on the "
                   "current parsed.json, then `python run.py aggregate qa export`.")
    if ro_orphan:
        out.append(f"STALE INPUT: {len(ro_orphan)} readout presentationId(s) not in parsed.json, e.g. "
                   f"{ro_orphan[:3]}. Rerun Readouts on the current parsed.json, then "
                   "`python run.py aggregate qa export`.")
    pr = ctx.get("parseReport") or {}
    for key in ("warnings", "issues", "knownIssues", "errors"):
        v = pr.get(key)
        if isinstance(v, list):
            out += [f"parse: {x}" if isinstance(x, str) else f"parse: {json.dumps(x, ensure_ascii=False)[:200]}"
                    for x in v[:20]]
    hm = (doc.get("aggregates") or {}).get("heatmap") or {}
    cov = hm.get("coverage") or {}
    if cov.get("total"):
        out.append(f"Heatmap covers {cov.get('presentationsInGrid')} of {cov.get('total')} presentations (needs both a "
                   "drug_target tag and a modality tag).")
    return out


def compute(doc: dict, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    probe = dict(doc)
    probe["qa"] = {"schemaValid": False, "checks": [], "unresolvedEntities": [], "pendingReview": [],
                   "knownIssues": []}
    errs = schema_errors(probe)
    c_schema = Check("schema_valid", "Document validates against conference-atlas-2.0.schema.json")
    for e in errs:
        c_schema.fail(e)
    checks = [c_schema] + numeric_checks(doc) + evidence_checks(doc) + integrity_checks(doc, ctx) + \
        coverage_checks(doc, ctx) + tag_rule_checks(doc) + zero_checks(doc)
    pending = sorted(r["id"] for r in doc.get("readouts", [])
                     if (r.get("review") or {}).get("status") not in REVIEWED)
    check_dicts = [c.as_dict() for c in checks]
    return {"schemaValid": not errs, "checks": check_dicts,
            "unresolvedEntities": unresolved_entities(doc), "pendingReview": pending,
            "knownIssues": known_issues(doc, ctx, check_dicts)}


def run(paths: dict | None = None, out_path=None) -> dict:
    from pipeline import export
    doc, ctx = export.build_document(paths)
    q = compute(doc, ctx)
    dump_json(q, out_path or ctx["paths"]["qa"])
    return q


if __name__ == "__main__":  # pragma: no cover
    res = run()
    print(json.dumps({c["id"]: c["failures"] for c in res["checks"]}, indent=1))
