# ADR 0001 — Python for everything (ingestion and serving)

**Status:** Accepted, 2026-10-17

## Context

The core engine is griffe, which is Python-only. MCP has first-class SDKs in both TypeScript (v2, split packages) and Python (`mcp` 2.3, `MCPServer`). One toolchain keeps maintenance small.

## Decision

Use one language, Python 3.12+, managed with uv, for ingestion, the query layer, the MCP server, the HTTP API and the CLI.

## Consequences

- One toolchain, one test suite, and shared models across every surface.
- The Python MCP SDK v2 is used through `MCPServer` and `streamable_http_app`. Its API was verified on `mcp==2.3.0`.
- Cloudflare Workers (TypeScript-first) is ruled out as the host; the optional hosted mode runs as a small container.
- If an npm ecosystem is added later (out of scope for v1), its extractor (TypeScript compiler API / api-extractor) can run as a separate offline job that writes the same snapshot JSON format.
