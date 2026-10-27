# ADR 0003 — A read-only SQLite index artifact with interval (span) encoding

**Status:** Accepted, 2026-10-17

## Context

Queries are point lookups: does path P exist at release R, what were P's events, what replaces P? Data changes only when new releases are ingested. A per-version symbol table grows as symbols × versions; transformers alone would be millions of rows.

## Decision

- Build a **SQLite file offline** and serve it **read-only**. Each package is rebuilt in one transaction (`IndexStore.rebuild_package`).
- Store **spans**: maximal runs of consecutive indexed releases in which a public path has an identical state. Store diff **events** and mined **replacements** alongside.
- Keep per-release **snapshots** (`.json.gz`) as the immutable intermediate, so the index can be rebuilt without re-downloading or re-analysing.

## Consequences

- Storage is proportional to the number of changes. Queries are indexed point reads.
- Deployment is "copy one file", and hot swap is an atomic rename plus reopen.
- Usage and billing data live in a **separate** read-write DB, so the index stays immutable.
- Inserting a release in the middle of a package's history means rebuilding that package from snapshots. That's cheap, and it's the expected path.
