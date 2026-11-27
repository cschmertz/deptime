# Traceback benchmark (DT-QA-01 / DT-QA-02)

**Purpose:** measure whether `explain_error` gives the right answer on *real* failures. This is the project's accuracy measure (root cause + boundary: floor 70%, target 80%).

## Results: every check, first run and now

The project README gives four first-run numbers. This is every check behind them, with the score when each was
first run and the score now.

Each check's right answers were written down before DepTime first ran on it ([bench/](https://github.com/cschmertz/deptime/tree/main/bench)). Judge DepTime by the first run: the fixes since then were designed from its misses, so the later numbers are flattering.

| Check | Cases | First run | Now |
|---|---|---|---|
| Right package, name and breaking release for a traceback from a GitHub issue, on the [2026-11f holdout](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11f/results-2026-11-11.md) | 26, after every fix so far | 76.9% | 76.9% |
| Code that still works after an upgrade, flagged as broken by `check_symbols` (lower is better) | 2,113 uses from 171 upgrades of real projects, labelled by running them | 0.1% | 0.1% |
| The same, by `diff_versions(used_symbols=...)` | 2,113 | 0.1% | 0.1% |
| Code that breaks, flagged by `check_symbols` / `diff_versions` | 152 uses | 100% / 96.7% | 100% / 96.7% |
| `check-upgrade` on [real Dependabot and Renovate update pull requests](https://github.com/cschmertz/deptime/blob/main/bench/upgrade-check/prs/results-2026-11-16.md) (DT-QA-22, [second run](https://github.com/cschmertz/deptime/blob/main/bench/upgrade-check/prs/results-2026-11-16-run2.md)): updates read; breaking uses found; working uses flagged; pull requests failed with no real break | 264 pull requests from 247 projects; 3,339 labelled uses, 48 of them breaks | 93.9%; 95.8%; 1 of 3,291; 2 of 264 | 96.2%; 95.8%; 0 of 3,291; 0 of 264 |
| The same four, on the [2026-11e holdout](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11e/results-2026-11-10.md): tracebacks; working code flagged by `check_symbols` / `diff_versions`; breaks flagged | 28; 2,024 / 2,052 uses; 140 | 85.7%; 0.05% / 0.2%; 99.3% / 97.9% | 96.4%; 0.05% / 0.05%; 100% / 100% |
| The same four, on the [2026-11d holdout](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11d/results-2026-11-10.md): tracebacks; working code flagged by `check_symbols` / `diff_versions`; breaks flagged | 30; 2,224 / 2,279 uses; 176 | 86.7%; 0.9% / 0.7%; 96.6% / 96.0% | 93.3%; 0.09% / 0.09%; 99.4% / 99.4% |
| The same four, on the [holdout of 2026-11-09](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11c/results-2026-11-09.md): tracebacks; working code flagged by `check_symbols` / `diff_versions`; breaks flagged | 35; 1,801 / 1,841 uses; 128 | 88.6%; 0.5% / 0.3%; 99.2% / 99.2% | 97.1%; 0.06% / 0%; 100% / 98.4% |
| The same four, on the [holdout of 2026-11-08](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11b/results-2026-11-08.md): tracebacks; working code flagged by `check_symbols` / `diff_versions`; breaks flagged | 37; 2,503 / 2,542 uses; 146 (140 once six wrong labels are corrected) | 91.9%; 1.5% / 0.2%; 95.9% / 95.9% | 100%; 0% / 0%; 100% / 100% |
| The same four, on the [holdout of 2026-11-07](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11/results-2026-11-07.md): tracebacks; working code flagged by `check_symbols` / `diff_versions`; breaks flagged | 35; 3,600 / 2,399 uses; 130 | 88.6%; 3.8% / 0.3%; 97.7% / 76.9% | 100%; 0.1% / 0.1%; 98.4% / 98.5% |
| The six holdout sets' tracebacks put to a language model alone, with no tools: right package, name and breaking release ([results](https://github.com/cschmertz/deptime/blob/main/bench/model-alone/results-2026-11-14.md)) | 191, three runs per model | Claude Opus 5.5 83.2% to 83.8%; Claude Sonnet 5.5 81.7% to 82.7%; Claude Haiku 5.5 66.0% to 68.6%. DepTime's first runs on the same 191: 86.9% | run once |
| The 2026-11f holdout's upgrade checks put to a language model alone, with no tools: breaks found and working uses flagged ([results](https://github.com/cschmertz/deptime/blob/main/bench/model-alone/upgrades/results-2026-11-15.md)) | 2,265 uses (152 breaks) from 171 upgrades, one run per model | Claude Sonnet 5.5: 90.8% of breaks found, 0.2% of working uses flagged; Claude Haiku 5.5: 73.7% and 0.3%. DepTime's first runs on the same uses: 96.7% and 0.1% | run once |
| The same two comparisons with two cheaper models from other vendors, through OpenRouter ([tracebacks](https://github.com/cschmertz/deptime/blob/main/bench/model-alone/results-2026-11-15.md), [upgrades](https://github.com/cschmertz/deptime/blob/main/bench/model-alone/upgrades/results-2026-11-15.md)) | 191 tracebacks; 2,265 uses, one run per model | Tracebacks: DeepSeek V4.1 Flash 59.2%, Qwen3.5 9B 18.8% (DepTime 86.9%). Upgrades, breaks found and working uses flagged: DeepSeek 80.9% and 2.7%, Qwen 30.9% and 15.4% (DepTime 96.7% and 0.1%) | run once |
| The same small model, Qwen3.5 9B, with DepTime's five tools available ([results](https://github.com/cschmertz/deptime/blob/main/bench/model-alone/with-tools/results-2026-11-15.md)) | 191 tracebacks; 2,265 uses, one run | Tracebacks 86.4% (alone 18.8%; DepTime by itself 94.8% now, 86.9% first run). Upgrades: 82.2% of breaks found and 4.5% of working uses flagged (alone 30.9% and 15.4%; DepTime 96.7% and 0.1%) | run once |
| The same model's upgrade checks with the six tools, `check_upgrade` among them (DT-QA-23, [results](https://github.com/cschmertz/deptime/blob/main/bench/model-alone/check-upgrade/results-2026-11-16.md)) | 2,265 uses, one run | 75.0% of breaks found and 3.5% of working uses flagged (five tools: 82.2% and 4.5%; DepTime 96.7% and 0.1%) | run once |
| Claude Code (Sonnet 5.5) fixing a dependency update's confirmed breaks, alone / with `/deptime:fix-upgrade` / with the plugin installed and not named (DT-QA-05, [results](https://github.com/cschmertz/deptime/blob/main/bench/agent-fix/results-2026-11-16.md)): sessions that leave every break fixed | 25 real updates (137 breaks), two runs alone and with the command, one with the plugin | 80% / 64% / 80%; no arm broke anything new; the command took a third of the turns and changed a tenth of the unasked lines. Bar missed: the command's instructions on `unknown` answers and a report too big to read lost it | run once |
| The same, on 66 new broken updates with the reworked command (DT-QA-26, [results](https://github.com/cschmertz/deptime/blob/main/bench/agent-fix/qa26/results-2026-11-16.md)): alone / with `/deptime:fix-upgrade` | 66 real updates (379 breaks), one run each | 69.7% / 72.7%; breaks fixed 78.4% / 81.5%; sessions with a new break 4 / 2; the command in about half the turns. Bar missed on its 80% line: the command's "leave the line if the fix isn't clear" lost 3 of the 4 tasks only the agent alone fixed | run once |
| The same, on 60 broken updates neither had seen, after fixing what lost QA-26 (DT-QA-27, [results](https://github.com/cschmertz/deptime/blob/main/bench/agent-fix/qa27/results-2026-11-17.md)): alone / with `/deptime:fix-upgrade` | 60 real updates (373 breaks), one run each | 76.7% / 86.7%; breaks fixed 89.3% / 96.0%; sessions with a new break 3 / 1; the command in about half the turns. Bar met | run once |
| Tracebacks with another cause (a typo, a broken install, a file named like the package, the project's own bug) that `explain_error` blames on a release, lower is better (DT-QA-17, [results](https://github.com/cschmertz/deptime/blob/main/bench/false-blame/results-2026-11-17.md)) | 30 from closed GitHub issues, one per package | 0 of 30 (95% interval 0% to 11.4%) | run once |
| Right package, name and breaking release for a traceback from a GitHub issue | 32 collected 2026-11-05 | 68.8% | 100% |
| The same, on a holdout fixed in advance from [TimeMachine-bench](https://github.com/tohoku-nlp/timemachine-bench) | 29 | 72.4% | 96.6% |
| Error kind and package read from the traceback: scripts, pytest, chained exceptions, `ExceptionGroup`, IPython, Python 3.10–3.14 | 736, holdout fixed in advance | 93.8% | 100% |
| Replacement hints at confidence ≥ 0.75 that are right | hints for 50 documented migrations | 11 of 11 | 42 of 45 |
| Documented migrations that get a right hint at ≥ 0.75 | 50 | 11 | 42 (32 without hand-checked hints from migration guides) |
| Names that exist at runtime but read as absent | 2,428 sampled at runtime from 10 dynamic packages | 5.1% | 1.2% |

`check_symbols` answers `unknown`, with the reason, where the index can't tell (a path through an instance such as `pytest.mark`, or into a distribution it doesn't cover): 66 working uses and 5 breaks of the 2026-11f holdout (28 working uses of 2026-11e's, 55 working uses and 2 breaks of 2026-11d's, 40 working uses and 4 breaks of 2026-11-09's, 43 working uses of 2026-11-08's, 79 working uses and 4 breaks of 2026-11-07's since that answer was added), left out of its rates above. Its [misses](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11f/results-2026-11-11.md#misses) are new items in the [backlog](https://github.com/cschmertz/deptime/blob/main/BACKLOG.md), to be scored on the next set. Both tools' two false alarms on 2026-11f are one scikit-learn import from a compiled module, which the index can't read; most of its traceback misses are errors as a web page pastes them (a keyword in backticks, `__init__` printed as `init`). The fixes for the 2026-11d holdout's misses are in the index of 2026-11-10 (`SNAPSHOT_FORMAT` 26): 2 of its 20 and 17 false alarms are left per tool, chromadb's embedding functions, which its `__init__` writes into `globals()`, and the dspy placeholder release that gave 15 of each is now read from the distribution it installs. Since 2026-11-10 the holdout's rule reads a parameter `diff_versions` reports as required at the older release going down (polars 0.19.6's `int_range(n)`), and one passed by position (`StateGraph(State)`), so its "now" figures for `diff_versions` count both right.

Across all 393 real tracebacks in the benchmark, including the ones fixes were designed from, DepTime names the right package, name and release for 97.2%, and a right replacement is among its first three for 74.6%. CI re-scores them and the holdouts on every code change and every published index, and fails on a drop of more than 2 points, on any traceback case that used to pass and now fails, or on a false-positive rate up by more than 1.

## Case format (`cases/*.jsonl`, one JSON object per line)

| Field | Meaning |
|---|---|
| `id` | Stable slug |
| `source` | URL of the GitHub issue / Stack Overflow post / migration guide it came from, or `"constructed"` |
| `evidence` | URL(s) backing the expected answer: changelog, release notes, maintainer reply, or the library's source at a tag. **Never DepTime's own output**, or the benchmark is circular |
| `traceback` | Verbatim text, trimmed of secrets and personal paths |
| `installed` | `{package: version}` the reporter was running |
| `expected.package` | Distribution that changed |
| `expected.symbol_suffix` | End of the expected `finding.symbol`, or of `finding.defined_at` (where the object is defined, DT-CORE-55), e.g. `Client.__init__(proxies)` |
| `expected.issue` | `symbol_removed` / `param_removed` / `module_removed` / … |
| `expected.last_version_with` | Optional; checked as a prefix (`"1.5"` matches `1.5.3`) |
| `expected.first_version_with` | Optional, for `*_not_yet_added`; checked as a prefix against DepTime's `first_version_with` |
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
- Report per-tier accuracy and the share of `known_gap` cases. CI fails on a drop of more than 2 points in any metric, or on any single case that passed in the baseline and fails now.

Boundary and replacement only count when the root cause is right. The headline metric, `root_cause_and_boundary`, counts a case as passed when the root cause is right and the boundary matches (or the case doesn't expect one).

## Running it

```bash
uv run deptime index pull                                                      # the published index, into DEPTIME_HOME
uv run python -m deptime.bench bench/cases                                     # per-case report and per-tier table
uv run python -m deptime.bench bench/cases --baseline bench/baseline.json      # exit 1 on a regression
uv run python -m deptime.bench bench/cases --write-baseline bench/baseline.json  # record an intended change
```

`.github/workflows/bench.yml` runs the baseline check on code changes and after every `build-index` run. A regression is measured on the cases that both the baseline and the current run contain. So adding new cases never fails it. Any one case that used to pass and now fails does, even when another fix keeps the totals level. After a fix makes cases pass, write a new baseline so they're protected too.

The seed file (`cases/seed.jsonl`) contains well-documented, real-world breaks. Cases marked `known_gap` say which backlog item fixes them.

## Curated hints (DT-DATA-08)

`src/deptime/curated.toml` pairs names with the replacement their project's migration guide or changelog gives, with the quote and URL. ADR 0006 sets what each must pass before it ships: `uv run pytest -m network tests/test_curated.py` checks every quote is on its page, the offline suite checks each quote names both names, and `uv run python bench/curated_check.py` (also run by `bench.yml`) checks the index has both. A guide can be the source of both a curated hint and a benchmark case, so the report also gives the replacement without curated hints (`replacement_uncurated`).

## Holdout (DT-QA-04)

`tmb/` holds 29 cases from TimeMachine-bench, an outside benchmark, collected after the fixes designed from `cases/` and scored once under a rule fixed in advance. Its first run, 72.4% root cause + boundary, is the number to quote; see `tmb/README.md` and `tmb/results-2026-10-31.md`. `bench.yml` checks it against `tmb/baseline.json`.

## A model alone (DT-QA-16)

`model-alone/` puts the six holdout sets' 191 tracebacks to a language model that has no tools and answers from
memory, and scores the answers with this benchmark's scorer. The prompt, the models and the rule were committed before
any model was asked, and it was run once, on 2026-11-14, three runs per model. Claude Opus 5.5 names the right package,
name and breaking release for 83.2% to 83.8%, Claude Sonnet 5.5 for 81.7% to 82.7% and Claude Haiku 5.5 for 66.0% to
68.6%, beside 86.9% for DepTime's first runs. The models name the change more often than DepTime (98% to 100% of root
causes for the two larger ones, against 87.4%) and the replacement far more often (87% to 99% of 85, against 48.2%).
DepTime is ahead on the release wherever the installed version doesn't give it away: 88.1% against 46% to 66% on the 59
cases where the boundary isn't the installed release itself. See `model-alone/README.md` for the protocol and
`model-alone/results-2026-11-14.md` for every table.

## A model alone on the upgrade checks (DT-QA-18)

`model-alone/upgrades/` asks the same of the upgrade checks: the 2026-11f holdout's 2,265 labelled uses, one question
per upgrade (the package, both releases and the uses), put to Claude Sonnet 5.5 with no tools, under a protocol and
four rules that know nothing committed first. Run once, on 2026-11-15: Sonnet found 90.8% of the 152 breaks and
flagged 0.2% of the 2,113 working uses, beside 96.7% and 0.1% for DepTime's first run; by upgrade, 24 of the 27 with a
break against DepTime's 26, and no false alarm on the 144 clean ones for either. Claude Haiku 5.5, added before it
was asked, found 73.7% and flagged 0.3%: 18 of the 27 broken upgrades, and false alarms on 3 clean ones. The best rule that knows nothing finds
90.1% while flagging 53.2%. Only 21 breaks come from releases newer than the model's training data, too few to test
the cutoff. See `model-alone/upgrades/README.md` and `model-alone/upgrades/results-2026-11-15.md`.

## Cheaper models (DT-QA-19)

`model-alone/cheaper-models.md` puts both comparisons to two cheaper models from other vendors through OpenRouter,
with the same prompts and scoring, protocol committed first, one run each, on 2026-11-15. DepTime's lead grows as the
model gets smaller or cheaper. On the tracebacks, DeepSeek V4.1 Flash names the right package, name and release for
59.2% and Qwen3.5 9B for 18.8%, against 86.9% for DepTime; Qwen is the first model DepTime beats on replacements too
(22.4% against 48.2%). On the upgrade checks, DeepSeek finds 80.9% of breaks but flags 2.7% of working uses (a false
alarm in 18 of 144 clean upgrades), and Qwen finds 30.9% while flagging 15.4%. OpenRouter's free models were busy
throughout and weren't run.

## A small model with DepTime's tools (DT-QA-20)

`model-alone/with-tools/` asks Qwen3.5 9B both comparisons again with DepTime's five MCP tools available, exactly as
the server lists them, protocol committed first, one run, on 2026-11-15. On the tracebacks the tools take it from
18.8% to 86.4%, level with DepTime's own first run; it called a tool on 188 of 191 questions unprompted. On the upgrade
checks it finds 82.2% of breaks (from 30.9%) but flags 4.5% of working uses (from 15.4%), against DepTime's 96.7% and
0.1% by itself: it loses much of DepTime's precision where it must combine many tool answers into one verdict per use.
See `model-alone/with-tools/results-2026-11-15.md`.

## The same model with `check_upgrade` (DT-QA-23)

`model-alone/check-upgrade/` asks the upgrade checks again with the six tools of 0.2.0, protocol committed first, one
run, on 2026-11-16. It finds 75.0% of breaks and flags 3.5% of working uses (five tools: 82.2% and 4.5%), missing the
bar of 90% and 1%. It called `check_upgrade` in 76 of 171 upgrades, but 65 of its 83 calls passed code the tool
couldn't read (the question's notation, or no import), so the result says little about the tool on real code and
much about a small model judging each use itself. See `model-alone/check-upgrade/results-2026-11-16.md`.

## An agent fixing an update's breaks (DT-QA-05)

`agent-fix/README.md` is the protocol, committed before any agent ran; `agent-fix/results-2026-11-16.md` the result.
Claude Code on Sonnet 5.5 fixed 25 real broken updates (2026-11f's 12 to a newer version, DT-QA-22's 13 pull
requests) alone, with `/deptime:fix-upgrade`, and with the plugin installed and not named; the probe judged the fixed
code at the new version. Alone 80% of sessions, with the command 64%, installed and not named 80% (it called DepTime in
all 25). The command lost on DepTime's `unknown` answers, which it told the agent to leave, on a pin
`check-upgrade` can't read, and on reports longer than the shell shows, in a sandbox that denied saving them. The
hand-off is reworked before 1.0 (DT-OSS-17 waits).

## The reworked hand-off on new updates (DT-QA-26)

`agent-fix/qa26/README.md` is the protocol, committed before any agent ran; `agent-fix/qa26/results-2026-11-16.md`
the result. The same agent on 66 broken updates the reworked command (0.2.2) was never shaped on, from the 2026-11 to
2026-11e holdouts: alone 69.7% of sessions, with the command 72.7%, fewer new breaks (2 against 4) and about half the
turns. The bar's first line (80%) is missed; the command no longer loses to the agent alone. Of the 4 tasks only the
agent alone fixed, 3 were breaks the command's agent found and then left, as its step 3 says when the fix isn't clear,
and 1 a required argument DepTime didn't see (a streamlit binding, DT-DATA-83). Per the protocol, a miss leaves the
command out of 1.0's promise; the maintainer chose instead to fix both causes and measure again on new updates
(DT-QA-27) before 1.0.

## The hand-off on updates nobody had seen (DT-QA-27)

`agent-fix/qa27/README.md` is the protocol, committed before any agent ran; `agent-fix/qa27/results-2026-11-17.md`
the result. After DT-GTM-09 (rewrite a break with no stated replacement instead of leaving it) and DT-DATA-83 (the
streamlit binding), 0.2.3 was measured on a holdout collected after its release, `holdout/2026-11g/`: 60 broken updates
of 58 projects, so neither DepTime nor the command had been shaped on any of them. Alone, the agent left the code
working in 76.7% of sessions; with the command, 86.7%, with one session whose fix broke something new against three,
96.0% of the 373 confirmed breaks fixed against 89.3%, and about half the turns. All three lines of the bar are met.
Paired by task, the command won 8 and lost 2 (McNemar's exact p = 0.109). Its two losses: a pydantic model's required
field DepTime doesn't check (crewai, held until it recurs), and a bug the project already had, which the command
leaves by design. DT-OSS-17 freezes the hand-off as it is.

## Per-release holdout (DT-QA-07 to DT-QA-11)

`holdout/` holds a new set before each minor release, committed before DepTime runs on it and scored once: tracebacks
like these, and code from real projects that keeps working (or breaks) after an upgrade, labelled by importing each
release in a sandbox, asked through `check_symbols` and `diff_versions` for precision, recall and false-positive rate.
The first set, `holdout/2026-11/`, scored 88.6% root cause + boundary on 35 new tracebacks, and a false-positive rate of
3.8% (`check_symbols`) and 0.3% (`diff_versions`) on 3,600 working uses; see `holdout/README.md` and
`holdout/2026-11/results-2026-11-07.md`. The second, `holdout/2026-11b/` (DT-QA-08), the first scored after the fixes
designed from 2026-11's misses, scored 91.9% on 37 new tracebacks, and 1.5% and 0.2% false positives on 2,542 working
uses, with 95.9% recall for both tools on 146 breaks (100% once six labels the probe got wrong are corrected); see
`holdout/2026-11b/results-2026-11-08.md`. The third, `holdout/2026-11c/` (DT-QA-09), the first scored after the fixes
designed from 2026-11b's misses, scored 88.6% on 35 new tracebacks, and 0.5% and 0.3% false positives on 1,841 working
uses, with 99.2% recall for both tools on 128 breaks; see `holdout/2026-11c/results-2026-11-09.md`. The fourth,
`holdout/2026-11d/` (DT-QA-10), the first scored after the fixes designed from 2026-11c's misses, scored 86.7% on 30
new tracebacks, and 0.9% and 0.7% false positives on 2,279 working uses (15 of each tool's from one upgrade, a dspy
placeholder release), with 96.6% and 96.0% recall on 176 breaks; see `holdout/2026-11d/results-2026-11-10.md`. The
fifth, `holdout/2026-11e/` (DT-QA-11), the first scored after the fixes designed from 2026-11d's misses, scored 85.7% on
28 new tracebacks, and 0.05% and 0.2% false positives on 2,052 working uses, with 99.3% and 97.9% recall on 140 breaks;
see `holdout/2026-11e/results-2026-11-10.md`. The sixth, `holdout/2026-11f/` (DT-QA-12), the first scored after the
fixes designed from 2026-11e's misses, scored 76.9% on 26 new tracebacks (four of its six misses are errors as a web
page pastes them), and 0.1% and 0.1% false positives on 2,113 working uses, with 100% and 96.7% recall on 152 breaks;
see `holdout/2026-11f/results-2026-11-11.md`. All six sets' tracebacks are in `cases/` now; `bench.yml` checks each
set's uses against its `baseline.json`.

## How often a real update is to a covered package (DT-QA-28)

`coverage-share/README.md` is the protocol, committed before any pull request was collected. Of the major library
updates in 12,766 of Dependabot's pull requests to Python projects (90 days, read on 2026-11-23), 31.7% were to one of
the 53 packages covered then, under the protocol's 60% bar (`coverage-share/results-2026-11-23.md`). With the six
packages its shortlist put first (pillow, uvicorn, cryptography, oauthlib, gunicorn, python-dateutil), the same sample
scores 60.3% (95% CI 58.2-62.3%; `coverage-share/results-2026-11-27.md`). The six were picked from that sample, so a
new one would likely score lower. pypdf was added afterwards and isn't counted.

## Published as a benchmark (DT-OSS-14)

`dataset/` builds the six holdout sets, 2026-11g, DT-QA-22's update pull requests, the real tracebacks in `cases/` and
the false-blame set into one download for measuring other tools and models: 24,873 labelled uses (1,287 breaks) and
423 tracebacks, with a scorer that needs only Python's standard library, DepTime's answers and the DT-QA-18/19 models'
answers as reference. On DepTime's answers its scorer matches `holdout/score.py` use for use and `deptime.bench` case
for case. See `dataset/README.md`, which is also the download's README; the download is the `benchmark-v1` release.
