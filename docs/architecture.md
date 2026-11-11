# Production Architecture

## Repository Shape

Use an installable **Python package with a CLI**, not a single ad-hoc script and not a generic scraper that guesses its way through unknown sites. The package owns the common enrichment, verification, QA, and export stages; versioned adapters own platform-specific discovery, capture, and HTML parsing.

```text
conference-intelligence-pipeline/
  conference_pipeline/         # public CLI, config validation, isolated runner, manifest & release gate
  capture/                     # EventScribe polite HTTP collector + controlled browser harvest fallback
  pipeline/                    # canonical stages: parse, tag, readouts, aggregate, qa, export
    kb/                        # curated drugs, targets, diseases, companies, polarity patterns
    schema/                    # Conference Atlas 2.0 JSON Schema (Draft 2020-12)
  configs/                     # validated conference + embargo configurations
  conferences/                 # conference embargo metadata records
  tests/
    fixtures_synthetic/        # 100% synthetic EventScribe HTML, readout, and gold fixtures
    test_*.py                  # unit, contract, QA, and release-gate suites
  docs/                        # architecture, governance, case study, adapter guide, audit, demo
  .github/workflows/           # multi-version Python CI (ruff, pytest, sdist/wheel build)
```

The repository preserves the battle-tested `pipeline/` transformation modules behind the installable `conference_pipeline` release orchestration layer. A future compatibility-preserving refactor can move platform-specific modules under `adapters/<platform>/` once a second conference platform is onboarded (see [Adapter Guide](adapter-guide.md)).

## Adapter Protocol

Every conference platform adapter owns four responsibilities:

1. **Platform identification** (`fingerprint` / `assert_supported`) from explicit configuration and stable platform cues—failing closed on unknown layouts.
2. **Responsible capture** (`RawBundle`), with attribution (`CONTACT_EMAIL`), `robots.txt` and site-terms checks, single-request pacing, `429` retry/backoff logs, per-record `SHA-256` hashes, and zero authenticated scraping.
3. **Deterministic parsing** (`ParsedConference`), mapping platform listings and detail cards into canonical presentations, sessions, people, sponsors, events, disclosures, and embargo fields while preserving exact source text.
4. **Synthetic contract fixtures** under `tests/fixtures_synthetic/` proving inventory counts, URL normalization, missing-value (`null`, never `0`) semantics, and deterministic replayability.

Downstream stages (`tag`, `readouts`, `aggregate`, `qa`, `export`) must remain platform-agnostic. They consume canonical `ParsedConference` records and emit only evidence-backed outputs.

## Release Flow

```text
Conference URL -> Adapter/Capture -> Raw Bundle -> Parser -> Tagging -> Readout Review
              -> Aggregates -> QA Gate -> JSON/CSV/Report/Manifest
```

Store raw bundles and release outputs in access-controlled object storage outside Git with versioning and retention rules (see [Data Governance](data-governance.md)). Record a `SHA-256` digest for the config, raw bundle, and every emitted artefact in `run-manifest.json`. Publish only datasets that clear both automated QA and the human-review/embargo gate.
