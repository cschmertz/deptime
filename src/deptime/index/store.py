"""SQLite-backed index: rebuilt per package offline, queried read-only at serve time."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from deptime.config import PackageSpec
from deptime.ingest.curated import CURATED_CONFIDENCE, CuratedHint, hints_for
from deptime.ingest.diff import ChangeEvent, diff_snapshots, initial_events
from deptime.ingest.replacements import PublicNames, mine
from deptime.ingest.snapshot import ApiSnapshot, LazySnapshot, SymbolRecord

SCHEMA_VERSION = "1"
REQUIREMENT_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")  # PEP 508's name
MOVED_CONFIDENCE = 0.6
# A removed module-level name whose object is still public under another module-level path (DT-CORE-22): importing
# that path gives the same object. Evidence from the index's own rows, not text, so not above prose's threshold.
STILL_PUBLIC_CONFIDENCE = 0.75
STRONG_HINT = 0.75  # a name that already has a hint this strong gets no DT-CORE-22 hint
# A parameter renamed in case or underscores on the same function (DT-CORE-24): the same name, so stronger
# evidence than a module-level name that reappears elsewhere.
RENAMED_PARAM_CONFIDENCE = 0.8


def _schema_sql() -> str:
    return resources.files("deptime.index").joinpath("schema.sql").read_text(encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def still_public(
    span_rows: Sequence[tuple[str, int, int, tuple]],
    removed: Sequence[tuple[str, int]],
    versions: Sequence[str],
) -> list[tuple[str, str, float, str]]:
    """Hints for removed module-level names from the path their object is still public under (DT-CORE-22):
    numpy 2.0 dropped `numpy.lib.arraysetops.union1d`, and `numpy.union1d` is the same function. Of the
    module-level paths that had the same canonical object in the last release with the name, and still exist in
    the release that removes it, one is offered: the same object still, then not deprecated, then in a module that
    holds its definition (langchain's `chains.combine_documents.refine`, not `chains.summarize`, which imports it),
    then the same name, then the shortest. At `STILL_PUBLIC_CONFIDENCE` when it is still the same object, else at `MOVED_CONFIDENCE`
    (pydantic 2.0's `pydantic.v1.ConstrainedBytes` is a copy of 1.x's). A class's member isn't paired: an
    inherited member's object is shared by every subclass. Rows are `(path, first, last, state)` with `state`
    `(kind, canonical, params, deprecated, deprecated_params)`; `removed` is `(path, ordinal)`."""
    spans: dict[str, list[tuple[int, int, tuple]]] = {}
    by_object: dict[str, list[tuple[str, int, int]]] = {}
    for path, first, last, state in span_rows:
        spans.setdefault(path, []).append((first, last, state))
        if state[1] and state[0] != "getattr":
            by_object.setdefault(state[1], []).append((path, first, last))

    def at(path: str, ordinal: int) -> tuple | None:
        return next((s for f, last, s in spans.get(path, ()) if f <= ordinal <= last), None)

    def named(path: str, ordinal: int) -> tuple | None:
        """The state of a public module-level name, at `ordinal`."""
        parent, _, leaf = path.rpartition(".")
        state = at(path, ordinal)
        owner = at(parent, ordinal) if parent else None
        ok = state and owner and owner[0] == "module" and not leaf.startswith("_")
        return state if ok and state[0] not in ("module", "alias", "getattr") else None

    hints = []
    for path, ordinal in removed:
        before = named(path, ordinal - 1)
        if before is None or not before[1]:
            continue
        leaf = path.rpartition(".")[2]
        best: tuple | None = None
        for other, first, last in by_object.get(before[1], ()):
            if other == path or not first <= ordinal - 1 <= last or (now := named(other, ordinal)) is None:
                continue
            module = other.rpartition(".")[0]
            home = before[1] == other or before[1].startswith(
                module + "."
            )  # a module that holds its definition
            rank = (
                now[1] != before[1],
                bool(now[3]),
                not home,
                other.rpartition(".")[2] != leaf,
                other.count("."),
                len(other),
            )
            if best is None or (rank, other) < best[:2]:
                best = (rank, other, now)
        if best is None:
            continue
        rank, other, _now = best
        version = versions[ordinal]
        if rank[0]:
            evidence = (
                f"{path} was removed in {version}; {other} had the same object until then and is still public"
            )
        else:
            evidence = f"{path} was removed in {version}; the same object is still public as {other}"
        hints.append((path, other, MOVED_CONFIDENCE if rank[0] else STILL_PUBLIC_CONFIDENCE, evidence))
    return hints


def curated_rows(
    span_rows: Sequence[tuple[str, int, int, tuple]], hints: Sequence[CuratedHint]
) -> list[tuple[str, str, str]]:
    """`(path, target, evidence)` for each curated hint (DT-DATA-08) whose name the package had, or whose argument
    the function took, under every path the package published the same object at: a re-export, a subclass's
    inherited method. Rows as in `still_public`."""
    had: dict[str, set[str]] = {}
    objects: dict[str, set[str]] = {}  # path -> its canonical objects
    paths_of: dict[str, set[str]] = {}  # canonical object -> its paths
    for path, _first, _last, state in span_rows:
        took = had.setdefault(path, set())
        took.update(p["name"] for p in json.loads(state[2] or "[]"))
        took.update(json.loads(state[4] or "{}"))
        if state[1] and state[0] not in ("module", "alias", "getattr"):
            objects.setdefault(path, set()).add(state[1])
            paths_of.setdefault(state[1], set()).add(path)
    rows = []
    for hint in hints:
        func, _, param = hint.path.partition("(")
        param = param.removesuffix(")")
        if func not in had or (param and param not in had[func]):
            continue
        same = {func} | {p for obj in objects.get(func, ()) for p in paths_of[obj]}
        for path in sorted(same):
            if not param or param in had[path]:
                rows.append((f"{path}({param})" if param else path, hint.target, hint.evidence))
    return rows


@dataclass(frozen=True)
class RebuildStats:
    package: str
    releases: int
    spans: int
    events: int
    replacements: int


class IndexStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def open(cls, path: Path, *, readonly: bool = True) -> IndexStore:
        if readonly:
            conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, check_same_thread=False)
            return cls(conn)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        store = cls(conn)
        store._init_schema()
        return store

    @classmethod
    def memory(cls) -> IndexStore:
        store = cls(sqlite3.connect(":memory:", check_same_thread=False))
        store._init_schema()
        return store

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(_schema_sql())
            # Columns added since an index file was created (the nightly adds to the published index). Readers
            # never need them, so the schema version doesn't change.
            spans = {r[1] for r in self.conn.execute("PRAGMA table_info(spans)")}
            if "deprecated_params" not in spans:
                self.conn.execute("ALTER TABLE spans ADD COLUMN deprecated_params TEXT")
            releases = {r[1] for r in self.conn.execute("PRAGMA table_info(releases)")}
            if "requires" not in releases:
                self.conn.execute("ALTER TABLE releases ADD COLUMN requires TEXT")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ write path
    def rebuild_package(
        self,
        spec: PackageSpec,
        snapshots: Sequence[ApiSnapshot | LazySnapshot],
        released_at: dict[str, str | None] | None = None,
        source_files: dict[str, str] | None = None,
        fingerprint: str | None = None,
        curated: Sequence[CuratedHint] | None = None,
    ) -> RebuildStats:
        """Replace everything stored for `spec.name` with data derived from `snapshots` (any order).

        Releases are visited once, in version order, holding at most two snapshots, so memory doesn't grow
        with the number of releases (DT-DATA-03). A `LazySnapshot` that loads as None is skipped.
        `fingerprint` describes the inputs (DT-DATA-41); it isn't stored if any snapshot was skipped.
        `curated`: the package's curated hints, by default those `deptime/curated.toml` ships (DT-DATA-08).
        """
        released_at, source_files = released_at or {}, source_files or {}
        ordered = sorted(snapshots, key=lambda s: Version(s.version))
        name = spec.name

        release_rows: list[tuple] = []
        span_rows: list[tuple] = []
        open_spans: dict[str, tuple[int, int, tuple]] = {}  # path -> (first, last, state)
        events: list[tuple[int, int | None, ChangeEvent]] = []
        prev: ApiSnapshot | None = None
        for item in ordered:
            snap = item.load() if isinstance(item, LazySnapshot) else item
            if snap is None:
                fingerprint = None  # the rows no longer match the inputs it describes
                continue
            i = len(release_rows)
            release_rows.append(
                (
                    name,
                    snap.version,
                    i,
                    released_at.get(snap.version),
                    source_files.get(snap.version),
                    len(snap.errors),
                    None if snap.requires is None else json.dumps(snap.requires),
                )
            )
            for path, rec in snap.symbols.items():
                state = (
                    rec.kind,
                    rec.canonical,
                    None if rec.params is None else json.dumps(rec.params),
                    rec.deprecated,
                    None
                    if rec.deprecated_params is None
                    else json.dumps(rec.deprecated_params, sort_keys=True),
                )
                cur = open_spans.get(path)
                if cur and cur[2] == state and cur[1] == i - 1:
                    open_spans[path] = (cur[0], i, state)
                    continue
                if cur:
                    span_rows.append((path, *cur))
                open_spans[path] = (i, i, state)
            for path in [p for p, (_f, last, _s) in open_spans.items() if last < i]:
                span_rows.append((path, *open_spans.pop(path)))
            if prev is None:
                events.extend((0, None, ev) for ev in initial_events(snap))
            else:
                events.extend((i, i - 1, ev) for ev in diff_snapshots(prev, snap))
            prev = snap
        span_rows.extend((path, *cur) for path, cur in open_spans.items())

        repl: dict[tuple[str, str], tuple[float, str, str]] = {}

        def add_repl(path: str, target: str, conf: float, source: str, evidence: str) -> None:
            if not target or target == path:
                return
            key = (path, target)
            if key not in repl or repl[key][0] < conf:
                repl[key] = (conf, source, evidence)

        # A mined target is checked against the package's own rows, as of the release that deprecates (DT-CORE-12).
        names = PublicNames(
            spec.import_names,
            (
                (path, last, None if state[0] == "getattr" else state[1])
                for path, _first, last, state in span_rows
            ),
            ((path, first, last, state[3]) for path, first, last, state in span_rows if state[3]),
            (path for path, _first, _last, state in span_rows if state[0] == "attribute"),
        )
        for ordinal, _prev, ev in events:
            if ev.kind == "deprecated":
                for hint in mine(ev.detail.get("message")):
                    hint = names.check(hint, ev.path, ordinal)
                    add_repl(ev.path, hint.target, hint.confidence, "deprecation_message", hint.evidence)
            elif ev.kind == "param_deprecated":
                for alt in ev.detail.get("alternatives", []):
                    add_repl(
                        f"{ev.path}({ev.detail['param']})",
                        alt["target"],
                        alt["confidence"],
                        "deprecation_message",
                        ev.detail.get("message", ""),
                    )
            elif ev.kind == "moved":
                to = ev.detail["to"]
                add_repl(
                    ev.path,
                    to,
                    MOVED_CONFIDENCE,
                    "moved_heuristic",
                    f"{ev.path} disappeared and {to} appeared in the same release",
                )
            elif ev.kind == "param_removed" and ev.detail.get("renamed_to"):
                param, to = ev.detail["param"], ev.detail["renamed_to"]
                add_repl(
                    f"{ev.path}({param})",
                    to,
                    RENAMED_PARAM_CONFIDENCE,
                    "moved_heuristic",
                    f"{ev.path} lost the parameter {param} and gained {to} in the same release",
                )
        strong = {path for (path, _target), (conf, _s, _e) in repl.items() if conf >= STRONG_HINT}
        versions = [row[1] for row in release_rows]
        removed = [(ev.path, ordinal) for ordinal, _prev, ev in events if ev.kind == "removed"]
        for path, target, conf, evidence in still_public(span_rows, removed, versions):
            if path not in strong:
                add_repl(path, target, conf, "moved_heuristic", evidence)
        hints = hints_for(name) if curated is None else curated
        for path, target, evidence in curated_rows(span_rows, hints):
            # The library's own text already says it: keep that, so what DepTime finds by itself stays visible.
            if repl.get((path, target), (0.0,))[0] < STRONG_HINT:
                add_repl(path, target, CURATED_CONFIDENCE, "curated", evidence)

        with self.conn:
            for table, col in (
                ("assembly", "package"),
                ("replacements", "package"),
                ("events", "package"),
                ("spans", "package"),
                ("releases", "package"),
                ("packages", "name"),
            ):
                self.conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (name,))
            if fingerprint:
                self.conn.execute(
                    "INSERT INTO assembly(package, fingerprint) VALUES (?, ?)", (name, fingerprint)
                )
            self.conn.execute(
                "INSERT INTO packages(name, import_names, tier, indexed_at) VALUES (?, ?, ?, ?)",
                (name, json.dumps(list(spec.import_names)), spec.tier, _now()),
            )
            self.conn.executemany(
                "INSERT INTO releases(package, version, ordinal, released_at, source_file, error_count, requires)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                release_rows,
            )
            self.conn.executemany(
                "INSERT INTO spans(package, path, tail, first_ordinal, last_ordinal, kind, canonical, params, deprecated, deprecated_params) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (name, path, path.rsplit(".", 1)[-1], first, last, *state)
                    for path, first, last, state in span_rows
                ],
            )
            self.conn.executemany(
                "INSERT INTO events(package, path, ordinal, prev_ordinal, kind, detail) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (name, ev.path, o, p, ev.kind, json.dumps(ev.detail, sort_keys=True))
                    for o, p, ev in events
                ],
            )
            self.conn.executemany(
                "INSERT INTO replacements(package, path, target, confidence, source, evidence) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (name, path, target, conf, source, evidence)
                    for (path, target), (conf, source, evidence) in repl.items()
                ],
            )
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('built_at', ?)", (_now(),))
        return RebuildStats(name, len(release_rows), len(span_rows), len(events), len(repl))

    def stamp_built_at(self) -> None:
        """Rows kept as they were (DT-DATA-41) are still current as of this run."""
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('built_at', ?)", (_now(),))

    def fingerprint(self, name: str) -> str | None:
        """The inputs `name`'s rows were assembled from (DT-DATA-41), if they came from snapshots alone."""
        try:
            row = self.conn.execute("SELECT fingerprint FROM assembly WHERE package = ?", (name,)).fetchone()
        except sqlite3.OperationalError:  # an index from before DT-DATA-41, opened read-only
            return None
        return row["fingerprint"] if row else None

    # ------------------------------------------------------------------ read path
    def built_at(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        return row["value"] if row else None

    def package_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM packages ORDER BY name").fetchall()

    def has_package(self, name: str) -> bool:
        """True only when the package has at least one indexed release (rows without releases are unusable)."""
        return (
            self.conn.execute("SELECT 1 FROM releases WHERE package = ? LIMIT 1", (name,)).fetchone()
            is not None
        )

    def releases(self, name: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM releases WHERE package = ? ORDER BY ordinal", (name,)
        ).fetchall()

    def snapshot_at(self, spec: PackageSpec, version: str) -> ApiSnapshot | None:
        """One indexed release, rebuilt from its spans: lets ingestion keep a release whose cached snapshot is
        gone. Symbols round-trip exactly; analysis error messages survive only as a count."""
        rel = self.conn.execute(
            "SELECT * FROM releases WHERE package = ? AND version = ?", (spec.name, version)
        ).fetchone()
        if rel is None:
            return None
        rows = self.conn.execute(
            "SELECT path, kind, canonical, params, deprecated, deprecated_params FROM spans "
            "WHERE package = ? AND first_ordinal <= ? AND last_ordinal >= ?",
            (spec.name, rel["ordinal"], rel["ordinal"]),
        ).fetchall()
        symbols = {
            r["path"]: SymbolRecord(
                r["path"],
                r["kind"],
                r["canonical"],
                None if r["params"] is None else json.loads(r["params"]),
                r["deprecated"],
                None if r["deprecated_params"] is None else json.loads(r["deprecated_params"]),
            )
            for r in rows
        }
        errors = ["analysis problem recorded by an earlier run"] * rel["error_count"]
        requires = rel["requires"] if "requires" in rel.keys() else None  # noqa: SIM118
        return ApiSnapshot(
            spec.name,
            version,
            list(spec.import_names),
            symbols,
            errors,
            requires=None if requires is None else json.loads(requires),
        )

    def declared_dependencies(self, name: str, ordinal: int | None = None) -> set[str]:
        """Canonical names of the distributions `name` requires, extras included (DT-DATA-14): at the release
        `ordinal`, or in any indexed release when that's None or the release's requirements are unknown. Empty
        when no release's are known (an index from before they were recorded)."""
        if "requires" not in {r[1] for r in self.conn.execute("PRAGMA table_info(releases)")}:
            return set()
        where, args = "package = ? AND requires IS NOT NULL", [name]
        if (
            ordinal is not None
            and self.conn.execute(
                "SELECT 1 FROM releases WHERE package = ? AND ordinal = ? AND requires IS NOT NULL",
                (name, ordinal),
            ).fetchone()
        ):
            where, args = where + " AND ordinal = ?", [name, ordinal]
        rows = self.conn.execute(
            f"SELECT DISTINCT r.value FROM releases, json_each(releases.requires) AS r WHERE {where}", args
        ).fetchall()
        return {canonicalize_name(m.group(0)) for (req,) in rows if (m := REQUIREMENT_NAME_RE.match(req))}

    def version_at(self, name: str, ordinal: int) -> str | None:
        row = self.conn.execute(
            "SELECT version FROM releases WHERE package = ? AND ordinal = ?", (name, ordinal)
        ).fetchone()
        return row["version"] if row else None

    def resolve_version(self, name: str, version: str) -> tuple[sqlite3.Row | None, bool]:
        """Nearest indexed release <= `version`, and whether it is an exact match."""
        try:
            wanted = Version(version)
        except InvalidVersion:
            return None, False
        best = None
        for row in self.releases(name):
            if Version(row["version"]) <= wanted:
                best = row
        return best, bool(best is not None and Version(best["version"]) == wanted)

    def release_on_or_before(self, name: str, day: str) -> sqlite3.Row | None:
        rows = [r for r in self.releases(name) if r["released_at"] and r["released_at"][:10] <= day]
        return rows[-1] if rows else None

    def spans(self, name: str, path: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM spans WHERE package = ? AND path = ? ORDER BY first_ordinal", (name, path)
        ).fetchall()

    def span_at(self, name: str, path: str, ordinal: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM spans WHERE package = ? AND path = ? AND first_ordinal <= ? AND last_ordinal >= ?",
            (name, path, ordinal, ordinal),
        ).fetchone()

    def events(self, name: str, path: str) -> list[sqlite3.Row]:
        # `+ordinal`: ordering by the bare column made SQLite read the package's events through events_by_ordinal
        # and filter on path, a scan of every event the package has (4.5 ms a call, hundreds of calls when the
        # error's owner is searched in every package).
        return self.conn.execute(
            "SELECT * FROM events WHERE package = ? AND path = ? ORDER BY +ordinal, kind", (name, path)
        ).fetchall()

    def events_between(
        self,
        name: str,
        after_ordinal: int,
        upto_ordinal: int,
        limit: int,
        *,
        severity: Mapping[str, int],
        include_added: bool = True,
        paths: list[str] | None = None,
    ) -> tuple[list[sqlite3.Row], int]:
        """Events in (after, upto], most severe first, and the total count before `limit`.

        Filtering, ordering and counting all happen in SQL, so a large release (thousands of `added` events)
        can never push breaking changes past the limit unseen. `paths` keeps events on those paths or below.
        """
        where = "package = ? AND ordinal > ? AND ordinal <= ?"
        args: list[object] = [name, after_ordinal, upto_ordinal]
        if not include_added:
            where += " AND kind != 'added'"
        if paths:
            where += (
                " AND EXISTS (SELECT 1 FROM json_each(?) AS u"
                " WHERE events.path = u.value OR substr(events.path, 1, length(u.value) + 1) = u.value || '.')"
            )
            args.append(json.dumps(paths))
        total = self.conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}", args).fetchone()[0]
        rank = "CASE kind " + " ".join("WHEN ? THEN ?" for _ in severity) + " ELSE 99 END"
        rank_args = [x for item in severity.items() for x in item]
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY {rank}, path, ordinal, kind LIMIT ?",
            [*args, *rank_args, limit],
        ).fetchall()
        return rows, total

    def replacements(self, name: str, path: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM replacements WHERE package = ? AND path = ? ORDER BY confidence DESC, target",
            (name, path),
        ).fetchall()

    def param_replacements(self, name: str, func_path: str) -> list[sqlite3.Row]:
        prefix = func_path + "("
        return self.conn.execute(
            "SELECT * FROM replacements WHERE package = ? AND substr(path, 1, ?) = ? ORDER BY path, confidence DESC",
            (name, len(prefix), prefix),
        ).fetchall()

    @staticmethod
    def _suffix_query(suffix: str, packages: Sequence[str] | None) -> tuple[str, list]:
        """A query for `package, path` of the public paths equal to or ending with `.suffix`, in any version.

        Rows are found by tail, and each costs a read from the table. A member's own tail can match tens of
        thousands of rows (61,023 for `__init__`), so a dotted suffix is matched on its parent's tail (38 for
        `Client`) and the member is then looked up by its path (DT-CORE-16): `Client.__init__` took 1.3 s cold.
        The suffix as a whole path is looked up by path too, since a top-level name such as `google.genai`'s
        `google` has no row of its own. Callers apply DISTINCT outside: inside, it made SQLite read every span of
        the package in path order rather than use the tail index.
        """
        in_packages = f" AND p.package IN ({','.join('?' * len(packages))})" if packages is not None else ""
        parent, _, leaf = suffix.rpartition(".")
        if not parent:
            sql = (
                f"SELECT p.package AS package, p.path AS path FROM spans p WHERE p.tail = ?{in_packages}"
                " AND (p.path = ? OR substr(p.path, -?) = ?)"
            )
            return sql, [suffix, *(packages or ()), suffix, len(suffix) + 1, "." + suffix]
        sql = (
            "SELECT p.package AS package, p.path || '.' || ? AS path FROM spans p"
            f" WHERE p.tail = ?{in_packages} AND (p.path = ? OR substr(p.path, -?) = ?)"
            " AND EXISTS (SELECT 1 FROM spans s WHERE s.package = p.package AND s.path = p.path || '.' || ?)"
            " UNION SELECT p.package, p.path FROM spans p"
            f" WHERE p.package IN (SELECT name FROM packages){in_packages} AND p.path = ?"
        )
        ptail = parent.rsplit(".", 1)[-1]
        whole = [*(packages or ()), suffix]
        return sql, [leaf, ptail, *(packages or ()), parent, len(parent) + 1, "." + parent, leaf, *whole]

    def packages_with_suffix(self, suffix: str) -> list[str]:
        """Indexed packages with a public path equal to or ending with `.suffix` in any indexed version."""
        sql, params = self._suffix_query(suffix, None)
        rows = self.conn.execute(f"SELECT DISTINCT package FROM ({sql}) ORDER BY package", params).fetchall()
        return [r["package"] for r in rows]

    def find_by_suffix(self, packages: list[str], suffix: str, limit: int = 20) -> list[tuple[str, str]]:
        """(package, path) pairs whose public path equals or ends with `.suffix`, across all indexed versions."""
        if not packages:
            return []
        sql, params = self._suffix_query(suffix, packages)
        rows = self.conn.execute(
            f"SELECT DISTINCT package, path FROM ({sql}) ORDER BY length(path), path LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [(r["package"], r["path"]) for r in rows]
