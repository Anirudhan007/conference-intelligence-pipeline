# Contributing

## Local Development Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Before opening a pull request, ensure all checks pass locally:

```powershell
python -m pytest -q
python -m ruff check .
python -m build
```

## Contribution Rules

1. **Zero confidential data in Git:** Never commit raw conference bundles (`data/raw/`), real HTML snapshots (`fixtures/`), real gold sets (`data/gold/`), draft readouts (`data/stage/`), generated exports (`out/`, `runs/`, `*.json.gz`, `*.csv`), or `.env` files. Read [docs/data-governance.md](docs/data-governance.md) first.
2. **Synthetic fixtures only:** Every parser, tagger, readout, and release-gate test must use synthetic, non-embargoed fixtures under `tests/fixtures_synthetic/`.
3. **Platform adapters:** Supporting a new conference website requires a dedicated, tested adapter—changing a configuration URL alone is never sufficient. Follow the step-by-step contract in [docs/adapter-guide.md](docs/adapter-guide.md).
4. **Determinism & provenance:** Missing values must be `null` (never `0`), outputs must sort deterministically, and every extracted tag or clinical number must carry an exact substring quote with character offsets.
