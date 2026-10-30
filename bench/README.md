# Traceback benchmark (DT-QA-01 / DT-QA-02)

**Purpose:** measure whether `explain_error` gives the right answer on *real* failures. This is the project's accuracy measure (root cause + boundary: floor 70%, target 80%).

## Case format (`cases/*.jsonl`, one JSON object per line)

| Field | Meaning |
|---|---|
| `id` | Stable slug |
| `source` | URL of the GitHub issue / Stack Overflow post / migration guide it came from, or `"constructed"` |
| `evidence` | URL(s) backing the expected answer: changelog, release notes, maintainer reply, or the library's source at a tag. **Never DepTime's own output**, or the benchmark is circular |
| `traceback` | Verbatim text, trimmed of secrets and personal paths |
| `installed` | `{package: version}` the reporter was running |
| `expected.package` | Distribution that changed |
| `expected.symbol_suffix` | End of the expected `finding.symbol`, e.g. `Client.__init__(proxies)` |
| `expected.issue` | `symbol_removed` / `param_removed` / `module_removed` / … |
| `expected.last_version_with` | Optional; checked as a prefix (`"1.5"` matches `1.5.3`) |
| `expected.first_version_with` | Optional, for `*_not_yet_added`; checked as a prefix against the version in DepTime's `pin_to_restore` (`pkg>=X`) |
| `expected.replacement_contains` | Optional; must appear in one of the top-3 replacement targets |
| `status` | `expected_pass`, or `known_gap` (with the backlog ID that fixes it) |
| `fixed_by` | For `known_gap`: the backlog ID(s) that should make it pass |

`expected.issue` is one of `symbol_removed`, `symbol_not_yet_added`, `param_removed`, `param_not_yet_added`, `param_now_required` or `module_removed`. The `*_not_yet_added` issues are for code written against a newer release than the one installed. Use the shortest `symbol_suffix` that identifies the symbol: `Class.method`, `Class.__init__(param)`, `module.name` for a module-level name, or the dotted module path for a missing module. Set `last_version_with` to the release series before the removal (`"0.27"` when 0.28.0 removed it), or to the exact previous version if a patch release removed it. Likewise, `first_version_with` is the series that added it (`"0.26"` when 0.26.0 added it), or the exact version if a patch release added it. Pre-releases never count, because the index skips them. Omit either field when the evidence doesn't pin it down.

## Collection rules (DT-QA-01)

- **Real tracebacks** from GitHub issues or Stack Overflow, with the issue or post URL in `source`. Cases built from a migration guide use `"source": "constructed"` and cite the guide in `evidence`. They're reported separately from real ones.
- **Versions are known.** The reporter states them, or they can be inferred from a `pip freeze`, lockfile or maintainer reply in the thread. The installed version is at or after the package's earliest indexed release (see `deptime coverage`).
- **Versions are checked.** Reported versions are often wrong. Confirm from the library's source that the installed version can produce the error, and skip broken installs (a package that fails to import its own names).
- **Independent ground truth.** Expected fields come from `evidence`, never from running DepTime.
- **Diversity.** At most 2 reported cases per distinct break. A break is one change in the library, usually one changelog entry: `Client` and `AsyncClient` losing `app=` is one break. At least 3 cases per Tier A package where real reports allow it, and at most half of each tier's cases are `*_not_yet_added` (a package whose only breaks are additions may have only those).
- **No temporary removals.** Skip a break that a later release reverted (a name missing from one patch release only). It's a release bug, not an API change.
- **Privacy** (docs/08). Replace usernames and project names in paths (`/home/user/...`, `/app/...`, `/venv/lib/python3.X/site-packages/...`), including in the final line, but keep the path's shape: `site-packages` versus `~/.local` can be the diagnosis. Drop tokens, keys and hostnames, and keep only the traceback, not the surrounding `pip freeze`.
- **One file per package:** `cases/<package>.jsonl`, named after the package in `expected.package`.
- New cases start as `expected_pass`. After a run, each failure becomes `known_gap` with a backlog ID, or a bug to fix. Status records what we expect DepTime to do, so it may follow DepTime's output; expected answers may not.

## Harvesting real cases

Search GitHub issues by error string plus package, for example:
- `"got an unexpected keyword argument 'proxies'" httpx`
- `"cannot import name" langchain is:issue`
- `"has no attribute 'append'" DataFrame`
- `"No module named 'mcp.server.fastmcp'"`

Keep only cases where the reporter's versions are known, or can be inferred from the issue or lockfile. Target ≥ 200 cases across Tier A and Tier B, with at least 3 per Tier A package.

## Scoring (runner: DT-QA-02)

- **Root cause:** `findings[0].package` and `symbol` suffix and `issue` all match.
- **Boundary:** `last_version_with` matches (prefix rule).
- **Replacement:** `replacement_contains` is among the top 3 targets.
- Report per-tier accuracy and the share of `known_gap` cases. CI fails on a regression of more than 2 points.

Boundary and replacement only count when the root cause is right. The headline metric, `root_cause_and_boundary`, counts a case as passed when the root cause is right and the boundary matches (or the case doesn't expect one).

## Running it

```bash
uv run deptime index pull                                                      # the published index, into DEPTIME_HOME
uv run python -m deptime.bench bench/cases                                     # per-case report and per-tier table
uv run python -m deptime.bench bench/cases --baseline bench/baseline.json      # exit 1 on a regression
uv run python -m deptime.bench bench/cases --write-baseline bench/baseline.json  # record an intended change
```

`.github/workflows/bench.yml` runs the baseline check on code changes and after every nightly `build-index`. A regression is measured on the cases that both the baseline and the current run contain. So adding new cases never fails it, and only a case that used to pass and now fails does. After a fix makes cases pass, write a new baseline so they're protected too.

The seed file (`cases/seed.jsonl`) contains well-documented, real-world breaks. Cases marked `known_gap` say which backlog item fixes them.
