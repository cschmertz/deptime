# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Backlog IDs (`DT-…`) point to
[BACKLOG.md](BACKLOG.md), which has the details and the measurements behind each change.

## Versioning

- **Releases** follow [Semantic Versioning](https://semver.org/). What it versions is the contract agents and scripts
  depend on: the MCP tool names and parameters, the fields of every result (`src/deptime/models.py`, also served by
  the HTTP API), and the `deptime` CLI.
- **Contract changes are additive.** A tool, parameter, field, `issue` or event `kind` is never renamed or removed in
  a 0.x patch release; new ones are added. Clients should ignore fields and values they don't know. Before 1.0 a
  minor release may change the contract, and this file says how.
- **Answers can change in any release.** Better extraction or a newer index changes findings, versions and
  replacements; the benchmarks in `bench/` measure each change.
- **The index has its own schema version** (`schema_version` in the index's `meta` table, shown by
  `deptime index status`): `1`, and `2` once a package is stored with its classes' own members (DT-DATA-16). A
  release reads the schema versions it knows and refuses a newer one, keeping the index it has and saying to upgrade:
  0.1.6 and later read `2`. A nullable column added to the index (`releases.requires`, `releases.project_urls`)
  doesn't change the schema version; older releases ignore it.
- `SNAPSHOT_FORMAT` (in `ingest/snapshot.py`) versions the build cache only; changing it rebuilds every snapshot on
  the next build and doesn't affect clients.

## [Unreleased]

### Added
- Signed build provenance for the index (DT-OSS-09): each published index has a GitHub artifact attestation from
  `build-index.yml`, with its Sigstore bundle published beside it as `index.sigstore.json`.
  `deptime index pull --verify` installs the index only when the attestation names this repository's build workflow
  on `main`, run on a GitHub-hosted runner (it needs the GitHub CLI, `gh`, but no login). `DEPTIME_VERIFY_INDEX=1`
  applies the check to every download, the automatic refresh included, and `deptime index status` shows the run and
  commit that built the installed file.
- `HistoryEvent.affects` (DT-CORE-33): `diff_versions(used_symbols=...)` also reports a used path's parent module or
  class removed, moved or retyped in the range (or added there), listing the used paths it takes with it, so mcp 2.0
  removing `mcp.server.fastmcp` reaches code that uses `mcp.server.fastmcp.FastMCP.tool`.

### Changed
- `diff_versions(used_symbols=...)` reports a used class member's events at the path given, as `symbol_history` does,
  not at the class that defines it, and leaves out that class's changes that don't reach the member (DT-CORE-33).
- The `used_symbols` filter reads each path's events by path: 20 transformers paths over 4.30..5.22 took 5 s, now
  19 ms.

## [0.1.8] - 2026-11-07

### Added
- Findings carry `signature_before` and `signature_after`, `links` (both releases on PyPI and the project's changelog
  from its `Project-URL` metadata) and a `fix` the index proves: the same object at another path, or the one
  replacement at confidence 0.75 or more that the installed release has (DT-CORE-29).
- `confidence_factors` on each finding, `explain_error(min_confidence=...)` with `below_min_confidence`, and project
  suppressions in `deptime.toml` or `[tool.deptime]` in `pyproject.toml`, counted in `suppressed` (DT-CORE-30).
- Standard-library names whose presence changes between Python 3.9 and 3.15 (`collections.Mapping`,
  `asyncio.coroutine`, `ssl.wrap_socket`), from typeshed's stubs (DT-DATA-47), with What's New's replacements for
  removed modules and names (DT-DATA-48). The Python version is also read from the file an `ImportError` names.
- `too_many_positional` errors are explained: a parameter that became keyword-only is the new issue
  `param_kind_changed`, with a `fix` of the new kind `keyword` (DT-CORE-09).
- A user's subclass of a library class is linked to it when the evidence allows: an attribute a class named like it
  lost (`'MACAddressField' object has no attribute 'fail'` in marshmallow 4), or a keyword argument the library's own
  code now passes to the user's override (click 8.2's `get_metavar(ctx)`). pytest's and IPython's frame lines are read
  as frames (DT-CORE-02).
- `check_symbols(code=...)` checks what a snippet uses from the package, read with `ast` (DT-CORE-07); `symbols` is
  now optional.
- `python -m deptime.site build` writes rename-map pages for five migrations from the index (DT-GTM-01).
- Answers about a package DepTime doesn't index point to `coverage` and to the package request form;
  `docs/coverage.md` lists what's indexed (DT-OSS-11).

### Changed
- Searching every package for a bare method name (`__init__() got an unexpected keyword argument`) takes one indexed
  lookup per package: 74 ms → under 1 ms for `__init__`.
- transformers' lazy packages export what their folders list (`define_import_structure`), not their stale
  `TYPE_CHECKING` imports: 4.57's SmolLM3, Apertus and 13 more models read present (DT-DATA-05), in the
  `SNAPSHOT_FORMAT` 18 index published 2026-11-07.

## [0.1.7] - 2026-11-06

### Changed
- Reads private class members and the members of private classes, such as weaviate's `_Collections._get_all`
  (DT-DATA-46), and every package's own-member rows quickly, as the `SNAPSHOT_FORMAT` 17 index stores every package
  with its classes' own members (DT-DATA-49, DT-DATA-32, DT-DATA-28).

## [0.1.6] - 2026-11-05

### Added
- Names a module binds that aren't public API, flagged `internal`, so a removed one reads `symbol_removed` with its
  versions (DT-DATA-22).
- Standard-library modules per Python version, from typeshed's `stdlib/VERSIONS` (DT-DATA-06).
- `coverage` gives each package's `latest_released_at`, and an installed release newer than the index is answered as
  such (DT-DATA-04).

### Changed
- Reads schema 2 indexes, which store each class's own members once (DT-DATA-16); 0.1.5 and older refuse them and
  keep the index they have.
- An import name another distribution also uses (`agents`) is told apart by the installed versions (DT-CORE-03).
- The index is replaced safely on Windows too, also while the old file is open (DT-OSS-08).

## [0.1.5] - 2026-11-05

### Added
- `last_version_without` and `first_version_with` on `*_not_yet_added` findings (DT-CORE-18).

### Changed
- Reads chained exceptions, `ExceptionGroup` sub-exceptions and lazy-loader messages (DT-CORE-25); a removed
  submodule reads `module_removed`, and Python 3.9's `__init__()` messages take the class from the call (DT-CORE-26).
- An error raised inside a library searches the packages that library requires first, and faster (DT-CORE-16).
- A failed first index download is retried with backoff, and answers say why there is no index (DT-OSS-10).

## [0.1.4] - 2026-11-01

### Changed
- Downloads the `.xz` index, 37% smaller than the `.gz` (59.2 MB against 94.7 MB when it shipped; DT-DATA-35).
- Offers the replacement an error message names, once the index has it (DT-CORE-06), and reads numpy 2.0-2.3's
  class-member removal messages (DT-CORE-21).

## [0.1.3] - 2026-10-29

### Changed
- The tool parameters carry descriptions in their schemas (DT-GTM-03).

## [0.1.2] - 2026-10-28

### Added
- Published to the official MCP Registry as `io.github.cschmertz/deptime` (DT-GTM-03).

## [0.1.1] - 2026-10-28

### Changed
- The PyPI page shows the current README (DT-OSS-02).

## [0.1.0] - 2026-10-28

### Added
- The local stdio MCP server (`deptime mcp`, `uvx deptime mcp`) with five read-only tools: `explain_error`,
  `symbol_history`, `check_symbols`, `diff_versions` and `coverage`.
- The prebuilt index of 52 packages, downloaded on first run and refreshed daily; `deptime index pull|status|pack`;
  offline mode (`DEPTIME_OFFLINE=1`).

[Unreleased]: https://github.com/cschmertz/deptime/compare/v0.1.8...HEAD
[0.1.8]: https://github.com/cschmertz/deptime/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/cschmertz/deptime/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/cschmertz/deptime/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/cschmertz/deptime/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/cschmertz/deptime/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/cschmertz/deptime/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/cschmertz/deptime/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/cschmertz/deptime/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/cschmertz/deptime/releases/tag/v0.1.0
