#!/usr/bin/env python3
"""Legacy stage CLI for development and diagnostics.

For a production release, use ``conference-pipeline run``. It isolates each
run, records an input/output manifest, and requires the QA/review gate to pass.

    python run.py parse      [--bundle data/raw/bundle.json] [--parsed data/stage/parsed.json]
    python run.py tag        [--parsed ...] [--tags data/stage/tags.json]
    python run.py readouts   [--parsed ...] [--tags ...] [--readouts data/stage/readouts.json]
    python run.py aggregate  [--parsed ...] [--tags ...] [--readouts ...] [--aggregates data/stage/aggregates.json]
    python run.py qa         (writes data/stage/qa.json and prints failing checks)
    python run.py export     (writes out/ctos-2026.json, .json.gz, _presentations.csv, reports/run_report.md)
    python run.py all        [--continue-on-missing]
    python run.py aggregate qa export      (several stages in one call, in the order given)

Stage contracts (BRIEF.md):
    pipeline.parse.run(bundle_path, out_path)
    pipeline.tag.run_files(parsed_path, out_path)
    pipeline.readouts.run(parsed_path, tags_path, out_path)
A missing module or function stops with a clear error (exit code 2). With `all --continue-on-missing`,
a missing readouts stage is skipped and readouts are treated as an empty list.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import inspect
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.assemble_common import DEFAULT_PATHS, dump_json, load_json  # noqa: E402

CONTRACTS = {
    "parse": ("pipeline.parse", "run", ["bundle_path", "out_path"], "Agent Parser"),
    "tag": ("pipeline.tag", "run_files", ["parsed_path", "out_path"], "Agent Tagger"),
    "readouts": ("pipeline.readouts", "run", ["parsed_path", "tags_path", "out_path"], "Agent Readouts"),
}


COMMANDS = ("parse", "tag", "readouts", "aggregate", "qa", "export", "all")


class StageUnavailable(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_stage(stage: str):
    mod_name, fn_name, params, owner = CONTRACTS[stage]
    file = ROOT / (mod_name.replace(".", "/") + ".py")
    if not file.exists():
        raise StageUnavailable(f"stage '{stage}': module {file.relative_to(ROOT)} not found "
                               f"(owned by {owner}; expected {mod_name}.{fn_name}({', '.join(params)}))")
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:  # noqa: BLE001
        raise StageUnavailable(f"stage '{stage}': importing {mod_name} failed: {type(e).__name__}: {e}") from e
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        hint = f"; it has main(argv), so `python -m {mod_name}` works standalone" if hasattr(mod, "main") else ""
        raise StageUnavailable(f"stage '{stage}': {mod_name} has no function {fn_name}({', '.join(params)}) "
                               f"(owned by {owner}){hint}")
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn
    positional = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)]
    problem = None
    if not any(p.kind == p.VAR_POSITIONAL for p in positional) and len(positional) < len(params):
        problem = "takes fewer positional arguments than the contract"
    else:
        for want, got in zip(params, positional):
            ann = got.annotation if isinstance(got.annotation, str) else getattr(got.annotation, "__name__",
                                                                                  str(got.annotation))
            if got.kind == got.VAR_POSITIONAL:
                break
            if re.search(r"\b(dict|list|Mapping)\b", str(ann)) or got.name == want.replace("_path", ""):
                problem = f"parameter '{got.name}: {ann}' looks like an object, contract expects a path '{want}'"
                break
    if problem:
        hint = " (module has main(argv): `python -m " + mod_name + "` works standalone)" if hasattr(mod, "main") else ""
        raise StageUnavailable(f"stage '{stage}': {mod_name}.{fn_name}{sig} does not match the BRIEF contract "
                               f"{fn_name}({', '.join(params)}): {problem}{hint}; owned by {owner}")
    return fn


def record(stage: str, started: str, activities_path: Path, kind: str = "transform") -> None:
    log = load_json(activities_path, []) or []
    log = [a for a in log if a.get("id") != stage]
    log.append({"id": stage, "type": kind, "startedAt": started, "endedAt": _now()})
    log.sort(key=lambda a: a["id"])
    dump_json(log, activities_path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="+", metavar="command",
                    help="one or more of: " + ", ".join(COMMANDS) + " (run in the order given)")
    for key in ("bundle", "parsed", "tags", "readouts", "aggregates", "qa"):
        ap.add_argument(f"--{key}", type=Path, default=DEFAULT_PATHS[key])
    ap.add_argument("--out", type=Path, default=DEFAULT_PATHS["out_json"], help="export JSON path")
    ap.add_argument("--continue-on-missing", action="store_true",
                    help="with `all`: skip a missing readouts stage (readouts = [])")
    ap.add_argument("--allow-qa-failures", action="store_true",
                    help="allow an export command to return success despite failed QA or pending readout review")
    a = ap.parse_args(argv)
    bad = [c for c in a.command if c not in COMMANDS]
    if bad:
        ap.error(f"unknown command(s) {bad}; choose from {list(COMMANDS)}")
    out = a.out
    paths = {"bundle": a.bundle, "parsed": a.parsed, "tags": a.tags, "readouts": a.readouts,
             "aggregates": a.aggregates, "qa": a.qa, "out_json": out,
             "out_gz": out.with_name(out.name + ".gz"),
             "out_csv": out.with_name(out.stem + "_presentations.csv"),
             "out_prev": out.with_name(out.stem + ".previous.json")}
    acts = DEFAULT_PATHS["activities"] if a.parsed == DEFAULT_PATHS["parsed"] else a.parsed.with_name("activities.json")

    def need(path: Path, what: str):
        if not Path(path).exists():
            raise StageUnavailable(f"input {what} not found at {path}; run the upstream stage first")

    released_doc = None

    def do(stage: str) -> None:
        nonlocal released_doc
        t0, started = time.time(), _now()
        if stage == "parse":
            need(a.bundle, "raw bundle")
            load_stage("parse")(str(a.bundle), str(a.parsed))
        elif stage == "tag":
            need(a.parsed, "parsed.json")
            load_stage("tag")(str(a.parsed), str(a.tags))
        elif stage == "readouts":
            need(a.parsed, "parsed.json")
            need(a.tags, "tags.json")
            load_stage("readouts")(str(a.parsed), str(a.tags), str(a.readouts))
        elif stage == "aggregate":
            need(a.parsed, "parsed.json")
            need(a.tags, "tags.json")
            from pipeline import aggregate
            aggregate.run(a.parsed, a.tags, a.readouts, a.aggregates)
        elif stage == "qa":
            need(a.parsed, "parsed.json")
            from pipeline import qa
            res = qa.run(paths, a.qa)
            failing = {c["id"]: c["failures"] for c in res["checks"] if c["failures"]}
            print(json.dumps({"schemaValid": res["schemaValid"], "failingChecks": failing}, indent=1))
        elif stage == "export":
            need(a.parsed, "parsed.json")
            from pipeline import export
            record("export", started, acts, "transform")  # so the export carries its own activity
            doc = export.run(paths)
            released_doc = doc
            print(f"wrote {out} ({len(doc['presentations'])} presentations, {len(doc['readouts'])} readouts, "
                  f"schemaValid={doc['qa']['schemaValid']})")
        record(stage, started, acts, "validation" if stage == "qa" else "transform")
        print(f"[{stage}] done in {time.time() - t0:.1f}s", file=sys.stderr)

    try:
        for command in a.command:
            if command == "all":
                for stage in ("parse", "tag", "readouts", "aggregate", "qa", "export"):
                    try:
                        do(stage)
                    except StageUnavailable as e:
                        if stage == "readouts" and a.continue_on_missing:
                            print(f"warning: {e}; continuing with readouts = []", file=sys.stderr)
                            continue
                        raise
            else:
                do(command)
        if released_doc is not None and not a.allow_qa_failures:
            failures = {c["id"]: c["failures"] for c in released_doc["qa"]["checks"] if c["failures"]}
            pending = released_doc["qa"]["pendingReview"]
            if failures or pending:
                detail = ", ".join([f"{key}={value}" for key, value in failures.items()] +
                                     ([f"pending_review={len(pending)}"] if pending else []))
                print(f"error: export blocked by quality gate: {detail}; use --allow-qa-failures for triage only",
                      file=sys.stderr)
                return 3
    except StageUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
