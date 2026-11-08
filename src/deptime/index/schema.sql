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
  requires    TEXT,                        -- JSON array of the artifact's Requires-Dist; NULL if unknown (DT-DATA-14)
  project_urls TEXT,                       -- JSON object of its Project-URL entries, label to URL (DT-CORE-29)
  namespaces  TEXT,                        -- JSON array of its namespace packages (DT-CORE-32)
  module_aliases TEXT,                     -- JSON object, sys.modules alias -> module (DT-DATA-52)
  star_sources TEXT,                       -- JSON object, module -> the unread modules it star-imports (DT-DATA-56)
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
  deprecated    TEXT,                      -- the whole symbol's deprecation text, if detected
  deprecated_params TEXT,                  -- JSON {argument: text} for arguments a warning deprecates (DT-DATA-42)
  PRIMARY KEY (package, path, first_ordinal)
);
CREATE INDEX IF NOT EXISTS spans_by_tail ON spans(tail, package);

-- Names modules bind that aren't public API (DT-DATA-22), encoded like spans: private names, a module's imports
-- from outside the standard library, and everything in private modules. Integration libraries import them, and
-- break when they go. Clients from before DT-DATA-22 don't read this table.
CREATE TABLE IF NOT EXISTS internal_spans (
  package       TEXT NOT NULL,
  path          TEXT NOT NULL,             -- where the module binds it, e.g. pymongo.cursor._QUERY_OPTIONS
  first_ordinal INTEGER NOT NULL,
  last_ordinal  INTEGER NOT NULL,
  kind          TEXT NOT NULL,             -- as in spans; alias for an import griffe can't follow
  canonical     TEXT,                      -- definition path; for an import, what it imports
  deprecated    TEXT,                      -- the warning a module __getattr__ serves a private name with
  PRIMARY KEY (package, path, first_ordinal)
) WITHOUT ROWID;

-- A package stored with its classes' own members (coverage.toml `own_members`, DT-DATA-16): its spans have no class
-- members. A public class's members are looked up when queried: its own, then those of each class in its MRO, as
-- Python finds them. An index with such a package has schema version 2: clients from before can't read it.
CREATE TABLE IF NOT EXISTS classes (
  package       TEXT NOT NULL,
  cls           TEXT NOT NULL,             -- canonical path, e.g. transformers.modeling_utils.PreTrainedModel
  first_ordinal INTEGER NOT NULL,
  last_ordinal  INTEGER NOT NULL,
  mro           TEXT,                      -- JSON array of canonical paths; NULL for a class recorded only as a base
  label         TEXT NOT NULL,             -- its shortest public path, else cls: where its members' events are
  external      TEXT,                      -- JSON array of its bases from outside the package (DT-CORE-32)
  PRIMARY KEY (package, cls, first_ordinal)
) WITHOUT ROWID;

-- Keyed by name before class (DT-DATA-49), so a lookup by name alone, the search for an error's attribute in every
-- package, reads only the rows with that name; a lookup by name and class reads the same rows either way.
CREATE TABLE IF NOT EXISTS members (
  package       TEXT NOT NULL,
  cls           TEXT NOT NULL,             -- canonical path of the class that defines it
  name          TEXT NOT NULL,
  first_ordinal INTEGER NOT NULL,
  last_ordinal  INTEGER NOT NULL,
  kind          TEXT NOT NULL,             -- as in spans, or 'hidden': ends the lookup, but isn't recorded
  canonical     TEXT,
  params        TEXT,
  deprecated    TEXT,
  deprecated_params TEXT,
  PRIMARY KEY (package, name, cls, first_ordinal)
) WITHOUT ROWID;

-- A class's own members that aren't public API (DT-DATA-46), for a package stored with own members, encoded like
-- internal_spans: its private names, and every member of a class that is neither public nor a public class's base
-- (weaviate's `_Collections._get_all`, inherited from `_CollectionsBase`). Looked up through `classes` as members
-- are. Clients from before DT-DATA-46 don't read this table.
CREATE TABLE IF NOT EXISTS internal_members (
  package       TEXT NOT NULL,
  cls           TEXT NOT NULL,             -- canonical path of the class that defines it
  name          TEXT NOT NULL,
  first_ordinal INTEGER NOT NULL,
  last_ordinal  INTEGER NOT NULL,
  kind          TEXT NOT NULL,             -- as in internal_spans, or 'hidden': ends the lookup, but isn't recorded
  canonical     TEXT,
  PRIMARY KEY (package, name, cls, first_ordinal)
) WITHOUT ROWID;

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

-- What each package's rows were assembled from (DT-DATA-41): a hash of its snapshot files, release dates, spec
-- and the assembly code. The nightly keeps a package's rows when tonight's inputs hash the same. Clients don't
-- read it; rows rebuilt from anything but snapshots alone have none, so they are always rebuilt.
CREATE TABLE IF NOT EXISTS assembly (
  package     TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL
);
