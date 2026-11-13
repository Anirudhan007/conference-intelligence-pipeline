"""Command line interface for safe, reproducible CTOS pipeline runs."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ._version import __version__
from .config import ConferenceConfig, ConfigurationError
from .runner import QualityGateError, capture_bundle, run_release, validate_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conference-pipeline", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a conference config and optional raw bundle")
    validate.add_argument("--config", required=True, type=Path)
    validate.add_argument("--bundle", type=Path)

    cap = commands.add_parser("capture", help="capture a raw bundle with the config's registered adapter")
    cap.add_argument("--config", required=True, type=Path)
    cap.add_argument("--out", required=True, type=Path)
    cap.add_argument("--contact", default=None,
                     help="optional contact (email/URL) added to the User-Agent; defaults to CONTACT_EMAIL if set")
    cap.add_argument("--delay", type=float, default=1.5, help="minimum seconds between requests (>= 1.0)")
    cap.add_argument("--limit", type=int, default=None, help="cap captured records (smoke tests)")

    commands.add_parser("adapters", help="list registered adapters and their access basis")

    run = commands.add_parser("run", help="run the release pipeline for one conference into an isolated directory")
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
    if args.command == "adapters":
        from .adapters import ADAPTERS

        print(json.dumps({name: {"platform": cls.platform, "accessBasis": cls.access_basis}
                          for name, cls in sorted(ADAPTERS.items())}, indent=2))
        return 0
    try:
        config = ConferenceConfig.load(args.config)
        if args.command == "validate":
            config.assert_supported()
            summary = {"config": str(config.path), "conference": config.conference_id, "platform": config.platform,
                       "adapter": config.adapter_name}
            if args.bundle:
                bundle = validate_bundle(args.bundle)
                summary["bundle"] = {"path": str(args.bundle), "listings": len(bundle["listings"]), "details": len(bundle["details"])}
            print(json.dumps(summary, indent=2))
            return 0
        if args.command == "capture":
            if args.delay < 1.0:
                raise ConfigurationError("--delay must be at least 1.0 second")
            path = capture_bundle(config=config, out_path=args.out,
                                  contact=args.contact or os.getenv("CONTACT_EMAIL"),
                                  delay=args.delay, limit=args.limit)
            bundle = validate_bundle(path)
            print(json.dumps({"bundle": str(path), "listings": len(bundle["listings"]),
                              "details": len(bundle["details"]), "errors": len(bundle.get("errors") or [])},
                             indent=2))
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
