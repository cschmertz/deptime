# DepTime backlog

**How to read this**
- **IDs** look like `DT-<EPIC>-NN` and are referenced from the docs and code. Put the ID in commit messages and PR titles.
- **Priority:**
  - `P0`: blocks the milestone.
  - `P1`: needed for the milestone's goal.
  - `P2`: nice to have.
- **Size:** `S` ≤ ½ day · `M` 1–2 days · `L` 3–5 days.
- **Direction:** free, open source (MIT), local-first ([ADR 0007](docs/adr/0007-open-source-local-first.md)). Success means adoption plus accuracy ([docs/06](docs/06-launch-and-adoption.md)).

| Milestone | Window | Exit criterion |
|---|---|---|
| **M0 Spikes** | Days 1–7 | S1 go/no-go (false-absent ≤ 10%); S2–S7 answered |
| **M1 Local alpha** | Days 7–14 | Public repo; nightly index published; `uvx deptime mcp` works from PyPI; benchmark ≥ 70% |
| **M2 Public launch** | Days ~14–21 | Registries, plugin, snippets, demo GIF, benchmark post, 5 rename-map pages |
| **M3 Optional hosted** | When wanted | Public endpoint for web clients and demos, with limits and a privacy notice |
| **M4 Review** | Days 60 and 90 | Review criteria in [docs/06](docs/06-launch-and-adoption.md) |

---

## Current state: public alpha, v0.1.2 (since 2026-10-28)

- The repo is public. `uvx deptime mcp` installs from PyPI, and pushing a `v*` tag publishes through trusted publishing (`release.yml`).
- `build-index` runs nightly at 04:17 UTC and replaces the rolling `index` release. The first full builds (2026-10-28) indexed 52 packages and 3,371 releases into a 78.9 MB `.gz`.
- Not covered yet: transformers (DT-DATA-16), fastmcp 3.3+ (DT-DATA-17), and names numpy only creates at runtime, such as `numpy.emath` (DT-DATA-02).

**Still open for M1:** the S1 false-absent measurement (DT-SPIKE-01) and the benchmark (DT-QA-01, DT-QA-02).

**Suggested order for the next sessions**
1. Check the first scheduled `build-index` run on the per-package pipeline (DT-OSS-04).
2. Measure S1 and build the benchmark, since both gate M1.
3. DT-DATA-17 (fastmcp 3.3+), then DT-OSS-07 (pin actions to SHAs, Dependabot, `pip-audit`).

---

## ✅ Done (2026-10-17)

- [x] Problem and prior-art research, specs, architecture, hosting and sustainability, launch, risks, privacy/security docs, and ADRs
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

- [ ] **DT-SPIKE-01**: S1 static-analysis accuracy on dynamic packages · `P0` `M` · **go/no-go**
  - AC: a false-absent rate measured on 10 packages × 5 versions × 50 runtime-sampled symbols, with the script in `bench/s1/`. Pass at ≤ 10%; otherwise build the sandboxed dynamic fallback and re-measure; stop if it's still above 10%.
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
- [ ] **DT-OSS-04**: First published index · `P0` `S` · deps: OSS-01, DATA-01
  - AC: `build-index.yml` run manually, then on schedule; `deptime index pull` works from the public URL on a clean machine; release-asset download counts visible.
  - Done so far (2026-10-28): manual runs published the index, and `index pull` works from the public URL in an empty `DEPTIME_HOME`. Still to see: a green scheduled run on the per-package pipeline.
- [x] **DT-OSS-05**: Tagged release through PyPI trusted publishing · `P0` `S` · deps: OSS-03
  - AC: trusted publisher configured; `git tag v0.1.0` publishes; `uvx deptime mcp` works from PyPI.
  - Done 2026-10-28: v0.1.0 published by `release.yml` with provenance; `uvx deptime@0.1.0 mcp` answered over stdio from an empty home.
- [ ] **DT-OSS-06**: CHANGELOG + versioning policy · `P2` `S`
  - AC: Keep-a-Changelog file; semver for the CLI and tool contract; the index schema version is documented.
- [ ] **DT-OSS-07**: Supply chain · `P1` `S`
  - AC: GitHub Actions pinned to SHAs; Dependabot for `uv.lock` and actions; `pip-audit` in CI.
- [ ] **DT-OSS-08**: Windows support for index replace and hot swap · `P1` `S` · deps: SPIKE-07
  - AC: download and refresh succeed while the server holds the old file open (e.g. versioned filenames plus a pointer file).
- [ ] **DT-OSS-09**: Signed index provenance · `P2` `M`
  - AC: `build-index.yml` produces GitHub artifact attestations; `deptime index pull --verify` checks them; documented in SECURITY.md.
- [ ] **DT-OSS-10**: A failed first download recovers without a restart (audit D-01) · `P0` `S`
  - AC: an index that appears on disk is opened on the next call (done); the refresh thread retries with backoff while there is no index; the note names the real cause (failed download, offline).

### Data (DT-DATA)
- [ ] **DT-DATA-01**: Index Tier A with the coverage policy · `P0` `M` · deps: SPIKE-01, SPIKE-04
  - AC: ≥ 90% of selected releases indexed for each package; failures listed in the workflow summary.
- [ ] **DT-DATA-02**: Index Tier B · `P1` `M` · deps: DATA-01
  - AC: same bar as DATA-01; numpy indexed via its `.pyi` stubs; compiled-only gaps noted in `coverage.toml`.
  - Done so far: names numpy's `__init__.py` imports and `__init__.pyi` defines (`add`, `float64`) are indexed for 2.0 and 2.1 too. `numpy.emath.sqrt` is still missing.
- [x] **DT-DATA-03**: Cold-build strategy for CI · `P1` `S` · deps: SPIKE-04
  - AC: the first full build completes (split into a matrix if needed); the snapshot cache stays within Actions cache limits.
  - Done: one job per package builds and caches its own snapshots, then a publish job assembles the index from them and from the published index; only snapshots stay on disk; a timed-out build still saves its cache. The first full builds (2026-10-28) took 35–61 min with every job green; the slowest is litellm (25 min, 290 releases). The snapshot cache is 2.3 GB of the 10 GB limit, including entries from older snapshot formats that expire unused.
- [ ] **DT-DATA-04**: Freshness SLO + yanked handling · `P1` `S`
  - AC: a new release is indexed within 24 h (nightly); yanked releases dropped on rebuild; build date shown by `coverage` and `index status`.
- [ ] **DT-DATA-05**: Lazy-module support (`__getattr__` re-exports) · `P1` `L` · deps: SPIKE-01
  - AC: langchain/transformers false-absent rate below 10%.
- [ ] **DT-DATA-06**: Stdlib module availability per Python version (typeshed `stdlib/VERSIONS`) · `P1` `S`
- [ ] **DT-DATA-07**: Cross-package replacement hints (`google.generativeai` → `google.genai`) · `P1` `S`
- [ ] **DT-DATA-08**: Curated migration map for the top 20 migrations (reviewed; `source="curated"`) · `P1` `M` · deps: SPIKE-02
- [ ] **DT-DATA-09**: Pydantic model fields treated as `__init__` kwargs · `P2` `M`
- [x] **DT-DATA-12**: Ingestion never drops an indexed release by accident (audit I-02) · `P0` `S`
  - AC: a failed retry, `--versions` and a PyPI outage keep already-indexed releases (from the snapshot cache, else rebuilt from the index) (done); `ingest` refuses a downloaded index without `--into-downloaded` and marks it locally built only after a rebuild (done). CI still starts each night from an empty index, so carrying releases forward there needs the previous published index as a base (see DT-DATA-03).
- [ ] **DT-DATA-16**: Store inherited members once per class, then index transformers again · `P1` `M` · deps: SPIKE-04
  - Why: transformers 5.21.0 has ~1.5M public paths, 87% of them inherited methods repeated across ~3,700 model classes (2026-10-28), so it was taken out of `coverage.toml`.
  - AC: snapshots record each class's own members and its bases; queries resolve `Class.member` through the bases; transformers is back in coverage with the `.gz` still under 100 MB.
- [ ] **DT-DATA-17**: Follow a wrapper release to the distribution that holds its code · `P1` `S`
  - Why: since 3.3.0, `fastmcp` is an empty wheel that pins `fastmcp-slim==<same version>`, so its 31 newest releases have no snapshot (2026-10-28). typer 0.12.0 (`typer-slim`) and pinecone 2.2.2–5.0.1 (`pinecone-client`) have the same shape.
  - AC: when a release has no code for its import names and pins one distribution at the same version, ingest analyses that distribution's artifact and records it as the source file; fastmcp 3.3.0+ is indexed. A pin to a different version (dspy 0.1.5 → `dspy-ai==2.4.5`) is still reported as failed.
- [ ] **DT-DATA-18**: Snapshot builds keep memory flat across releases · `P2` `S`
  - Why: building litellm's 290 releases in one process grows by ~45 MB per release and never gives it back, peaking at 11.9 GB of a 16 GB runner (2026-10-28). Only cold builds are affected; nightly runs build a few releases.
  - AC: memory after each release returns to a fixed baseline (e.g. clear griffe's caches, or build each release in a subprocess); litellm's cold build stays under 4 GB.

### Core logic (DT-CORE)
- [ ] **DT-CORE-01**: Benchmark-driven tuning of candidate selection and ranking · `P0` `M` · deps: QA-01
  - AC: root-cause accuracy ≥ 80% (M1 floor: 70%).
- [ ] **DT-CORE-02**: Instance-chain hints for `object_attribute` (prefer frame and referenced-name evidence; show ambiguity) · `P2` `M`
- [ ] **DT-CORE-03**: Import-name collision disambiguation (`agents`) via installed versions · `P1` `S`
- [ ] **DT-CORE-04**: Signature-similarity "moved" heuristic (only if S2 shows a gain) · `P2` `M`
- [ ] **DT-CORE-05**: Library-specific migration exceptions (openai `APIRemovedInV1`, pydantic `PydanticImportError`) · `P1` `S` · deps: SPIKE-03
  - AC: bench cases `openai-v1-chatcompletion` and `pydantic-basesettings-moved` pass.
- [ ] **DT-CORE-06**: Mine replacement hints from the live error text (e.g. mcp 2.x's FastMCP → MCPServer hint), checked against the index · `P1` `S`
- [ ] **DT-CORE-07**: `check_symbols` from a code snippet (extract imports and attributes with `ast`) · `P2` `M`
- [ ] **DT-CORE-08**: Sanitize mined text (control characters, instruction-like content), with adversarial tests · `P0` `S`
- [ ] **DT-CORE-09**: Positional-argument changes (`too_many_positional` → `param_kind_changed`) · `P2` `S`
- [x] **DT-CORE-10**: `diff_versions` filters, sorts and counts in SQL, so `truncated` is truthful on large diffs (audit Q-01) · `P0` `S`
- [x] **DT-CORE-11**: Long tracebacks keep their tail; the MCP input limit is 200K characters (audit Q-02) · `P0` `S`
- [ ] **DT-CORE-12**: Replacement hints are trustworthy before they reach prose (audit Q-03) · `P0` `M`
  - AC: only hints with confidence ≥ 0.75 appear in `explanation` (done); the generic `use X` pattern only takes identifier-shaped targets; targets are checked against the next indexed snapshot.
- [x] **DT-CORE-15**: Optional → required parameters: `param_now_required` / `param_now_optional` events, used for "missing required argument" and checked against the installed release (audit I-10, Q-17) · `P0` `S`
- [ ] **DT-CORE-16**: Errors raised inside a dependency consider the package being called (audit Q-18) · `P0` `S`
  - AC: other indexed packages are searched, installed first, keeping only concrete changes (done; bench case `openai-httpx-proxies-internal`); candidates ranked by `Requires-Dist` once per-release dependency data exists.

### Quality (DT-QA)
- [ ] **DT-QA-01**: 200-traceback benchmark from real GitHub issues (`bench/cases/*.jsonl`) · `P0` `L`
- [ ] **DT-QA-02**: Benchmark runner + CI report (fail on a regression above 2 points) · `P0` `M` · deps: QA-01
- [ ] **DT-QA-03**: Golden tests per Tier A package against the published index (nightly, network-marked) · `P1` `M`

## M2 — Public launch (DT-GTM)

- [ ] **DT-GTM-01**: Generate the 5 rename-map pages from the index (`site/`) · `P0` `M` · deps: DATA-01
- [ ] **DT-GTM-02**: Landing page (or GitHub Pages) with install, demo, benchmark, privacy · `P1` `S`
- [ ] **DT-GTM-03**: Registry listings · `P0` `S` · deps: OSS-05
  - AC: official MCP Registry `server.json` (PyPI package, stdio) under `io.github.cschmertz/deptime`; Smithery, Glama, PulseMCP, mcp.so, mcpservers.org, MCP Market; Glama quality score checked.
  - Done 2026-10-28: listed on the official MCP Registry as `io.github.cschmertz/deptime` 0.1.2. `release.yml` publishes `server.json` after PyPI on every tag.
    Glama: claimed; a build of `deptime mcp` passed (license A, maintenance B); tool scores pending. awesome-mcp-servers: PR #17910 open.
  - Still to do: mcp.so, mcpservers.org (web forms); PulseMCP (submissions paused; it reads the official registry); Smithery; MCP Market.
- [ ] **DT-GTM-04**: Claude Code plugin release (marketplace) · `P0` `S` · deps: SPIKE-06, OSS-05
- [ ] **DT-GTM-05**: Cursor rule + AGENTS.md snippet, tested in fresh projects · `P1` `S`
- [ ] **DT-GTM-06**: Benchmark-led launch post (Show HN, r/Python, r/LangChain, r/ClaudeAI, dev.to) · `P0` `M` · deps: QA-02
- [ ] **DT-GTM-07**: Outreach: since-cutoff (collaborate or share the backend), Context7, Ref, Tessl · `P1` `S`

## M3 — Optional hosted instance (DT-HOST)

Options and costs: [docs/05](docs/05-hosting-and-sustainability.md).

- [ ] **DT-HOST-01**: Deploy a public instance (Cloud Run free tier or Fly.io) · `P1` `S`
  - AC: `/healthz` green; index downloaded at start-up and persisted (volume or baked into the image); works as a claude.ai custom connector.
- [ ] **DT-HOST-02**: Public-host hardening · `P0` `S` · before HOST-01 goes public
  - AC: MCP transport allowed hosts and origins; proxy burst limits; body caps confirmed.
- [ ] **DT-HOST-03**: Persistent rate-limit store (replaces in-memory) · `P2` `S`
- [ ] **DT-HOST-04**: Privacy notice for the hosted instance ([docs/08](docs/08-privacy-and-security.md)) · `P0` `S` · before HOST-01 goes public

## M4 — Review

- [ ] **DT-REVIEW-60**: Accuracy ≥ 70%, and ≥ 50 stars or ≥ 100 index downloads/week · `P0`
- [ ] **DT-REVIEW-90**: Keep investing, or move to maintenance mode ([docs/06](docs/06-launch-and-adoption.md)) · `P0`

## Parked

- npm/TypeScript ecosystem (Augments covers npm).
- GitHub Action (checks a PR's dependency bumps against the index).
- A dataset export or licensing API for docs-context tools.
- MCP resources: rename maps as prefetchable context.
