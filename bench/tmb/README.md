# TimeMachine-bench holdout (DT-QA-04)

**Purpose:** measure `explain_error` on real failures that nobody picked with DepTime in mind. The cases in `bench/cases` were collected while fixes were being designed from them, so their score isn't a fair headline. These come from an outside, published benchmark.

**Source:** [TimeMachine-bench](https://github.com/tohoku-nlp/timemachine-bench) (Fujii et al., EACL 2026; Apache-2.0), dataset at commit `9928dbf`. Its *verified* subset has 100 repositories whose tests pass under their original dependencies and fail after migrating to the PyPI index of 2025-07-31 and Python 3.12.11. Each comes with a human label of the module(s) behind the failure (`related_modules`) and a gold fix patch.

## Selection

| Step | Repos |
|---|---|
| Verified subset | 100 |
| Labelled with a covered package | 47 |
| The break is a public name or parameter removed after the package's first indexed release | 32 |
| The tests produce a traceback for that break | 29 |

The 15 labelled repositories that aren't in scope:
- **Removed at or before the first indexed release (7):** numpy's `np.int`/`np.float`/`np.str` aliases (gone in 1.24.0, the first indexed numpy) ×5, pandas `DataFrame.from_items` (1.0), flask `helpers.safe_join` (2.1).
- **Behaviour changes with no removed name (6):** numpy ragged-array creation ×2, pydantic 2 making `Optional` fields without a default required ×2, Django 5's `USE_TZ` default, marshmallow 3's `load()` results.
- **Not a covered package's public API (2):** Flask-SQLAlchemy's `create_all(app=)` (labelled flask), private scikit-learn helpers.

Three in-scope repositories fail first on something else and have no case: pysnmp and flask_sqlalchemy (not covered), and scikit-learn's `OneHotEncoder(n_values=)` (removed in 0.22, before the index). `triage.json` lists all 47 with their category, expected breaks and reasons.

## Cases

One case per repository: the first traceback in its test log for its in-scope break.

- **Tracebacks** come from running each repository's test command at the dataset's commit with the dataset's setup patch, in `python:3.12.11` (linux/arm64). The dataset's recorded dependency versions are enforced as pip constraints (builds: setuptools ≤ 80.9.0, wheel ≤ 0.45.1), and the network is disconnected while tests run. Every environment's `pip freeze` matched the dataset. `run.py` reproduces the logs.
- The text is verbatim pytest or unittest output, paths included (`/work/` is the container's checkout). Five repositories have no licence: for these, the repository's own source lines are removed, and frame locations, library frames and the error are kept.
- **Expected answers** come from the gold patch and the shipped wheels on PyPI: the last release that has the name and the release that removed it. Never from DepTime. `replacement_contains` is what the gold patch switched to, when that's a name in the same package.
- `installed` is the expected package's version in the migrated environment.
- DepTime was not run on these cases before this file was committed.

## Scoring (fixed before the first run)

```bash
uv run python -m deptime.bench bench/tmb/cases --write-baseline <scratch>/tmb.json   # against the published index of the day
```

- **Headline:** `root_cause_and_boundary` over all 29 cases.
- **Also reported:**
  1. At most 2 cases per break, keeping the two lowest ids. This drops `tmb-numpy-nan-inferelator`, `tmb-numpy-nan-optics-functions` and `tmb-numpy-product-sdmpy`, leaving 26 cases.
  2. Split by whether the same break (package, symbol, issue) is already in `bench/cases`: 13 cases are, 16 aren't.
  3. Coverage: 32 of the 47 labelled breaks on covered packages are in scope, and 29 produced a case.
- With 29 cases, the 95% interval is roughly ±15 points around a score near 80%.
- The first run is the holdout result and is reported as is. A case's `expected` changes only to fix an error of fact, recorded below. Later runs are regression checks, not new holdout numbers.

## Corrections

None.
