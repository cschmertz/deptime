# DepTime backlog

**Maintenance mode since 2026-11-13.** DepTime does what its alpha set out to do, and the index rebuilds itself twice
a day. Work now starts from a reported wrong answer or a failed scheduled check, not from a schedule. Everything done
before that date, with its measurements, is in [docs/backlog-archive.md](docs/backlog-archive.md), which is frozen.

**How to read this**
- **IDs** look like `DT-<EPIC>-NN` and are referenced from the docs and code. Put the ID in commit messages and PR
  titles. An ID that isn't on this page is in the archive.
- **Priority:** `P0` blocks a release · `P1` needed · `P2` nice to have · `P3` rare.
- **Size:** `S` ≤ ½ day · `M` 1–2 days · `L` 3–5 days.
- **Direction:** free, open source (MIT), local-first ([ADR 0007](docs/adr/0007-open-source-local-first.md)).
- **Keeping this page short:** one line per change. Measurements go in `bench/**/results-*.md` and in commit bodies,
  not here.

---

## Current state: public alpha, v0.1.13

- **Released:** 0.1.13 is on PyPI, the MCP Registry and GitHub (2026-11-11). `main` has no unreleased code.
- **Index:** 53 packages and 6,800 releases on 2026-11-13 (`SNAPSHOT_FORMAT` 26, schema 2); `index.sqlite.xz` is
  51.0 MB and the `.gz` 78.5 MB. `build-index` runs at 04:17 and 16:17 UTC, replaces the rolling `index` release and
  signs it. A build with no extraction change usually takes under 20 minutes; a `SNAPSHOT_FORMAT` bump means one cold
  build of about 3.7 hours.
- **Checks that run without anyone:** `ci` on every push (489 offline tests), and `bench` on every code change and
  every published index, which fails on a drop of more than 2 points or a false-positive rate up by more than 1.
- **How well it works:** the README's table, which leads with first runs on unseen cases. Across the six holdout sets'
  first runs, `explain_error` names the right package, name and release for 86.9% of tracebacks (166/191), and
  `check_symbols` has flagged under 1% of working code for four sets running while finding 96.6% or more of the
  breaks.

| Milestone | Exit criterion | Status |
|---|---|---|
| **M0 Spikes** | S1 go/no-go (false-absent ≤ 10%); S2–S7 answered | S1 to S4 passed (S1 at 1.2%); S5 done but for the GUI clients; S6 and S7 open, below |
| **M1 Local alpha** | Public repo; index published; `uvx deptime mcp` works from PyPI; benchmark ≥ 70% | Met: every holdout's first run is above 70% |
| **M2 Distribution** | Registries, plugin, snippets, demo GIF, 5 rename-map pages | Registries, plugin, pages and the demo done; the hosting is below |
| **M3 Optional hosted** | Public endpoint for web clients | Not planned |

## Why maintenance mode

Six holdout sets were scored between 2026-11-07 and 2026-11-11, each after the fixes designed from the one before.

| First run on | 2026-11 | 11b | 11c | 11d | 11e | 11f |
|---|---|---|---|---|---|---|
| Tracebacks right (`explain_error`) | 88.6% | 91.9% | 88.6% | 86.7% | 85.7% | 76.9% |
| Working code flagged (`check_symbols`) | 3.8% | 1.5% | 0.5% | 0.9% | 0.05% | 0.1% |

- The false-positive rate fell and stayed down on code the fixes had never seen.
- The traceback score didn't rise. Each set of 26 to 37 tracebacks found new rare shapes and added 6 to 12 items.
- Each extraction fix adds a reader for one more thing a library does at import time, and a cold build.

So accuracy work on rare shapes stops here. The tool keeps answering at the level above, says `unknown` where it can't
tell, and gets fixed where someone shows it is wrong.

## Rules

1. **No new holdout sets.** The six sets, the benchmark and the golden tests stay as regression checks in CI. This
   closes DT-QA-13.
2. **A report comes first.** An item below is worked on when someone reports the wrong answer it causes (the "wrong
   answer" issue form), or when a scheduled check fails. The report becomes a benchmark case before the fix.
3. **Abstain before adding a reader.** Where the index can't follow what a library does, the order is: an `unknown` or
   `not_covered` answer with the reason (Invariant 2); then a `[[package.served]]` entry in `coverage.toml`; new
   extraction code only for a shape several packages share.
4. **Batch extraction changes.** Fixes that need a `SNAPSHOT_FORMAT` bump wait for each other and share one cold
   build. Query-side fixes don't need one.
5. **Release when a fix lands,** not on a schedule.
6. **No hosted instance,** and no new packages unless one is asked for (the coverage issue form). `deptime serve`
   stays for self-hosting ([docs/05](docs/05-self-hosting.md)).

## Upkeep

What still needs a person, about once a month:

- **Merge Dependabot's pull requests** when `ci` and `bench` are green. griffe is kept out of the grouped update on
  purpose: its upgrades change what extraction reads, so look at the benchmark first.
- **Keep the schedule alive.** GitHub disables scheduled workflows in a public repository after 60 days without
  repository activity. A merged pull request counts. If the index stops updating, re-enable `build-index` in the
  Actions tab.
- **Read a build's log before trusting it.** A green build can carry a release over from the previous index when its
  download or snapshot fails (Invariant 6). DT-QA-14 turns this into a check.

## To do

Small, and each one makes the mode above hold by itself.

- [ ] **DT-QA-14**: A scheduled health check that opens an issue · `P2` `M`
  - Why: nothing scheduled watches for decay. `bench` re-scores cases whose answers are known, so a new release that
    binds its names a new way lowers accuracy without failing anything, and a release that keeps failing to build is
    carried over without a word.
  - AC: once a month, (a) list each package whose newest PyPI release has been missing from the index, or carried
    over, for more than three days; (b) run S1's runtime probe (`bench/s1/`) on the newest release of its ten
    dynamic packages and compare with the index. It opens an issue when (a) lists anything or (b) is over 3% false
    absent.
- [ ] **DT-OSS-15**: Dependency updates that merge themselves · `P2` `S`
  - AC: Dependabot's minor-and-patch groups merge once `ci` and `bench` pass, a few days after the release they
    bring; griffe and major versions still wait for a person. Each merge is also the activity that keeps the schedule
    alive.

## Done since 2026-11-13

- [x] **DT-OSS-02** (2026-11-13): README proof. An animated demo drawn from the real answer (`tools/demo_svg.py`),
  "How well it works" as four first-run numbers with the full table in `bench/README.md`, a limits section and a
  status line.

## Optional, once

One-off measurements and finishing touches. None is needed for upkeep, and none starts a series.

- [ ] **DT-QA-05** with **DT-SPIKE-06**: does an agent fix more dependency breakages with DepTime, and does it call
  it unprompted? · `P2` `L` (pilot `M`)
  - The one question the benchmarks don't answer. The pilot is as designed in the archive: about 20 covered tasks ×
    2 arms × 3 runs, success by each task's own tests, the pass bar written down before the run. One run, reported
    in the README whichever way it comes out.
- [ ] **DT-GTM-02**: Host the generated rename-map pages (GitHub Pages) · `P2` `S`
- [ ] **DT-GTM-05**: Try the Cursor rule and the AGENTS.md snippet in a fresh project · `P2` `S`
- [ ] **DT-SPIKE-05**, **DT-SPIKE-07**: Cursor, VS Code and Claude Desktop, and a first run on Windows · `P2` `S`
  - Until then the README lists the GUI clients as not checked.
- [ ] **DT-OSS-14**: Publish the holdout sets as a benchmark · `P2` `M`

## Held

Known gaps, most with a case kept as `known_gap` in the benchmark. None is scheduled. A report moves its item to
"To do", and the groups are in the order to take them. Each item's detail is in the archive under its ID.
"Extraction" means the fix needs a `SNAPSHOT_FORMAT` batch (rule 4).

**Wrong answers** (a false alarm, a wrong finding or a wrong replacement)

| ID | Gap | Fix is in |
|---|---|---|
| DT-DATA-75 | Names in compiled extension modules read `missing` (scikit-learn's `tree._tree`) | extraction |
| DT-DATA-51 | Names computed at import time read absent (instructor's provider factories, chromadb's embedding functions) | extraction |
| DT-DATA-77 | A keyword only a decorator takes reads `never_existed` (huggingface_hub's `use_auth_token`) | extraction |
| DT-CORE-64 | A keyword that went with a removed `**kwargs` reads `never_existed` (mcp's `FastMCP(description)`) | query |
| DT-DATA-78 | openai 1.0's removed resources point to response types of the same name | curated hints, pairing rule |
| DT-DATA-73 | matplotlib 3.10's `cm.ScalarMappable` reads private, so its members read missing | extraction |
| DT-DATA-71 | Names a module only imports read public through a membership-test shim (instructor's `hooks`) | extraction |
| DT-DATA-72 | A name only a stub binds reads present where the runtime raises (numpy 2.0's `cast`) | extraction |

**Missed breaks and deprecations**

| ID | Gap | Fix is in |
|---|---|---|
| DT-CORE-60 | A module that became a function: names under it read `unknown` (fastmcp 3's `prompts.prompt`) | query |
| DT-CORE-34 | Constructor keywords of dataclasses and pydantic models; required arguments | query, extraction |
| DT-DATA-09 | Pydantic model fields as `__init__` keywords | extraction |
| DT-CORE-62 | `diff_versions` on a deprecation in force at both ends, or dropped before `to` | query |
| DT-CORE-61 | `diff_versions` is silent where `check_symbols` answers `unknown` | query |
| DT-DATA-76 | A name a lazy table serves doesn't carry its target's deprecation (langchain-community's `llms.OpenAI`) | extraction |

**Tracebacks with no finding**

| ID | Gap | Fix is in |
|---|---|---|
| DT-CORE-63 | Four shapes of a traceback as a web page pastes it | query (parser) |
| DT-CORE-49 | A callable that only raises its own message | query |
| DT-CORE-65 | An attribute missing on an instance of the project's own subclass | query |
| DT-DATA-68 | Private methods' parameters, and the members of classes in internal modules | extraction |
| DT-DATA-02 | numpy names only compiled code registers (`ndarray.tostring`) | extraction |

**Missing replacements**

| ID | Gap | Fix is in |
|---|---|---|
| DT-CORE-40 | Replacements the package's own text states but the holdout tracebacks don't get | hint mining, curated hints |
| DT-DATA-70 | A renaming wrapper bound to the old name; "Deprecated name for the X keyword argument" | extraction |
| DT-DATA-79 | A table of removed keywords and their messages (fastmcp 3's `_REMOVED_KWARGS`) | extraction |
| DT-DATA-80 | A module's warning when imported | extraction |
| DT-CORE-04 | Signature-similarity "moved" heuristic | hint mining |

## Not planned

- **DT-QA-13** (the next holdout set): closed by rule 1.
- **DT-HOST-01 to 04** (a public hosted instance): running one is continuous work. The code for self-hosting stays.
- **DT-DATA-74** (hundreds of packages instead of 53): the build time and the checks grow with it.
- Parked as before: an npm/TypeScript index, a bulk dataset export, MCP resources for rename maps.

## If active development resumes

In this order:

1. **Measure first:** DT-QA-05's pilot, if it hasn't been run.
2. **Whole-project checks:** DT-CORE-59 (a project's deprecation report), DT-CORE-58 (a dependency change checked
   before it lands), DT-GTM-06 (the same as a GitHub Action on update pull requests) and DT-CORE-57 (code checked as
   it is written). The upgrade checks are the tool's strongest results, and no command runs them over a whole
   project yet.
3. **A runtime floor for dynamic packages (DT-DATA-81, proposed):** ADR 0002 keeps a sandboxed runtime probe in
   reserve for packages that static reading can't follow. A name that probe sees in a recent release would read
   `unknown` at worst, never `missing`, in place of one more static reader per idiom. It needs ADR 0002 revisited
   first.
