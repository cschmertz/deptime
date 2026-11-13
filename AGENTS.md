# AGENTS.md — working in the DepTime repo

The guide for every coding agent (and person) working in this repo. `CLAUDE.md` only imports this file, so edit this one.

DepTime is an open-source (MIT), **local-first** MCP server. It answers "what changed in this Python library, in which release, and what replaces it?" from a prebuilt index of static API snapshots.

Read `README.md` first. The plan is in `BACKLOG.md`; put its IDs (e.g. `DT-CORE-05`) in commit messages.

**Maintenance mode (since 2026-11-13).** Read the rules in `BACKLOG.md` before starting work:
- work starts from a reported wrong answer or a failed scheduled check, and no new holdout sets are collected;
- answer `unknown` or add a `[[package.served]]` entry before writing new extraction code;
- keep `BACKLOG.md` short: one line per change, with measurements in `bench/**/results-*.md` and commit bodies.

Finished work and its measurements are in `docs/backlog-archive.md`, which is frozen: don't edit it.

**Commit messages** (from 2026-11-10 on; don't reword or rebase older commits):
- **Subject:** one short plain-English sentence, aiming for 72 characters or fewer, that someone who has never opened the codebase can follow: what changed and why it matters.
  - No function names, file paths or jargon. The five tool names (`check_symbols`, `explain_error`, `diff_versions`, `symbol_history`, `coverage`) are fine.
  - Backlog IDs go in parentheses at the end: the one or two main ones. List any others on the body's first line (`Also DT-..., DT-...`); `git log --grep` searches the body too.
  - Example: "Point to a module's new home when a library removes it (DT-CORE-45)".
- **Body:** the full technical detail, as before: why, numbers, checks, gaps.
- **Numbers:** the subject never states a number the body doesn't back up. Round down, or leave it out.
- **Scope:** this is for the maintainer's commits. Contributors don't have to follow it.

## Commands

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run pytest     # before every commit
uv run pytest -m network                  # opt-in: real PyPI (Python sockets may be blocked in some sandboxes)
uv run pytest -m published                # opt-in: golden tests on the published index (`deptime index pull` first)
uv run deptime mcp                        # local stdio server
uv run python -m deptime.bench bench/cases --baseline bench/baseline.json   # exit 1 on a regression (`deptime index pull` first; CI's bench.yml runs it)
```

The default home is `$XDG_CACHE_HOME/deptime`, else `~/.cache/deptime` (override with `DEPTIME_HOME`). Set `DEPTIME_OFFLINE=1` to disable all network access.

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
8. **Never persist or log raw tracebacks or `pip freeze`** (docs/08).
9. **Hosted-mode middleware short-circuits return a typed `CallToolResult`.** Spec 2026-07-28 requires `resultType`.
10. **Text mined from library source is untrusted data.** Every field that carries it passes through `untrusted_text` on its way out (`untrusted.py`, docs/08).

## Where things go

- Parser shapes: `tracebacks.py` (+ a failing case in `tests/test_tracebacks.py` first)
- Diff, deprecations, replacements: `ingest/diff.py`, `ingest/snapshot.py`, `ingest/replacements.py`
- Tool behaviour: `query.py`. Tests use the synthetic 3-release httpx index in `tests/conftest.py`.
- Download, refresh, packing: `index/distribution.py` (`tests/test_distribution.py`, all via `httpx.MockTransport`)
- Covered packages: `src/deptime/coverage.toml` (ships in the wheel; set correct `import_names`)
- Curated migration hints: `src/deptime/curated.toml` (ships in the wheel; each must pass the checks in its header, ADR 0006)

## Verified external APIs

- **MCP Python SDK `mcp==2.3.0` (checked 2026-10-17):**
  - `from mcp.server import MCPServer`; `@mcp.tool(...)`; `mcp.run(transport="stdio")`; `mcp.custom_route(...)`;
  - `streamable_http_app(stateless_http=True, json_response=True)`;
  - `ServerMiddleware` = `async (ctx, call_next)`;
  - tests: `mcp.Client(server)` in-process, or `mcp.Client(StdioServerParameters(command=..., args=[...], env=...))`.
- **griffe 2.4 (checked 2026-10-31):** `griffe.load(name, search_paths=[...], allow_inspection=False, resolve_aliases=True, resolve_external=None)` (`None` loads only a private `_<package>` sibling such as `_pytest`, from `search_paths` alone); aliases expose `final_target`; `Class.all_members` includes inherited members.
- **Claude Code plugins (checked 2026-11-10 on 2.1.322):** `claude plugin validate integrations/claude-code` passes with no plugin-directory warnings. The icon is `.claude-plugin/icon.png` (1024 px, from `docs/assets/deptime-icon.svg`); the directory takes it only from the first submission. Failed Bash commands fire `PostToolUseFailure`.
- **Claude Code instruction files (2.1.322):** with no `CLAUDE.md`, a project's `AGENTS.md` loads in its place (the `instructionFiles` setting); the import in `CLAUDE.md` also covers older versions and setups that turn that off.
