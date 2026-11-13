"""Generates conference-atlas-2.0.schema.json (JSON Schema 2020-12). Owned by Agent Assembler.

The schema is kept as Python so shared fragments (nullable types, evidence, arms) are defined once.
Run: python -m pipeline.schema.build_schema
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("conference-atlas-2.0.schema.json")


def N(t):  # nullable
    if isinstance(t, str):
        return {"type": [t, "null"]}
    if "$ref" in t:
        return {"anyOf": [t, {"type": "null"}]}
    t = dict(t)
    ty = t.get("type")
    t["type"] = [ty, "null"] if isinstance(ty, str) else list(ty) + ["null"]
    if "enum" in t:
        t["enum"] = list(t["enum"]) + [None]
    return t


def obj(props: dict, required: list | None = None, extra: bool = False, **kw):
    o = {"type": "object", "properties": props, "required": list(props) if required is None else required,
         "additionalProperties": extra}
    o.update(kw)
    return o


def arr(items, **kw):
    a = {"type": "array", "items": items}
    a.update(kw)
    return a


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


STR = {"type": "string"}
NSTR = N("string")
INT0 = {"type": "integer", "minimum": 0}
NINT0 = N(INT0)
NUM = {"type": "number"}
NNUM = N("number")
BOOL = {"type": "boolean"}
DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
NDATE = N(DATE)
UTC = {"type": "string", "format": "date-time", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$"}
NUTC = N(UTC)
SHA = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
PID = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]*:[A-Za-z]+:[^\s]+$"}
IDS = arr(STR, uniqueItems=True)
PIDS = arr(PID, uniqueItems=True)
STRS = arr(STR)

defs = {}

defs["evidence"] = obj({
    "field": NSTR,
    "charStart": NINT0,
    "charEnd": NINT0,
    "quote": {"type": "string", "minLength": 1},
}, required=["field", "quote"], extra=False, description="Exact substring of the referenced source field.")

defs["ci"] = N(arr(NNUM, minItems=2, maxItems=2))
defs["pValue"] = {"anyOf": [{"type": "number", "minimum": 0, "maximum": 1},
                            {"type": "string", "pattern": r"^[<>≤≥]=?\s*0?\.\d+$"}, {"type": "null"}]}

defs["arm"] = obj({
    "label": NSTR, "role": NSTR, "n": NINT0,
    "value": {"type": ["number", "string", "null"]},
    "ci": ref("ci"), "ciLevel": NNUM, "count": NINT0, "denominator": NINT0,
    "evidence": arr(ref("evidence")),
})

defs["hr"] = obj({"value": NNUM, "ci": ref("ci"), "level": NNUM, "p": ref("pValue")})

defs["metric"] = obj({
    "key": STR, "label": NSTR, "unit": NSTR, "endpointType": NSTR, "population": NSTR, "status": NSTR,
    "hr": N({"$ref": "#/$defs/hr"}), "reportedValue": NSTR, "arms": arr(ref("arm")),
})

defs["safety"] = obj({
    "key": STR, "label": NSTR, "unit": NSTR, "gradeRange": NSTR, "arms": arr(ref("arm")), "note": NSTR,
})

defs["review"] = obj({"status": STR, "by": NSTR, "at": NSTR})

defs["readout"] = obj({
    "id": STR, "code": NSTR, "presentationId": N(PID), "drug": NSTR, "trial": NSTR, "targets": STRS,
    "modality": NSTR, "disease": NSTR, "setting": NSTR, "phase": NSTR, "design": NSTR, "studyN": NINT0,
    "cutoff": NSTR, "followUp": NSTR, "analysis": NSTR, "primaryEndpoint": NSTR, "readoutType": NSTR,
    "metrics": arr(ref("metric")), "safety": arr(ref("safety")), "limitation": NSTR,
    "evidence": arr(ref("evidence")), "review": ref("review"),
})

TAG_KINDS = ["drug", "target", "modality", "disease", "company", "trial", "histology"]
POLARITY = ["positive", "negative", "low", "mutant", "wild_type", "amplified", "fusion", "unknown"]
defs["tag"] = obj({
    "kind": {"enum": TAG_KINDS},
    "entityId": NSTR,
    "label": STR,
    "role": NSTR,
    "polarity": N({"type": "string", "enum": POLARITY}),
    "evidence": arr(ref("evidence"), minItems=1),
    "method": NSTR,
    "confidence": N({"type": "number", "minimum": 0, "maximum": 1}),
}, extra=True,
    allOf=[
        {"if": {"properties": {"kind": {"const": "target"}}},
         "then": {"properties": {"role": {"enum": ["drug_target", "selection_biomarker", "biomarker_status",
                                                   "mentioned"]}}}},
        {"if": {"properties": {"kind": {"const": "drug"}}},
         "then": {"properties": {"role": {"enum": ["investigational", "backbone", "control", "mentioned"]}}}},
    ])

defs["author"] = obj({
    "personId": NSTR, "name": STR, "degrees": {"type": ["array", "string", "null"], "items": STR}, "pronouns": NSTR,
    "role": {"enum": ["author", "coauthor", "presenter", "chair", "moderator", "speaker"]},
    "position": NSTR, "institution": NSTR, "city": NSTR, "region": NSTR, "country": NSTR, "order": NINT0,
})

defs["embargoFlag"] = obj({"embargoed": BOOL, "liftsAtUtc": NUTC})
defs["rights"] = obj({"displayFullText": {"const": False}, "basis": STR})

defs["presentation"] = obj({
    "id": PID, "number": NSTR, "title": STR, "titleNormalized": NSTR,
    "format": {"enum": ["poster", "oral", "lecture", "other"]},
    "tip": BOOL, "track": NSTR, "sessionId": NSTR, "sessionTitle": NSTR, "date": NDATE,
    "startTime": NSTR, "endTime": NSTR, "timezone": {"type": "string", "pattern": "^[A-Za-z]+(?:/[A-Za-z0-9_+-]+)+$|^UTC$"}, "location": NSTR,
    "authors": arr(ref("author")),
    "abstract": NSTR,
    "abstractSections": N({"type": "object", "additionalProperties": NSTR}),
    "disclosures": arr(obj({"name": NSTR, "text": NSTR})),
    "slidesAvailable": NINT0, "views": NINT0, "url": NSTR,
    "sourceKind": STR, "sourceId": STR, "contentHash": NSTR,
    "embargo": ref("embargoFlag"),
    # legacy flat arrays (derived from tags) for v1 consumers
    "drug": STRS, "developers": STRS, "diseases": STRS, "modalities": STRS, "targets": STRS,
    "tags": arr(ref("tag")),
    "readoutIds": IDS,
    "rights": ref("rights"),
})

defs["session"] = obj({
    "id": STR, "title": NSTR, "date": NDATE, "startTime": NSTR, "endTime": NSTR, "location": NSTR,
    "chairs": arr({"type": ["string", "object"]}), "moderators": arr({"type": ["string", "object"]}),
    "presentationIds": arr(STR),
}, extra=True)

defs["person"] = obj({"id": STR, "name": STR, "institutions": STRS, "countries": STRS}, extra=True)
defs["sponsor"] = {"type": "object", "required": ["id"], "properties": {"id": STR, "name": NSTR},
                   "additionalProperties": True}
defs["event"] = {"type": "object", "required": ["id"], "properties": {"id": STR, "title": NSTR, "date": NDATE},
                 "additionalProperties": True}

defs["entity"] = obj({
    "id": STR, "kind": {"enum": TAG_KINDS}, "label": STR, "source": STR,
    "xrefs": {"type": "object", "additionalProperties": {"type": ["string", "array", "null"]}},
    "attributes": {"type": "object"},
    "presentationIds": IDS, "readoutIds": IDS, "count": INT0,
})

defs["countRow"] = obj({"value": STR, "label": NSTR, "count": INT0}, required=["value", "count"])
defs["axis"] = obj({"key": STR, "label": STR, "count": INT0})

defs["heatmap"] = obj({
    "rows": arr(ref("axis")), "cols": arr(ref("axis")),
    "cells": arr(obj({"row": STR, "col": STR, "count": {"type": "integer", "minimum": 1}, "presentationIds": PIDS})),
    "scale": obj({"max": NINT0, "p95": NNUM, "breaks": arr(NUM)}),
    "roleFilter": {"const": "drug_target"},
    "coverage": obj({"presentationsInGrid": INT0, "total": INT0}),
})

defs["theme"] = obj({
    "id": STR, "label": STR,
    "definition": obj({"templateSet": {"enum": ["pan_oncology", "sarcoma", "derived"]}, "parent": NSTR,
                       "query": {"type": "object"}, "text": STR, "minSize": NINT0, "foldedFrom": IDS,
                       "activation": {"type": "object"}},
                      required=["templateSet", "parent", "query", "text", "minSize", "foldedFrom"]),
    "presentationIds": PIDS, "readoutIds": IDS, "count": INT0,
})

defs["aggregates"] = obj({
    "heatmap": ref("heatmap"),
    "designs": arr(obj({"design": NSTR, "phase": NSTR, "readoutType": NSTR, "count": INT0, "readoutIds": IDS})),
    "themes": arr(ref("theme")),
    "targetMap": arr(obj({"targetId": STR, "label": STR, "group": STR,
                          "drugs": arr(obj({"entityId": STR, "label": STR})), "modalityFamilies": STRS,
                          "presentationIds": PIDS, "readoutIds": IDS, "count": INT0})),
    "facets": obj({k: arr(ref("countRow")) for k in
                   ["format", "track", "session", "date", "disease", "modality", "target", "country", "institution"]}),
    "overview": obj({
        "talksPerDay": arr(obj({"date": DATE, "count": INT0,
                                "byFormat": {"type": "object", "additionalProperties": INT0}})),
        "postersPerTrack": arr(obj({"track": STR, "count": INT0})),
        "sessionGroups": arr(obj({"group": STR, "count": INT0, "sessionIds": IDS})),
        "diseaseCounts": arr(obj({"entityId": STR, "label": STR, "count": INT0, "sarcoma": BOOL})),
        "topInstitutions": arr(obj({"name": STR, "count": INT0})),
        "topCountries": arr(obj({"name": STR, "count": INT0})),
    }),
})

defs["qaCheck"] = obj({"id": STR, "description": STR, "failures": INT0, "examples": arr({})})

schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://conferenceatlas.example/schema/conference-atlas-2.0.schema.json",
    "title": "Conference Atlas data layer v2",
    "description": "One conference snapshot: presentations, sessions, people, sponsors, normalized tags with "
                   "evidence, clinical readouts, aggregates and QA. Missing values are null, never zero.",
    "type": "object",
    "additionalProperties": False,
    "required": ["schemaVersion", "datasetVersion", "conference", "embargo", "provenance", "coverage",
                 "presentations", "sessions", "people", "sponsors", "events", "entities", "readouts",
                 "aggregates", "watchlist", "contextUpdates", "glossary", "qa"],
    "properties": {
        "schemaVersion": {"const": "conference-atlas/2.0.0"},
        "datasetVersion": obj({"snapshot": NUTC, "pipeline": STR,
                               "components": {"type": "object", "additionalProperties": NSTR}}),
        "conference": obj({
            "id": STR, "name": STR, "organization": NSTR, "start": DATE, "end": DATE, "timezone": STR,
            "venue": NSTR, "city": NSTR, "country": NSTR, "platform": {"enum": ["eventscribe", "crossref"]},
            "url": STR, "sourceUrls": arr(STR, uniqueItems=True)}),
        "embargo": obj({"rules": arr(obj({"class": STR, "releaseUtc": UTC, "sourceQuote": NSTR,
                                          "sourceUrl": NSTR, "quoteType": {"enum": ["verbatim", "derived"]},
                                          "note": NSTR},
                                         required=["class", "releaseUtc", "sourceQuote", "sourceUrl"]), minItems=1),
                        "verifiedAt": NSTR}, required=["rules"]),
        "provenance": obj({
            "activities": arr(obj({"id": STR, "type": STR, "startedAt": NUTC, "endedAt": NUTC, "agent": STR,
                                   "method": NSTR})),
            "sources": arr(obj({"id": STR, "url": NSTR, "retrievedAt": NUTC, "bytes": NINT0, "sha256": N(SHA)})),
        }),
        "coverage": obj({
            "discovered": NINT0, "captured": NINT0, "withAbstract": INT0, "withPoster": INT0, "tagged": INT0,
            "reviewed": INT0, "byFormat": {"type": "object", "additionalProperties": INT0},
            "deltaSincePrevious": N(obj({"previousSnapshot": NSTR, "previousPipeline": NSTR,
                                         "added": PIDS, "removed": PIDS, "changed": PIDS})),
        }),
        "presentations": arr(ref("presentation")),
        "sessions": arr(ref("session")),
        "people": arr(ref("person")),
        "sponsors": arr(ref("sponsor")),
        "events": arr(ref("event")),
        "entities": obj({k: arr(ref("entity")) for k in ["drugs", "targets", "diseases", "companies", "trials"]}),
        "readouts": arr(ref("readout")),
        "aggregates": ref("aggregates"),
        "watchlist": arr(obj({"id": STR, "kind": STR, "entityId": STR, "label": STR, "reason": STR,
                              "presentationIds": PIDS, "readoutIds": IDS})),
        "contextUpdates": arr(obj({"id": STR, "kind": STR, "entityId": NSTR, "summary": STR,
                                   "presentationId": N(PID), "readoutId": NSTR, "date": NDATE,
                                   "reviewStatus": NSTR})),
        "glossary": arr(obj({"term": STR, "definition": STR, "category": STR})),
        "qa": obj({
            "schemaValid": BOOL,
            "checks": arr(ref("qaCheck")),
            "unresolvedEntities": arr(obj({"presentationId": STR, "kind": STR, "label": NSTR, "reason": STR})),
            "pendingReview": IDS,
            "knownIssues": STRS,
        }),
    },
    "$defs": defs,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(schema, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
