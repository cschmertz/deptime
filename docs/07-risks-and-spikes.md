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

### S2 — Replacement precision
- **Method:** 50 known migrations from changelogs and migration guides.
- **Pass:** ≥ 80% precision at confidence ≥ 0.75. Misses feed pattern fixes or the curated map (DT-DATA-08).

### S3 — Traceback → package/version mapping
- **Method:** 100 tracebacks spanning Python 3.10–3.14, pytest output, chained exceptions, and library-specific exceptions (openai `APIRemovedInV1`, pydantic `PydanticImportError`, mcp 2.x's annotated `ModuleNotFoundError`).
- **Pass:** ≥ 90% correct kind + package.

### S4 — Index size, build time, download size
- **Method:** build the full policy history for Tier A + B in GitHub Actions. Measure:
  - `index.sqlite` and `.gz` sizes;
  - cold build minutes;
  - nightly incremental minutes;
  - p95 query latency.
- **Pass:**
  - `.gz` under 100 MB, so a first-run download takes seconds;
  - nightly incremental under 30 min;
  - p95 under 20 ms;
  - a cold build fits a 6-hour Actions job, otherwise split it into a matrix.

### S5 — Client compatibility for local and hosted modes
- **Local:**
  - `uvx deptime mcp` in Claude Code, Cursor, Codex CLI, VS Code and Claude Desktop (stdio).
  - Does the first-run background download plus the "index downloading" note behave well in each client?
- **Hosted:**
  - Do handshake-era clients (≤ 2025-11-25) work against the stateless 2026-07-28 server?
  - Does a claude.ai custom connector work against a test deployment?
- **Pass:** a documented compatibility matrix in the README.

### S6 — Do agents call it unprompted?
- **Method:** 20 scripted failures run with and without the plugin (skill + hook).
- **Pass:** with the plugin, `explain_error` is called in ≥ 70% of scenarios, with ≥ 25% fewer turns to a fix.

### S7 — First-run and cross-platform UX
- **Method:** fresh macOS, Linux and Windows machines run `uvx deptime mcp`. Measure time to first useful answer, and check whether Windows can replace the index file while it's open (DT-OSS-08).
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
