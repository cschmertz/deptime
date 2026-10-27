# 04 — Architecture

DepTime is **local-first** ([ADR 0007](adr/0007-open-source-local-first.md)). The expensive part, analysing every release of ~50 libraries, runs once a night in GitHub Actions. Users download the result as a single SQLite file, and every question is answered on their own machine.

## Overview

```
 ┌──────────────── GitHub Actions, nightly (.github/workflows/build-index.yml): free on public repos ────────────────┐
 │                                                                                                                 │
 │  PyPI JSON Simple API ─► select versions ─► download + sha256 verify ─► safe extract ─► griffe STATIC load       │
 │  (PEP 691/700)          (coverage policy)   (200 MB cap)               (zip-slip guard,  (never import/exec)    │
 │                                                                          tar "data" filter)       │             │
 │      snapshot cache (actions/cache) ◄── snapshots/<pkg>/<ver>.json.gz ◄───────────────────────────┘             │
 │                       │                                                                                         │
 │                       └─► diff consecutive releases ─► spans + events + replacements ─► index.sqlite            │
 │                                                     deptime index pack ─► index.sqlite.gz + manifest.json       │
 │                                          guard: refuse to publish if packages drop > 10% vs. previous           │
 └──────────────────────────────────────────────────────────────────────────────┬──────────────────────────────────┘
                                                                                │ gh release upload index --clobber
                                         rolling pre-release `index` on GitHub ▼ (stable URLs)
 ┌──────────────── the user's machine ───────────────────────────────────────────┴────────────────────────────────┐
 │  Claude Code / Cursor / Codex ──stdio──► `uvx deptime mcp` ──► DepTime query layer ──► ~/.cache/deptime/index   │
 │                                              │   (read-only SQLite)                       ▲                     │
 │                                              └─ background thread: first-run download / daily refresh check ───┘ │
 │                                                 (sha256 + schema verified, atomic swap, never replaces a          │
 │                                                  locally built index; DEPTIME_OFFLINE=1 disables it)              │
 │  plugin hook (local): sees a Python version-change traceback ─► suggests calling `explain_error`                 │
 └─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
   Optional hosted mode: the same code behind `deptime serve` (MCP over HTTP at /mcp + JSON at /v1/*). See docs/05.
```

## Modes

| Mode | Command | Transport | Who uses it | Metering |
|---|---|---|---|---|
| **Local (default)** | `uvx deptime mcp` | stdio | Claude Code, Cursor, Codex, VS Code, Claude Desktop | none |
| CLI | `deptime explain/history/check/diff/coverage` | terminal | humans, scripts | none |
| Hosted (optional) | `deptime serve` | Streamable HTTP (stateless, spec 2026-07-28) + JSON | web clients that need a URL; demos | anonymous per-IP limits |
| Index builder | `deptime ingest` + `deptime index pack` | n/a | CI and contributors | n/a |

## Code map

| Module | Responsibility |
|---|---|
| `deptime/config.py` | `Settings` from env (home dir, index URL, offline, hosted options); loads `deptime/coverage.toml` |
| `deptime/coverage.toml` | Packages, import names, tiers, version policy (shipped inside the wheel) |
| `deptime/ingest/pypi.py` | PEP 691 client, version policy, artifact choice, download, safe extraction |
| `deptime/ingest/snapshot.py` | griffe → `ApiSnapshot`: public import paths, kinds, params, deprecation text |
| `deptime/ingest/diff.py`, `replacements.py` | Change events; "use X instead" mining, including at parameter level |
| `deptime/ingest/pipeline.py` | One package end to end; snapshot cache; rebuilds from cache when PyPI is down; never wipes on empty runs |
| `deptime/index/schema.sql`, `store.py` | SQLite schema (span encoding), atomic per-package rebuild, read queries |
| `deptime/index/distribution.py` | `pack` (publish), `download`/`ensure_index` (consume), background refresh with hot swap |
| `deptime/tracebacks.py` | Traceback → `ErrorSignal` (stdlib only, capped input) |
| `deptime/query.py` | `DepTime`: the five tools; deterministic; `set_store` for hot swaps |
| `deptime/server/mcp_app.py` | `MCPServer` + tool registrations (names and descriptions are product copy) |
| `deptime/server/http_app.py`, `metering.py` | Hosted mode: `/mcp`, `/v1/*`, `/healthz`, anonymous rate limits |
| `deptime/cli.py` | `mcp`, `explain`, `history`, `check`, `diff`, `coverage`, `index pull|status|pack`, `ingest`, `serve` |

## Data model

The full schema is in `src/deptime/index/schema.sql`.

- **`releases`**: the indexed releases per package, each with an `ordinal` (0…n-1 in version order).
- **`spans`**: interval encoding. Each row is a *maximal run of consecutive indexed releases in which a public path has an identical state* (kind, canonical path, params, deprecation).
  - Storage grows with the number of changes, not symbols × versions.
  - "Does X exist at version V?" is a point query.
- **`events`**: diff facts per release. `prev_ordinal IS NULL` marks state already present in the earliest indexed release, which is why `deprecated_in = "<=X"`.
- **`replacements`**: candidates per symbol, or per `func(param)`, with confidence, source and evidence.
- **Snapshots** (`.json.gz`) are the immutable intermediate. CI caches them, so a nightly run only analyses new releases, and the whole index can be rebuilt from them after any change to the diff logic.

## Index distribution

| Asset (rolling release `index`) | Contents |
|---|---|
| `manifest.json` | `schema_version`, `built_at`, `sha256` and `size` of the `.gz`, package/release counts, `deptime_version` |
| `index.sqlite.gz` | The VACUUMed SQLite index (gzip, deterministic header) |

The client logic lives in `distribution.ensure_index`:

- **Offline mode:** use whatever exists.
- **Locally built index** (no `index.sqlite.meta.json`): never touched.
- **Downloaded index:** fresh within `DEPTIME_INDEX_MAX_AGE_HOURS` (24) means no network at all. Otherwise fetch the manifest, and download only if the sha256 changed.
- **Every download:** stream with a size cap → verify the sha256 → gunzip to a temp file → validate it opens with a matching `schema_version` → `os.replace` into place → write the meta file.
- **Any failure** keeps the current index.
- **`deptime mcp` and `deptime serve`** run this in a background thread and hot-swap the store, so first-run start-up is never blocked. Until the index lands, tools return a note saying it's downloading.

## Serving

- **Local:** `MCPServer.run(transport="stdio")`. stdout carries only protocol frames; all logs go to stderr (guarded by `tests/test_stdio.py`).
- **Hosted:** `streamable_http_app(stateless_http=True, json_response=True)`.
  - `ServerMiddleware` applies anonymous limits before validation.
  - Over the limit, it returns a typed `CallToolResult(is_error=True)` (spec 2026-07-28 requires `resultType`) whose message points to `uvx deptime mcp`.
- **Performance:** each call is a few indexed SQLite point reads; target p95 is under 20 ms in-process.

## Costs

| Piece | Cost |
|---|---|
| Nightly ingestion + publishing (GitHub Actions, public repo) | $0 |
| Index hosting (GitHub Releases) | $0 |
| PyPI | $0 |
| Optional hosted instance | $0 (Cloud Run free tier) to about $3–6/month (Fly.io); see [05](05-hosting-and-sustainability.md) |
