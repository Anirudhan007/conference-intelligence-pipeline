# GitHub Deployment Checklist

## 1. Pre-Push Safe-File Verification

Before creating or pushing to GitHub (especially when publishing as a **public** repository), verify all data-governance invariants (see [Data Governance](data-governance.md)):

1. Confirm that **only** code, configuration schemas, curated ontologies, documentation, and synthetic fixtures (`tests/fixtures_synthetic/`) are tracked.
2. Confirm that `git status` and `git ls-files` contain **none** of the following:
   - `data/raw/`, `data/stage/`, `data/gold/`
   - root `fixtures/` (real conference HTML)
   - `runs/`, `out/`, `work/`, `reports/`
   - `*.json.gz`, `*.csv`, `*readouts_draft*.json`, `run_report.md`
   - `.env`, `.venv/`, `dist/`, `build/`, `*.egg-info/`
   - parent-directory archives or exports outside the repository root
3. Confirm `.env.example` contains only the placeholder `CONTACT_EMAIL=research@example.org`.
4. Confirm offline checks pass cleanly without confidential files:
   ```powershell
   python -m pytest -q
   python -m ruff check .
   python -m build
   ```
   *(Clean up local `dist/` and `build/` or keep them gitignored so build artefacts are never committed.)*

## 2. Create the Repository and Push

From the repository root (`ctos_pipeline`):

### Option A — GitHub CLI (Preferred)

Authenticate once if needed, then create the GitHub repository and push `main`:

```powershell
gh auth login
gh repo create conference-intelligence-pipeline --public --source=. --remote=origin --push
```

### Option B — Manual Remote Setup

If creating an empty repository `conference-intelligence-pipeline` in the GitHub web UI first:

```powershell
git remote add origin https://github.com/<OWNER>/conference-intelligence-pipeline.git
git branch -M main
git push -u origin main
```

## 3. Post-Push Repository Hardening

1. Verify `git ls-files` on GitHub contains zero raw captures, abstracts, or generated exports.
2. Add a branch protection rule for `main`:
   - Require a pull request before merging (at least 1 approval).
   - Require status checks to pass before merging (`test (3.11)`, `test (3.12)`, `test (3.13)`, and `build`).
   - Block force pushes.
3. Enable secret scanning and push protection in GitHub **Settings -> Code security**.
4. Do not upload raw conference bundles or embargoed exports to GitHub Releases. Only attach cleared artefacts after QA gate passage, human readout sign-off, and written embargo/governance approval.
