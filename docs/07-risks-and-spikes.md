# 07 — Risks and spikes

## Pre-spike results (done during scaffolding, 2026-10-17)

| Check | Result |
|---|---|
| griffe on real wheels (httpx 0.27.2 → 0.28.1) | ✅ Loads both in about 0.2 s. Detects `proxies`/`app` removed from `Client.__init__` and from the top-level helpers. |
| Mapping private definition paths → public import paths | ✅ Reports `httpx.request`, not `httpx._api.request`. |
| Deprecation text from a function body | ✅ `"The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead."` becomes replacements `proxy` (0.80) and `mounts` (0.79). |
| Full pipeline on real artifacts | ✅ Real PyPI Simple JSON + wheels → index in 0.24 s (438 spans, 28 events) → `param_removed`, high confidence, pin `httpx<=0.27.2`. |
| Hosted server with a real MCP client (Streamable HTTP) | ✅ Works. Found and fixed a spec 2026-07-28 `resultType` bug on the rate-limit path. |
| **Local mode, as users run it** | ✅ The built wheel run through `uvx --from <wheel> deptime mcp` over stdio, driven by a real MCP client, returns the correct `explain_error` on the real httpx index. stdout stays clean (test-guarded). |
| Claude Code plugin | ✅ `claude plugin validate` passes (Claude Code 2.1.281) with the stdio `uvx deptime mcp` config. |
| `mcp` 1.x → 2.x as a benchmark case | ✅ `import mcp.server.fastmcp` on `mcp==2.3.0` raises `ModuleNotFoundError` with a migration hint. |
| Environment note | ⚠️ Some sandboxes block Python sockets to PyPI (curl and uv still work). The offline suite uses `httpx.MockTransport`; `pytest -m network` is opt-in. |

## Spikes (each with a pass bar)

### S1 — Static-analysis accuracy on dynamic packages (go/no-go)
- **Method:**
  1. Take 10 hard packages × 5 versions each: langchain (lazy `__getattr__`), langchain-community, transformers (lazy module), openai, pydantic.
  2. Sample 50 public symbols per version from **runtime** introspection, in throwaway `uv` venvs.
  3. Compare against DepTime's spans.
- **Pass:** false-absent rate ≤ 10%.
- **If it fails:** add a sandboxed dynamic fallback for packages flagged `lazy`, then re-measure. If it still fails, stop.
- **Result (2026-10-29): passed, 5.1% false absent** (123/2,428; `bench/s1/results-2026-10-29.md`).
  - **Packages:** langchain, langchain-community, langchain-core, openai, pydantic, huggingface-hub, litellm, pydantic-ai-slim, instructor, numpy. transformers left coverage (DT-DATA-16).
  - **Sandbox:** runtime names were sampled only in a GitHub Actions sandbox (`.github/workflows/s1.yml`).
  - **Gate amendment:** the gate leaves out a plain module's imports from elsewhere in its package, which the index excludes by design (22.4% with them; see `bench/s1/README.md`).
  - **Misses:** lazy loaders were nearly clean (langchain-community 0 of 14,813 runtime names). The misses are mostly numpy's computed `__all__` (DT-DATA-25) and litellm's `litellm.types` (DT-DATA-23).
  - **Re-scored** with the same probes as those fixes shipped: 4.2% (2026-10-30), 2.4% (2026-11-01), 1.2% (2026-11-02, litellm 15.6% → 1.5%), and still 1.2% on the `SNAPSHOT_FORMAT` 11 index (2026-11-03).

### S2 — Replacement precision
- **Method:** 50 known migrations from changelogs and migration guides.
- **Pass:** ≥ 80% precision at confidence ≥ 0.75. Misses feed pattern fixes or the curated map (DT-DATA-08).
- **Result (2026-11-02): passed, 11 of 11 hints at confidence ≥ 0.75 right** (`bench/s2/results-2026-11-02.md`).
  - **Set:** 50 migrations (34 symbols, 16 parameters, 41 packages), each with a primary source naming the old and the new API, collected without running DepTime and pre-registered with the scorer (`bench/s2/`).
  - **Recall is the gap:** only 11 of the 50 get a hint at all. The misses are assigned in `BACKLOG.md` under DT-SPIKE-02; the largest group is functions with several warnings, of which only the first is read (DT-DATA-42). With 11 hints the precision estimate is loose, so it is re-scored after each extraction fix.
  - **Re-scored (2026-11-03)** on the `SNAPSHOT_FORMAT` 11 index, after DT-DATA-42, -43, -40, -37 and -38 and DT-CORE-12: a right hint at confidence ≥ 0.75 for 24 of the 50, and 24 of the 25 hints at ≥ 0.75 graded right (96%; `bench/s2/results-2026-11-03.md`). The 25th is flask's "Use `app.json` instead" for `JSONEncoder`, which names `flask.Flask.json` through an instance, so the scorer's path rule can't grade it. After DT-CORE-23 (same day): 26 of the 50, and 26 of 27 right (`bench/s2/results-2026-11-03b.md`). After DT-CORE-24 (same day): 27 of the 50, and 27 of 28 right (`bench/s2/results-2026-11-03c.md`). After DT-DATA-31 (2026-11-04, `SNAPSHOT_FORMAT` 13): 28 of the 50, and 28 of 31 graded right (`bench/s2/results-2026-11-04.md`); the other two are `colormaps.get_cmap` and `pyplot.get_cmap`, which matplotlib lists as alternatives to `cm.get_cmap` beside the documented `matplotlib.colormaps`. After DT-DATA-39 and DT-CORE-22 (2026-11-04, `SNAPSHOT_FORMAT` 14): 32 of the 50, and 32 of 35 graded right (`bench/s2/results-2026-11-04b.md`); the four new ones are arguments that a function passes on to the one that warns about them. With DT-DATA-07 and DT-DATA-08's curated hints (2026-11-05): 42 of the 50, and 42 of 45 graded right (`bench/s2/results-2026-11-05.md`); the 10 gained are all curated, quoted from the same kind of guide the migrations come from, so the figure without them (32 of 50) is what DepTime finds by itself.

### S3 — Traceback → package/version mapping
- **Method:** 100 tracebacks spanning Python 3.10–3.14, pytest output, chained exceptions, and library-specific exceptions (openai `APIRemovedInV1`, pydantic `PydanticImportError`, mcp 2.x's annotated `ModuleNotFoundError`).
- **Pass:** ≥ 90% correct kind + package.
- **Result (2026-11-05): passed on the holdout, 93.8%** (690/736; `bench/s3/results-2026-11-05.md`).
  - **Cases:** real output of known breaks, run in GitHub Actions on Python 3.10–3.14 (`bench/s3/`, `s3.yml`) and printed ten ways: a script (and with colour on 3.13+), pytest (default and `--tb=short`), unittest, `raise … from`, an exception raised while handling another, `logging.exception`, an `asyncio.TaskGroup` and IPython. Generated and committed before DepTime ran on them.
  - **First run, 22 breaks: 73.1%** (708/968). Chained exceptions scored 0%: the parser read the wrapper. So did huggingface_hub's own "No huggingface_hub attribute …" message. Library-specific messages inside an `ExceptionGroup` were missed. pytest, unittest, logging, IPython, colour and 3.14's new suggestions were already fine.
  - **Fix:** DT-CORE-25 follows a chain to the exception the last one wraps, reads `ExceptionGroup` sub-exceptions, and reads the lazy-loader message.
  - **Holdout, 16 other breaks, scored once with the fix: 93.8%.** Kind is right in all 736; the 46 misses are one break, a user's `ParamType` subclass that click 8.2 calls with a new argument (DT-CORE-02).

### S4 — Index size, build time, download size
- **Method:** build the full policy history for Tier A + B in GitHub Actions. Measure:
  - `index.sqlite`, `.xz` and `.gz` sizes;
  - cold build minutes;
  - nightly incremental minutes;
  - p95 query latency.
- **Pass:**
  - `.xz` under 100 MB, so a first-run download takes seconds (the `.gz` beside it is only for older clients);
  - nightly incremental under 30 min;
  - p95 under 20 ms;
  - a cold build fits a 6-hour Actions job, otherwise split it into a matrix.
- **Result (2026-11-05): passed** on the published index (52 packages, 6,599 releases):
  - `.xz` 70.9 MB;
  - nightly 8.4 min, and 162 min for the last cold build (the longest job 124 min, one job per package);
  - p95 in-process over the 196 bench and holdout tracebacks: `explain_error` 3.5 ms, `symbol_history` 1.6 ms, `coverage` 7.6 ms. `explain_error` was 25.5 ms until DT-CORE-16 fixed two query plans.
  - still passing on the `SNAPSHOT_FORMAT` 15 index (DT-DATA-14, same day): `.xz` 71.1 MB, cold build 150 min (litellm's job 114), `explain_error` p95 3.7 ms.

### S5 — Client compatibility for local and hosted modes
- **Local:**
  - `uvx deptime mcp` in Claude Code, Cursor, Codex CLI, VS Code and Claude Desktop (stdio).
  - Does the first-run background download plus the "index downloading" note behave well in each client?
- **Hosted:**
  - Do handshake-era clients (≤ 2025-11-25) work against the stateless 2026-07-28 server?
  - Does a claude.ai custom connector work against a test deployment?
- **Pass:** a documented compatibility matrix in the README.
- **Progress (2026-11-05):** the matrix is in the README (`bench/s5/`).
  - **Local:** Claude Code 2.1.318 and Codex CLI 0.166.0 run `uvx deptime@0.1.4 mcp` and get the right answer. With an empty home, the first call returns the "no index yet" note, and the index lands about 10 s later. A first install on macOS with an x86_64 `uv` compiles `cryptography` (62 s), past Claude Code's 30 s startup limit; the README says to run `uvx deptime --help` once first. Cursor, VS Code and Claude Desktop aren't checked yet.
  - **Hosted:** MCP Python SDK clients 1.8–1.30 and TypeScript SDK clients 1.10–1.32, which open with the `initialize` handshake (protocols 2024-11-05 to 2025-11-25), all work against `deptime serve`. The same clients from 1.0 work over stdio. A claude.ai custom connector against a test deployment isn't checked yet.

### S6 — Do agents call it unprompted?
- **Method:** 20 scripted failures run with and without the plugin (skill + hook).
- **Pass:** with the plugin, `explain_error` is called in ≥ 70% of scenarios, with ≥ 25% fewer turns to a fix.

### S7 — First-run and cross-platform UX
- **Method:** fresh macOS, Linux and Windows machines run `uvx deptime mcp`. Measure time to first useful answer, and check whether Windows can replace the index file while it's open (DT-OSS-08: it can't, so each download gets a file of its own; tested on `windows-latest`).
- **Pass:** a useful answer within 60 s of first launch on a typical connection; no crash on Windows.

## Risk register

| ID | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R1 | Static analysis misses lazy or dynamic exports → false "removed" | M | H | S1 gate; dynamic fallback; confidence downgrade for `lazy` packages; `never_existed` only when every indexed version agrees |
| R2 | Low usage frequency: version errors are occasional | H | M | Plugin hook at the moment of need; `check_symbols` preflight; rename-map pages document common migrations |
| R3 | Other tools (docs-context servers, since-cutoff, the installed environment) cover part of the need | H | M | Lead with cross-version facts, replacements and pins; benchmark against a docs-only baseline |
| R4 | Prompt injection through mined text | L | M | Allowlisted popular packages; length caps; DT-CORE-08 sanitising; fields labelled untrusted |
| R5 | PyPI rate limits or outages during nightly builds | L | M | Snapshot cache; rebuild-from-cache fallback (tested); a publish guard against shrunken indexes |
| R6 | Wrong answers erode trust | M | H | Evidence + confidence on every claim; benchmark in CI; a wrong-answer issue template that feeds the benchmark |
| R7 | Index supply chain: a tampered release asset | L | H | sha256 manifest over HTTPS from GitHub; schema validation; DT-OSS-09 adds signed build provenance (GitHub artifact attestations) |
| R8 | Index staleness | M | M | Nightly builds; `coverage` and `index status` show the build date |
