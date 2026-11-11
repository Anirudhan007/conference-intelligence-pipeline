# Conference Intelligence Pipeline: shared brief for all agents

## Goal
> **Repository governance note:** Committed tests use synthetic fixtures in `tests/fixtures_synthetic/`. Real CTOS HTML fixtures (`fixtures/`), raw captures (`data/raw/`), stage/draft files (`data/stage/`), gold sets (`data/gold/`), and generated reports (`reports/`, `out/`, `runs/`) are intentionally excluded from Git; see `docs/data-governance.md`.

Input: one conference URL (trial: https://ctos2026.eventscribe.net/index.asp?launcher=1, CTOS 2026, Seoul, 18 to 21 Nov 2026, times in KST).
Output: one JSON file (`out/ctos-2026.json`) that reproduces and improves the Conference Atlas data layer:
presentations, sessions, people, sponsors, normalized tags with evidence, clinical readouts with arm level numbers,
aggregates (heatmap, themes, overview), and a QA report.

## Environment facts
* No network from this workspace to eventscribe. Capture runs in the user's browser and lands here as a raw bundle.
* No LLM API key. Language model work is done by agents directly (you read text and produce structured output),
  and code verifies it (quotes must be exact substrings of the source).
* Python 3.13. Available: bs4, lxml, jsonschema, pydantic. pip install works for PyPI packages (use --break-system-packages).
* Knowledge bases (ChEMBL, HGNC, OncoTree, NCIt) are not reachable. Build curated dictionaries from your own knowledge,
  include standard ids where you are confident (HGNC symbols, OncoTree codes, NCIt codes), and mark `source: "curated"`.

## Site structure (eventscribe platform)
Listing pages (raw bundle `listings`):
* `posters`: PosterTitles.asp, 645 `li[data-posterid]`, each with poster number (e.g. "P 01") and title.
* `postersByCategory`: same posters grouped by category/track.
* `presentations`: SearchByPresentation.asp, 148 `li[data-presid]` talks with hidden author blocks.
* `agenda`, `day_2026_11_18` .. `day_2026_11_21`: agenda by day, `li.list-row` with time (KST), title, location, data-url to PresentationInfo or SessionInfo.
* `sponsorEvents`, `networking`, `ancillary`, `spagn`, `sponsors`, `home`.
Detail cards (raw bundle `details`, key = "<Kind>:<id>"):
* `PosterInfo:<PosterID>`: `.trackname` (category/track), `h1` "(P 01) TITLE", sections `h2.role-title` (Author(s), Co-Author(s), Poster Presenter(s)) with `li.speakerrow[data-presenterid]` > `.speaker-name`, `.prof-text` (title<br>institution<br>city, region, country), `.PosterAbstractText` (abstract HTML with bold section labels), `.presentation-disclosure-block`, views count in gutter.
* `PresentationInfo:<PresentationID>`: optional session name in `<b>`, `h1` ("Paper 86 - TITLE" or event title), date, time span with KST, `Location: ...`, role sections, disclosures, sometimes abstract.
* `SessionInfo:<PresentationID>`: session `h1`, date, time, location, Chair(s), Moderator(s), list of child presentations (`li[data-presid]`, slide counts in `original-title`).
* `ExhibitorInfo:<BoothID>`: sponsor details.
Fixtures with real samples: `fixtures/*.html` and `fixtures/fixtures.json`.

## Raw bundle format (arrives at data/raw/bundle.json)
```
{"source": {...}, "listings": {name: {url, retrievedAt, bytes, html}}, "details": {"PosterInfo:900101": {url, retrievedAt, bytes, html}, ...}}
```

## Canonical stage contracts (all agents must follow)
`data/stage/parsed.json`:
```
{"conference": {...}, "presentations": [Presentation], "sessions": [Session], "people": [Person], "sponsors": [Sponsor], "events": [Event], "parseReport": {...}}
Presentation = {id: "ctos-2026:<sourceKind>:<sourceId>", number, title, titleNormalized, format: poster|oral|lecture|other,
  tip: bool, track, sessionId, sessionTitle, date (YYYY-MM-DD), startTime, endTime, timezone: "Asia/Seoul", location,
  authors: [{personId, name, degrees, pronouns, role: author|coauthor|presenter|chair|moderator|speaker, position, institution, city, region, country, order}],
  abstract (plain text, section labels kept as "Objective: ..." lines) | null, abstractSections: {objective, methods, results, conclusion, ...} | null,
  disclosures: [{name, text}], slidesAvailable: int|null, views: int|null, url, sourceKind, sourceId, contentHash, embargo: {embargoed: bool, liftsAtUtc}}
Session = {id, title, date, startTime, endTime, location, chairs[], moderators[], presentationIds[]}
Person = {id, name, institutions[], countries[]}
```
`data/stage/tags.json`: `{presentationId: [Tag]}` with Tag = `{kind: drug|target|modality|disease|company|trial|histology, entityId, label, role, polarity, evidence: [{field, charStart, charEnd, quote}], method, confidence}`.
Roles for targets: drug_target, selection_biomarker, biomarker_status, mentioned. Roles for drugs: investigational, backbone, control, mentioned.
`data/stage/readouts.json`: list of Readout objects (see docs/design summary below).

## Hard rules
1. Missing means null. Never zero, never imputed.
2. Every tag and every extracted number carries an evidence quote that is an exact substring of the source field.
3. Modality comes from the drug (INN grammar, curated mechanism), never from words near a target. Combination regimens are tagged per component.
4. Biomarker mentions carry polarity (positive, negative, low, mutant, wild_type, amplified, fusion, unknown). HER2-negative is not a HER2 drug target.
5. Abstract text is embargoed until 2026-11-18T14:00:00Z (09:00 EST). Every presentation carries `embargo`. Output is for private analysis only.
6. Deterministic: same input gives same output. Sort keys and lists.
7. Do not write outside the repository workspace. Do not edit another stage's contract without updating shared schema and tests.

## Directory layout
```
pipeline/parse.py        (Agent Parser)
pipeline/kb/*.json       (Agent Tagger: dictionaries)
pipeline/tag.py          (Agent Tagger)
pipeline/readouts.py     (Agent Readouts: candidate scoring + verifier)
pipeline/schema/conference-atlas-2.0.schema.json, pipeline/aggregate.py, pipeline/qa.py, pipeline/export.py (Agent Assembler)
reports/*.md             (method comparisons and evaluation results)
tests/                   (pytest tests per module)
```
