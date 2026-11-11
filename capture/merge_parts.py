"""Merge chunked browser exports (exportBundle parts) into one raw bundle.

Usage:
    python capture/merge_parts.py part0.json part1.json ... -o data/raw/bundle.json

Accepts plain JSON part files (from a browser download) or harness tool result files.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def load_part(path: str) -> dict:
    with open(path) as fh:
        txt = fh.read()
    try:
        v = json.loads(txt)
    except json.JSONDecodeError:
        v = txt
    if isinstance(v, list) and v and isinstance(v[0], dict) and "text" in v[0]:
        v = v[0]["text"].split("\n\n(captured at origin")[0].strip()
    while isinstance(v, str):
        v = json.loads(v)
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parts", nargs="+")
    ap.add_argument("-o", "--out", default="data/raw/bundle.json")
    a = ap.parse_args()
    bundle = {"source": None, "listings": {}, "details": {}, "errors": [], "rate": None}
    seen_parts, expected = set(), None
    for p in a.parts:
        part = load_part(p)
        seen_parts.add(part["part"])
        expected = part["parts"]
        bundle["source"] = bundle["source"] or part.get("source")
        bundle["listings"].update(part.get("listings") or {})
        bundle["details"].update(part.get("details") or {})
        bundle["errors"] += part.get("errors") or []
        bundle["rate"] = part.get("rate") or bundle["rate"]
    missing = set(range(expected)) - seen_parts
    if missing:
        sys.exit(f"missing parts: {sorted(missing)}")
    for rec in list(bundle["listings"].values()) + list(bundle["details"].values()):
        rec["sha256"] = hashlib.sha256(rec["html"].encode()).hexdigest()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    kinds = {}
    for k in bundle["details"]:
        kinds[k.split(":")[0]] = kinds.get(k.split(":")[0], 0) + 1
    print(f"wrote {a.out}: listings={len(bundle['listings'])} details={len(bundle['details'])} {kinds} errors={len(bundle['errors'])}")


if __name__ == "__main__":
    main()
