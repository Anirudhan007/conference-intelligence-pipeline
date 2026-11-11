"""Assemble and export the Conference Atlas v2 JSON. Owned by Agent Assembler.

    export.build_document(paths) -> (doc, ctx)     in-memory document, qa block still a placeholder
    export.run(paths=None) -> doc                   writes out/ctos-2026.json(.gz), CSV and reports/run_report.md

`paths` is a dict with any of the keys of assemble_common.DEFAULT_PATHS.
"""
from __future__ import annotations

import csv
import gzip
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from pipeline import aggregate as agg
from pipeline.assemble_common import (CONFERENCE_DEFAULTS, DEFAULT_PATHS, EMBARGO_LIFT_UTC, PIPELINE_DIR, ROOT,
                                      SCHEMA_VERSION, canonical_bytes, dump_json, load_json, norm_space,
                                      presentations_of, readouts_list, sha256_bytes, sha256_file, sha256_text,
                                      tags_index)
from pipeline.readout_norm import norm_evidence, normalize_readout

PRESENTATION_KEYS = ["id", "number", "title", "titleNormalized", "format", "tip", "track", "sessionId",
                     "sessionTitle", "date", "startTime", "endTime", "timezone", "location", "authors", "abstract",
                     "abstractSections", "disclosures", "slidesAvailable", "views", "url", "sourceKind", "sourceId",
                     "contentHash", "embargo"]
AUTHOR_KEYS = ["personId", "name", "degrees", "pronouns", "role", "position", "institution", "city", "region",
               "country", "order"]
LIST_KEYS = {"authors", "disclosures"}
REVIEWED_STATUSES = {"approved", "verified", "reviewed", "accepted", "human_verified", "auto_verified"}
ENTITY_BUCKET = {"drug": "drugs", "target": "targets", "disease": "diseases", "histology": "diseases",
                 "company": "companies", "trial": "trials"}
RIGHTS_BASIS = (f"Abstract text is embargoed until {EMBARGO_LIFT_UTC} and remains the authors' and the society's "
                "copyright; it is kept for private analysis only. Display titles, metadata, normalized tags and "
                "short evidence quotes, not full text.")
DETAIL_RE = re.compile(r"ajaxcalls/(PosterInfo|PresentationInfo|SessionInfo|ExhibitorInfo)\.asp\?[^\"'\s>]*?"
                       r"(?:PosterID|PresentationID|BoothID)=(\d+)", re.I)

GLOSSARY = [
    ("ORR", "Objective response rate: share of patients with complete or partial response (RECIST unless stated).", "endpoint"),
    ("DCR", "Disease control rate: complete or partial response or stable disease.", "endpoint"),
    ("CBR", "Clinical benefit rate: response or stable disease lasting a protocol-defined minimum time.", "endpoint"),
    ("PFS", "Progression-free survival: time from start (randomization or treatment) to progression or death.", "endpoint"),
    ("OS", "Overall survival: time from start to death from any cause.", "endpoint"),
    ("EFS", "Event-free survival: time to a protocol-defined event (progression, relapse, death).", "endpoint"),
    ("DOR", "Duration of response: time from first response to progression or death in responders.", "endpoint"),
    ("RFS", "Recurrence-free survival: time from curative treatment to recurrence or death.", "endpoint"),
    ("pCR", "Pathologic complete response: no viable tumor in the resected specimen.", "endpoint"),
    ("HR", "Hazard ratio: relative event rate between arms; below 1 favors the experimental arm.", "statistic"),
    ("CI", "Confidence interval around an estimate, at the stated level (95% unless stated).", "statistic"),
    ("TRAE", "Treatment-related adverse event; grades follow CTCAE.", "safety"),
    ("TEAE", "Treatment-emergent adverse event, regardless of attribution.", "safety"),
    ("RECIST", "Response Evaluation Criteria in Solid Tumors (v1.1 unless stated).", "method"),
    ("ctDNA", "Circulating tumor DNA measured in plasma (liquid biopsy).", "biomarker"),
    ("ADC", "Antibody-drug conjugate: antibody linked to a cytotoxic payload.", "modality"),
    ("TKI", "Tyrosine kinase inhibitor (small molecule).", "modality"),
    ("drug_target", "Tag role: the molecular target a drug in the presentation acts on (used by the heatmap).", "tag role"),
    ("selection_biomarker", "Tag role: a biomarker used to select or stratify patients.", "tag role"),
    ("biomarker_status", "Tag role: biomarker state reported for patients (with polarity, e.g. HER2-negative).", "tag role"),
    ("mentioned", "Tag role: entity mentioned without a therapeutic or selection role.", "tag role"),
    ("polarity", "Biomarker state: positive, negative, low, mutant, wild_type, amplified, fusion or unknown.", "tag field"),
    ("embargo", "Abstract text may not be published before the release time in the embargo block.", "rights"),
    ("GIST", "Gastrointestinal stromal tumor.", "disease"),
    ("TGCT", "Tenosynovial giant cell tumor.", "disease"),
    ("RPS", "Retroperitoneal sarcoma.", "disease"),
]

STAGE_AGENTS = {
    "parse": ("Agent Parser", "pipeline/parse.py: bs4/lxml parse of eventscribe listings and detail cards"),
    "tag": ("Agent Tagger", "pipeline/tag.py: curated dictionaries (pipeline/kb) with exact-quote evidence"),
    "readouts": ("Agent Readouts", "pipeline/readouts.py: candidate scoring and quote verifier"),
    "aggregate": ("Agent Assembler", "pipeline/aggregate.py: heatmap, themes, facets, overview"),
    "qa": ("Agent Assembler", "pipeline/qa.py: schema, numeric, evidence, integrity and coverage checks"),
    "export": ("Agent Assembler", "pipeline/export.py: JSON, gzip, CSV and run report"),
}


def _paths(paths: dict | None) -> dict:
    p = dict(DEFAULT_PATHS)
    p.update({k: Path(v) for k, v in (paths or {}).items() if v is not None})
    return p


# ------------------------------------------------------------------------------------- bundle facts
def _html_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        return norm_space(BeautifulSoup(html or "", "lxml").get_text(" "))
    except Exception:  # pragma: no cover
        return norm_space(re.sub(r"<[^>]+>", " ", html or ""))


def conference_embargo(parsed: dict, facts: dict, config_path: Path | None = None) -> dict:
    """Prefer an observed release quote, then the reviewed config supplied for this release."""
    if facts.get("embargoQuote"):
        return {"rules": [{"class": "abstract",
                           "releaseUtc": (parsed.get("conference") or {}).get("embargoLiftsAtUtc") or EMBARGO_LIFT_UTC,
                           "sourceQuote": facts["embargoQuote"], "sourceUrl": facts["embargoUrl"]}]}
    cid = (parsed.get("conference") or {}).get("id") or "ctos-2026"
    cfg = Path(config_path) if config_path else ROOT / "conferences" / f"{cid}.json"
    if cfg.exists():
        conf = json.loads(cfg.read_text(encoding="utf-8"))
        if conf.get("embargo", {}).get("rules"):
            return {"rules": conf["embargo"]["rules"], "verifiedAt": conf["embargo"].get("verifiedAt")}
    return {"rules": [{"class": "abstract",
                       "releaseUtc": (parsed.get("conference") or {}).get("embargoLiftsAtUtc") or EMBARGO_LIFT_UTC,
                       "sourceQuote": facts["embargoQuote"], "sourceUrl": facts["embargoUrl"]}]}


def bundle_facts(bundle: dict | None) -> dict:
    facts = {"sources": [], "snapshot": None, "listingCounts": {}, "listingIds": {}, "discovered": None, "captured": None,
             "embargoQuote": None, "embargoUrl": None, "sourceUrls": []}
    if not bundle:
        return facts
    listings = bundle.get("listings") or {}
    details = bundle.get("details") or {}
    times = []
    discovered = set()
    for name, item in sorted(listings.items()):
        html = item.get("html") or ""
        facts["sources"].append({"id": f"listing:{name}", "url": item.get("url"), "retrievedAt": item.get("retrievedAt"),
                                 "bytes": item.get("bytes"), "sha256": sha256_text(html)})
        if item.get("url"):
            facts["sourceUrls"].append(item["url"])
        times.append(item.get("retrievedAt"))
        for kind, sid in DETAIL_RE.findall(html):
            discovered.add(f"{kind}:{sid}")
        if facts["embargoQuote"] is None and re.search(r"embargo", html, re.I):
            text = _html_text(html)
            m = re.search(r"[^.!?]*\bembargo[^.!?]*[.!?]?", text, re.I)
            if m:
                facts["embargoQuote"] = m.group(0).strip()
                facts["embargoUrl"] = item.get("url")
    facts["listingIds"] = {
        "posters": sorted(set(re.findall(r'data-posterid="(\d+)"', (listings.get("posters") or {}).get("html") or ""))),
        "presentations": sorted(set(re.findall(r'data-presid="(\d+)"',
                                               (listings.get("presentations") or {}).get("html") or ""))),
    } if listings else {}
    facts["listingCounts"] = {k: len(v) for k, v in facts["listingIds"].items()}
    for key, item in sorted(details.items()):
        facts["sources"].append({"id": key, "url": item.get("url"), "retrievedAt": item.get("retrievedAt"),
                                 "bytes": item.get("bytes"), "sha256": sha256_text(item.get("html") or "")})
        times.append(item.get("retrievedAt"))
        discovered.add(key)
    times = [t for t in times if t]
    facts["snapshot"] = max(times) if times else None
    facts["discovered"] = len(discovered) if (listings or details) else None
    facts["captured"] = len(details) if details else None
    facts["sourceUrls"] = sorted(set(facts["sourceUrls"]))
    return facts


# ------------------------------------------------------------------------------------- versions
def pipeline_hash() -> tuple[str, dict]:
    comps = {}
    entries = []
    for f in sorted(PIPELINE_DIR.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts and f.suffix in (".py", ".json"):
            rel = f.relative_to(ROOT).as_posix()
            h = sha256_bytes(f.read_bytes())
            entries.append(f"{rel}\t{h}")
    for name in ("parse", "tag", "readouts", "aggregate", "qa", "export"):
        comps[name] = sha256_file(PIPELINE_DIR / f"{name}.py")
    kb = [e for e in entries if e.startswith("pipeline/kb/")]
    comps["kb"] = sha256_text("\n".join(kb)) if kb else None
    comps["schema"] = sha256_file(PIPELINE_DIR / "schema" / "conference-atlas-2.0.schema.json")
    return "sha256:" + sha256_text("\n".join(entries)), comps


# ------------------------------------------------------------------------------------- projection
def _author(a: dict) -> dict:
    return {k: a.get(k) for k in AUTHOR_KEYS}


def project_presentation(p: dict, tl: list[dict], readout_ids: list[str], notes: dict) -> dict:
    out = {}
    for k in PRESENTATION_KEYS:
        v = p.get(k)
        if k in LIST_KEYS:
            v = list(v or [])
        out[k] = v
    out["authors"] = [_author(a) for a in out["authors"] if isinstance(a, dict)]
    out["disclosures"] = [{"name": d.get("name"), "text": d.get("text")} if isinstance(d, dict)
                          else {"name": None, "text": str(d)} for d in out["disclosures"]]
    extras = sorted(set(p) - set(PRESENTATION_KEYS))
    for e in extras:
        notes["droppedFields"][e] += 1
    emb = out.get("embargo")
    if not isinstance(emb, dict) or "embargoed" not in emb:
        notes["embargoFilled"] += 1
        out["embargo"] = {"embargoed": out.get("abstract") is not None, "liftsAtUtc": EMBARGO_LIFT_UTC}
    else:
        out["embargo"] = {"embargoed": emb.get("embargoed"), "liftsAtUtc": emb.get("liftsAtUtc")}
    tags_sorted = sorted(tl, key=lambda t: (t.get("kind") or "", t.get("entityId") or "", t.get("label") or "",
                                            t.get("role") or ""))
    norm_tags = []
    for t in tags_sorted:
        nt = dict(t)
        nt["evidence"] = norm_evidence(t.get("evidence"))
        for key in ("entityId", "role", "polarity", "method", "confidence"):
            nt.setdefault(key, None)
        nt.pop("presentationId", None)
        norm_tags.append(nt)

    def labels(pred):
        return sorted({t.get("label") for t in norm_tags if pred(t) and t.get("label")})

    out["drug"] = labels(lambda t: t.get("kind") == "drug" and t.get("role") != "mentioned")
    out["developers"] = labels(lambda t: t.get("kind") == "company")
    out["diseases"] = labels(lambda t: t.get("kind") == "disease" and t.get("polarity") != "negative")
    out["modalities"] = labels(lambda t: t.get("kind") == "modality")
    out["targets"] = labels(lambda t: t.get("kind") == "target" and t.get("role") == "drug_target")
    out["tags"] = norm_tags
    out["readoutIds"] = sorted(readout_ids)
    out["rights"] = {"displayFullText": False, "basis": RIGHTS_BASIS}
    return out


def _kb_index() -> dict[str, dict]:
    idx: dict[str, dict] = {}

    def walk(node, key=None):
        if isinstance(node, dict):
            rid = node.get("id") or node.get("entityId")
            if isinstance(rid, str) and ("label" in node or "name" in node or "synonyms" in node):
                idx.setdefault(rid, node)
            for k, v in node.items():
                if isinstance(v, dict) and "id" not in v and ("label" in v or "synonyms" in v):
                    idx.setdefault(k, v)
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)

    for f in sorted((PIPELINE_DIR / "kb").glob("*.json")) if (PIPELINE_DIR / "kb").exists() else []:
        try:
            walk(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return idx


_XREF_RE = re.compile(r"^(hgnc|oncotree|ncit|chembl|nct|unii|cas|drugbank|mesh|umls|uniprot|entrez)", re.I)
_ATTR_KEYS = ("modality", "mechanism", "moa", "targets", "developer", "developers", "inn", "class", "family",
              "group", "parent", "tissue", "phase", "sponsor", "type")


def build_entities(pres: list[dict], readouts: list[dict]) -> dict:
    kb = _kb_index()
    buckets: dict[str, dict[str, dict]] = {b: {} for b in set(ENTITY_BUCKET.values())}
    ro_by_pres: dict[str, set] = defaultdict(set)
    for r in readouts:
        ro_by_pres[r.get("presentationId")].add(r["id"])
    for p in pres:
        for t in p["tags"]:
            b = ENTITY_BUCKET.get(t.get("kind"))
            eid = t.get("entityId")
            if not b or not eid:
                continue
            e = buckets[b].setdefault(eid, {"id": eid, "kind": t["kind"], "label": t.get("label") or eid,
                                            "source": t.get("source") or "curated", "xrefs": {},
                                            "attributes": {}, "presentationIds": set(), "readoutIds": set()})
            e["presentationIds"].add(p["id"])
            e["readoutIds"] |= ro_by_pres.get(p["id"], set())
    for b, ents in buckets.items():
        for eid, e in ents.items():
            rec = kb.get(eid)
            if rec:
                e["source"] = rec.get("source") or e["source"]
                for k, v in rec.items():
                    if _XREF_RE.match(k) and isinstance(v, (str, list)) and v:
                        e["xrefs"][k] = v
                    elif k in ("xrefs", "ids") and isinstance(v, dict):
                        e["xrefs"].update({kk: vv for kk, vv in v.items() if isinstance(vv, (str, list))})
                    elif k in _ATTR_KEYS and v not in (None, "", []):
                        e["attributes"][k] = v
    out = {}
    for b in ("drugs", "targets", "diseases", "companies", "trials"):
        rows = []
        for e in buckets.get(b, {}).values():
            rows.append(dict(e, presentationIds=sorted(e["presentationIds"]), readoutIds=sorted(e["readoutIds"]),
                             count=len(e["presentationIds"]), xrefs=dict(sorted(e["xrefs"].items())),
                             attributes=dict(sorted(e["attributes"].items()))))
        rows.sort(key=lambda x: (-x["count"], x["id"]))
        out[b] = rows
    return out


def build_conference(parsed: dict, facts: dict) -> dict:
    c = dict(CONFERENCE_DEFAULTS)
    pc = (parsed or {}).get("conference") or {}
    for k in ("id", "name", "organization", "start", "end", "timezone", "venue", "city", "country", "url"):
        alias = {"start": ("start", "startDate"), "end": ("end", "endDate"), "url": ("url", "sourceUrl")}.get(k, (k,))
        for a in alias:
            if pc.get(a):
                c[k] = pc[a]
                break
    c["platform"] = "eventscribe"
    urls = set(facts.get("sourceUrls") or []) | set(pc.get("sourceUrls") or [])
    urls.add(c["url"])
    c["sourceUrls"] = sorted(u for u in urls if u)
    return c


def build_activities(activity_log: Any) -> list[dict]:
    log = {a.get("id"): a for a in (activity_log or []) if isinstance(a, dict)}
    out = []
    for stage, (agent, method) in STAGE_AGENTS.items():
        a = log.get(stage, {})
        out.append({"id": stage, "type": a.get("type") or ("transform" if stage != "qa" else "validation"),
                    "startedAt": a.get("startedAt"), "endedAt": a.get("endedAt"), "agent": agent,
                    "method": method})
    return out


def build_coverage(pres: list[dict], readouts: list[dict], facts: dict, parse_report: dict,
                   previous: dict | None) -> dict:
    by_format: dict[str, int] = defaultdict(int)
    for p in pres:
        by_format[p.get("format") or "unknown"] += 1
    pr = parse_report or {}
    delta = None
    if previous and isinstance(previous.get("presentations"), list):
        prev = {p.get("id"): p.get("contentHash") for p in previous["presentations"]}
        cur = {p["id"]: p.get("contentHash") for p in pres}
        delta = {"previousSnapshot": (previous.get("datasetVersion") or {}).get("snapshot"),
                 "previousPipeline": (previous.get("datasetVersion") or {}).get("pipeline"),
                 "added": sorted(set(cur) - set(prev)), "removed": sorted(set(prev) - set(cur)),
                 "changed": sorted(k for k in set(cur) & set(prev) if cur[k] != prev[k])}
    discovered = facts.get("discovered") if facts.get("discovered") is not None else pr.get("discovered")
    captured = facts.get("captured") if facts.get("captured") is not None else pr.get("captured")
    return {
        "discovered": discovered if isinstance(discovered, int) else None,
        "captured": captured if isinstance(captured, int) else None,
        "withAbstract": sum(1 for p in pres if p.get("abstract")),
        "withPoster": sum(1 for p in pres if p.get("format") == "poster"),
        "tagged": sum(1 for p in pres if p.get("tags")),
        "reviewed": sum(1 for r in readouts if (r.get("review") or {}).get("status") in REVIEWED_STATUSES),
        "byFormat": dict(sorted(by_format.items())),
        "deltaSincePrevious": delta,
    }


def build_document(paths: dict | None = None) -> tuple[dict, dict]:
    P = _paths(paths)
    parsed = load_json(P["parsed"], {}) or {}
    tags = load_json(P["tags"], {}) or {}
    raw_readouts = load_json(P["readouts"], []) or []
    bundle = load_json(P["bundle"], None)
    previous = load_json(P["out_json"], None)
    aggregates = load_json(P["aggregates"], None)
    if aggregates is None:
        aggregates = agg.compute(parsed, tags, raw_readouts)
    facts = bundle_facts(bundle)
    tix = tags_index(tags)
    readouts = sorted((normalize_readout(r, i) for i, r in enumerate(readouts_list(raw_readouts))),
                      key=lambda r: r["id"])
    ro_by_pres: dict[str, list] = defaultdict(list)
    for r in readouts:
        ro_by_pres[r.get("presentationId")].append(r["id"])
    notes = {"droppedFields": defaultdict(int), "embargoFilled": 0}
    pres = [project_presentation(p, tix.get(p.get("id"), []), ro_by_pres.get(p.get("id"), []), notes)
            for p in presentations_of(parsed)]
    pres.sort(key=lambda p: str(p["id"]))
    pipe, comps = pipeline_hash()
    comps.update({"input:bundle": sha256_file(P["bundle"]), "input:parsed": sha256_file(P["parsed"]),
                  "input:tags": sha256_file(P["tags"]), "input:readouts": sha256_file(P["readouts"])})
    snapshot = facts["snapshot"] or ((parsed.get("conference") or {}).get("snapshot"))

    def sort_by_id(items):
        return sorted((dict(x) for x in items or [] if isinstance(x, dict)), key=lambda x: str(x.get("id")))

    sessions = sort_by_id(parsed.get("sessions"))
    for s in sessions:
        for k in ("title", "date", "startTime", "endTime", "location"):
            s.setdefault(k, None)
        for k in ("chairs", "moderators", "presentationIds"):
            s[k] = list(s.get(k) or [])
    people = sort_by_id(parsed.get("people"))
    for pp in people:
        pp["institutions"] = list(pp.get("institutions") or [])
        pp["countries"] = list(pp.get("countries") or [])
    doc = {
        "schemaVersion": SCHEMA_VERSION,
        "datasetVersion": {"snapshot": snapshot, "pipeline": pipe, "components": dict(sorted(comps.items()))},
        "conference": build_conference(parsed, facts),
        "embargo": conference_embargo(parsed, facts, P.get("conference_config")),
        "provenance": {"activities": build_activities(load_json(P["activities"], [])),
                       "sources": facts["sources"]},
        "coverage": build_coverage(pres, readouts, facts, parsed.get("parseReport") or {}, previous),
        "presentations": pres,
        "sessions": sessions,
        "people": people,
        "sponsors": sort_by_id(parsed.get("sponsors")),
        "events": sort_by_id(parsed.get("events")),
        "entities": build_entities(pres, readouts),
        "readouts": readouts,
        "aggregates": aggregates.get("aggregates", aggregates),
        "watchlist": aggregates.get("watchlist", []),
        "contextUpdates": aggregates.get("contextUpdates", []),
        "glossary": [{"term": t, "definition": d, "category": c} for t, d, c in sorted(GLOSSARY)],
        "qa": {"schemaValid": False, "checks": [], "unresolvedEntities": [], "pendingReview": [], "knownIssues": []},
    }
    pids = {p["id"] for p in pres}
    ctx = {"listingCounts": facts["listingCounts"], "listingIds": facts["listingIds"],
           "orphanTagKeys": sorted(k for k in tix if k not in pids),
           "missingTagKeys": sorted(pids - set(tix)) if tix else [],
           "orphanReadoutPresentations": sorted({r["presentationId"] for r in readouts
                                                 if r.get("presentationId") not in pids}, key=str),
           "inputMtimes": {k: (Path(P[k]).stat().st_mtime if Path(P[k]).exists() else None)
                           for k in ("parsed", "tags", "readouts")}, "notes": notes, "parseReport": parsed.get("parseReport") or {},
           "previous": previous, "paths": P, "hasBundle": bundle is not None,
           "readoutsPresent": Path(P["readouts"]).exists()}
    return doc, ctx


# ------------------------------------------------------------------------------------- writers
CSV_COLUMNS = ["id", "number", "format", "tip", "title", "track", "sessionTitle", "date", "startTime", "endTime",
               "location", "presenters", "authors", "institutions", "countries", "drugs", "targets", "modalities",
               "diseases", "developers", "trials", "hasAbstract", "embargoed", "readoutIds", "views",
               "slidesAvailable", "url"]


def _csv_rows(doc: dict):
    for p in doc["presentations"]:
        authors = sorted(p["authors"], key=lambda a: (a.get("order") is None, a.get("order") or 0))
        yield {
            "id": p["id"], "number": p.get("number"), "format": p.get("format"), "tip": p.get("tip"),
            "title": p.get("title"), "track": p.get("track"), "sessionTitle": p.get("sessionTitle"),
            "date": p.get("date"), "startTime": p.get("startTime"), "endTime": p.get("endTime"),
            "location": p.get("location"),
            "presenters": "; ".join(a["name"] for a in authors if a.get("role") == "presenter" and a.get("name")),
            "authors": "; ".join(dict.fromkeys(a["name"] for a in authors if a.get("name"))),
            "institutions": "; ".join(sorted({a["institution"] for a in authors if a.get("institution")})),
            "countries": "; ".join(sorted({agg.norm_country(a["country"]) for a in authors if a.get("country")})),
            "drugs": "; ".join(p["drug"]), "targets": "; ".join(p["targets"]),
            "modalities": "; ".join(p["modalities"]), "diseases": "; ".join(p["diseases"]),
            "developers": "; ".join(p["developers"]),
            "trials": "; ".join(sorted({t["label"] for t in p["tags"] if t.get("kind") == "trial"})),
            "hasAbstract": p.get("abstract") is not None, "embargoed": (p.get("embargo") or {}).get("embargoed"),
            "readoutIds": "; ".join(p["readoutIds"]), "views": p.get("views"),
            "slidesAvailable": p.get("slidesAvailable"), "url": p.get("url"),
        }


def write_csv(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for row in _csv_rows(doc):
            w.writerow({k: ("" if v is None else v) for k, v in row.items()})


def write_gzip(src: Path, dst: Path) -> None:
    with src.open("rb") as fi, gzip.GzipFile(filename=src.name, mode="wb", fileobj=dst.open("wb"), mtime=0) as fo:
        shutil.copyfileobj(fi, fo)


def render_report(doc: dict, ctx: dict) -> str:
    cov, qa, agg_ = doc["coverage"], doc["qa"], doc["aggregates"]
    L = [f"# Run report: {doc['conference']['name']}", "",
         f"* Schema: `{doc['schemaVersion']}`; schema valid: **{qa['schemaValid']}**",
         f"* Snapshot: {doc['datasetVersion']['snapshot']}; pipeline: `{doc['datasetVersion']['pipeline'][:23]}...`",
         f"* Content hash (excluding provenance.activities): `{content_hash(doc)[:16]}`", "",
         "## Counts", "", "| block | count |", "|---|---|"]
    for k in ("presentations", "sessions", "people", "sponsors", "events", "readouts", "watchlist", "contextUpdates"):
        L.append(f"| {k} | {len(doc[k])} |")
    for k, v in doc["entities"].items():
        L.append(f"| entities.{k} | {len(v)} |")
    L.append(f"| tags (total) | {sum(len(p['tags']) for p in doc['presentations'])} |")
    L += ["", "## Coverage", "", "| metric | value |", "|---|---|"]
    for k in ("discovered", "captured", "withAbstract", "withPoster", "tagged", "reviewed"):
        L.append(f"| {k} | {cov[k]} |")
    L.append(f"| byFormat | {', '.join(f'{k}: {v}' for k, v in cov['byFormat'].items())} |")
    if ctx.get("listingCounts"):
        L.append(f"| listing counts | {', '.join(f'{k}: {v}' for k, v in ctx['listingCounts'].items())} |")
    hm = agg_["heatmap"]
    L.append(f"| heatmap | {len(hm['rows'])} target groups x {len(hm['cols'])} modality families, "
             f"{hm['coverage']['presentationsInGrid']}/{hm['coverage']['total']} presentations in grid |")
    L.append(f"| themes | {', '.join(f'{t['label']} ({t['count']})' for t in agg_['themes'])} |")
    L += ["", "## QA", "", "| check | failures | description |", "|---|---|---|"]
    for c in qa["checks"]:
        L.append(f"| {c['id']} | {c['failures']} | {c['description']} |")
    L.append("")
    L.append(f"Unresolved entities: {len(qa['unresolvedEntities'])}; readouts pending review: "
             f"{len(qa['pendingReview'])}.")
    if qa["knownIssues"]:
        L += ["", "Known issues:", ""] + [f"* {i}" for i in qa["knownIssues"]]
    failing = [c for c in qa["checks"] if c["failures"]]
    if failing:
        L += ["", "### Examples of failures (first 3 per check)", ""]
        for c in failing:
            L.append(f"* **{c['id']}**: " + "; ".join(json.dumps(e, ensure_ascii=False)[:240]
                                                       for e in c["examples"][:3]))
    L += ["", "## Stage consistency", ""]
    import datetime as _dt
    mt = ctx.get("inputMtimes") or {}
    for k in ("parsed", "tags", "readouts"):
        v = mt.get(k)
        L.append(f"* {k}: " + (_dt.datetime.fromtimestamp(v, _dt.timezone.utc).isoformat(timespec="seconds")
                               if v else "absent"))
    if mt.get("parsed") and mt.get("tags") and mt["tags"] < mt["parsed"]:
        L.append("* WARNING: tags.json is older than parsed.json; rerun the Tagger, then `python run.py aggregate qa export`.")
    if mt.get("parsed") and mt.get("readouts") and mt["readouts"] < mt["parsed"]:
        L.append("* WARNING: readouts.json is older than parsed.json; rerun Readouts, then `python run.py aggregate qa export`.")
    L.append(f"* tags.json keys not in parsed.json: {len(ctx.get('orphanTagKeys') or [])}; parsed presentations "
             f"without a tags.json key: {len(ctx.get('missingTagKeys') or [])}; readouts pointing at unknown "
             f"presentations: {len(ctx.get('orphanReadoutPresentations') or [])}")
    L += ["", "## Diff versus previous export", ""]
    d = cov["deltaSincePrevious"]
    if d is None:
        L.append("No previous export found.")
    else:
        L.append(f"Previous snapshot {d['previousSnapshot']}, pipeline `{str(d['previousPipeline'])[:23]}`: "
                 f"{len(d['added'])} added, {len(d['removed'])} removed, {len(d['changed'])} changed presentations.")
        prev = ctx.get("previous") or {}
        rows = []
        for k in ("presentations", "sessions", "people", "readouts"):
            rows.append(f"| {k} | {len(prev.get(k) or [])} | {len(doc[k])} |")
        L += ["", "| block | previous | current |", "|---|---|---|"] + rows
        pq = {c["id"]: c["failures"] for c in ((prev.get("qa") or {}).get("checks") or [])}
        changes = [f"{c['id']}: {pq.get(c['id'])} -> {c['failures']}" for c in qa["checks"]
                   if pq.get(c["id"]) != c["failures"]]
        if changes:
            L += ["", "QA failure changes: " + "; ".join(changes)]
    return "\n".join(L) + "\n"


def content_hash(doc: dict) -> str:
    d = dict(doc)
    d["provenance"] = dict(d["provenance"], activities=[])
    d["coverage"] = dict(d["coverage"], deltaSincePrevious=None)
    return sha256_bytes(canonical_bytes(d))


def run(paths: dict | None = None) -> dict:
    from pipeline import qa as qa_mod
    doc, ctx = build_document(paths)
    doc["qa"] = qa_mod.compute(doc, ctx)
    P = ctx["paths"]
    if Path(P["out_json"]).exists():
        shutil.copyfile(P["out_json"], P["out_prev"])
    dump_json(doc, P["out_json"], indent=None)
    write_gzip(Path(P["out_json"]), Path(P["out_gz"]))
    write_csv(doc, Path(P["out_csv"]))
    Path(P["report"]).parent.mkdir(parents=True, exist_ok=True)
    Path(P["report"]).write_text(render_report(doc, ctx), encoding="utf-8")
    return doc


if __name__ == "__main__":  # pragma: no cover
    run()
