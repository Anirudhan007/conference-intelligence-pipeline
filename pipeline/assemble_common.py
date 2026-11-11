"""Shared helpers for the Assembler stages (aggregate, qa, export). Owned by Agent Assembler.

Nothing here imports the Parser, Tagger or Readouts modules: the Assembler works from the stage
files only (data/stage/parsed.json, tags.json, readouts.json), so it can be tested in isolation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = ROOT / "pipeline"
SCHEMA_PATH = PIPELINE_DIR / "schema" / "conference-atlas-2.0.schema.json"
SCHEMA_VERSION = "conference-atlas/2.0.0"

DEFAULT_PATHS = {
    "bundle": ROOT / "data" / "raw" / "bundle.json",
    "parsed": ROOT / "data" / "stage" / "parsed.json",
    "tags": ROOT / "data" / "stage" / "tags.json",
    "readouts": ROOT / "data" / "stage" / "readouts.json",
    "aggregates": ROOT / "data" / "stage" / "aggregates.json",
    "qa": ROOT / "data" / "stage" / "qa.json",
    "activities": ROOT / "data" / "stage" / "activities.json",
    "out_json": ROOT / "out" / "ctos-2026.json",
    "out_gz": ROOT / "out" / "ctos-2026.json.gz",
    "out_csv": ROOT / "out" / "ctos-2026_presentations.csv",
    "out_prev": ROOT / "out" / "ctos-2026.previous.json",
    "report": ROOT / "reports" / "run_report.md",
    "conference_config": ROOT / "conferences" / "ctos-2026.json",
}

# Conference-level facts from BRIEF.md. Used only where parsed.json leaves a field empty.
CONFERENCE_DEFAULTS = {
    "id": "ctos-2026",
    "name": "CTOS 2026 Annual Meeting",
    "organization": "Connective Tissue Oncology Society",
    "start": "2026-11-18",
    "end": "2026-11-21",
    "timezone": "Asia/Seoul",
    "venue": None,
    "city": "Seoul",
    "country": "South Korea",
    "platform": "eventscribe",
    "url": "https://ctos2026.eventscribe.net/index.asp?launcher=1",
}
EMBARGO_LIFT_UTC = "2026-11-18T14:00:00Z"


# --------------------------------------------------------------------------------------------- io
def load_json(path: str | os.PathLike | None, default: Any = None) -> Any:
    if path is None:
        return default
    p = Path(path)
    if not p.exists():
        return default
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(obj: Any, path: str | os.PathLike, indent: int | None = 1) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def sha256_file(path: str | os.PathLike) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    return sha256_bytes(p.read_bytes())


# ------------------------------------------------------------------------------------ stage access
def presentations_of(parsed: dict | None) -> list[dict]:
    return list((parsed or {}).get("presentations") or [])


def tags_index(tags: Any) -> dict[str, list[dict]]:
    """tags.json is {presentationId: [Tag]}; tolerate a list of tags carrying presentationId."""
    if not tags:
        return {}
    if isinstance(tags, dict):
        if "tags" in tags and isinstance(tags["tags"], (dict, list)) and len(tags) <= 3:
            return tags_index(tags["tags"])
        return {k: list(v or []) for k, v in tags.items() if isinstance(v, list)}
    out: dict[str, list[dict]] = {}
    for t in tags:
        pid = t.get("presentationId")
        if pid:
            out.setdefault(pid, []).append(t)
    return out


def readouts_list(readouts: Any) -> list[dict]:
    if not readouts:
        return []
    if isinstance(readouts, dict):
        for key in ("readouts", "items", "data"):
            if isinstance(readouts.get(key), list):
                return list(readouts[key])
        return []
    return list(readouts)


def norm_space(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def slug(s: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s or "none"


def resolve_field(obj: dict, field: str | None) -> Any:
    """Resolve a dotted field path ("abstractSections.results", "authors.0.institution")."""
    if not field:
        return None
    cur: Any = obj
    for part in str(field).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and not (isinstance(x, float) and math.isnan(x))


def quantile(sorted_vals: list[float], q: float) -> float | None:
    """Linear interpolation quantile (type 7, numpy default) over pre-sorted values."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    v = sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)
    return round(float(v), 4)


def sorted_unique(items: Iterable[Any]) -> list:
    return sorted({i for i in items if i not in (None, "")})


def counter_rows(counts: dict[str, set], key_name: str = "value", label_map: dict | None = None,
                 limit: int | None = None) -> list[dict]:
    """{value: set(presentationIds)} -> rows sorted by count desc, then value."""
    rows = []
    for k, ids in counts.items():
        if k in (None, ""):
            continue
        row = {key_name: k, "count": len(ids)}
        if label_map is not None:
            row["label"] = label_map.get(k, k)
        rows.append(row)
    rows.sort(key=lambda r: (-r["count"], str(r[key_name])))
    return rows[:limit] if limit else rows
