# 04 — Architecture

DepTime is **local-first** ([ADR 0007](adr/0007-open-source-local-first.md)). The expensive part, analysing every release of ~50 libraries, runs once a night in GitHub Actions. Users download the result as a single SQLite file, and every question is answered on their own machine.

## Overview

```
 ┌──────────────── GitHub Actions, twice a day (.github/workflows/build-index.yml) ──────────────────────────────────┐
 │                                                                                                                 │
 │  PyPI JSON Simple API ─► select versions ─► download + sha256 verify ─► safe extract ─► griffe STATIC load       │
 │  (PEP 691/700)          (coverage policy)   (200 MB cap)               (zip-slip guard,  (never import/exec)    │
 │                                                                          tar "data" filter)       │             │
 │      snapshot cache (actions/cache) ◄── snapshots/<pkg>/<ver>.json.gz ◄───────────────────────────┘             │
 │                       │                                                                                         │
 │                       └─► diff consecutive releases ─► spans + events + replacements ─► index.sqlite            │
 │                                                     deptime index pack ─► index.sqlite.{xz,gz} + manifest.json  │
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

- **`releases`**: the indexed releases per package, each with an `ordinal` (0…n-1 in version order) and the `Requires-Dist` of the artifact it was analysed from (NULL when unknown).
- **`spans`**: interval encoding. Each row is a *maximal run of consecutive indexed releases in which a public path has an identical state* (kind, canonical path, params, the symbol's own deprecation, and the arguments its warnings, decorators or docstring deprecate).
  - Storage grows with the number of changes, not symbols × versions.
  - "Does X exist at version V?" is a point query.
- **`internal_spans`**: the same encoding for names modules bind that aren't public API: private names, a module's imports from outside the standard library, the private names a module `__getattr__` serves, and every name in a private module (DT-DATA-22). Integration libraries import them (motor imported pymongo 4.8's `pymongo.cursor._QUERY_OPTIONS`) and break when they go. Kind and canonical path only, no signatures, and no events, so `diff_versions` is unchanged. Not recorded: class members other than the public ones, names only type checkers see, and the standard library's names. Clients from before DT-DATA-22 don't read the table.
- **`classes`** and **`members`**: for a package whose coverage entry sets `own_members` (DT-DATA-16), each class's own members, once, keyed by the class's canonical path, and its MRO; the package's `spans` then have no class members. A member of a public class is looked up when queried, in the class and then each class of its MRO at that release, as Python finds it, and the store returns rows shaped like `spans` rows, so the tools don't change. transformers' latest release has 1.6M public paths, 83% of them inherited members (`PreTrainedModel`'s 400 methods under each of ~3,700 model classes); stored this way it has 37k paths and 109k own members.
  - A member's events are stored once, at its defining class's label (the class's shortest public path), which is where `diff_versions` reports them; a subclass's member history is worked out from its states release by release, as a build would, so `symbol_history` and the findings read the same either way. Hints mined from a deprecation are stored at the label too, and named as the subclass's member when it has the same one.
  - An index with such a package has schema version 2; schema 1 has none. Clients before DT-DATA-16 read only schema 1, since they'd read inherited members as missing.
- **`events`**: diff facts per release. `prev_ordinal IS NULL` marks state already present in the earliest indexed release, which is why `deprecated_in = "<=X"`.
- **`replacements`**: candidates per symbol, or per `func(param)`, with confidence, source and evidence.
- **Snapshots** (`.json.gz`) are the immutable intermediate. CI caches them, so a scheduled run only analyses new releases, and the whole index can be rebuilt from them after any change to the diff logic.
  - Each covered package gets its own CI job, with its own runner, log and snapshot cache, so one heavy package can't take the others down. These jobs only build snapshots (`deptime ingest --snapshots-only`). A final job assembles the index from their snapshots with `deptime ingest --all --cached-only`, which never downloads or analyses a release (DT-DATA-03).
  - The index rebuild streams snapshots from disk in version order, holding at most two, so memory doesn't grow with a package's release count.
  - Each package's rows are stored with a fingerprint of what they were assembled from: a hash of its snapshot files, release dates, coverage entry and the assembly code. A package whose fingerprint matches this run's inputs keeps its rows, so a scheduled run assembles only the packages with a new release (DT-DATA-41). Rows that took anything from the index itself have no fingerprint and are always rebuilt.

## Index distribution

| Asset (rolling release `index`) | Contents |
|---|---|
| `manifest.json` | `schema_version`, `built_at`, `sha256` and `size` of the `.gz`, `xz_sha256` and `xz_size` of the `.xz`, package/release counts, `deptime_version`. Uploaded after the other two. |
| `index.sqlite.xz` | The VACUUMed SQLite index (xz, preset 6). What deptime downloads when the manifest lists it. |
| `index.sqlite.gz` | The same file (gzip, deterministic header), about 60% larger. Kept for deptime 0.1.3 and earlier, which read only the manifest keys they know, and for a Python built without `lzma`. |

The client logic lives in `distribution.ensure_index`:

- **Offline mode:** use whatever exists.
- **Locally built index** (no `index.sqlite.meta.json`): never touched.
- **Downloaded index:** fresh within `DEPTIME_INDEX_MAX_AGE_HOURS` (24) means no network at all. Otherwise fetch the manifest, and download only if the sha256 changed.
- **Every download:** stream the `.xz` (or the `.gz`) with a size cap → verify that file's sha256 → decompress to a temp file → validate it opens with a `schema_version` this client reads → `os.replace` it to a name of its own, `index-<sha256[:16]>.sqlite` → write the meta file, whose `file` key names it → delete the older files nothing has open.
- **Why a new name each time** (DT-OSS-08): Windows refuses to replace or delete a file another process has open, and every running `deptime mcp` holds the index open. Each process opens the file the meta file names when it starts; a server that refreshes switches to the new file and closes the old one a minute later, and a server still running on an old file keeps answering from it until it restarts. Without a `file` key (a locally built index, or one downloaded before DT-OSS-08) the index is `index.sqlite` itself. `ci.yml` runs the whole suite on Windows.
- **Any failure** keeps the current index.
- **`deptime mcp` and `deptime serve`** run this in a background thread and hot-swap the store, so first-run start-up is never blocked. Until the index lands, tools return a note saying it's downloading. If the first download fails, the thread retries after 30 s, 1, 2, 5 and 10 minutes, then every 30 minutes, and the note names the error and the next attempt; with `DEPTIME_OFFLINE` set, it says so (DT-OSS-10).

## Serving

- **Local:** `MCPServer.run(transport="stdio")`. stdout carries only protocol frames; all logs go to stderr (guarded by `tests/test_stdio.py`).
- **Hosted:** `streamable_http_app(stateless_http=True, json_response=True)`.
  - `ServerMiddleware` applies anonymous limits before validation.
  - Over the limit, it returns a typed `CallToolResult(is_error=True)` (spec 2026-07-28 requires `resultType`) whose message points to `uvx deptime mcp`.
- **Performance:** each call is a few indexed SQLite point reads; target p95 is under 20 ms in-process.
