"""SQLite-backed index: rebuilt per package offline, queried read-only at serve time."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from packaging.version import InvalidVersion, Version

from deptime.config import PackageSpec
from deptime.ingest.diff import ChangeEvent, diff_snapshots, initial_events
from deptime.ingest.replacements import mine
from deptime.ingest.snapshot import ApiSnapshot, LazySnapshot, SymbolRecord

SCHEMA_VERSION = "1"


def _schema_sql() -> str:
    return resources.files("deptime.index").joinpath("schema.sql").read_text(encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
    ) -> RebuildStats:
        """Replace everything stored for `spec.name` with data derived from `snapshots` (any order).

        Releases are visited once, in version order, holding at most two snapshots, so memory doesn't grow
        with the number of releases (DT-DATA-03). A `LazySnapshot` that loads as None is skipped.
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
                )
            )
            for path, rec in snap.symbols.items():
                state = (
                    rec.kind,
                    rec.canonical,
                    None if rec.params is None else json.dumps(rec.params),
                    rec.deprecated,
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

        for _ordinal, _prev, ev in events:
            if ev.kind == "deprecated":
                for hint in mine(ev.detail.get("message")):
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
                    0.6,
                    "moved_heuristic",
                    f"{ev.path} disappeared and {to} appeared in the same release",
                )

        with self.conn:
            for table, col in (
                ("replacements", "package"),
                ("events", "package"),
                ("spans", "package"),
                ("releases", "package"),
                ("packages", "name"),
            ):
                self.conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (name,))
            self.conn.execute(
                "INSERT INTO packages(name, import_names, tier, indexed_at) VALUES (?, ?, ?, ?)",
                (name, json.dumps(list(spec.import_names)), spec.tier, _now()),
            )
            self.conn.executemany(
                "INSERT INTO releases(package, version, ordinal, released_at, source_file, error_count) VALUES (?, ?, ?, ?, ?, ?)",
                release_rows,
            )
            self.conn.executemany(
                "INSERT INTO spans(package, path, tail, first_ordinal, last_ordinal, kind, canonical, params, deprecated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "SELECT ordinal, error_count FROM releases WHERE package = ? AND version = ?",
            (spec.name, version),
        ).fetchone()
        if rel is None:
            return None
        rows = self.conn.execute(
            "SELECT path, kind, canonical, params, deprecated FROM spans "
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
            )
            for r in rows
        }
        errors = ["analysis problem recorded by an earlier run"] * rel["error_count"]
        return ApiSnapshot(spec.name, version, list(spec.import_names), symbols, errors)

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
        return self.conn.execute(
            "SELECT * FROM events WHERE package = ? AND path = ? ORDER BY ordinal, kind", (name, path)
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

    def packages_with_suffix(self, suffix: str) -> list[str]:
        """Indexed packages with a public path equal to or ending with `.suffix` in any indexed version."""
        dotted = "." + suffix
        rows = self.conn.execute(
            "SELECT DISTINCT package FROM spans WHERE tail = ? AND (path = ? OR substr(path, -?) = ?) ORDER BY package",
            (suffix.rsplit(".", 1)[-1], suffix, len(dotted), dotted),
        ).fetchall()
        return [r["package"] for r in rows]

    def find_by_suffix(self, packages: list[str], suffix: str, limit: int = 20) -> list[tuple[str, str]]:
        """(package, path) pairs whose public path equals or ends with `.suffix`, across all indexed versions."""
        if not packages:
            return []
        tail = suffix.rsplit(".", 1)[-1]
        dotted = "." + suffix
        marks = ",".join("?" * len(packages))
        rows = self.conn.execute(
            f"SELECT DISTINCT package, path FROM spans WHERE tail = ? AND package IN ({marks}) "
            "AND (path = ? OR substr(path, -?) = ?) ORDER BY length(path), path LIMIT ?",
            (tail, *packages, suffix, len(dotted), dotted, limit),
        ).fetchall()
        return [(r["package"], r["path"]) for r in rows]
