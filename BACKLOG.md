# DepTime backlog

**Active again since 2026-11-15: the upgrade check.** Maintenance mode ran from 2026-11-13. It ended when the
model comparisons (DT-QA-16 to DT-QA-20) showed where DepTime adds the most: deciding, with no model in the loop,
which of a project's code a dependency update breaks. Everything done before 2026-11-13, with its measurements, is in
[docs/backlog-archive.md](docs/backlog-archive.md), which is frozen.

**How to read this**
- **IDs** look like `DT-<EPIC>-NN` and are referenced from the docs and code. Put the ID in commit messages and PR
  titles. An ID that isn't on this page is in the archive.
- **Priority:** `P0` blocks a release · `P1` needed · `P2` nice to have · `P3` rare.
- **Size:** `S` ≤ ½ day · `M` 1–2 days · `L` 3–5 days.
- **Direction:** free, open source (MIT), local-first ([ADR 0007](docs/adr/0007-open-source-local-first.md)).
- **Keeping this page short:** one line per change. Measurements go in `bench/**/results-*.md` and in commit bodies,
  not here.

---

## Current state: stable, v1.1.4

- **Released:** 1.1.3 (2026-11-21) is on PyPI, the MCP Registry and GitHub: the six tools, the upgrade check (a
  command, a GitHub Action, an MCP tool and a Claude Code plugin command), and the 1.x promise in CHANGELOG.md's
  versioning section: the contract only grows.
- **Index:** 53 packages and 6,823 releases on 2026-11-17 (`SNAPSHOT_FORMAT` 29, schema 2); `index.sqlite.xz` is
  51.4 MB and the `.gz` 79.2 MB. `build-index` runs at 04:17 and 16:17 UTC, replaces the rolling `index` release and
  signs it. A build with no extraction change usually takes under 20 minutes; a `SNAPSHOT_FORMAT` bump means one cold
  build of about 3 hours (format 27's: 2 h 48 min).
- **Checks that run without anyone:** `ci` on every push (689 offline tests), and `bench` on every code change and
  every published index. `bench` fails on a benchmark drop of more than 2 points, a traceback case that passed in the
  baseline and fails now, or a false-positive rate up by more than 1, and on the published index itself (`tests/test_golden.py`, DT-QA-03): older than 48 h, a package's newest
  release on PyPI for more than 24 h without being indexed, or a Tier A package's newest release without the names
  its documentation uses.
- **How well it works:** the README's table, which leads with first runs on unseen cases. Across the six holdout sets'
  first runs, `explain_error` names the right package, name and release for 86.9% of tracebacks (166/191), and
  `check_symbols` has flagged under 1% of working code for four sets running while finding 96.6% or more of the
  breaks. A model alone, with no tools, scores 66% to 84% on the same tracebacks (DT-QA-16), and Claude Sonnet 5.5 alone
  finds 90.8% of 2026-11f's breaks while flagging 0.2% of its working code (DT-QA-18).

| Milestone | Exit criterion | Status |
|---|---|---|
| **M0 Spikes** | S1 go/no-go (false-absent ≤ 10%); S2–S7 answered | S1 to S4 passed (S1 at 1.2%); S5 done; S6 answered by DT-QA-05 (yes, 25 of 25); S7 passed (DT-SPIKE-07) |
| **M1 Local alpha** | Public repo; index published; `uvx deptime mcp` works from PyPI; benchmark ≥ 70% | Met: every holdout's first run is above 70% |
| **M2 Distribution** | Registries, plugin, snippets, demo GIF, 5 rename-map pages | Met: registries, plugin, demos, and the rename maps hosted at cschmertz.github.io/deptime (DT-GTM-02) |
| **M3 Optional hosted** | Public endpoint for web clients | Not planned |

## Why the upgrade check

On the 2026-11f holdout's upgrades (2,265 uses of covered packages in 171 upgrades of real projects, labelled by
running them; first runs, abstentions counted as not flagged):

| Who decides | Breaks found | Working code flagged | Clean upgrades with a false alarm |
|---|---|---|---|
| DepTime by itself (`check_symbols`) | 96.7% | 0.1% | 0 of 144 |
| Claude Sonnet 5.5 alone | 90.8% | 0.2% | 0 of 144 |
| DeepSeek V4.1 Flash alone | 80.9% | 2.7% | 18 of 144 |
| Qwen3.5 9B alone | 30.9% | 15.4% | 74 of 144 |
| Qwen3.5 9B with DepTime's tools | 82.2% | 4.5% | 25 of 144 |

- DepTime by itself already answers at the level a check on pull requests needs, offline, free and the same every
  time. No command runs it over a whole project yet.
- A model in the loop costs precision: Qwen with the tools re-decided uses DepTime had right. So the check decides
  with DepTime alone, and a model only drafts the fix afterwards, in the user's own agent (ADR 0006: no model inside
  DepTime).
- The traceback work of maintenance mode stands: first runs on six unseen sets stayed between 77% and 92% while each
  set found new rare shapes, so accuracy work on rare shapes still waits for a report.

## Rules

1. **A protocol before every first score.** Each new measurement is fixed and committed before its first scored run,
   as DT-QA-16 to DT-QA-20 were, and reported whichever way it comes out.
2. **New holdouts only for the upgrade check** (DT-QA-22). The six traceback and use sets stay as regression checks;
   DT-QA-13 stays closed.
3. **A report comes first for the long tail.** Held items move up when someone reports the wrong answer they cause,
   or a scheduled check fails; the report becomes a benchmark case before the fix.
4. **Abstain before adding a reader.** `unknown` or `not_covered` with the reason first (Invariant 2); then a
   `[[package.served]]` entry in `coverage.toml`; new extraction code only for a shape several packages share.
5. **Batch extraction changes.** Fixes that need a `SNAPSHOT_FORMAT` bump share one cold build.
6. **Release when a feature lands.** No hosted instance ([docs/05](docs/05-self-hosting.md) stays for self-hosting),
   and no model call inside DepTime (ADR 0006).

## Upkeep

What still needs a person, about once a month:

- **Merge Dependabot's pull requests** when `ci` and `bench` are green. griffe is kept out of the grouped update on
  purpose: its upgrades change what extraction reads, so look at the benchmark first.
- **Keep the schedule alive.** GitHub disables scheduled workflows in a public repository after 60 days without
  repository activity. A merged pull request counts. If the index stops updating, re-enable `build-index` in the
  Actions tab.
- **A red `bench` run after a build means the index is behind,** or a new release dropped a documented name: the
  checks above ran on it. A green build can still carry a release over when its download or snapshot fails
  (Invariant 6); `bench` catches that once the release is a day old.

## To do

In order. The first line of each phase is what it delivers.

Phases 2 and 3 (the command and the GitHub Action) shipped in 0.2.0. DT-QA-22 met every bar on real update pull
requests; its three wrong answers are fixed in the published index (DT-CORE-72, DT-DATA-82, DT-DATA-51: a second,
in-sample run flags 0 of 3,291 working uses and fails no pull request without a confirmed break,
`bench/upgrade-check/prs/results-2026-11-16-run2.md`).
- [x] **DT-CORE-73** (2026-11-17): a name bound to a union of the package's classes (`A | B`, `Union[A, B]`) is
  recorded as a union, and `check_symbols` reads a member of it as missing: mcp 2.0's
  `types.ClientRequest.model_construct`, DT-QA-22's last missed break. `SNAPSHOT_FORMAT` 29.
- [x] **DT-CORE-76** (2026-11-17): a name listed under its missing module or class keeps its own replacement in the
  report and the agent block (mcp 2's `mcp.server.fastmcp` is gone; its `FastMCP` is proven to be `MCPServer`), and
  the block names each line once. The instructions are unchanged: the hand-off gains a fact, not a rework.
- [x] **DT-GTM-10** (2026-11-17): the README leads with the upgrade check: its tagline, a second demo drawn by
  `tools/demo_svg.py` from a real `check-upgrade` run (`docs/assets/upgrade-*.svg`), and "Get started" with
  `uvx deptime check-upgrade` before the MCP setup. The error demo moves to its own section.
- [x] **DT-GTM-11** (2026-11-18): the plugin's README and manifest lead with the upgrade fix, as the plugin
  directory now shows them as the listing; the README says the command runs `git`.
- [x] **DT-DATA-78** (2026-11-18): openai 1.0's removed `ChatCompletion`, `Embedding`, `Image` and the rest of the
  family point to the client resource that replaces them (curated, from the migration guide), and a removed class
  is never paired with a same-name class in a `types` module that has none of its methods (64 openai pairings).
- [x] **DT-CORE-61** (2026-11-18, PR #22 by rise11hx-cmyk): `diff_versions` lists each used name `check_symbols`
  reads `unknown` at an end of the range in a new `unchecked_uses` field, with its reason, and the note points to it.
  Still to do: count such an answer as an abstention in the bench.
- [x] **DT-CORE-77** (2026-11-18): a plain word before more prose ("use Python client", "use v3 code") and a name
  at a stored message's 400-character cut are no longer mined as replacements; weaviate's `weaviate.Client` gets the
  curated `weaviate.WeaviateClient` instead. Over the index's 4,138 deprecation messages only weaviate's two change.
- [x] **DT-CORE-78** (2026-11-18): replacements mined from "use the <kind> method X" (sqlalchemy 1.4's 8
  `Select.append_*` messages) and "`return X(...)` instead of `return X.member(...)`" (gradio 3.x's 31 component
  `update` messages); no other message changes. Part of DT-CORE-40.
- [x] **DT-CORE-62** (2026-11-19, PR #23 by rise11hx-cmyk): `diff_versions` lists used names deprecated at
  `to_version` in a new `deprecated_uses` field, wherever the deprecation began, and an in-range deprecation that
  ended before `to_version` carries `detail.until`.
- [x] **DT-CORE-60** (2026-11-19, PR #32 by swpswpswp): a name under a module that became a function reads
  `missing` once its own history ends (fastmcp 3's `prompts.prompt`), and `diff_versions` dates its removal; not
  under a `__getattr__`-served name or an alias. Upgrade-check bench: breaks found 97.8% → 98.5%, no new false alarm.
- [x] **DT-CORE-65** (2026-11-19, PR #41 by bfs2021): a private method missing from the project's own subclass is
  explained when another class still has it, if the traceback ran through the module of the one class that lost it
  (transformers' `GenerationMixin`); the one-class rule still comes first. Traceback bench: one more case passes.
- [x] **DT-CORE-49** (2026-11-19, PR #34 by swpswpswp): `explain_error` explains an error from a function that
  only raises, when its message matches the installed release's record exactly: a `now_raises` finding with
  `raises` and `raises_since` (llama-index-core's `ServiceContext.from_defaults`).
- [x] **DT-CORE-79** (2026-11-21): an update to a release the index doesn't have yet is listed as not checked
  (`not_indexed`, a warning annotation, exit 0), never "No breaks found"; from an older release the check goes as
  far as the newest indexed one (`newer_than_index`).
- [x] **DT-CORE-80** (2026-11-21): `check-upgrade` exits 2 whenever it couldn't run: an unexpected error, no index,
  an unreadable pin file (`pins_unread`), a version that isn't one; suppressions that aren't tables no longer stop
  it or `deptime mcp`.
- [x] **DT-CORE-81** (2026-11-21): files with a byte-order mark are read (Python files and pins); files over 2 MB
  are counted as unread.
- [x] **DT-CORE-82** (2026-11-21): `installed_as` distributions (`pydantic-ai`, `typer-slim`, ...) are checked as
  the covered package.
- [x] **DT-OSS-20** (2026-11-21): the index download replaces a damaged file of the current build (and records
  provenance only for verified bytes), caps decompression, refuses views and triggers, deletes an interrupted
  download's temp files, survives odd metadata times, refuses `--verify` before downloading when it can't verify,
  and reports a bad manifest, a damaged file or a full disk without a traceback.
- [x] **DT-CORE-83** (2026-11-21): untrusted text: every `!` before `[` and HTML attributes that fetch a URL are
  defused, the other default-ignorable characters removed; a changelog URL cleaning would change, and a
  Requires-Dist marker in a hint, go through the same check. No text in the published index changes.
- [x] **DT-HOST-05** (2026-11-21): the Docker image no longer trusts `X-Forwarded-For` by default; `fly.toml`,
  behind Fly's proxy, sets it.
- [x] **DT-OSS-21** (2026-11-21): the build starts only from a published index whose signed provenance verifies,
  and stops when the pull fails (`fresh` dispatches start from nothing); the shrink guard counts releases too.
- [x] **DT-DATA-85** (2026-11-21): ingestion: snapshots saved atomically, a damaged one is a cache miss; a PyPI
  outage rebuilds only the indexed releases, after retries; folder-link loops and backslash runs no longer hang a
  build; exception text stays on one log line. Snapshots are unchanged, so no `SNAPSHOT_FORMAT` bump.
- [x] **DT-CORE-84** (2026-11-21): a line number or argument count of thousands of digits no longer stops
  `explain_error`.
- [x] **DT-HOST-06** (2026-11-21): `serve --host` also sets the MCP transport's Host/Origin check; the JSON API
  checks bodies against the MCP tools' parameters and answers a malformed one with 400.
- [x] **DT-OSS-22** (2026-11-21): a release reaches PyPI only from a `v*` tag on `main` (the `pypi` environment's
  tag rule and a check in the job); rulesets stop `main` and `v*` tags being force-pushed or deleted. No required
  reviewer, so releases stay hands-off.
- [x] **DT-CORE-85** (2026-11-21): a name bound by `import google.genai` reads only chains under `google.genai`;
  `google.auth.default()` is no longer checked as google-genai's (also `llama_index.*`, `_pytest.*`).
- [x] **DT-CORE-86** (2026-11-21): a chained exception's block starts at its header or the chain marker before it,
  so a long chain parses in linear time (0.38 s → 0.01 s on 20K chars).
- [x] **DT-HOST-07** (2026-11-21): the JSON API runs tools in a worker thread, caps bodies at 4 MiB, takes only
  `application/json`, and on loopback checks Host/Origin on `/v1` and `/healthz` as `/mcp` does.
- [x] **DT-CORE-87** (2026-11-22, PR #58 by Burakceliks): `deptime explain --installed` refuses an item that
  isn't `PKG==VER` with a usage error (exit 2) instead of a traceback.

**Phase 4, the fix (the model's part, outside DepTime): done.** DT-QA-27 met its bar on broken updates nobody had
seen (`bench/agent-fix/qa27/results-2026-11-17.md`). Held from it: DepTime doesn't check a pydantic model's required
fields (crewai `Task(expected_output=)`, one case), until it recurs.

**Phase 5, 1.0: done.**
- [x] **DT-OSS-17** (2026-11-17): released 1.0.0 with the 1.x promise. Done in the end: nothing renamed, three
  fields described, Python 3.12 to 3.15 in CI, first launch on every OS (DT-SPIKE-07). Claude Desktop, VS Code and
  Cursor checked since (DT-SPIKE-05); the hand-off is frozen as it is at 0.2.3.

## Done since 2026-11-13

- [x] **DT-QA-17** (2026-11-17): 30 real errors with another cause (a typo, a broken install, a file named like the
  package, the project's own bug): `explain_error` blamed a release for none (`bench/false-blame/results-2026-11-17.md`).
- [x] **DT-SPIKE-05** (2026-11-17): Claude Desktop, VS Code and Cursor run `uvx deptime mcp` from the README's
  setup and give the right answer (`bench/s5/README.md`).
- [x] **DT-SPIKE-07** (2026-11-17): first launch from nothing to the right answer in 8 s on Linux, 10 s on
  Apple-silicon macOS and 20 s on Windows (`bench/s7/`). macOS with an x86_64 `uv` takes about 3 minutes, compiling
  `cryptography` for the MCP SDK; the README says to run `uvx deptime --help` once first.
- [x] **DT-QA-27** (2026-11-17): bar met on 60 broken updates neither DepTime nor the command had seen (new holdout
  2026-11g): `/deptime:fix-upgrade` 86.7% of sessions fixed, alone 76.7%; new breaks 1 against 3; breaks fixed 96.0%
  against 89.3%; half the turns. B fixed 253 of the 267 breaks DepTime listed with no replacement.
- [x] **DT-DATA-83** (2026-11-16): a name bound to a method of a module-level instance takes the method's signature
  when the instance's annotation names the class (streamlit 1.33+'s `st.<name>`; DT-QA-26's `st.pyplot()`).
  `SNAPSHOT_FORMAT` 28.
- [x] **DT-GTM-09** (2026-11-16): `/deptime:fix-upgrade` rewrites a break with no replacement from the new version's
  source or release notes, leaving it only when the new version can't do what the code did.

- [x] **DT-QA-26** (2026-11-16): the reworked hand-off on 66 new broken updates: alone 69.7% of sessions fixed,
  `/deptime:fix-upgrade` 72.7%, with fewer new breaks (2 against 4) and half the turns. Bar missed on its 80% line;
  3 of B's 4 losses were breaks it found and left, as the command says when the fix isn't clear.
- [x] **DT-GTM-08** (2026-11-16): `/deptime:fix-upgrade` has Claude check the uses DepTime can't tell about (also
  in the report's agent block), rerun with `--from` when a pin is unread, and read the report from a file.
- [x] **DT-QA-05** (2026-11-16): bar missed. Sessions fixed: alone 80%, `/deptime:fix-upgrade` 64% (a third of the
  turns, no new breaks), plugin installed and not named 80%. Also DT-SPIKE-06: unprompted, the agent called DepTime in
  25 of 25 sessions.
- [x] **DT-CORE-75** (2026-11-16): `check_upgrade`'s note says when the code never imports the package (DT-QA-23: 18
  of 83 calls), where it read as no break with `uses: 0` and nothing else.
- [x] **DT-QA-23** (2026-11-16): Qwen3.5 9B with the six tools on the upgrade checks: 75.0% of breaks found and 3.5%
  of working uses flagged (five tools: 82.2%, 4.5%), missing the bar (90%, 1%). 65 of its 83 `check_upgrade` calls
  passed code the tool couldn't read (`bench/model-alone/check-upgrade/results-2026-11-16.md`).
- [x] **DT-CORE-74** (2026-11-16): `check-upgrade` reads `uv.lock` and `poetry.lock` in any directory (a lockfile's
  own project isn't a dependency); the Action's documented trigger paths match. On DT-QA-22's set (in-sample,
  unscored): one more update read, the Renovate monorepo update's title change among 21 checked, no new break.
- [x] **DT-OSS-16** (2026-11-16): 0.2.0 released with the command, the Action, the tool and the plugin command; the
  README's new "Check a dependency update" section carries DT-QA-22's first-run numbers.
- [x] **DT-GTM-07** (2026-11-16): the report's "For a coding agent" block (plain names only) and the plugin's
  `/deptime:fix-upgrade`, which runs `check-upgrade` and asks Claude to fix exactly the lines it reports.
- [x] **DT-CORE-68** (2026-11-16): a sixth MCP tool, `check_upgrade(package, from_version, to_version?, code?,
  files?)`, and `POST /v1/check-upgrade`: `check-upgrade`'s check for one package as one call. Code, not a path:
  the hosted server would read its own disk.
- [x] **DT-DATA-51**, the `global` shape (2026-11-16): names a top-level function the module calls at import binds
  through `global` and imports are the module's: sqlalchemy 2.x's `orm.collections.attribute_mapped_collection` and
  six more. With DT-CORE-72 and DT-DATA-82, `SNAPSHOT_FORMAT` 27. instructor's and chromadb's shapes stay held.
- [x] **DT-DATA-82** (2026-11-16): a class or function defined in an `except ImportError` handler isn't recorded as
  raising: dspy 3.4.1's `GEPA` stand-in, which raises only where gepa can't install. Extraction, in the batch.
- [x] **DT-CORE-72** (2026-11-16): a `@dataclass(init=False)` class keeps the constructor it inherits. griffe made
  one up taking only its dataclass bases' fields, so pydantic-ai 2's `RequestUsage(input_tokens=)` read rejected
  where `UsageBase.__init__` takes `**kwargs`. Extraction, in the next `SNAPSHOT_FORMAT` batch.
- [x] **DT-CORE-71** (2026-11-15): `check-upgrade` reads `requirements*.txt` and `requirements/*.txt` in any
  directory, leaving out virtual environments, build output and hidden directories; the Action's documented trigger
  paths match them. Of DT-QA-22's 7 unread subfolder updates, 6 are now read (in-sample, no break reported, as
  labelled); the seventh has two files pinning different versions.
- [x] **DT-QA-22** (2026-11-16): the check on 264 real Dependabot and Renovate update pull requests (247 projects),
  protocol committed first: all five bars met. Updates read 93.9%, breaks found 95.8% (46/48), working uses
  flagged 1 of 3,291, pull requests failed with no confirmed break 0.8% (2/264), labelled uses read 99.6%; every pull
  request with a break fails (13/13) (`bench/upgrade-check/prs/results-2026-11-16.md`).
- [x] **DT-GTM-06** (2026-11-15): `action.yml`, a composite action: pulls the index, runs `check-upgrade --base`
  against the pull request's base with `--annotations` (an error on each broken line), writes the report to the job
  summary, fails only on a break; `contents: read` only. Dogfooded on DepTime's own dependency updates
  (`upgrade-check.yml`); the optional comment through `workflow_run` is in `docs/github-action.md`.
- [x] **DT-CORE-70** (2026-11-15): a keyword that names a field of a class whose `__init__` is inherited isn't
  rejected, with a note: transformers 5's `LlamaConfig(vocab_size=)` and the other configs. No change on the six sets.
- [x] **DT-QA-25** (2026-11-15): the probe on every break reported on a clean upgrade: 2.4% of clean upgrades
  (23/974) fail with no confirmed break (bar 5%, met), so Phase 3 starts. 6 of the 9 "false" upgrades are weaviate
  4's raising `Client`, which the probe can't see; the rest are DT-CORE-70, DT-DATA-51 and DT-DATA-78
  (`bench/upgrade-check/audit/results-2026-11-15.md`).
- [x] **DT-QA-24** (2026-11-15): DT-QA-21 again after DT-CORE-69, in-sample: breaks found 97.8%, working uses
  flagged 0.04%, labelled uses read 99.9% (met); clean upgrades failed 8.3% (missed, unchanged)
  (`bench/upgrade-check/results-2026-11-15-second.md`).
- [x] **DT-CORE-69** (2026-11-15): `check-upgrade` and `check_symbols(code=)` read the module of `from X import Y`
  and `from google import genai`, and report each member used through a class gone at the new version, at its own
  lines; the Markdown report lists them under the missing module or class.
- [x] **DT-QA-21** (2026-11-15): `check-upgrade` end to end on 1,129 holdout upgrades (636 projects): breaks found
  73.3% (bar 90%), working uses flagged 0.04% (bar 1%), clean upgrades failed 8.3% (bar 5%), labelled uses read 91.1%
  (bar 95%); 151 of 155 upgrades with a break fail. Most misses are a break reported on the class or module next to
  the labelled path (`bench/upgrade-check/results-2026-11-15.md`).
- [x] **DT-CORE-66**, **DT-CORE-58**, **DT-CORE-59** (2026-11-15): `deptime check-upgrade [path] [--base REV]
  [--package P --to V]` reads the version change from `uv.lock`, `poetry.lock`, `requirements*.txt` or `pyproject.toml`
  pins (git base against the working tree), reads the project with `snippet.py` and runs `check_symbols` at both
  versions: a break is flagged at `to` and not at `from`, with its file and line and explain_error's finding;
  deprecations are warnings; Markdown or JSON, exit 1 only on a break. Not yet released.
- [x] **DT-CORE-67** (2026-11-15): `check_symbols` reads `timeout=` as `timeout` and leaves out `_` and empty names,
  saying so in `note`. DT-QA-23 measures what it does for a small model.
- [x] **DT-CORE-63**, **DT-CORE-64** (2026-11-15): backticked keywords, a message continued on the next line, Django's
  `import_string` message, `X.init()` tried as `X.__init__`, and a keyword a removed `**kwargs` took reads
  `param_removed`. Three of 2026-11f's four cases now pass (the 191 holdout tracebacks: 184, in-sample); selenium's
  finds the right release on three same-named `WebDriver` classes, which `note` lists, and stays a known gap: its
  line names no browser.
- [x] **DT-QA-20** (2026-11-15): Qwen3.5 9B with DepTime's five tools, protocol committed first. Tracebacks 86.4%
  (alone 18.8%, DepTime by itself 94.8% now); upgrades 82.2% of breaks found with 4.5% of working uses flagged (alone
  30.9% and 15.4%) (`bench/model-alone/with-tools/results-2026-11-15.md`).
- [x] **DT-QA-19** (2026-11-15): two cheaper models alone, through OpenRouter, protocol committed first. Tracebacks:
  DeepSeek V4.1 Flash 59.2%, Qwen3.5 9B 18.8% (DepTime 86.9%). Upgrades: DeepSeek 80.9% of breaks found with 2.7% of
  working uses flagged, Qwen 30.9% and 15.4% (DepTime 96.7% and 0.1%). The free models were busy and are left for
  another day (`bench/model-alone/cheaper-models.md`).
- [x] **DT-QA-18** (2026-11-15): the same comparison for the upgrade checks. The 2026-11f holdout's 2,265 uses put
  to Claude Sonnet 5.5 with no tools, one run, protocol committed first: 90.8% of breaks found and 0.2% of working uses
  flagged, against 96.7% and 0.1% for DepTime's first run; Claude Haiku 5.5, 73.7% and 0.3%
  (`bench/model-alone/upgrades/results-2026-11-15.md`).
- [x] **DT-QA-16** (2026-11-14): a model-alone baseline beside the traceback score. The six sets' 191 tracebacks,
  put to three Claude models with no tools under a prompt and rule committed first: 66% to 84% against DepTime's
  86.9%, with the models ahead on naming the change and its replacement and DepTime ahead on the release
  (`bench/model-alone/results-2026-11-14.md`). The README has the row.
- [x] **DT-GTM-04** (2026-11-14): the Claude Code plugin runs `uvx deptime==<release>` and names its icon in the
  manifest, as Anthropic's plugin directory requires; a test keeps the pin at the released version.
- [x] **DT-QA-14** (2026-11-14): closed without code. The check it asked for, an alert when the index falls behind,
  already runs: `bench` runs DT-QA-03's tests on the published index after every build (59 of the last 60 such runs
  passed; today's ran 82 tests).
- [x] **DT-OSS-02** (2026-11-13): README proof. An animated demo drawn from the real answer (`tools/demo_svg.py`),
  "How well it works" as four first-run numbers with the full table in `bench/README.md`, a limits section and a
  status line.

## Optional, once

One-off measurements and finishing touches. None is needed for upkeep, and none starts a series.

- [ ] **DT-QA-15**: The same for accuracy: S1's runtime probe on each package's newest release · `P3` `M`
  - Split from DT-QA-14. The probe (`bench/s1/`) runs a fixed plan of releases, up to 150 minutes a package, so
    following new releases means reworking it, and a check that needs upkeep defeats its purpose. DT-QA-03's
    documented names already watch each Tier A package's newest release. Wait for a report that shows decay.
- [ ] **DT-OSS-15**: Dependency updates that merge themselves · `P3` `S`
  - Needs auto-merge and required checks turned on for `main` (neither is set), which changes how every commit
    lands, and lets unattended updates into the environment that builds the signed index. The monthly upkeep merges
    them instead.
- [x] **DT-GTM-02** (2026-11-17): the rename maps and a landing page are on GitHub Pages
  (https://cschmertz.github.io/deptime/), rebuilt by `pages.yml` after every index build. Designed the same day:
  brand header, landing page with the upgrade demo, a filter on each map, link previews, no row cap.
- [x] **DT-GTM-05** (2026-11-18): the `AGENTS.md` block now covers dependency updates and was tried in Claude Code
  in a fresh project; Cursor reads `AGENTS.md`, so the separate Cursor rule is gone.
- [x] **DT-OSS-14** (2026-11-17): the holdout sets, the update pull requests and the tracebacks are published as one
  benchmark with a standard-library scorer (`bench/dataset/`, release `benchmark-v1`); its scorer matches the
  repository's own, answer for answer.
- [ ] **DT-OSS-18**: A copy of the benchmark on Hugging Face Datasets, where people look for benchmarks · `P3` `S`
  - The same `benchmark-v1` files and README, linked from the release. Needs a Hugging Face account to publish from.

## Held

Known gaps, most with a case kept as `known_gap` in the benchmark. None is scheduled. A report moves its item to
"To do", and the groups are in the order to take them. Each item's detail is in the archive under its ID.
"Extraction" means the fix needs a `SNAPSHOT_FORMAT` batch (rule 4).

**Wrong answers** (a false alarm, a wrong finding or a wrong replacement)

| ID | Gap | Fix is in |
|---|---|---|
| DT-DATA-51 | Names computed at import time read `missing`: instructor's provider factories, chromadb's `globals()` writes | extraction |
| DT-DATA-75 | Names in compiled extension modules read `missing` (scikit-learn's `tree._tree`) | extraction |
| DT-DATA-77 | A keyword only a decorator takes reads `never_existed` (huggingface_hub's `use_auth_token`) | extraction |
| DT-DATA-73 | matplotlib 3.10's `cm.ScalarMappable` reads private, so its members read missing | extraction |
| DT-DATA-71 | Names a module only imports read public through a membership-test shim (instructor's `hooks`) | extraction |
| DT-DATA-72 | A name only a stub binds reads present where the runtime raises (numpy 2.0's `cast`) | extraction |

**Missed breaks and deprecations**

| ID | Gap | Fix is in |
|---|---|---|
| DT-CORE-34 | Constructor keywords of dataclasses and pydantic models; required arguments | query, extraction |
| DT-DATA-09 | Pydantic model fields as `__init__` keywords | extraction |
| DT-DATA-76 | A name a lazy table serves doesn't carry its target's deprecation (langchain-community's `llms.OpenAI`) | extraction |

**Tracebacks with no finding**

| ID | Gap | Fix is in |
|---|---|---|
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

## Later

- **DT-OSS-19** (proposed): ship the survey's rename checker as a contributor tool: for "old name → new name" pairs,
  whether the index already answers each, can't take it, or would gain it, so a hint PR is checked before review.
- **DT-DATA-84** (proposed, needs the maintainer's call on build time): index further back than `since = 2023-01-01`
  for packages whose best-known breaks are older: selenium 4.3's `find_element_by_*`, pymongo 4.0, celery's
  `celery.task`, click 8.0. Measure the build time, the index size and the share of reported tracebacks it would
  answer first.
- **DT-CORE-57**: check code as it is written (a Claude Code hook after each edit), once `check-upgrade` and its
  reader are measured.
- **DT-DATA-81** (proposed): a sandboxed runtime floor for dynamic packages, so a name that runs reads `unknown` at
  worst, never `missing`. Needs ADR 0002 and Invariant 1 revisited first.
