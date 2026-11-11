# Synthetic Test Fixtures

All HTML snippets, raw-bundle payloads, readout drafts, and gold-label samples in `tests/fixtures_synthetic/` are **100% synthetic**.

- Every presentation ID (`900xxx`, `910xxx`, `920xxx`, `950xxx`), person name, institution, sponsor name, and abstract paragraph is fictional.
- HTML markup mirrors the public DOM structure of the EventScribe layout (`li[data-posterid]`, `li[data-presid]`, `li.list-row`, `.PosterAbstractText`, `h2.role-title`, `ul.speakers-wrap`, `.pres-tidbit`, `.presentation-disclosure-block`) so parser, tagger, readout verifier, QA, and release-gate tests run offline in CI without any embargoed or proprietary conference material.
- Real conference captures (`data/raw/`), real HTML fixtures (`fixtures/`), real gold sets (`data/gold/`), and real draft readouts (`data/stage/`) are excluded from version control. See `docs/data-governance.md`.
