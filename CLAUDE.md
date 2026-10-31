# CLAUDE.md — working in the DepTime repo

DepTime is an open-source (MIT), **local-first** MCP server. It answers "what changed in this Python library, in which release, and what replaces it?" from a prebuilt index of static API snapshots.

- **Default mode:** `deptime mcp` over stdio.
- **The index:** built nightly in GitHub Actions and downloaded from GitHub Releases.
- **Optional hosted mode:** `deptime serve`.

Read `README.md` first. The plan is in `BACKLOG.md`; put its IDs (e.g. `DT-CORE-05`) in commit messages.

## Commands

```bash
uv sync                                   # Python 3.12, managed by uv
uv run ruff format src tests && uv run ruff check src tests && uv run pytest     # before every commit
uv run pytest -m network                  # opt-in: real PyPI (Python sockets may be blocked in some sandboxes)
uv run deptime mcp                        # local stdio server
uv run deptime ingest <pkg> [--versions ...] | index pull|status|pack | explain | history | check | diff | coverage | serve
```

The default home is `~/.cache/deptime` (override with `DEPTIME_HOME`). Set `DEPTIME_OFFLINE=1` to disable all network access.

## Invariants (don't break these)

1. **Never import or execute indexed packages.** Use griffe with `allow_inspection=False` (ADR 0002).
2. **No LLM calls at answer time** (ADR 0006). Answers are deterministic and carry evidence and `confidence`. Prefer `not_covered` or a `note` over guessing.
3. **Tool names, descriptions and `models.py` fields are a public contract.** Add; don't rename.
4. **In stdio mode, stdout belongs to the MCP protocol.** Log to stderr only (`tests/test_stdio.py` guards this).
5. **Index distribution must stay safe** (`index/distribution.py`):
   - verify the sha256 and the schema;
   - replace the file atomically;
   - never replace a locally built index (no `.meta.json`);
   - never damage the current index when a download fails.
6. **Ingestion must never lose data:**
   - an empty run doesn't wipe an index;
   - a PyPI outage rebuilds from cached snapshots;
   - one package's failure doesn't stop the run.
7. **Version semantics:**
   - `pin_to_restore` uses `<=last_version_with`;
   - a deprecation present in the earliest indexed release is `<=X`;
   - an unindexed installed version produces a "window" finding at `medium`.
8. **Never persist or log raw tracebacks or `pip freeze`** (docs/08). Mined text is untrusted data.
9. **Hosted-mode middleware short-circuits return a typed `CallToolResult`.** Spec 2026-07-28 requires `resultType`.

## Where things go

- Parser shapes: `tracebacks.py` (+ a failing case in `tests/test_tracebacks.py` first)
- Diff, deprecations, replacements: `ingest/diff.py`, `ingest/snapshot.py`, `ingest/replacements.py`
- Tool behaviour: `query.py`. Tests use the synthetic 3-release httpx index in `tests/conftest.py`.
- Download, refresh, packing: `index/distribution.py` (`tests/test_distribution.py`, all via `httpx.MockTransport`)
- Covered packages: `src/deptime/coverage.toml` (ships in the wheel; set correct `import_names`)
- Benchmark cases: `bench/cases/*.jsonl`

## Verified external APIs (2026-10-17)

- **MCP Python SDK `mcp==2.3.0`:**
  - `from mcp.server import MCPServer`; `@mcp.tool(...)`; `mcp.run(transport="stdio")`; `mcp.custom_route(...)`;
  - `streamable_http_app(stateless_http=True, json_response=True)`;
  - `ServerMiddleware` = `async (ctx, call_next)`;
  - tests: `mcp.Client(server)` in-process, or `mcp.Client(StdioServerParameters(command=..., args=[...], env=...))`.
- **griffe 2.4 (re-checked 2026-10-31):** `griffe.load(name, search_paths=[...], allow_inspection=False, resolve_aliases=True, resolve_external=None)` (`None` loads only a private `_<package>` sibling such as `_pytest`, from `search_paths` alone); aliases expose `final_target`; `Class.all_members` includes inherited members.
- **Claude Code plugins:** `claude plugin validate integrations/claude-code` passes on 2.1.281. Failed Bash commands fire `PostToolUseFailure`.
