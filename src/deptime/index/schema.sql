-- DepTime index schema (v1). Built offline by `deptime ingest`, served read-only.
-- Rebuild a package atomically with IndexStore.rebuild_package(); bump meta.schema_version on incompatible changes.

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
  name         TEXT PRIMARY KEY,           -- canonical PEP 503 distribution name
  import_names TEXT NOT NULL,              -- JSON array, e.g. ["google.genai"]
  tier         TEXT NOT NULL,
  indexed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
  package     TEXT NOT NULL,
  version     TEXT NOT NULL,
  ordinal     INTEGER NOT NULL,            -- 0..n-1 in version order among *indexed* releases
  released_at TEXT,
  source_file TEXT,                        -- artifact the snapshot was built from
  error_count INTEGER NOT NULL DEFAULT 0,  -- griffe load/resolution problems recorded in the snapshot
  PRIMARY KEY (package, version),
  UNIQUE (package, ordinal)
);

-- Interval encoding: one row per maximal run of consecutive indexed releases in which a public path has
-- an identical state. Keeps the index ~O(changes) instead of O(symbols x versions).
CREATE TABLE IF NOT EXISTS spans (
  package       TEXT NOT NULL,
  path          TEXT NOT NULL,             -- public import path, e.g. httpx.Client.__init__
  tail          TEXT NOT NULL,             -- last path component, for suffix lookups
  first_ordinal INTEGER NOT NULL,
  last_ordinal  INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  canonical     TEXT,                      -- definition path, e.g. httpx._client.Client.__init__
  params        TEXT,                      -- JSON [{name, kind, default, annotation}] for callables
  deprecated    TEXT,                      -- deprecation text, if detected
  PRIMARY KEY (package, path, first_ordinal)
);
CREATE INDEX IF NOT EXISTS spans_by_tail ON spans(tail, package);

CREATE TABLE IF NOT EXISTS events (
  package      TEXT NOT NULL,
  path         TEXT NOT NULL,
  ordinal      INTEGER NOT NULL,           -- release where the change is first observed
  prev_ordinal INTEGER,                    -- NULL for state already present in the first indexed release
  kind         TEXT NOT NULL,
  detail       TEXT NOT NULL               -- JSON
);
CREATE INDEX IF NOT EXISTS events_by_path ON events(package, path);
CREATE INDEX IF NOT EXISTS events_by_ordinal ON events(package, ordinal);

CREATE TABLE IF NOT EXISTS replacements (
  package    TEXT NOT NULL,
  path       TEXT NOT NULL,                -- symbol path, or 'func.path(param)' for a parameter
  target     TEXT NOT NULL,
  confidence REAL NOT NULL,
  source     TEXT NOT NULL,                -- deprecation_message | moved_heuristic | curated
  evidence   TEXT,
  PRIMARY KEY (package, path, target)
);
