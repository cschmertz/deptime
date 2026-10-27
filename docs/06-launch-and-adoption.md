# 06 — Launch and adoption

**Goal:** a widely used, well-regarded open-source tool, earned through real usage and answers agents can trust.

## Core insight

With about 43K MCP servers, **agents don't call tools they don't know they need**, and people don't star repos they can't evaluate in 30 seconds. Three layers:
1. **Proof.** Benchmark numbers on real tracebacks, plus a 10-second demo GIF.
2. **Presence.** MCP registries, PyPI, the Claude Code plugin, snippets.
3. **Trigger.** The plugin hook suggests `explain_error` at the exact moment of need.

## Channels

| Channel | Action | Backlog |
|---|---|---|
| GitHub repo | README with demo GIF, benchmark table, one-line install, honest prior-art section; topics: `mcp`, `python`, `coding-agents` | DT-OSS-01/02 |
| PyPI | `uvx deptime mcp` must just work (trusted publishing on tags) | DT-OSS-05 |
| Official MCP Registry | `server.json` for the **local** server (PyPI package, stdio) under `io.github.cschmertz/deptime` | DT-GTM-03 |
| Smithery, Glama, PulseMCP, mcp.so, mcpservers.org, MCP Market | List it; tune tool descriptions against Glama's quality score | DT-GTM-03 |
| Claude Code plugin | MCP (stdio) + skill + traceback hook, published to a marketplace | DT-GTM-04 |
| Cursor rule + AGENTS.md snippet | Copy-paste setup for other agents | DT-GTM-05 |
| Rename-map pages | Generated from the index; they rank in search and get cited by AI answers | DT-GTM-01 |

## Content: rename-map pages

Generated from the index, never written by hand:
1. **MCP Python SDK 1.x → 2.x.** `FastMCP` → `MCPServer` (verified on `mcp==2.3.0`).
2. **LangChain 0.3 → 1.x.**
3. **`google-generativeai` → `google-genai`.**
4. **pydantic v1 → v2.**
5. **openai 0.x → 1.x.**

## Launch plan

| When | What |
|---|---|
| Days 1–7 | **Spikes S1–S6** ([07](07-risks-and-spikes.md)). Go/no-go on S1: false-absent rate ≤ 10%. |
| Days 7–14 | Turn on the nightly index build; index Tier A + B; publish to PyPI; build the 200-traceback benchmark; record the demo GIF |
| ~Day 14 | **Public launch:** repo public, registries, plugin, snippets |
| Days 14–21 | **Benchmark-led posts:** Show HN, r/Python, r/LangChain, r/ClaudeAI, dev.to. Write-up: "We replayed 200 real tracebacks from GitHub issues…" |
| Days 14–30 | **Outreach:** since-cutoff (collaborate or share the backend), Context7, Ref, Tessl |
| Days 60 / 90 | Reviews (below) |

## What we can measure in local mode (no telemetry, by design)

| Signal | Source |
|---|---|
| **Index downloads per week** | GitHub API: `download_count` on the `index.sqlite.gz` release asset. This is the best proxy for active installs, since each install refreshes it at most once a day. |
| PyPI downloads | pypistats.org / BigQuery public dataset |
| Stars, forks, watchers | GitHub |
| Issues and PRs from outside contributors | GitHub. Wrong-answer reports also count as engagement. |
| Benchmark accuracy | `bench/` in CI |
| Hosted instance usage (if any) | Server logs (privacy rules in [08](08-privacy-and-security.md)) |

## Review criteria (written in advance)

- **Day 60:**
  - Benchmark root-cause accuracy **≥ 70%**. Otherwise, fix accuracy before any promotion; a wrong-answers tool does reputational damage.
  - Adoption: **≥ 50 stars or ≥ 100 index downloads/week.**
- **Day 90:**
  - **Keep investing** if there are ≥ 150 stars, ≥ 300 index downloads/week, ≥ 3 outside contributors, or one notable integration (since-cutoff, a docs vendor, an agent framework).
  - **Otherwise, maintenance mode.** Keep the nightly build running (it's free) and keep fixing reported wrong answers, but stop adding features.
