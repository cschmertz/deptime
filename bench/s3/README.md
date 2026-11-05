# S3: traceback mapping (DT-SPIKE-03)

**Question:** given the text a user or an agent actually has, does `explain_error` find the right kind of error and the right package? Python's messages change between 3.10 and 3.14, and the same error reaches DepTime printed by a script, pytest, unittest, a logger, an asyncio `TaskGroup` or IPython, often wrapped in another exception. The traceback benchmark (`bench/cases`) mostly holds tracebacks people posted, which are plain or pytest output from one Python version each. S3 holds the break fixed and varies the rest.

**Pass** ([docs/07](../../docs/07-risks-and-spikes.md)): kind + package right for at least 90% of the cases.

## Cases

- **Breaks** (`scenarios.json`): 22 known breaks in 18 covered packages. Each has a few lines of code written against the old API (`app`), the requirement that installs a release where it breaks, the expected `kind` and `package`, and the benchmark case or migration that documents the break (`evidence`). Kinds: `import_name` 5, `module_missing` 3, `module_attribute` 4, `object_attribute` 3, `unexpected_kwarg` 7. Eight raise the library's own exception or message: pydantic's `PydanticImportError` and `PydanticUserError`, openai's `APIRemovedInV1`, numpy's "was removed in the NumPy 2.0 release" and "was removed from the ndarray class" (numpy 2.0–2.3; 2.4 prints Python's own message), mcp 2.x's annotated `ModuleNotFoundError`, huggingface_hub's lazy loader ("No huggingface_hub attribute …") and pluggy's `PluginValidationError`. One (`openai-httpx-proxies-internal`) is raised in openai's code by a change in httpx.
- **Presentations** (`run.py`), each run on Python 3.10, 3.11, 3.12, 3.13 and 3.14:

  | Presentation | What runs | Pythons |
  |---|---|---|
  | `script` | `python app.py` | all |
  | `script-color` | `python app.py` with `PYTHON_COLORS=1` | 3.13+ (earlier versions print no colour) |
  | `pytest` | `python -m pytest test_app.py` (default `--tb=auto`) | all |
  | `pytest-short` | the same with `--tb=short` | all |
  | `unittest` | `python -m unittest test_unit` | all |
  | `raise-from` | `raise RuntimeError("could not start the service") from exc` around the call | all |
  | `during-handling` | the same without `from` ("During handling of the above exception…") | all |
  | `logging` | `log.exception("startup failed")` with a timestamped format | all |
  | `taskgroup` | the call in a task of an `asyncio.TaskGroup` (an `ExceptionGroup`) | 3.11+ |
  | `ipython` | `python -m IPython -c "import app; app.main()"` | all |

  The pytest hook break (`pytest-collect-file-path`) only exists under pytest, so it runs in the two pytest presentations.
- **How the text was made:** `.github/workflows/s3.yml` runs `run.py` on GitHub Actions, one job per Python version. Each break gets a fresh venv at `/home/dev/project/.venv`: wheels only, resolved as of 2026-11-01 (`--exclude-newer`), with pytest and IPython beside the break's requirement. Each presentation runs as a throwaway user with no network and an empty environment; stdout and stderr are kept together, in order, as a terminal shows them. Nothing is edited afterwards.
- **Selection** (`collect.py`): a run becomes a case when its output contains the break's `expect` text, which shows the intended error happened. Runs that failed to install or didn't produce it are listed in the results and left out.
- `installed` is the venv's `uv pip freeze`.
- Two corrections were made to `scenarios.json` after a first generation run and before any scoring, and the generation was run again: huggingface_hub's `expect` text (its lazy loader words the error its own way), and matplotlib's requirement (3.9.1 restored `cm.get_cmap`, deprecated, and 3.11 removed it again, so the break needs `matplotlib>=3.11`, which doesn't install on Python 3.10).
- DepTime was not run on these cases before `cases.jsonl` and `score.py` were committed.

## Scoring (fixed before the first run)

```bash
uv run python bench/s3/score.py bench/s3/cases.jsonl [--index PATH] [--json OUT]
```

- A case passes when `signal.kind` equals the expected kind and `findings[0].package` equals the expected package. For a chained or grouped exception, the expected answer is the break, not the wrapper.
- **Headline:** kind + package over all cases, with `installed` passed as `installed_versions`. Also reported: the same without installed versions, and the split by presentation, Python version, kind and break.
- Every case counts once, so presentations that run on more Python versions weigh more. The bar applies to the headline.
- The first run is the spike's result and is reported as is. Later runs, after fixes designed from its misses, are regression checks.

## Holdout

The first run's misses were fixed from those same cases (DT-CORE-25), so a second score on them isn't a fresh measurement. `holdout.json` holds 16 more breaks in 14 packages: none is in `scenarios.json`, and each is documented by a case in `bench/cases` or `bench/tmb`. Kinds: `import_name` 3, `module_missing` 1, `module_attribute` 4, `object_attribute` 3, `unexpected_kwarg` 5. They include shapes the first set lacks: scikit-learn's message that names no function (`got an unexpected keyword argument 'squared'`), click calling the user's own `ParamType.get_metavar` with an argument it lacks (a known gap in `bench/cases`), and streamlit's appended advice. They were generated by the same `run.py` and presentations (`s3.yml`, `set: holdout`), collected by the same rule into `holdout-cases.jsonl`, and committed before DepTime ran on them. They are scored once, with the fix, under the rule above:

```bash
uv run python bench/s3/score.py bench/s3/holdout-cases.jsonl
```

## Results

- **First run (2026-11-05): 73.1%** kind + package (708/968), below the bar: chained exceptions 0%, huggingface_hub's own message 0%, library messages inside an `ExceptionGroup` missed. DT-CORE-25 fixes all three; afterwards 968/968, a regression check on the cases the fix was designed from.
- **Holdout (2026-11-05): 93.8%** (690/736), above the bar. Kind is right in every case; the 46 misses are one break, click's `get_metavar(ctx)` (DT-CORE-02).
- See `results-2026-11-05.md`.
