# S1: static-analysis accuracy on dynamic packages (DT-SPIKE-01)

**Question:** how often does the index say a name is absent at a release where it exists at runtime? That is R1 in docs/07: lazy or dynamic exports turning into false "removed" or "never existed" answers.

**Pass:** at most 10% false absent over the sample. Otherwise build the sandboxed dynamic fallback for lazy packages and re-measure, and stop if it's still above 10%.

## Sample

- **Packages (10):** langchain, langchain-community, langchain-core, openai, pydantic, huggingface-hub, litellm, pydantic-ai-slim, instructor and numpy. That's docs/07's list without transformers, which is out of coverage (DT-DATA-16), plus the packages the index shows to be most dynamic: lazy `__getattr__` hooks, `TYPE_CHECKING` re-exports, unresolved aliases and stubs.
- **Releases (5 per package):** spread evenly in time across the indexed range, first and latest included (`plan.py` → `plan.json`).
- **Python:** each release runs on the newest Python that had been out for six months when it shipped. Dependencies are resolved as of a week after the release (`--exclude-newer`).
- **Symbols (50 per release):** 25 from package `__init__` modules, where lazy re-exports live, and 25 from all other modules, with a fixed seed per release. When one stratum runs short, the other tops it up. The report also gives the rate over every runtime name.

## What counts as public at runtime

These rules mirror what the index can hold (`probe.py`):
- **Modules:** every importable submodule of the package's import names, skipping `_private` segments and bundled test suites.
- **Names:** in each module, the names in `__all__` when it is defined. Otherwise, `dir()` minus `_names`, keeping only classes, functions and modules defined inside the package (so `os` and `typing.Any` don't count).
- **Confirmation:** each name is confirmed by `getattr()`, which also runs lazy `__getattr__` loaders.
- **Class members** are out of scope.

The report breaks misses down by shape. One shape to read with care: a plain module's imports from elsewhere in the package (`module / dir / package`) are public under this rule, but the index treats them as implementation by design (see `snapshot._Walker._public`).

Not sampled:
- names that need an extra that isn't installed (they don't resolve at runtime);
- submodules that fail to import;
- lazy names that are missing from `__all__`, because `dir()` doesn't list them.

## Sandbox

Runtime sampling imports the package, which invariant 1 forbids everywhere else. So it happens only in `.github/workflows/s1.yml`, which runs on a push to the `spike/s1` branch on an ephemeral runner. For each release, `run.py`:

1. installs into a fresh venv, wheels only (`--only-binary :all:`), so no package code runs at install time;
2. runs `probe.py` with `python -I` as a throwaway system user, under `unshare --net` (no network), with no capabilities, `no_new_privs` and `env -i`;
3. deletes the venv.

Before probing, the job checks from inside the sandbox that the probe isn't root, can't write the checkout and can't reach the network, and stops if any check fails. `run.py` refuses to run outside GitHub Actions. The probe records exception type names only, never messages or tracebacks.

## Running it

```bash
uv run python bench/s1/plan.py --index <index.sqlite> > bench/s1/plan.json   # choose the sample
git push origin spike/s1                                                      # runs s1.yml
gh run download <run id> -D <new empty dir>
uv run python bench/s1/compare.py <that dir> --index <index.sqlite> --json <results.json>
```

`compare.py` reads the artifacts as data and imports nothing it measures. Score against the same index `plan.json` was built from (its `index_built_at`).
