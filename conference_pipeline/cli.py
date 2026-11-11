"""Command line interface for safe, reproducible CTOS pipeline runs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._version import __version__
from .config import ConferenceConfig, ConfigurationError
from .runner import QualityGateError, run_release, validate_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conference-pipeline", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a conference config and optional raw bundle")
    validate.add_argument("--config", required=True, type=Path)
    validate.add_argument("--bundle", type=Path)

    run = commands.add_parser("run", help="run the CTOS release pipeline into an isolated directory")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--bundle", required=True, type=Path)
    run.add_argument("--run-dir", required=True, type=Path)
    run.add_argument("--readouts-draft", type=Path,
                     help="human-curated readouts draft; omitted means export no readouts")
    run.add_argument("--allow-qa-failures", action="store_true",
                     help="write blocked artefacts but return success even if QA or review gates fail")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ConferenceConfig.load(args.config)
        if args.command == "validate":
            config.assert_supported()
            summary = {"config": str(config.path), "conference": config.conference_id, "platform": config.platform}
            if args.bundle:
                bundle = validate_bundle(args.bundle)
                summary["bundle"] = {"path": str(args.bundle), "listings": len(bundle["listings"]), "details": len(bundle["details"])}
            print(json.dumps(summary, indent=2))
            return 0
        result = run_release(
            config=config,
            bundle_path=args.bundle,
            run_dir=args.run_dir,
            readouts_draft=args.readouts_draft,
            allow_qa_failures=args.allow_qa_failures,
        )
    except (ConfigurationError, QualityGateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "document": str(result.document_path),
        "manifest": str(result.manifest_path),
        "qaFailures": result.qa_failures,
        "pendingReview": list(result.pending_review),
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
