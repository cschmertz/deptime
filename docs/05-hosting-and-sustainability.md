# 05 — Hosting and sustainability

DepTime is **free, open source (MIT) and local-first**. `uvx deptime mcp` runs on the user's machine, downloads one prebuilt index file from this repo's GitHub Releases, and never sends code or errors anywhere. Building and publishing that index happens in GitHub Actions, which is free for public repos.

**The baseline therefore costs $0 to run.** Hosting is an *option*, not a requirement.

## When a hosted instance is worth it

| Need | Local mode covers it? |
|---|---|
| Claude Code, Cursor, Codex CLI, VS Code, Claude Desktop | ✅ They all launch local (stdio) MCP servers |
| Web/mobile clients that only accept a **remote URL** (claude.ai custom connectors, ChatGPT apps) | ❌ Needs a hosted HTTPS endpoint |
| A zero-install "try it now" link in the README or launch post | ❌ Needs a hosted endpoint |
| Scripts and CI calling a plain JSON API (`/v1/<tool>`) | ⚠️ Possible locally with `deptime serve`; a shared instance is more convenient |

The server is already built for this. `deptime serve` exposes MCP at `/mcp` (stateless, spec 2026-07-28) plus `/v1/*` and `/healthz`, downloads the index in the background, and applies anonymous per-IP rate limits. When a limit is hit, the message points people to unlimited local use.

## Hosted options (checked 2026-10-17)

| Option | Cost | Pros | Cons |
|---|---|---|---|
| **Google Cloud Run** | Likely **$0** at hobby traffic. The free tier covers 2M requests, 180K vCPU-seconds and 360K GiB-seconds per month ([pricing](https://cloud.google.com/run/pricing)). | Scales to zero; the stateless MCP spec fits it perfectly; managed TLS | Cold starts. Bake the index into the image nightly, or mount it from Cloud Storage, so a cold start doesn't download it. Needs a GCP account. |
| **Fly.io** | About **$3–6/month** for one small always-on machine plus a 1 GB volume | `Dockerfile` and `fly.toml` are already in the repo; no cold starts; the volume keeps the index | Small fixed cost; you manage one machine |
| **Smithery hosting** | The registry is free; hosted servers are on paid tiers (check current pricing) | Managed endpoint, plus a listing in a large MCP registry | Less control; check their terms on usage and data before committing |
| **A VPS** (Hetzner and similar) | A few dollars a month | Full control; can run several projects | You own TLS, updates and uptime |
| **Hugging Face Spaces** | Free hardware, but **Docker Spaces need a PRO subscription** (2026), and free Spaces sleep after 48h idle | ML-community visibility | Paywalled for Docker; sleeping breaks agents |
| **Cloudflare Workers** | Free tier | Edge, cheap | Not a fit: the server is Python. It would need a TypeScript port plus D1. |

### Recommendation

1. **Until launch:** local-only.
2. **For a public demo and web clients:** Cloud Run if you're comfortable with GCP (likely free), otherwise Fly.io (already configured, $3–6/month).
3. **Either way,** keep anonymous rate limits on (already implemented) and make every limit message point to `uvx deptime mcp`.

Backlog: DT-HOST-01…04.

## Sustainability (no paywall)

- **GitHub Sponsors / Buy Me a Coffee** links in the README once there are users.
- **The upside that stays open:** licensing or partnering on the dataset with docs-context tools (Context7, Ref, Tessl), or becoming the backend for [since-cutoff](https://github.com/MohammadHijjawi97/since-cutoff).
- **Maintenance budget:** the nightly build is automated. The real cost is triaging wrong-answer reports, and the issue template turns each one into a benchmark case.
