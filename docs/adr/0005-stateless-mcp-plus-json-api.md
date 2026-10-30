# ADR 0005 — Stateless MCP (spec 2026-07-28) plus a JSON API in one process

**Status:** Accepted, 2026-10-17. Since [ADR 0007](0007-open-source-local-first.md), this describes the *optional hosted* mode; the default is local stdio (`deptime mcp`).

## Context

The current MCP spec (2026-07-28) carries the protocol version per request in `_meta` and adds a mandatory `server/discover`, so servers can be stateless. Some agents and scripts can't speak MCP. Rate limits must cover both surfaces.

## Decision

- Serve MCP at `/mcp` with `MCPServer.streamable_http_app(stateless_http=True, json_response=True)`.
- Add `/v1/<tool>` JSON routes (through `custom_route`) that call the same `DepTime` query layer and return the same models.
- Rate-limit MCP calls in a `ServerMiddleware`, which runs before validation for every `tools/call`. Rate-limit REST calls with the same quota store in the route.

## Consequences

- No session affinity, so scaling out is just adding machines.
- Middleware short-circuits must return **typed** results (`CallToolResult`). A bare dict lacks the `resultType` required by 2026-07-28, and real clients reject it; this was found in the live smoke test and is now covered by a test.
- Back-compat with handshake-era clients relies on the SDK (to verify in spike S5).
