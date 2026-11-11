# CTOS 2026 Case Study (Founder-Safe Aggregate Summary)

> **Data-governance notice:** This document reports high-level aggregate metrics and engineering verification outcomes only. It contains **no abstract text, no raw HTML, no clinical trial quotes, and no personal speaker records**. All raw CTOS source captures and generated datasets are excluded from this repository under embargo and governance controls (see [Data Governance](data-governance.md)).

## Baseline Implementation

The **CTOS 2026 Annual Meeting** (Connective Tissue Oncology Society, Seoul, South Korea, `Asia/Seoul`) serves as the baseline end-to-end validation of the Conference Intelligence Pipeline using the `eventscribe-ctos-v1` adapter.

Rather than relying on superficial listing pages alone, the validated `eventscribe-ctos-v1` adapter pairs public programme listings with structured detail-card endpoints (`PosterInfo`, `PresentationInfo`, `SessionInfo`, `ExhibitorInfo`) to recover complete schedule hierarchy, investigator roles, institutional geography, disclosures, and abstract sections.

## Extracted Corpus Summary

Across the complete CTOS 2026 programme snapshot, the pipeline produced the following canonical inventory:

| Entity / Artefact | Validated Count | Notes |
|---|---:|---|
| **Presentations** | **772** | 645 posters, 117 oral talks, 2 named lectures, 8 other programme items |
| **Abstracts** | **645** | Structured into normalized sections (`objective`, `methods`, `results`, `conclusion`) with embargo metadata |
| **Sessions** | **20** | Scientific sessions with chairs, moderators, room locations, KST schedules, and child presentation links |
| **People** | **3,956** | Deduplicated authors, presenters, chairs, and moderators with normalized institutions and countries |
| **Sponsors** | **11** | Exhibitor and sponsor records linked to booth metadata |
| **Evidence-Linked Tags** | **3,844** | Disease, drug, drug-target, modality, biomarker-polarity, company, histology, and trial-registry tags |
| **Clinical Readouts** | **59** | Structured arm-level efficacy and safety readouts verified against source text spans |

## Why Evidence-Linked Enrichment Matters

Traditional keyword matchers routinely corrupt oncology conference analytics. On the CTOS baseline, our deterministic enrichment engine (`pipeline/tag.py`) resolved three classic failure modes:

1. **Biomarker polarity vs. drug targets:** Cohorts described as `HER2-negative` or `SMARCB1-deficient` are tagged as `biomarker_status` or `selection_biomarker` with explicit polarity—never miscounted as `HER2` drug targets.
2. **Drug-derived modalities and targets:** Antibody-drug conjugates (ADCs) derive their `adc` modality and primary target from the conjugate entry in the curated drug dictionary; cytotoxic payloads never pollute small-molecule or chemotherapy counts.
3. **Exact substring provenance:** Every one of the **3,844** exported tags carries character-offset quotes verified as exact substrings of the underlying presentation record.

## Why Schema Validity Is Not Enough: Release Gates in Action

A central architectural principle of Conference Intelligence Pipeline is that **structural JSON Schema validity (`schemaValid: true`) does not make a clinical dataset safe to publish**.

On the baseline CTOS 2026 candidate run:

- The assembled dataset passed Draft 2020-12 JSON Schema validation (`schemaValid: true`), inventory reconciliation (`772` presentations, `645` posters), and tag substring checks (`0` broken tag offsets).
- However, the automated QA and clinical review gate (`conference-pipeline run`) **correctly blocked public release**, surfacing five real-world items requiring clinical adjudication:
  - **3 numeric readout consistency failures** (`readout_ci_contains_estimate: 1`, `readout_count_denominator: 1`, `readout_arm_n_sum: 1`), triggered where source abstracts themselves printed arithmetic or rounding discrepancies (for example, a reported point estimate outside a printed confidence interval bound, or a responder fraction whose denominator differed from the evaluable cohort count).
  - **2 clinical readouts flagged `needs_review`** (`pendingReview`), awaiting explicit human reviewer sign-off.

**Current disposition:** The existing CTOS 2026 candidate dataset is **not publicly releasable**. The runner enforces a non-zero exit code (`QualityGateError`) until all numeric QA checks and human-review gates clear and the configured embargo window (`2026-11-18T14:00:00Z`) has been satisfied and cleared by governance.

## Why Raw Source Data Is Excluded

Raw CTOS HTML captures (`data/raw/bundle.json`), intermediate stage files (`data/stage/`), real HTML fixtures, gold annotations, and candidate exports (`out/`, `runs/`) are excluded from Git because:

1. Conference abstracts are subject to explicit delegate and public embargo timestamps (`configs/ctos-2026.json`).
2. Raw detail cards contain full-text abstracts and individual speaker affiliation/disclosure strings governed by organizer terms.
3. Separating code from raw data allows engineering teams and prospective partners to audit, test, and build the entire software stack offline using synthetic fixtures (`tests/fixtures_synthetic/`) with zero embargo risk.
