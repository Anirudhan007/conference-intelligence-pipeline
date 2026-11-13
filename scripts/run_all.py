"""Capture (if needed), derive the release rule, and run the full pipeline for several conferences.

Usage:
  python scripts/run_all.py sitc-2026 sabcs-2025 ...            # capture + run
  python scripts/run_all.py --skip-capture asco-2026 ...         # reuse data/raw/<id>.json
Outputs: runs/<id>/out/<id>.json (+ .json.gz, CSV, report, manifest) and runs/summary.json (merged).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conference_pipeline.config import ConferenceConfig  # noqa: E402
from conference_pipeline.runner import capture_bundle, run_release  # noqa: E402


def summarize(cid: str, result, seconds: float) -> dict:
    doc = json.loads(result.document_path.read_text(encoding="utf-8"))
    pres = doc["presentations"]
    kinds: dict[str, int] = {}
    for p in pres:
        for t in p["tags"]:
            kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    return {
        "conference": doc["conference"]["name"], "platform": doc["conference"]["platform"],
        "presentations": len(pres), "withAbstract": doc["coverage"]["withAbstract"],
        "byFormat": doc["coverage"]["byFormat"], "people": len(doc["people"]), "sessions": len(doc["sessions"]),
        "tagged": doc["coverage"]["tagged"], "tagsByKind": dict(sorted(kinds.items())),
        "qaFailures": result.qa_failures, "schemaValid": doc["qa"]["schemaValid"],
        "output": str(result.document_path.relative_to(ROOT)), "seconds": round(seconds, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--skip-capture", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--summary", default="runs/summary.json", help="summary file (one per concurrent job)")
    a = ap.parse_args()
    summary_path = ROOT / a.summary
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    for cid in a.ids:
        cfg_path = ROOT / "configs" / f"{cid}.json"
        bundle = ROOT / "data" / "raw" / f"{cid}.json"
        t0 = time.time()
        try:
            if not (a.skip_capture and bundle.exists()):
                print(f"=== capture {cid}", flush=True)
                capture_bundle(config=ConferenceConfig.load(cfg_path), out_path=bundle,
                               contact=os.getenv("CONTACT_EMAIL"), delay=a.delay)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not cfg.get("embargo", {}).get("rules"):
                subprocess.run([sys.executable, str(ROOT / "scripts" / "derive_embargo_rule.py"), str(cfg_path),
                                str(bundle)], check=True)
            print(f"=== run {cid}", flush=True)
            result = run_release(config=ConferenceConfig.load(cfg_path), bundle_path=bundle,
                                 run_dir=ROOT / "runs" / cid, allow_qa_failures=True)
            summary[cid] = summarize(cid, result, time.time() - t0)
            print(json.dumps(summary[cid]), flush=True)
        except Exception as exc:  # keep going; record the failure for the report
            traceback.print_exc()
            summary[cid] = {"error": repr(exc), "seconds": round(time.time() - t0, 1)}
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(dict(sorted(summary.items())), indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
