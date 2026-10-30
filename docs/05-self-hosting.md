# 05 — Self-hosting (optional)

DepTime is local-first. `uvx deptime mcp` runs on your machine, downloads one prebuilt index file from this repo's GitHub Releases, and never sends code or errors anywhere. Local mode needs no server.

## When a hosted instance helps

| Need | Local mode covers it? |
|---|---|
| Claude Code, Cursor, Codex CLI, VS Code, Claude Desktop | ✅ They all launch local (stdio) MCP servers |
| Web or mobile clients that only accept a **remote URL** (claude.ai custom connectors, ChatGPT apps) | ❌ Needs a hosted HTTPS endpoint |
| Scripts and CI calling a plain JSON API (`/v1/<tool>`) | ⚠️ Possible locally with `deptime serve`; a shared instance is more convenient |

`deptime serve` exposes MCP at `/mcp` (stateless, spec 2026-07-28) plus `/v1/*` and `/healthz`, downloads the index in the background, and applies anonymous per-IP rate limits. When a limit is hit, the message points people to unlimited local use. Settings are listed in [`.env.example`](../.env.example).

## Deployment notes

The server is a stateless Python container, so any container host works. The repo includes a `Dockerfile` and a `fly.toml`.

| Platform | Notes |
|---|---|
| **Google Cloud Run** | Scales to zero, which suits the stateless MCP spec. To keep cold starts fast, bake the index into the image or mount it from Cloud Storage instead of downloading it at start-up. |
| **Fly.io** | `fly.toml` is ready. One small always-on machine with a 1 GB volume keeps the index, so there are no cold starts. |
| **A VPS** | Full control; you own TLS, updates and uptime. |
| **Cloudflare Workers** | Not a fit: the server is Python. |

- **Behind a proxy:** set `DEPTIME_TRUST_PROXY_HEADERS=true`, and `DEPTIME_CLIENT_IP_HEADER` where the platform sets one, so rate limits key on the real client address.
- **Limits:** keep the anonymous limits on. Trusted callers can use keys (`DEPTIME_DEV_API_KEYS`).
- **Privacy:** a public instance receives users' tracebacks, so publish a privacy notice before opening it ([08](08-privacy-and-security.md)).

Backlog: DT-HOST-01…04.
