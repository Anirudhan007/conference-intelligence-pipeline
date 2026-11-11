# Data Governance, Embargo, and Traceability Policy

Conference Intelligence Pipeline processes scientific programme schedules, speaker affiliations, abstracts, and clinical trial summaries. Strict data governance ensures compliance with conference organizer policies, embargo windows, copyright, and research integrity standards.

## 1. Repository Exclusion Rules (Never Commit)

The following artefacts must **never** be committed to Git, attached to issues or pull requests, or included in build distributions:

- **Raw bundles** (`data/raw/bundle.json`, chunked browser exports, raw HTML snapshots).
- **Embargoed abstracts** or verbatim abstract excerpts from real conferences (`fixtures/`, `data/stage/`, `reports/`).
- **Real clinical readout drafts** (`data/stage/readouts_draft.json`, `work/`) or real-corpus gold annotations (`data/gold/`).
- **Generated datasets and reports** (`out/`, `runs/`, `*.json.gz`, `*.csv`, `run_report.md`).
- **Secrets, tokens, credentials, and real contact addresses** (`.env`, API keys, session cookies, personal email addresses).

All unit, integration, and release-gate tests committed to version control must execute exclusively against synthetic fixtures in `tests/fixtures_synthetic/`.

## 2. Public Code vs. Private Data Separation

This repository publishes **software, schemas, curated ontologies, and 100% synthetic test fixtures only**:

- Source code, deterministic QA gates, and synthetic contract tests may be hosted publicly because zero raw conference content or embargoed material is committed.
- Raw bundles, real HTML fixtures, gold annotations, draft clinical readouts, and candidate exports **must remain in access-controlled private storage outside Git**—never in public repositories, public object buckets, issues, pull requests, or GitHub Releases—until formal legal, licensing, and embargo clearance is documented in writing.
- Branch protection on `main` should require passing CI checks and code review before merge so no contributor can inadvertently check in captured source data.

## 3. Config-Driven, Verified Embargo Enforcement

Embargo handling is never hardcoded silently or left to operator memory:

- Every conference configuration (`configs/<conference-id>.json`) must declare explicit `embargo.rules` entries containing:
  - `class` (for example `delegate_access` and `public`),
  - `releaseUtc` (ISO-8601 UTC timestamp),
  - `sourceQuote` (verbatim rule from official conference documentation), and
  - `sourceUrl` (authoritative policy URL).
- `ConferenceConfig.load()` rejects any configuration whose embargo rules lack provenance fields.
- During release assembly, every presentation carrying abstract text is marked with `embargo.embargoed = true` and `embargo.liftsAtUtc`, and `rights.displayFullText` defaults to `false` until cleared.
- Automated QA (`embargo_flag`) verifies that no presentation with abstract text omits embargo metadata.

## 4. Mandatory Source Evidence Linking

No entity tag or clinical metric is trusted without verifiable provenance:

- **Tags:** Every disease, drug, drug target, modality, company, histology, and trial registry tag must carry one or more `evidence` spans (`field`, `charStart`, `charEnd`, `quote`) where `quote` is an exact character-level substring of the source field.
- **Clinical readouts:** Every study `N`, arm `n`, numerator `count`, `denominator`, point estimate `value`, confidence interval bound, hazard ratio, and cutoff date must appear in its linked verbatim `evidence` quote.
- **No zero imputation:** Missing values are represented strictly as `null`, never `0`. Any numeric `0` must be backed by explicit text evidence (`no_zero_imputation` QA rule).

## 5. Quality Gate Before Publication

Schema validity (`schemaValid: true`) proves structural conformance only; it **does not** authorize release. `conference-pipeline run` enforces a hard release gate (`QualityGateError`, non-zero exit) whenever:

1. Any automated QA check reports `failures > 0` (schema, referential integrity, coverage against raw listings, exact quote substring/offset checks, biomarker-polarity rules, or numeric clinical consistency checks such as CI bounds and `count / denominator` agreement).
2. Any clinical readout has `review.status != "auto_verified"` and lacks signed-off human review (`pendingReview` non-empty).

The `--allow-qa-failures` flag exists strictly to materialize blocked artefacts into an isolated run directory for engineering/clinical triage. Blocked candidates must never be distributed externally.

## 6. Cryptographic Traceability and Run Manifests

Every execution of `conference-pipeline run` writes an isolated `runs/<run-id>/run-manifest.json` recording:

- Pipeline package version and UTC creation timestamp.
- Path and `SHA-256` digest of the validated conference configuration (`configs/*.json`).
- Path and `SHA-256` digest of the immutable input `rawBundle`.
- Paths and `SHA-256` digests of every emitted artefact (`out_json`, `out_gz`, `out_csv`, `report`).
- Complete `quality` disposition (`schemaValid`, `failingChecks`, `pendingReview`).

Archive `run-manifest.json` alongside every internal evaluation or approved release so any number, tag, or export row can be traced back to the exact raw bundle byte-for-byte.
