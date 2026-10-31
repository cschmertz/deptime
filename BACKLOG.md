# DepTime backlog

**How to read this**
- **IDs** look like `DT-<EPIC>-NN` and are referenced from the docs and code. Put the ID in commit messages and PR titles.
- **Priority:**
  - `P0`: blocks the milestone.
  - `P1`: needed for the milestone's goal.
  - `P2`: nice to have.
- **Size:** `S` ≤ ½ day · `M` 1–2 days · `L` 3–5 days.
- **Direction:** free, open source (MIT), local-first ([ADR 0007](docs/adr/0007-open-source-local-first.md)).

| Milestone | Exit criterion |
|---|---|
| **M0 Spikes** | S1 go/no-go (false-absent ≤ 10%); S2–S7 answered |
| **M1 Local alpha** | Public repo; nightly index published; `uvx deptime mcp` works from PyPI; benchmark ≥ 70% |
| **M2 Distribution** | Registries, plugin, snippets, demo GIF, 5 rename-map pages |
| **M3 Optional hosted** | Public endpoint for web clients, with limits and a privacy notice |

---

## Current state: public alpha, v0.1.3 (since 2026-10-29)

- The repo is public. `uvx deptime mcp` installs from PyPI, and pushing a `v*` tag publishes through trusted publishing (`release.yml`).
- `build-index` runs nightly at 04:17 UTC and replaces the rolling `index` release. The first full builds (2026-10-28) indexed 52 packages and 3,371 releases into a 78.9 MB `.gz`. The first `SNAPSHOT_FORMAT` 4 build (cold, 2026-10-29) took 40 minutes and holds 3,377 releases in 79.8 MB. The first whole-series build (DT-DATA-19, warm caches, 2026-10-29) took 38 minutes and holds 6,508 releases in 92.9 MB. The first `SNAPSHOT_FORMAT` 5 build (cold, 2026-10-30, DT-DATA-26 and DT-DATA-27) took 53 minutes and holds 6,513 releases in 92.9 MB. The first build with DT-DATA-17 (warm caches, 2026-10-31) took 26 minutes and holds 6,564 releases in 93.3 MB.
- Not covered yet: transformers (DT-DATA-16), names numpy only creates at runtime, such as 1.x's `numpy.emath` (DT-DATA-02), `numpy.ndarray` methods its stub defines only through `@overload`, such as `reshape` (DT-DATA-29), and modules in folders without `__init__.py`, such as `litellm.types` (DT-DATA-23).

**Benchmark (2026-10-31):** 159 real tracebacks from 47 packages (`bench/`), on the index built 2026-10-31: root cause 95.6%, root cause + boundary 95.0%, replacement 19.5% (from 95.0% and 94.3% before DT-DATA-17, 93.7% and 83.6% on the first `SNAPSHOT_FORMAT` 4 index, 89.9% and 76.7% on format 3, and 78.0% and 64.8% before DT-CORE-19 and DT-CORE-05). The whole-series index made the 17 DT-DATA-19 cases pass and changed no other case, the `SNAPSHOT_FORMAT` 5 index (2026-10-30) changes no case, and DT-DATA-17 (2026-10-31) made the fastmcp 4.0 `fastmcp.server.proxy` case pass and changed no other. Each of the eight remaining real misses is assigned to an item below. These are the cases the fixes were designed from, so this isn't the number to quote.

**Holdout (DT-QA-04, 2026-10-31): root cause + boundary 72.4%** (21/29), root cause 82.8%, replacement 0%, on 29 tracebacks from TimeMachine-bench's verified subset (`bench/tmb/`), pre-registered in `22dea55` and scored once on the same index. That's above the 70% floor and below the 80% target, at about ±15 points. Breaks already in `bench/cases` score 76.9% (all with the right root cause), and new breaks 68.8%. 32 of the 47 labelled breaks on covered packages are in scope. The eight misses are assigned below: DT-DATA-25 (4), DT-DATA-22, DT-DATA-29 (2), DT-CORE-21 and DT-CORE-02 (`bench/tmb/results-2026-10-31.md`).

**S1 (DT-SPIKE-01) passed on 2026-10-29: 5.1% false absent** on 10 dynamic packages (bar: 10%), so there's no runtime fallback. Re-scored on the `SNAPSHOT_FORMAT` 5 index (2026-10-30): 4.2%, and 30 of the misses read as removed (from 37); langchain and pydantic are near zero (`bench/s1/results-2026-10-30.md`). The worst packages are numpy (21%) and litellm (16%), assigned to DT-DATA-25 and DT-DATA-23.

**M1:** the benchmark bar (≥ 70%) now holds on fresh cases: 72.4% on the holdout.

**Next up**
1. numpy `ndarray` methods missing from the index (DT-DATA-29): `reshape`, `astype` and `sum` are absent too, not only the two holdout misses.
2. numpy names only the runtime has (DT-DATA-25): 4 holdout misses and 1 in `bench/cases`.
3. Replacements: 0% on the holdout, though numpy 2's own messages name them (DT-CORE-06).

---

## ✅ Done (2026-10-17)

- [x] Problem and prior-art research, specs, architecture, self-hosting, risks, privacy/security docs, and ADRs
- [x] Ingestion: PEP 691 client, version policy, sha256-verified download, safe extraction, griffe public-path snapshots, diffs, replacement mining, span-encoded SQLite index
- [x] Resilience: a failed run never wipes an index; a PyPI outage rebuilds from cached snapshots; per-package failure isolation
- [x] Query layer: 5 tools, confidence semantics, `<=X` deprecations, unindexed-version windows, `never_existed`
- [x] **Local mode:** `deptime mcp` (stdio); first-run background download + daily refresh check + hot swap; `deptime index pull|status|pack`; offline mode
- [x] **Index distribution:** `pack` → `manifest.json` + `index.sqlite.gz`; nightly `build-index.yml` publishing to a rolling `index` release, with a shrink guard
- [x] Hosted mode (optional): `/mcp` (stateless, spec 2026-07-28) + `/v1/*` + anonymous limits
- [x] OSS hygiene: MIT LICENSE, PyPI metadata, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue forms (wrong answer / coverage / bug), PR template, `release.yml` (trusted publishing)
- [x] Claude Code plugin (stdio `uvx deptime mcp` + skill + hook), validated with `claude plugin validate`
- [x] 53 offline tests, including a real stdio launch, distribution and pipeline end to end. Verified the built wheel via `uvx` over stdio on real httpx wheels.

---

## M0 — Spikes

Method and pass bars: [docs/07](docs/07-risks-and-spikes.md).

- [x] **DT-SPIKE-01**: S1 static-analysis accuracy on dynamic packages · `P0` `M` · **go/no-go**
  - AC: a false-absent rate measured on 10 packages × 5 versions × 50 runtime-sampled symbols, with the script in `bench/s1/`. Pass at ≤ 10%; otherwise build the sandboxed dynamic fallback and re-measure; stop if it's still above 10%.
  - **Passed 2026-10-29: 123/2,428 = 5.1% false absent** (`init` modules 6.2%, other modules 3.9%). Run 40915960477 on the whole-series index; results in `bench/s1/results-2026-10-29.md`, method in `bench/s1/README.md`. The dynamic fallback isn't needed.
  - **Sample:** 10 packages: langchain, langchain-community, langchain-core, openai, pydantic, huggingface-hub, litellm, pydantic-ai-slim, instructor, numpy (transformers is out of coverage). 5 releases each, evenly in time. Sampled at runtime in `.github/workflows/s1.yml` only (no network, a throwaway user, wheels-only installs).
  - **The gate was amended after the first run.** It leaves out a plain module's imports from elsewhere in its package (`hf_api.hf_hub_url`), which the index excludes by design. With them in, the rate is 549/2,452 = 22.4%; the README gives the reasoning.
  - **Results by package:**
    - 0% on huggingface-hub, langchain-community, langchain-core, openai and pydantic-ai-slim (0 of 14,813 langchain-community runtime names missed);
    - langchain 1.2%, instructor 6.3%, pydantic 8.0%, litellm 15.6%, numpy 21.2%.
    - 37 sampled misses would read as removed.
  - **Where the misses go:** litellm → DT-DATA-23, numpy → DT-DATA-25, pydantic → DT-DATA-26, langchain → DT-DATA-27, instructor → DT-DATA-28.
- [ ] **DT-SPIKE-02**: S2 replacement precision on 50 known migrations · `P0` `S`
  - AC: precision ≥ 80% at confidence ≥ 0.75; misses triaged.
- [ ] **DT-SPIKE-03**: S3 traceback mapping (Python 3.10–3.14, pytest, library-specific exceptions) · `P0` `S`
  - AC: 100 cases at ≥ 90% kind + package accuracy.
- [ ] **DT-SPIKE-04**: S4 index size, build time, download size · `P0` `S`
  - AC: `.gz` under 100 MB; nightly incremental under 30 min; p95 under 20 ms; cold build fits Actions limits (or a matrix plan).
  - Measured 2026-10-28 (52 packages, 3,371 releases): `.gz` 78.9 MB (773 MB unpacked); cold build 35–61 min as one job per package. Nightly incremental and p95 not measured yet.
- [ ] **DT-SPIKE-05**: S5 client compatibility matrix (local and hosted) · `P0` `M`
  - AC: Claude Code, Cursor, Codex CLI, VS Code and Claude Desktop with `uvx deptime mcp`; handshake-era clients against hosted mode; the matrix published in the README.
- [ ] **DT-SPIKE-06**: S6 do agents call it unprompted (plugin vs. no plugin)? · `P1` `M`
- [ ] **DT-SPIKE-07**: S7 first-run UX on macOS, Linux and Windows · `P1` `S`
  - AC: a useful answer within 60 s of first launch; Windows behaviour documented (feeds OSS-08).

## M1 — Local alpha

### Open source and release (DT-OSS)
- [x] **DT-OSS-01**: Publish the repo as `github.com/cschmertz/deptime` · `P0` `S`
  - AC: public repo; description and topics (`mcp`, `python`, `coding-agents`, `deprecation`); private vulnerability reporting enabled; CI green. (`config.DEFAULT_INDEX_URL` assumes this name.)
- [ ] **DT-OSS-02**: README proof · `P0` `M` · deps: QA-02, SPIKE-05
  - AC: a 10-second demo GIF (traceback → answer); benchmark table; compatibility matrix; prior-art section.
- [x] **DT-OSS-03**: Reserve the name on PyPI · `P0` `S`
  - AC: `deptime` 0.0.x published (also proves the packaging); name reserved.
- [x] **DT-OSS-04**: First published index · `P0` `S` · deps: OSS-01, DATA-01
  - AC: `build-index.yml` run manually, then on schedule; `deptime index pull` works from the public URL on a clean machine.
  - Done 2026-10-28: manual runs published the index, and `index pull` works from the public URL in an empty `DEPTIME_HOME`.
  - Done 2026-10-29: the first scheduled run on the per-package pipeline was green in 15 min. All 52 jobs restored their snapshot cache; 5 built the 6 new releases. Publish assembled 3,377 releases (78.9 MB .gz) from the published index, and `bench` ran on `workflow_run`. The run started at 10:53 UTC, 6.5 h after its 04:17 cron, because GitHub delays scheduled runs.
- [x] **DT-OSS-05**: Tagged release through PyPI trusted publishing · `P0` `S` · deps: OSS-03
  - AC: trusted publisher configured; `git tag v0.1.0` publishes; `uvx deptime mcp` works from PyPI.
  - Done 2026-10-28: v0.1.0 published by `release.yml` with provenance; `uvx deptime@0.1.0 mcp` answered over stdio from an empty home.
- [ ] **DT-OSS-06**: CHANGELOG + versioning policy · `P2` `S`
  - AC: Keep-a-Changelog file; semver for the CLI and tool contract; the index schema version is documented.
- [x] **DT-OSS-07**: Supply chain · `P1` `S`
  - AC: GitHub Actions pinned to SHAs; Dependabot for `uv.lock` and actions; `pip-audit` in CI.
  - Done 2026-10-31: every action is pinned to the commit its major tag pointed at (no change in behaviour), with the release in a comment. `.github/dependabot.yml` proposes weekly `uv` and `github-actions` updates, minor and patch grouped per ecosystem; griffe gets a PR of its own, since a new extractor can change snapshots without a `SNAPSHOT_FORMAT` bump. A ci job runs `pip-audit --strict` over every package in `uv.lock`, with pip-audit itself locked in an `audit` dependency group. Its first run found certifi 2026.7.22 still trusting the emSign roots behind compromised eMudhra intermediates (PYSEC-2026-4320); the lock now has 2026.10.23.
- [ ] **DT-OSS-08**: Windows support for index replace and hot swap · `P1` `S` · deps: SPIKE-07
  - AC: download and refresh succeed while the server holds the old file open (e.g. versioned filenames plus a pointer file).
- [ ] **DT-OSS-09**: Signed index provenance · `P2` `M`
  - AC: `build-index.yml` produces GitHub artifact attestations; `deptime index pull --verify` checks them; documented in SECURITY.md.
- [ ] **DT-OSS-10**: A failed first download recovers without a restart · `P0` `S`
  - AC: an index that appears on disk is opened on the next call (done); the refresh thread retries with backoff while there is no index; the note names the real cause (failed download, offline).

### Data (DT-DATA)
- [ ] **DT-DATA-01**: Index Tier A with the coverage policy · `P0` `M` · deps: SPIKE-01, SPIKE-04
  - AC: ≥ 90% of selected releases indexed for each package; failures listed in the workflow summary.
- [ ] **DT-DATA-02**: Index Tier B · `P1` `M` · deps: DATA-01
  - AC: same bar as DATA-01; numpy indexed via its `.pyi` stubs; compiled-only gaps noted in `coverage.toml`.
  - Done so far: names numpy's `__init__.py` imports and `__init__.pyi` defines (`add`, `float64`) are indexed for 2.0 and 2.1 too. `numpy.emath.sqrt` is indexed for 2.x once a `SNAPSHOT_FORMAT` 4 index is published (DT-DATA-24); in 1.x `numpy.emath` is still only an attribute from the stub.
- [x] **DT-DATA-03**: Cold-build strategy for CI · `P1` `S` · deps: SPIKE-04
  - AC: the first full build completes (split into a matrix if needed); the snapshot cache stays within Actions cache limits.
  - Done: one job per package builds and caches its own snapshots, then a publish job assembles the index from them and from the published index; only snapshots stay on disk; a timed-out build still saves its cache. A cached snapshot is reused only if its format and its import names match (2026-10-28), so a `coverage.toml` edit reaches every release without a format bump.The first full builds (2026-10-28) took 35–61 min with every job green; the slowest is litellm (25 min, 290 releases). The snapshot cache is 2.3 GB of the 10 GB limit, including entries from older snapshot formats that expire unused.
- [ ] **DT-DATA-04**: Freshness SLO + yanked handling · `P1` `S`
  - AC: a new release is indexed within 24 h (nightly); yanked releases dropped on rebuild; build date shown by `coverage` and `index status`.
- [ ] **DT-DATA-05**: Lazy-module support (`__getattr__` re-exports) · `P1` `L` · deps: SPIKE-01
  - AC: langchain/transformers false-absent rate below 10%.
  - Progress 2026-10-28: DT-DATA-20 records names a module `__getattr__` serves when they are compared with literals or looked up in a module-level literal table (langchain-community's lazy `llms` imports). Names computed at runtime are still missed.
- [ ] **DT-DATA-06**: Stdlib module availability per Python version (typeshed `stdlib/VERSIONS`) · `P1` `S`
- [ ] **DT-DATA-07**: Cross-package replacement hints (`google.generativeai` → `google.genai`) · `P1` `S`
- [ ] **DT-DATA-08**: Curated migration map for the top 20 migrations (reviewed; `source="curated"`) · `P1` `M` · deps: SPIKE-02
- [ ] **DT-DATA-09**: Pydantic model fields treated as `__init__` kwargs · `P2` `M`
- [x] **DT-DATA-12**: Ingestion never drops an indexed release by accident · `P0` `S`
  - AC: a failed retry, `--versions` and a PyPI outage keep already-indexed releases (from the snapshot cache, else rebuilt from the index) (done); `ingest` refuses a downloaded index without `--into-downloaded` and marks it locally built only after a rebuild (done). CI still starts each night from an empty index, so carrying releases forward there needs the previous published index as a base (see DT-DATA-03).
- [ ] **DT-DATA-16**: Store inherited members once per class, then index transformers again · `P1` `M` · deps: SPIKE-04
  - Why: transformers 5.21.0 has ~1.5M public paths, 87% of them inherited methods repeated across ~3,700 model classes (2026-10-28), so it was taken out of `coverage.toml`.
  - AC: snapshots record each class's own members and its bases; queries resolve `Class.member` through the bases; transformers is back in coverage with the `.gz` still under 100 MB.
- [x] **DT-DATA-17**: Follow a wrapper release to the distribution that holds its code · `P1` `S`
  - Why: since 3.3.0, `fastmcp` is an empty wheel that pins `fastmcp-slim==<same version>`, so its 31 newest releases have no snapshot (2026-10-28). typer 0.12.0 (`typer-slim`) and pinecone 2.2.2–5.0.1 (`pinecone-client`) have the same shape.
  - AC: when a release has no code for its import names and pins one distribution at the same version, ingest analyses that distribution's artifact and records it as the source file; fastmcp 3.3.0+ is indexed. A pin to a different version (dspy 0.1.5 → `dspy-ai==2.4.5`) is still reported as failed.
  - Done 2026-10-31: ingest follows the first same-version pin that has code for the import names (typer 0.12.0 pins the empty `typer-cli` before `typer-slim`), and each snapshot records the analysed artifact as `source_file`. `build-index` run 41196805868 (26 minutes, warm caches) added 48 releases: fastmcp 3.3.0–4.1.4 (31), pinecone 2.2.2–5.0.1 (16) and typer 0.12.0; 6,564 releases in 93.3 MB. dspy 0.1.5 still fails, as intended. On that index the fastmcp `fastmcp.server.proxy` case passes and no other case changes (real root cause + boundary 94.3% → 95.0%).
- [x] **DT-DATA-18**: Snapshot builds keep memory flat across releases · `P2` `S`
  - Why: building litellm's 290 releases in one process grows by ~45 MB per release and never gives it back, peaking at 11.9 GB of a 16 GB runner (2026-10-28). Only cold builds are affected; nightly runs build a few releases.
  - AC: memory after each release returns to a fixed baseline (e.g. clear griffe's caches, or build each release in a subprocess); litellm's cold build stays under 4 GB.
  - Done 2026-10-29: griffe 2.3's dataclasses extension memoizes `_dataclass_parameters` per `Class` in an unbounded `functools.cache`, and each cached class keeps its whole package tree alive through its parents. `build_snapshot` now empties griffe's module-level caches after every release. Live objects after a litellm build stay at ~19k instead of growing by ~95k per release; litellm's 100 newest releases built in one process peak at 552 MB (CI passed 5 GB ten minutes into the same build before). Snapshots are unchanged (three pydantic-ai releases rebuilt with identical symbols), so no format bump.
- [x] **DT-DATA-19**: Index density: boundaries that fall between indexed releases · `P0` `M` · data-policy decision
  - Why: older minor series are indexed only at their first and last release, so a patch-level removal or addition (chromadb `API` removed in 0.4.15; only 0.4.0 and 0.4.24 are indexed) gets the wrong boundary. 17 bench misses, the largest group (bench cases with `fixed_by` DT-DATA-19).
  - AC: measure the index size of indexing every release since 2023-01-01 (or every release for Tier A); decide; re-run the bench. Until then DT-CORE-18 keeps the wording honest.
  - Measured 2026-10-29 by building every candidate release of all 52 packages locally, over the published format 4 index: today's policy selects 3,422 releases; every release since 2023-01-01, 6,381 (16 of the 17 cases; sqlalchemy's 1.4.29 is from 2021); every release of every minor series active since 2023, 6,557 (all 17); the latter for Tier A only, 5,883 (15). The whole-series index is 93.2 MB `.gz` against 79.9 MB (both packed locally), and 955 MB unpacked: Tier A adds 13.0 MB (langchain 9.9 MB: only 2 of its 349 `0.0.x` releases were indexed, and the rest add 75,000 import paths from mid-2023; weaviate-client 1.9 MB) and Tier B 0.3 MB, so a Tier-A-only policy saves nothing. Spans split only where the API changes, so most packages grow 0–30% for 2–25× the releases. On that index all 17 cases pass and no other case changes: real cases root cause 95.0%, root cause + boundary 94.3%. A single-process cold build needed DT-DATA-18 first (langchain and litellm projected at 21–24 GB).
  - Decided 2026-10-29: every release of every minor series active since 2023 (`whole_series = true` in `coverage.toml`). It leaves ~7 MB below the 100 MB target, and the index now grows faster than before: releases older than 12 months are no longer thinned to their series' first and last. At 2023–2026 rates that's 1–5 MB more per year (the 13.3 MB covers ~2.8 years, and 9.9 MB of it is langchain's 2023), so watch the size in each build.
  - Done 2026-10-29: `build-index` run 40901087042 published it (6,508 releases, 92.9 MB, 38 minutes on warm caches; litellm built 503 more releases in 10 minutes, at most 1.1 GB in use). On it all 17 cases pass and no other case changes.
- [x] **DT-DATA-20**: Names served by a `__getattr__` deprecation shim count as present · `P0` `M`
  - Why: many libraries keep a removed name alive for a few releases through a module `__getattr__`, so static analysis dates the removal too early. Found in the shipped wheels: pydantic-ai 1.100.0 maps `'BuiltinToolCallPart': 'NativeToolCallPart'`; fastmcp 2.12.5 has `if name == "BearerAuthProvider":`; also werkzeug `__version__` (3.0) and django `timezone.utc` (4.1–4.2). 5 bench misses.
  - AC: snapshots read string literals that a module's `__getattr__` compares against or looks up (and dicts such as numpy's `__deprecated_attrs__`), record those names as present and deprecated, and take a mapped target as a replacement. Still static: never import the package (ADR 0002).
  - Done 2026-10-28: snapshots parse a module's `__getattr__` with `ast` (never imported) and follow which names reach a `return`: `name == "X"`, `name in <literal or module-level dict/set/list>`, `or`/`and` of those, and guards such as `if name != "utc": raise`. A name whose branch raises stays absent (numpy 2.0's expired attributes); conditions it can't read serve nothing; `try`, `with` and `match` bodies aren't followed (numpy 1.26's `try: __deprecated_attrs__[attr]` shape is still missed). Served names are kind `getattr` (canonical: the `__getattr__`), and real members win. Their deprecation text is the branch's `warn(...)`, with the f-string filled in from the source (the name, the module, a rename table's target), so pydantic-ai's rename map yields "use `pydantic_ai.NativeToolCallPart` instead"; a deprecation-type category counts too (django's `RemovedInDjango50Warning`). Checked on the shipped wheels: pydantic-ai-slim 1.100.0, fastmcp 2.12.5, werkzeug 3.0.6, django 4.2.16, numpy 1.26.4 and 2.0.2. On a scratch index of the boundary releases all 5 cases get the right boundary (django and pydantic-ai also the replacement); they pass once a `SNAPSHOT_FORMAT` 4 index is published. Latest releases of all 52 packages gain about 250 names (most: langchain-community 104 lazy `llms` imports, weaviate 24, instructor 24, litellm 23, click 22). werkzeug's "Use feature detection or …" now mines the replacement `feature` at 0.6 (DT-CORE-12).
- [x] **DT-DATA-21**: Index names re-exported from another distribution · `P0` `S`
  - Why: `flask.escape` (from markupsafe, flask ≤2.3) and `typer.clear` (from click) are absent from the index, so DepTime answers `never_existed`, a confident wrong answer. 2 bench misses; it also inflates S1's false-absent rate.
  - AC: an alias whose target is in another distribution is recorded under the re-exporting package's public path (kind from the alias, no signature); the two bench cases pass.
  - Done 2026-10-28: in a package `__init__` without `__all__`, a name imported from another distribution is public when the import is a top-level `from x import y as y` (the explicit re-export convention: typer's click API, fastapi's `starlette.status`, sqlmodel's SQLAlchemy names), or, in the package's top-level `__init__` only, a top-level `from x import y` from outside the standard library (flask 2.2's `escape` and `Markup`). Read with `ast`. Module imports, stdlib and `typing_extensions` plain imports, renames, imports under `if`/`try`, and plain imports in subpackages stay out: probed on the first and latest indexed release of all 52 packages, subpackage plain imports were about 90 names, nearly all dependencies imported for use (`chromadb.server.fastapi.Request`, `crewai.flow.runtime.BaseModel`). What's kept is about 260 names, mostly sqlmodel (~115 per release) and typer (22). Recorded as kind `alias` with the foreign target as canonical and no signature. On a scratch index both cases get the right boundary: `flask.escape` is now present from 2.2.0 (alias), deprecated 2.3.0 (shim), removed 3.0.0; typer 0.26.0 removes `clear`, `echo_via_pager`, `edit`, `open_file`, `pause` and `unstyle`, and the click names it vendored change kind. They pass once a `SNAPSHOT_FORMAT` 4 index is published. flask's "Import 'markupsafe.escape' instead" isn't mined as a replacement yet (DT-CORE-06).
- [ ] **DT-DATA-22**: Decide whether to index private names that users import (`flask._app_ctx_stack`, `pymongo.database._check_name`) · `P2` `S`
  - 3 bench misses, plus `flask._request_ctx_stack` in the holdout. Today they come back `never_existed`; at minimum the explanation should say private names aren't indexed. The third is an implementation import another library patched: `instructor.core.patch.handle_response_model` (a plain `from ..processing.response import ...` in instructor ≤1.15.1, wrapped by openinference).
- [ ] **DT-DATA-23**: Index modules in folders without `__init__.py` · `P0` `M` · deps: DATA-18
  - Why: griffe deliberately skips a folder of `.py` files that has no `__init__.py` ("Missing __init__ module?"), but Python imports it as a namespace package. litellm has 180 such folders in 1.106.5 (1,596 files, 24 MB of source), including all of `litellm.types` and `litellm.litellm_core_utils`, so `from litellm.types.utils import ModelResponse` looks absent. Also `flask.sansio` (3.x), `qdrant_client.embed`, selenium's generated `webdriver.common.devtools`. 39 of 52 packages have none. 1 bench miss (`litellm.llms.prompt_templates`, gone in 1.55.0).
  - AC: such folders under an import root load as packages (skipping tests and non-identifier names); the litellm case passes; litellm's cold build stays within the runner's memory and time, and the index within its size target (measure first: litellm already peaks at 11.8 of 16 GB).
  - Measured 2026-10-28 on litellm 1.106.5 (empty `__init__.py` written into a scratch copy): 55,645 → 306,860 symbols, snapshot 785 KB → 4.1 MB gz, 4 s → 15 s, peak 252 → 627 MB. Most of it is the bundled proxy server (`litellm.proxy`, 33k → 192k); leaving `litellm/proxy` out still gives 162k symbols (2.3 MB gz), with `litellm.types` 20k and `litellm.llms` 41k. With the cold build's per-release memory growth (DT-DATA-18), either variant would likely exhaust the runner over 290 releases, so DT-DATA-18 comes first; whether to index `litellm.proxy` is a scope decision.
  - S1 (2026-10-29): litellm is the second-worst package, at 32/205 sampled (1,370 of 7,734 runtime names). 27 of the 32 are top-level names that `litellm/__init__.py` star-imports from `litellm.types` (`OpenAIChatCompletionSystemMessage`, `ToolResourcesFileSearchVectorStore`).
- [x] **DT-DATA-24**: Re-exports in plain modules (`sqlalchemy.schema`, `sqlalchemy.types`) · `P0` `S`
  - Why: the public-import rule (9c1a21f) only covers package `__init__`s. sqlalchemy's `schema.py` and `types.py` are plain modules that only re-export (`from .sql.schema import Column` in 1.4, `... import Column as Column` in 2.x) without `__all__`, so `sqlalchemy.schema.Column`, `MetaData` and `Table` are absent in every release, and the published index says `sqlalchemy.types.Integer` (with 47 more `sqlalchemy.types` names) was removed in 2.0.0: a confident wrong answer. 1 bench miss (`sqlalchemy.schema.ThreadLocalMetaData`, last in 1.4).
  - AC: those names are present in the releases that have them, with no flood of implementation imports from ordinary modules (measure across packages); the bench case passes.
  - Done 2026-10-28: in any module without `__all__`, a top-level `from x import y as y` re-exports, and so does every non-stdlib `from x import y` in a module that is nothing but imports and a docstring (sqlalchemy 1.4's "compatibility namespace" `schema.py`, numpy 2's `lib/scimath.py`). Other plain-module imports stay private: an ordinary module imports to use (langchain-community 0.4.2 has ~9,900 such names). Compared with the previous rule on the first and latest indexed release of all 52 packages: +12,979 symbols (+1.3%), nothing lost, no build-time change; 94% is sqlalchemy, whose re-exported classes now carry their members. The rest is mostly real API that was missing: `fastapi.middleware.cors.CORSMiddleware`, `fastapi.responses`, `fastapi.concurrency.run_in_threadpool`, `matplotlib.pyplot.rcParams`, `numpy.emath.sqrt` (2.x), langgraph's `utils.config.get_config`. On a scratch sqlalchemy index the bench case passes, `sqlalchemy.types.Integer` and `sqlalchemy.schema.Column` span 1.4.0–2.1.6, the 48 false `sqlalchemy.types` removals at 2.0.0 are gone, and no other sqlalchemy case changes. It passes once a `SNAPSHOT_FORMAT` 4 index is published.
- [ ] **DT-DATA-25**: A stub's bare `__all__: list[str]` hides runtime names · `P1` `S`
  - Why: numpy's `core/fromnumeric.pyi` declares `__all__: list[str]` with no value; griffe reads that as an empty `__all__` that overrides the `.py`'s literal one, so the `from .fromnumeric import *` chain brings nothing and the top-level names come from `numpy/__init__.pyi` alone. That stub dropped `product`, `cumproduct`, `sometrue`, `alltrue`, `round_` and `find_common_type` in 1.25, when they were deprecated, so the index dates their removal to 1.25.0 instead of 2.0. 1 bench miss (`numpy.product`).
  - AC: an annotation-only `__all__` in a stub doesn't override the source's; measure what it changes across numpy 1.24–2.x before shipping; the bench case passes.
  - S1 (2026-10-29): numpy is the worst package, at 53/250 sampled (5,374 of 17,532 runtime names), all names listed in a computed `__all__`. Seen in `numpy.matlib` (`fliplr`, `append`, missing at 1.24 but present elsewhere), `numpy.core` (`log`, `longfloat`), `numpy.core.numeric` and `numpy.core.numerictypes`. Check how many of them this fixes.
  - Tried 2026-10-30, not shipped: a griffe extension that drops a stub's annotation-only `__all__` when griffe reads the whole `.py` list. The chain to `numpy.product` breaks in three places, and this fixes only the first. `core/numeric.py` adds fromnumeric's names at runtime through `extend_all(fromnumeric)`, a helper that calls `__all__.append`, and `numpy/__init__.py` uses `__all__.extend(core.__all__)`. griffe follows neither, so `numpy.product` stays missing. The top-level name only ever came from `numpy/__init__.pyi`, which dropped it in 1.25. The fix alone loses more than it gains: 1.24.0 +568/−468 symbols, 1.26.4 +526/−489, 2.0.0 +110/−296, and 2.2+ unchanged (no bare stubs). An empty `__all__` makes griffe call every non-private name public. With the real list back, importable names it leaves out disappear: `numpy.lib.NumpyVersion` (documented), `numpy.core.getlimits.MachArLike`, `array_function_dispatch`. A real fix needs a static evaluator for `__all__` mutations (`.extend`, `.append`, numpy's `extend_all`) that can't drop names `is_public` lets through today; measure losses first.
  - Holdout (2026-10-31): 4 of its 8 misses. `numpy.product` ×3 (last 1.24.4, expected 1.26), and `numpy.math`, which 1.24 lists in `numpy.lib.__all__` and 1.25–1.26 serve from `__deprecated_attrs__` inside a `try` (the `__getattr__` shape DT-DATA-20 doesn't follow).
- [x] **DT-DATA-26**: pydantic 1.x names re-exported by `from .types import *` are missing when defined under `if TYPE_CHECKING:` … `else:` · `P1` `S`
  - Why (S1): `pydantic.types.StrictBool` is indexed for every 1.10 release, but `pydantic.StrictBool` isn't; the index has it only from 2.0 on. `pydantic.ConstrainedInt`, defined unconditionally, comes through. So for 1.10 users, `StrictBool`, `UUID1`, `DirectoryPath` and about 20 more look added in 2.0. The same 24 names are missing from `pydantic.v1` in every 2.x release. 2.x's top level also misses 13 names per release (`NaiveDatetime`, `FutureDatetime`) that `pydantic.types` has.
  - AC: trace griffe's wildcard expansion for conditionally defined names; the 1.10 and `pydantic.v1` names are present; measure the change across packages.
  - Done 2026-10-30 (`SNAPSHOT_FORMAT` 5). The cause is a griffe 2.3 bug: `visit_if` marks the whole `if TYPE_CHECKING:` statement as type-checking only, its `else:` branch included, and `import *` skips objects that aren't available at runtime. A griffe extension (`_RuntimeElse`) marks objects and imports under that `else:` as runtime. Measured against DT-DATA-27's commit on the first and latest release of all 52 packages, plus pydantic 1.10.26, 2.0 and 2.7.0 and 15 middle releases of five lazy-export packages: only pydantic changes, and nothing is lost. 1.10.x gains its 24 top-level names (`StrictBool`, `PositiveInt`, `UUID1`, `DirectoryPath`…; 118 symbols with members), and so does `pydantic.v1` in 2.x and in 1.10.26. 2.7+ gains 9–12 top-level names (`NaiveDatetime`, `FutureDatetime`, `EmailStr`, `ImportString`), and 2.0's change from kind `getattr` to the real class.
  - Verified 2026-10-30 on the published index (build-index run 41067535328, 54/54 jobs, cold, 53 minutes): only langchain and pydantic spans changed (pydantic 8,677 → 9,113). `pydantic.StrictBool` spans 1.10.0–2.14.2. S1's pydantic misses: 20/250 sampled → 0, every runtime name 168/3,650 → 3. The benchmark is unchanged.
- [x] **DT-DATA-27**: Lazy exports listed only as a dict's keys (`__all__ = list(_module_lookup.keys())`) · `P1` `S`
  - Why (S1): langchain 0.3.4's `langchain/chains/__init__.py` (read from the wheel) defines a literal `_module_lookup = {"NatBotChain": "langchain.chains.natbot.base", ...}`, sets `__all__ = list(_module_lookup.keys())`, and serves names through `__getattr__` → `create_importer(__package__, module_lookup=_module_lookup)`, with no `TYPE_CHECKING` imports. Neither the computed `__all__` nor `getattr_served` yields those names, so 49 names, such as `NatBotChain` and `HypotheticalDocumentEmbedder`, are indexed up to 0.3.x and then missing at 0.3.4. 48 of them would read as removed. langchain-community uses the same lookup, but its `TYPE_CHECKING` block lists every name, so nothing is missed there.
  - AC: when `__all__` is `list(<literal dict>.keys())` and the module has a `__getattr__`, the keys are recorded as kind `getattr`; langchain 0.3's `langchain.chains` names are present; measure across packages.
  - Done 2026-10-30 (`SNAPSHOT_FORMAT` 5): a `__getattr__` `return` that every name reaches serves the names a static `__all__` lists: a literal, or a module-level table's keys (`list(_module_lookup.keys())`). The warning on the way there is the deprecation, as in DT-DATA-20. The gap was wider than S1 showed. langchain 0.1–0.3's migration shims (`langchain.llms`, `tools`, `vectorstores`, `document_loaders`, `chat_models`…) list their names in a literal `__all__` and serve every name with "Importing … from langchain is deprecated … `from langchain_community.llms import AI21`". The published index ends `langchain.llms.AI21` and `chat_models.ChatOpenAI` at 0.0.354, `chains.NatBotChain` at 0.1.16, and has `vectorstores.FAISS` missing for 0.1.0–0.1.18. Measured against format 4: langchain 0.1.0 and 0.1.6 gain 683 names each, and 0.1.20–0.3.30 gain 287–288 each. All are kind `getattr`, and nothing is lost. The first and latest release of all 52 packages, plus middle releases of langchain-community, llama-index-core, openai-agents and huggingface-hub, don't change.
  - Verified 2026-10-30 on the published index (build-index run 41067535328): langchain spans 144,215 → 144,935. `langchain.vectorstores.FAISS` and `langchain.llms.OpenAI` read deprecated in 0.1.0 and removed in 1.0.0, with no false removal and re-add in between. S1's langchain misses: 3/242 sampled → 1, every runtime name 53/4,964 → 4. Sampled misses that read as removed fell from 37 to 30 overall. The benchmark is unchanged, case by case.
- [ ] **DT-DATA-28**: instructor's lazy exports (`instructor.v2`, `instructor.providers.*`, `instructor.dsl.validators`) · `P2` `S`
  - Why (S1): instructor misses 15/237 sampled names (152 of 1,443 runtime names), mostly in 1.17.0: 32 in `instructor.v2` (`from_cerebras`), 20 in `instructor.providers` and 20 in `providers.openai`. The cause isn't traced yet.
  - AC: the cause is found and fixed, or assigned to DT-DATA-23/27.
- [ ] **DT-DATA-29**: numpy `ndarray` methods missing from the index: stub methods defined only through `@overload`, and methods the stub leaves out · `P1` `S`
  - Why (holdout, 2026-10-31): `numpy.ndarray` has 44 indexed members. Methods that `numpy/__init__.pyi` defines inside the class only through `@overload` are absent (`reshape`, `astype`, `sum`, `view`, `item`, `resize`, `itemset`), while module-level overloads (`numpy.sum`, `numpy.prod`) are present. `tostring` is absent because the stub leaves it out on purpose ("deprecated and therefore excluded"); the runtime registers it through `_core/_add_newdocs.py` until 2.2.6. 2 holdout misses: `ndarray.itemset` (removed in 2.0) and `ndarray.tostring` (removed in 2.3).
  - AC: overload-only stub methods are indexed with their overload signatures (find whether griffe or the walker drops them; measure across packages); decide whether to read `add_newdoc(..., 'ndarray', (name, ...))` for methods the stub omits; both holdout cases get the right boundary.

### Core logic (DT-CORE)
- [ ] **DT-CORE-01**: Benchmark-driven tuning of candidate selection and ranking · `P0` `M` · deps: QA-01
  - AC: root-cause accuracy ≥ 80% (M1 floor: 70%).
- [ ] **DT-CORE-02**: Instance-chain hints for `object_attribute` (prefer frame and referenced-name evidence; show ambiguity) · `P2` `M`
  - Bench: user subclasses of library classes (`LogParam.get_metavar(ctx)` in click 8.2, `ValidatorSchema.context` in marshmallow 4) and an owner-less `sort()` that should be `DataFrame.sort`, not `Expr.sort` (3 cases). Holdout: `'MACAddressField' object has no attribute 'fail'`, a user `marshmallow.fields.Field` subclass in marshmallow 4 (no finding).
- [ ] **DT-CORE-03**: Import-name collision disambiguation (`agents`) via installed versions · `P1` `S`
- [ ] **DT-CORE-04**: Signature-similarity "moved" heuristic (only if S2 shows a gain) · `P2` `M`
- [x] **DT-CORE-05**: Library-specific migration exceptions (openai `APIRemovedInV1`, pydantic `PydanticImportError`) · `P1` `S` · deps: SPIKE-03
  - AC: bench cases `openai-v1-chatcompletion` and `pydantic-basesettings-moved` pass; also numpy 2's "`np.float_` was removed in the NumPy 2.0 release" messages, pydantic's `PydanticUserError` for removed kwargs (`regex`), and pytest's `PluginValidationError` for hook arguments (bench cases with `fixed_by` DT-CORE-05).
  - Done 2026-10-28: the parser matches these by exception class, with message templates copied from the numpy 2.0.2, pydantic 2.9.2, openai 1.55.0 and pluggy 1.6.0 wheels, and reads the lines after the exception line (openai and pluggy put the details there). It also strips pytest's `INTERNALERROR> ` prefix. pydantic's `removed-kwargs` takes its owner from the raising frame (`conlist` raises it too), else `Field`. Query: a `**kwargs` catch-all no longer counts as accepting a parameter that earlier releases declared by name (pydantic 2's `Field(**extra)`). pytest now also indexes `_pytest.hookspec`, the path pytest documents its hooks under. 9 of the 11 cases pass; the two hook cases pass on a scratch index built from the 8.4.2–9.1.1 wheels (last with 9.0.3) and need the format-4 index. Real cases: root cause 85.5% → 89.9%, root cause + boundary 72.3% → 76.7%. The messages also name the replacement ("Use `np.float64` instead"); DT-CORE-06 can mine those.
- [ ] **DT-CORE-06**: Mine replacement hints from the live error text (e.g. mcp 2.x's FastMCP → MCPServer hint), checked against the index · `P1` `S`
- [ ] **DT-CORE-07**: `check_symbols` from a code snippet (extract imports and attributes with `ast`) · `P2` `M`
- [ ] **DT-CORE-08**: Sanitize mined text (control characters, instruction-like content), with adversarial tests · `P0` `S`
- [ ] **DT-CORE-09**: Positional-argument changes (`too_many_positional` → `param_kind_changed`) · `P2` `S`
- [x] **DT-CORE-10**: `diff_versions` filters, sorts and counts in SQL, so `truncated` is truthful on large diffs · `P0` `S`
- [x] **DT-CORE-11**: Long tracebacks keep their tail; the MCP input limit is 200K characters · `P0` `S`
- [ ] **DT-CORE-12**: Replacement hints are trustworthy before they reach prose · `P0` `M`
  - AC: only hints with confidence ≥ 0.75 appear in `explanation` (done); the generic `use X` pattern only takes identifier-shaped targets; targets are checked against the next indexed snapshot.
- [x] **DT-CORE-15**: Optional → required parameters: `param_now_required` / `param_now_optional` events, used for "missing required argument" and checked against the installed release · `P0` `S`
- [ ] **DT-CORE-16**: Errors raised inside a dependency consider the package being called · `P0` `S`
  - AC: other indexed packages are searched, installed first, keeping only concrete changes (done; bench case `openai-httpx-proxies-internal`); candidates ranked by `Requires-Dist` once per-release dependency data exists.
- [x] **DT-CORE-17**: `never_existed` for a name the traceback shows was in use: say it may predate the first indexed release or be created at runtime, at `low` confidence, instead of "may be misremembered" at `medium` · `P0` `S`
  - Found by the first benchmark run (2026-10-28): `numpy.float` comes back `never_existed` at medium. numpy is indexed from 1.24.0, the release that removed it, and before that numpy served it from a module `__getattr__`. Bench case `numpy-float-alias`.
  - Done 2026-10-28: when the owner module or class defines `__getattr__` at the resolved release, `never_existed` is `low` and says the name may be created at runtime. Every `never_existed` explanation (symbol and parameter) names the earliest indexed release. Still open: `medium` should mean the index covers the package's whole history, which needs ingest to record whether earlier releases exist.
- [ ] **DT-CORE-18**: `*_not_yet_added` between indexed releases: say "added between X and Y" instead of "first appears in Y" · `P0` `S`
  - Found by the pilot cases (2026-10-28): `langchain_core.tools.InjectedToolArg` was added in 0.2.21, but older series are indexed only at their first and last release, so DepTime says it "first appears in 0.2.43". The `>=0.2.43` advice works; the stated fact doesn't. Removals already get a window; additions should too. A matching `first_version_with` needs denser indexing of older series (a data-policy decision).
  - Since DT-DATA-19 (2026-10-29), every release of a series active since 2023 is indexed, and its bench cases pass. It still applies to series that ended before 2023 and to releases whose snapshot failed.
- [x] **DT-CORE-19**: Parse real-world error text, not only clean tracebacks · `P0` `M`
  - Why: 13 bench misses where the error is plainly there but the parser finds no exception line: log prefixes (`api-1 | AttributeError: …`, `ERROR - …`), pytest's `E   TypeError: …` followed by `path:line: TypeError`, indented or `#`-commented lines, errors wrapped in JSON or in another exception (`Exception: module 'dspy' has no attribute 'Assert'`), warnings printed after the exception, bare messages without the type, `module aiohttp has no attribute X` without quotes, and `got an unexpected keyword argument` without a callable name.
  - AC: the bench cases with `fixed_by` DT-CORE-19 get a finding; a failing case in `tests/test_tracebacks.py` for each shape first; still deterministic and still capped in length.
  - Done 2026-10-28: 12 of the 13 cases pass; the 13th now parses and moved to DT-CORE-20. Real cases: root cause 78.0% → 85.5%, root cause + boundary 64.8% → 72.3%.
- [x] **DT-CORE-20**: A parameter check against an unknown signature isn't "never accepted" · `P0` `S`
  - Why: `pytest.skip` is indexed as an alias without parameters, so `skip(msg=...)` comes back `never_existed` at medium confidence although pytest 7.x accepted `msg`. A missing signature should give no parameter verdict (or a note), never a negative one. Bench case `pytest-skip-msg-kwarg-odl`.
  - Done 2026-10-28: the cause wasn't a decorator. pytest's API lives in the private top-level package `_pytest` in the same wheel, which `resolve_external=False` never loaded, so 80 of pytest's 82 public names were aliases (no signatures, classes or members). Ingest now uses griffe's default, which loads only a `_<package>` sibling; pytest 8.0.2 goes from 82 to 859 symbols. No other covered package has one. `SNAPSHOT_FORMAT` 4, so the next build is cold. The query side gives no parameter verdict when the installed release, or any release in a "never accepted" claim, has no signature, and an unknown signature no longer closes an unindexed-version window. Checked on real wheels: `skip(msg)` → `param_removed`, last with 8.0.2, gone from 8.1.1. The case passes once the format-4 index is published.
- [ ] **DT-CORE-21**: Parse numpy 2's class-member removal message (`` `itemset` was removed from the ndarray class in NumPy 2.0. Use `arr[index] = value` instead. ``) · `P1` `S`
  - Why (holdout, 2026-10-31): DT-CORE-05 reads numpy's module-level message ("`np.NaN` was removed in the NumPy 2.0 release") but not this one, so the case gets "not a version-change symptom". A failing case in `tests/test_tracebacks.py` first. The boundary also needs DT-DATA-29.

### Quality (DT-QA)
- [ ] **DT-QA-01**: 200-traceback benchmark from real GitHub issues (`bench/cases/*.jsonl`) · `P0` `L`
  - Done so far (2026-10-28): 159 real cases from 47 packages; none yet for langchain-openai, pinecone and celery (no verifiable report of a break since 2023). Lesson: verify against the shipped wheel, not the GitHub tag.
- [ ] **DT-QA-02**: Benchmark runner + CI report (fail on a regression above 2 points) · `P0` `M` · deps: QA-01
  - Done so far (2026-10-28): `python -m deptime.bench` scores cases as `bench/README.md` defines; `bench.yml` runs it against the published index on code changes and after each `build-index`, failing on a drop of more than 2 points on the cases `bench/baseline.json` shares with the run. First number, 8 seed cases: root cause + boundary 50% (Tier B 80%, Tier A 0%; 4 of 8 are `known_gap`).
- [ ] **DT-QA-03**: Golden tests per Tier A package against the published index (nightly, network-marked) · `P1` `M`
- [x] **DT-QA-04**: Cases derived from TimeMachine-bench (Fujii et al., EACL 2026; github.com/tohoku-nlp/timemachine-bench) · `P1` `M` · deps: QA-02
  - Python repos whose tests pass under old dependencies and fail under new ones, with exact versions. Run the failing tests in a sandbox to get real tracebacks; take ground truth from the fix commits, never from DepTime. An outside, published source avoids the selection bias of self-picked cases.
  - First step: count how many of its repos break on one of our 52 covered packages.
  - Also the holdout: fixes are being designed from the current 159 cases, so the reported number must be measured again on cases collected after them.
  - Done 2026-10-31: 47 of the 100 verified repositories break on a covered package (numpy 22; none on the LLM packages), 32 of them on a removal after the first indexed release, and 29 produce that traceback in their migration environment (`bench/tmb/`: README with the selection and the rule, `triage.json`, `run.py`). Pre-registered in `22dea55`, then scored once: root cause + boundary 72.4%, root cause 82.8%, replacement 0% (`bench/tmb/results-2026-10-31.md`). `bench.yml` now checks these cases too. The full set (1,145, unlabelled) could add more; a filter flags about 655.
- [ ] **DT-QA-05**: Agent-uplift eval on GitChameleon 2.0 (328 version-conditioned Python problems with unit tests, NeurIPS 2025) · `P2` `L`
  - The same agent with and without DepTime's MCP tools; report the pass-rate difference. This answers "does it make my agent better?", which the traceback benchmark doesn't. VersiCode is a backup dataset.
- [x] **DT-QA-06**: Investigate the unexplained bench misses · `P0` `S`
  - `never_existed` or a wrong boundary for `sqlalchemy.schema.ThreadLocalMetaData`, `langchain.server`, `litellm.llms.prompt_templates`, `instructor.core.patch.handle_response_model`, `AnthropicModel.__init__(api_key)` in pydantic-ai, and `numpy.product` (gone after 1.24.4 in the index; really 1.26). Each is an index false-absent or a wrong case: check the shipped wheels, never GitHub tags (crewai 0.67.1's tag lacks `LLM`, its wheel has it).
  - Done 2026-10-28, all six explained against the shipped wheels: `langchain.server` (lived only between the indexed 0.0.1 and 0.0.354) and pydantic-ai's `api_key` (removed in 0.0.45; 0.0.x indexed at 0.0.6 and 0.0.55) are DT-DATA-19; instructor's is an implementation import (DT-DATA-22); the other three are index bugs, now DT-DATA-23 (litellm), DT-DATA-24 (sqlalchemy) and DT-DATA-25 (numpy).

## M2 — Distribution (DT-GTM)

- [ ] **DT-GTM-01**: Generate the 5 rename-map pages from the index (`site/`) · `P0` `M` · deps: DATA-01
- [ ] **DT-GTM-02**: Landing page (or GitHub Pages) with install, demo, benchmark, privacy · `P1` `S`
- [ ] **DT-GTM-03**: Registry listings · `P0` `S` · deps: OSS-05
  - AC: official MCP Registry `server.json` (PyPI package, stdio) under `io.github.cschmertz/deptime`; listed in the main MCP directories.
  - Done 2026-10-28: listed on the official MCP Registry as `io.github.cschmertz/deptime`, and on Glama. `release.yml` publishes `server.json` after PyPI on every tag, and makes a GitHub release.
  - 0.1.3 describes every tool parameter and says when to use each tool over its siblings; a test guards the parameter descriptions. Tool names stay as they are (noun-style `symbol_history`, `coverage`), because renaming breaks the public contract.
- [ ] **DT-GTM-04**: Claude Code plugin release (marketplace) · `P0` `S` · deps: SPIKE-06, OSS-05
- [ ] **DT-GTM-05**: Cursor rule + AGENTS.md snippet, tested in fresh projects · `P1` `S`

## M3 — Optional hosted instance (DT-HOST)

Deployment notes: [docs/05](docs/05-self-hosting.md).

- [ ] **DT-HOST-01**: Deploy a public instance (e.g. Cloud Run or Fly.io) · `P1` `S`
  - AC: `/healthz` green; index downloaded at start-up and persisted (volume or baked into the image); works as a claude.ai custom connector.
- [ ] **DT-HOST-02**: Public-host hardening · `P0` `S` · before HOST-01 goes public
  - AC: MCP transport allowed hosts and origins; proxy burst limits; body caps confirmed.
- [ ] **DT-HOST-03**: Persistent rate-limit store (replaces in-memory) · `P2` `S`
- [ ] **DT-HOST-04**: Privacy notice for the hosted instance ([docs/08](docs/08-privacy-and-security.md)) · `P0` `S` · before HOST-01 goes public

## Parked

- npm/TypeScript ecosystem.
- GitHub Action (checks a PR's dependency bumps against the index).
- A bulk dataset export.
- MCP resources: rename maps as prefetchable context.
