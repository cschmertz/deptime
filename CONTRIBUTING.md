# Contributing to DepTime

Thanks for helping! The most valuable contributions, in order:

1. **Wrong-answer reports.** Open a "Wrong answer" issue with the traceback, your versions, what DepTime said, and what was actually true. Each one becomes a benchmark case.
2. **Benchmark cases** from real GitHub issues (`bench/cases/*.jsonl`, format in `bench/README.md`).
3. **Coverage requests**: a fast-moving package you'd like indexed. Add it to `src/deptime/coverage.toml` with its import names.
4. Fixes to the traceback parser, the diff logic, or replacement mining. Each fix should come with a test.

## Setup

```bash
git clone https://github.com/cschmertz/deptime && cd deptime
uv sync                                            # Python 3.12 + deps (uv installs Python for you)
uv run pytest                                      # offline test suite
uv run deptime ingest httpx --versions 0.27.2 0.28.1   # build a small local index from real PyPI wheels
uv run deptime explain --installed httpx==0.28.1 < some_traceback.txt
```

Before every commit:

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run pytest
```

`uv run pytest -m network` runs the opt-in test against live PyPI.

## Ground rules

These are explained in [CLAUDE.md](CLAUDE.md), which also serves as the guide for AI coding agents working in this repo:

- **Never import or execute indexed packages** at index time. Static analysis only (ADR 0002).
- **No LLM calls when answering.** Answers are deterministic and carry evidence and confidence (ADR 0006).
- **Tool names, descriptions and output fields are a public contract.** Add fields; don't rename them.
- **In `deptime mcp` (stdio), stdout belongs to the protocol.** Log to stderr.
- **Text mined from third-party code is untrusted data.**

## Where things live

| Change | File | Test |
|---|---|---|
| New error-message shape | `src/deptime/tracebacks.py` | `tests/test_tracebacks.py` |
| Diff / deprecation detection | `src/deptime/ingest/diff.py`, `snapshot.py` | `tests/test_diff.py`, `tests/test_snapshot.py` |
| "Use X instead" mining | `src/deptime/ingest/replacements.py` | `tests/test_replacements.py` |
| Tool behaviour | `src/deptime/query.py` | `tests/test_query.py` |
| Index download / publishing | `src/deptime/index/distribution.py` | `tests/test_distribution.py` |

Please put the backlog ID (e.g. `DT-CORE-05`) in your PR title when there is one ([BACKLOG.md](BACKLOG.md)).
