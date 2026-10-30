# ADR 0007 — Open source (MIT), local-first, index distributed through GitHub Releases

**Status:** Accepted, 2026-10-17.

## Context

The tool is most useful when anyone can run it, free and locally, without an account or a key.

Coding agents are the primary users, and they all run local (stdio) MCP servers. A hosted service would receive users' tracebacks and need a privacy policy.

The index is read-only data that changes daily, so it can be shipped as a file.

## Decision

1. **License:** MIT for code and data.
2. **Default mode is local:** `uvx deptime mcp` runs the MCP server over stdio. Tracebacks and `pip freeze` never leave the machine.
3. **Index distribution:**
   - A nightly GitHub Actions job (`build-index.yml`) ingests every covered package, using cached snapshots so it's incremental.
   - It packs `index.sqlite.gz` + `manifest.json` (sha256, schema version, counts) and uploads both to a **rolling pre-release tagged `index`**.
   - Clients download from stable URLs (`…/releases/download/index/<asset>`).
   - A guard refuses to publish an index that lost more than 10% of its packages.
4. **Client behaviour:**
   - Download in the background on first run, so server start isn't blocked, and hot-swap the store when it lands.
   - Refresh check at most every 24 h.
   - Verify sha256, validate the schema, replace atomically.
   - Never auto-replace a locally built index.
   - `DEPTIME_OFFLINE=1` disables all network access.
5. **Hosted mode remains** (`deptime serve`, ADR 0005) as an option for web clients and demos (see [05](../05-self-hosting.md)), with anonymous rate limits whose message points to local use.

## Consequences

- **No server by default.** Tracebacks never leave the user's machine.
- **No usage telemetry.**
- **Distribution chain:** the index URL depends on the repo being published as `cschmertz/deptime` (`config.DEFAULT_INDEX_URL`, overridable with `DEPTIME_INDEX_URL`).
- **stdout belongs to the protocol in stdio mode.** All logging goes to stderr, and `tests/test_stdio.py` guards this.
- **Windows:** replacing an index file that's open may fail. Tracked as DT-OSS-08.
