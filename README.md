# Conference Intelligence Pipeline

Turn conference programme websites into evidence-linked, structured data: schedules, sessions, speakers, abstracts, sponsors, biomedical tags, clinical readouts, CSV exports, and quality reports.

## The Problem

Scientific and medical conference programmes hold high-signal competitive, clinical, and translational intelligence, yet that data is fragmented and unstructured across incompatible web portals, session calendars, modal detail cards, and free-text abstracts. Manual spreadsheet curation does not scale, drops provenance, and regularly introduces domain errors—such as misclassifying negative biomarker cohorts as drug targets or publishing unverified trial outcomes without exact source quotes.

Conference Intelligence Pipeline replaces ad-hoc scraping and unverified extraction with a deterministic, evidence-linked data pipeline governed by explicit embargo rules and automated release gates.

## Validated Scope

Adapters live in `conference_pipeline/adapters/` and are registered by name; a config names its adapter in `runner.adapter`.

| Adapter | Platform | Shipped configs (`configs/`) | Access basis |
|---|---|---|---|
| `eventscribe-ctos-v1` | EventScribe | `ctos-2026` (original, CTOS-only id) | Public programme pages, robots.txt checked |
| `eventscribe-v1` | EventScribe (any `*.eventscribe.net` meeting) | `ctos-2025`, `sno-2023` | Public programme pages, robots.txt checked |
| `crossref-supplement-v1` | Journal-supplement abstracts via the Crossref REST API | `asco-2026`, `asco-gi-2026`, `aacr-2026`, `sabcs-2025`, `ash-2025`, `astro-2026`, `wclc-2026`, `esmo-2025`, `sitc-2026` | Public scholarly-metadata API; the meeting website is never contacted |

See [Conference Coverage](docs/conference-coverage.md) for per-meeting results, field coverage, and the meetings whose sites forbid automated capture.

```powershell
conference-pipeline adapters                                   # list adapters and their access basis
conference-pipeline capture --config configs/asco-2026.json --out data/raw/asco-2026.json
python scripts/derive_embargo_rule.py configs/asco-2026.json data/raw/asco-2026.json
conference-pipeline run --config configs/asco-2026.json --bundle data/raw/asco-2026.json --run-dir runs/asco-2026
python scripts/run_all.py asco-2026 ash-2025                   # all three steps for several meetings
```

**Platform boundary:** The common pipeline—canonical schema, biomedical dictionary and INN-grammar tagger, biomarker polarity resolution, clinical readout verifier, aggregates, QA rules, exports, and cryptographic run manifests—is reusable across conferences, **but each different website platform requires a tested adapter**. Changing a URL alone is not sufficient for a new conference layout, and the CLI deliberately fails closed rather than silently applying CTOS/EventScribe parser assumptions to an unvalidated site.

## Architecture

```text
Conference URL -> Adapter/Capture -> Raw Bundle -> Parser -> Tagging -> Readout Review -> Aggregates -> QA Gate -> JSON/CSV/Report/Manifest
```

| Stage | Responsibility |
|---|---|
| **Adapter / Capture** | Polite, single-threaded public-page collection (`robots.txt` check, rate limiting, `429` backoff, attributable `User-Agent`) producing an immutable `bundle.json` with SHA-256 hashes. |
| **Parser** | Deterministic mapping from platform HTML listings and detail cards into canonical presentations, sessions, people, sponsors, events, disclosures, and embargo metadata. |
| **Tagging** | Curated oncology dictionary + INN drug grammar + biomarker polarity tagger where every tag carries exact character offsets and verbatim source quotes. |
| **Readout Review** | Candidate ranking and deterministic numeric/quote verification for human-curated clinical trial readouts. |
| **Aggregates** | Target-by-modality heatmaps, disease hierarchy rollups, conference themes, and speaker/country facets. |
| **QA Gate & Export** | JSON Schema (`2020-12`) validation, referential integrity, numeric consistency, and review-status enforcement before releasing JSON, `.json.gz`, CSV, `run_report.md`, and `run-manifest.json`. |

## Installation (Windows PowerShell)

Use Python 3.10+ (CI tests Python 3.11, 3.12, and 3.13):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Run CTOS

Validate the conference configuration (and optional raw bundle):

```powershell
conference-pipeline validate --config configs/ctos-2026.json
```

Execute an isolated release candidate run once an authorized raw bundle is staged locally at `data/raw/bundle.json`:

```powershell
conference-pipeline run --config configs/ctos-2026.json --bundle data/raw/bundle.json --run-dir runs/ctos-run-01
```

If human-curated clinical readouts are included, pass `--readouts-draft data/stage/readouts_draft.json`. Each run writes isolated artefacts under `runs/ctos-run-01/` (`stage/`, `out/`, `reports/run_report.md`, and `run-manifest.json`) and exits non-zero if any QA check fails or any readout remains unreviewed.

### Optional Controlled Capture

Capture only public pages where site terms, `robots.txt`, and embargo rules permit access, using a real contact email in your local `.env` (never committed):

```powershell
$env:CONTACT_EMAIL = "research@example.org"
python capture/capture.py "https://ctos2026.eventscribe.net/index.asp?launcher=1" `
  --contact-email $env:CONTACT_EMAIL --delay 1.5 -o data/raw/bundle.json
```

## Privacy and Data Governance

**Raw CTOS data is intentionally excluded from this repository.** Raw HTML bundles, abstract text, speaker contact details, real HTML fixtures, gold-label corpora, draft clinical readouts, and generated JSON/CSV exports may be subject to conference embargo policies and data-governance controls. All automated tests in this repository run offline against **100% synthetic fixtures** under `tests/fixtures_synthetic/`.

## Testing and Quality Checks

Run the full offline test suite and linter:

```powershell
python -m pytest -q
python -m ruff check .
```

Build source and wheel distributions:

```powershell
python -m build
```

## Documentation

- [CTOS 2026 Case Study (Aggregate & Governance Summary)](docs/ctos-case-study.md)
- [Adapter Guide: Supporting a New Conference Website](docs/adapter-guide.md)
- [Data Governance, Embargo & Traceability Policy](docs/data-governance.md)
- [Founder Product Demo Script (3-Minute Flow)](docs/founder-demo.md)
- [Production Architecture](docs/architecture.md)
- [Engineering & Release Audit (2026-11-11)](docs/audit-2026-11-11.md)
- [GitHub Deployment Checklist](docs/github-deployment.md)
- [Canonical Stage Brief](BRIEF.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Security & Responsible Capture](SECURITY.md)
