# Adapter Guide: Supporting a New Conference Website

Conference Intelligence Pipeline separates **website-specific acquisition and HTML parsing** from **reusable biomedical normalization, verification, QA, and export**.

> **Core rule: Changing only `sourceUrl` in a config file is never sufficient to support a new website layout.**
> Different conference platforms (EventScribe, cOASIS, Freeman, Confex, Oxford Abstracts, custom society Next.js/SPA portals) expose completely different routing, pagination, DOM selectors, speaker encodings, and embargo notices. Attempting to guess through an unvalidated DOM silently drops abstracts, corrupts schedules, or misattributes authors. For that reason, `ConferenceConfig.assert_supported()` fails closed unless an explicit, tested adapter is registered.

---

## Step-by-Step Checklist to Add a New Website Adapter

### 1. Identify the Platform and Public Page Inventory

Before writing code, document the target site's public architecture and legal/embargo posture:

- **Platform family & rendering model:** Server-rendered HTML, AJAX detail fragments (like EventScribe `PosterInfo.asp` / `SessionInfo.asp`), or JSON API endpoints backing a public SPA.
- **Public page types required for 100% inventory:**
  - Poster index / category listings
  - Oral presentation / abstract listings
  - Day-by-day session agenda (dates, start/end times, conference timezone, rooms)
  - Detail cards (abstract sections, author order, roles, affiliations, disclosures)
  - Exhibitor / sponsor directory
- **Terms & embargo rules:** Locate the official abstract submission or press embargo document, record verbatim quotes and UTC timestamps, and confirm public unauthenticated access is permitted by `robots.txt` and site terms.

### 2. Add a Responsible Capture Adapter

Create a platform capture module under `capture/` (or `conference_pipeline/adapters/<platform>/`) that emits the canonical **Raw Bundle** contract (`source`, `listings`, `details`, `errors`):

- **Attribution:** Require a valid `CONTACT_EMAIL` via `build_user_agent()` so site operators can identify and contact the collector.
- **Robots & terms controls:** Fetch and parse `/robots.txt` before any content request. Fail closed if `/robots.txt` cannot be established (network error or unexpected status) or disallows target routes. Never authenticate, send cookies, or bypass login walls.
- **Rate limits & retries:** Default to single-request concurrency (`delay >= 1.5s`), honour `Retry-After` headers on HTTP `429`, increase pacing dynamically on rate-limit signals, and bound retries on transient `5xx`/`403` responses.
- **Source hashes:** Compute and store `SHA-256` digests for every captured listing/detail payload and for the bundle as a whole so downstream runs are byte-for-byte auditable.

### 3. Add a Deterministic Parser Mapping to the Canonical Model

Implement a parser that transforms the platform's `RawBundle` into the canonical `parsed.json` contract (`BRIEF.md`):

- **Canonical top-level keys:** `conference`, `presentations`, `sessions`, `people`, `sponsors`, `events`, `parseReport`.
- **Presentation invariants:**
  - Stable IDs: `<conference-id>:<sourceKind>:<sourceId>`
  - Normalized format enum: `poster | oral | lecture | other` (demote breaks, registration, and social functions to `events`)
  - Explicit timezone (IANA string, e.g. `Europe/Amsterdam`, `America/Chicago`) and `YYYY-MM-DD` / `HH:MM` 24-hour times
  - Structured `authors` in exact listed order with `role` (`author`, `coauthor`, `presenter`, `chair`, `moderator`, `speaker`) and deduplicated `personId`
  - Clean plain-text `abstract` with preserved section headings (`Objective:`, `Methods:`, `Results:`, `Conclusion:`), `abstractSections` dictionary, and `disclosures` stripped out into their dedicated list
  - **Missing means `null`:** Never emit `0`, `""`, or placeholder text for absent fields
  - **Determinism:** Sort all entities and keys so identical raw bundles produce identical JSON byte-for-byte.

### 4. Add a Validated Conference Configuration

Create `configs/<conference-id>.json` conforming to `conference-pipeline/config/v1`:

```json
{
  "schemaVersion": "conference-pipeline/config/v1",
  "conference": {
    "id": "example-2027",
    "name": "Example Oncology Congress 2027",
    "organization": "Example Oncology Society",
    "sourceUrl": "https://congress2027.example.org/programme",
    "platform": "example-platform",
    "timezone": "Europe/Berlin",
    "startDate": "2027-05-14",
    "endDate": "2027-05-17",
    "city": "Berlin",
    "country": "Germany"
  },
  "runner": {
    "adapter": "example-platform-v1",
    "requiresHumanReadoutReview": true
  },
  "embargo": {
    "rules": [
      {
        "class": "public",
        "releaseUtc": "2027-05-14T07:00:00Z",
        "sourceQuote": "Verbatim embargo lift statement from official congress policy.",
        "sourceUrl": "https://congress2027.example.org/embargo-policy"
      }
    ]
  }
}
```

Implement an `Adapter` subclass in `conference_pipeline/adapters/<platform>.py` (`validate_config`, `capture`, `parse`, `inventory`) and register its identifier (`example-platform-v1`) in `conference_pipeline/adapters/__init__.py` only after contract tests pass. `ConferenceConfig.adapter()` resolves and validates it; `run_release()` and `conference-pipeline capture` dispatch through it, so no runner change is needed. Use `conference_pipeline/adapters/http.py` (`PoliteHttp`) for all live requests: it enforces robots.txt (fail closed), pacing, and `Retry-After` handling, and it never handles bot challenges or logins.

Shipped examples: `eventscribe.py` (HTML portal; wraps `capture/capture.py` + `pipeline/parse.py` via `parse.conference_context`) and `crossref.py` (JSON API; journal-supplement abstracts selected by `source.filter` + `source.match` regexes, numbering and format rules in config).

### 5. Add Synthetic Fixtures and Contract Tests

**Never commit real conference HTML or embargoed abstracts as test fixtures.** Instead:

1. Author synthetic HTML/JSON samples under `tests/fixtures_synthetic/<platform>_synthetic.json` that mirror the target platform's exact CSS classes, attributes, pagination markers, and edge cases using fictional names, institutions, IDs, and synthetic abstracts.
2. Add parser contract tests verifying:
   - Inventory parity between listings and parsed presentations
   - Schedule, timezone, session linkage, and logistics demotion
   - Author role/affiliation splitting and person deduplication
   - Abstract section extraction, disclosure separation, and embargo flagging
   - Byte-for-byte determinism across repeated runs
3. Add an end-to-end `run_release()` test proving the new adapter flows cleanly through tagging, QA, export, and manifest generation.

### 6. Reuse Shared Enrichment, Verification, QA, and Export Stages

Once your parser emits canonical `parsed.json` records, **zero changes** are needed in the downstream intelligence core unless new therapeutic areas require dictionary additions (`pipeline/kb/*.json`):

- `pipeline/tag.py` — Curated drug/target/disease/company tagging, INN grammar, regimen expansion, and ConText biomarker polarity with exact character-offset evidence verification.
- `pipeline/readouts.py` — Clinical abstract candidate scoring and numeric/quote verification for human-curated readouts.
- `pipeline/aggregate.py` — Target-by-modality heatmaps, disease rollups, themes, and programme overview facets.
- `pipeline/qa.py` — Draft 2020-12 JSON Schema validation, referential integrity, exact quote verification, no-zero-imputation checks, and numeric clinical consistency checks.
- `pipeline/export.py` & `conference_pipeline/runner.py` — Canonical JSON, `.json.gz`, analyst CSV, `run_report.md`, `run-manifest.json` (`SHA-256` provenance), and hard release-gate enforcement.
