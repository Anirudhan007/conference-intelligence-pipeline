# Founder Demo Script: Conference Intelligence Pipeline

This guide provides a concise, truthful 3-minute product walkthrough using only founder-safe synthetic artefacts and aggregate metrics.

---

## Key Positioning Points

1. **The Problem:** Scientific conferences publish critical clinical trial readouts, target trends, and investigator networks across fragmented HTML schedules, modal popups, and unstructured abstracts. Manual tracking is slow, unrepeatable, and error-prone; naive LLM scraping hallucinates numbers, conflates negative biomarkers (such as `HER2-negative`) with drug targets, and ignores embargoes.
2. **The Solution:** An offline-verifiable, evidence-linked pipeline that separates platform-specific capture from deterministic biomedical normalization, quote-verified clinical readouts, and hard QA release gates.
3. **Validated Today vs. Expansion Model:** Today we ship one production-hardened adapter (**CTOS 2026 on EventScribe**). The core normalization, ontology, readout verification, QA, and export engine is platform-agnostic; adding each new conference platform (e.g., cOASIS, Freeman, custom society portals) is a bounded adapter increment with contract tests—not a rewrite.
4. **Commercial Value:** Delivers audit-ready, structured conference intelligence (JSON knowledge graph + analyst CSV + QA certificate) where every tag and clinical endpoint links to an exact character-offset quote in the source document.

---

## Pipeline Stages Overview

```text
Conference URL -> Adapter/Capture -> Raw Bundle -> Parser -> Tagging -> Readout Review -> Aggregates -> QA Gate -> JSON/CSV/Report/Manifest
```

- **Capture boundary:** Acquires public pages politely (`robots.txt` verified, rate-limited, SHA-256 hashed) into an immutable raw bundle.
- **Deterministic enrichment:** Curated dictionaries + INN drug grammar assign targets and modalities from the drug (never from stray words near a gene) and attach ConText-style biomarker polarity (`positive`, `negative`, `mutant`, `amplified`, `fusion`).
- **Clinical & QA gate:** Verifies arm-level numbers, confidence intervals, denominators, and review status before permitting release.

---

## Synthetic Output Example (Safe for Live Screen-Share)

Every extracted entity and readout carries exact character spans and provenance:

```json
{
  "presentation": {
    "id": "ctos-2026:PosterInfo:900101",
    "number": "P 01",
    "format": "poster",
    "title": "SYNTHETIC COHORT STUDY OF LOCAL ABLATIVE RADIOTHERAPY IN SOFT TISSUE SARCOMA",
    "timezone": "Asia/Seoul",
    "embargo": {
      "embargoed": true,
      "liftsAtUtc": "2026-11-18T14:00:00Z"
    }
  },
  "tags": [
    {
      "kind": "drug",
      "entityId": "sunitinib",
      "label": "sunitinib",
      "role": "investigational",
      "modality": "small_molecule_kinase_inhibitor",
      "evidence": [
        {
          "field": "abstract",
          "charStart": 49,
          "charEnd": 58,
          "quote": "sunitinib"
        }
      ]
    },
    {
      "kind": "target",
      "entityId": "KIT",
      "label": "KIT",
      "role": "selection_biomarker",
      "polarity": "mutant",
      "evidence": [
        {
          "field": "abstract",
          "charStart": 103,
          "charEnd": 113,
          "quote": "KIT-mutant"
        }
      ]
    }
  ],
  "readout": {
    "id": "ro-synth-900101",
    "phase": "2",
    "design": "Single-arm",
    "studyN": 40,
    "primaryEndpoint": "orr",
    "reportedValue": "35.0% (14/40; 95% CI, 20.6-51.7)",
    "review": {
      "status": "auto_verified"
    }
  }
}
```

---

## CTOS 2026 Baseline Aggregate Summary (No Raw Text Shown)

On our baseline CTOS 2026 EventScribe run (raw data withheld under governance/embargo policy), the pipeline structured:

- **772** presentations (**645** abstracts, **20** sessions, **3,956** people, **11** sponsors)
- **3,844** evidence-linked biomedical tags (`100%` exact substring verified)
- **59** structured clinical readouts
- **Release-gate proof in action:** Although `schemaValid` is `true`, the release gate automatically blocked publication because it caught **3 numeric source/draft inconsistencies** (such as a published abstract reporting an estimate outside its own printed confidence interval) and **2 readouts flagged for human review**.

---

## 3-Minute Demonstration Flow

### Minute 0:00–0:45 — Frame the Problem & Governance Boundary
1. Open `README.md` and point to the value proposition and architecture flow.
2. Highlight why teams cannot rely on unverified scraping: conference layouts vary, abstracts carry strict embargoes, and clinical numbers require exact quote verification.

### Minute 0:45–1:45 — Live CLI Validation & Synthetic Gate Execution
Run these three commands live in PowerShell:

```powershell
# 1. Validate the conference configuration and adapter guardrails
conference-pipeline validate --config configs/ctos-2026.json

# 2. Run the 117-test offline contract suite (parser, tagger, polarity, readout verifier, QA gate)
python -m pytest -q

# 3. Confirm code hygiene
python -m ruff check .
```

Point out `tests/test_release_layer.py`: show how a valid synthetic bundle + readout produces a signed `run-manifest.json`, whereas corrupting a confidence interval immediately raises `QualityGateError` and blocks release.

### Minute 1:45–2:30 — Walk Through CTOS Baseline Metrics
Open `docs/ctos-case-study.md`:
- Share the aggregate scale (**772 presentations**, **645 abstracts**, **3,844 evidence-linked tags**, **59 clinical readouts**).
- Emphasize trust: our QA gate distinguishes *structurally valid JSON* from *clinically releasable data*, catching real-world abstract arithmetic discrepancies before clients ever see an error.

### Minute 2:30–3:00 — Adapter Expansion & Commercial Model
Open `docs/adapter-guide.md`:
- Show the clean seam between **platform adapters** (capture + HTML-to-canonical parse) and the **reusable intelligence core** (curated ontology, drug/modality/polarity engine, readout verifier, aggregates, QA, JSON/CSV exports).
- Close on commercial value: each new major medical conference platform is a repeatable adapter module feeding one unified, traceable biomedical knowledge graph.
