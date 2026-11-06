"""SQLite-backed index: rebuilt per package offline, queried read-only at serve time."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from deptime.config import PackageSpec
from deptime.ingest.curated import CURATED_CONFIDENCE, CuratedHint, hints_for
from deptime.ingest.diff import (
    ChangeEvent,
    diff_snapshots,
    initial_events,
    initial_path_events,
    labels,
    path_events,
)
from deptime.ingest.replacements import PublicNames, mine
from deptime.ingest.snapshot import HIDDEN, ApiSnapshot, ClassRecord, LazySnapshot, SymbolRecord

SCHEMA_VERSION = "1"
# Written when a package is stored with its classes' own members (DT-DATA-16): a client that doesn't resolve
# inherited members would read them as missing, so it must not load the index.
OWN_MEMBERS_SCHEMA_VERSION = "2"
READABLE_SCHEMA_VERSIONS = (SCHEMA_VERSION, OWN_MEMBERS_SCHEMA_VERSION)
MEMBER_CACHE_SIZE = 4096  # resolved member paths kept per open index
REQUIREMENT_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")  # PEP 508's name
MOVED_CONFIDENCE = 0.6
# A removed module-level name whose object is still public under another module-level path (DT-CORE-22): importing
# that path gives the same object. Evidence from the index's own rows, not text, so not above prose's threshold.
STILL_PUBLIC_CONFIDENCE = 0.75
STRONG_HINT = 0.75  # a name that already has a hint this strong gets no DT-CORE-22 hint
# A parameter renamed in case or underscores on the same function (DT-CORE-24): the same name, so stronger
# evidence than a module-level name that reappears elsewhere.
RENAMED_PARAM_CONFIDENCE = 0.8


_STATE_COLUMNS = ("kind", "canonical", "params", "deprecated", "deprecated_params")


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


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def internal_moves(
    span_rows: Sequence[tuple[str, int, int, tuple]],
    internal_rows: Sequence[tuple[str, int, int, tuple]],
    versions: Sequence[str],
    import_names: Sequence[str],
) -> list[tuple[str, str, float, str]]:
    """Hints for internal names that go away (DT-DATA-22), from the index's own rows.

    An import points to what it imported: at `STILL_PUBLIC_CONFIDENCE` when that is in the package and still there
    (instructor 1.15.3 dropped `core.patch.handle_response_model`, which only imported
    `processing.response.handle_response_model`), at `MOVED_CONFIDENCE` when it is in another distribution
    (sqlmodel's `AsyncConnection` is sqlalchemy's). Otherwise, when exactly one other path defines a name of
    the same leaf in the release that drops it, that path, at `MOVED_CONFIDENCE` (pymongo 4.9 moved
    `cursor._QUERY_OPTIONS` to `cursor_shared`). Rows are `(path, first, last, state)`; public states are as in
    `still_public`, internal ones `(kind, canonical, deprecated)`."""
    alive: dict[str, list[tuple[int, int]]] = {}
    defines: dict[str, list[tuple[str, int, int, bool]]] = {}  # leaf -> paths that define it, is it a module
    for path, first, last, state in [*span_rows, *internal_rows]:
        alive.setdefault(path, []).append((first, last))
        if state[1] == path:
            defines.setdefault(path.rpartition(".")[2], []).append((path, first, last, state[0] == "module"))

    def at(path: str, ordinal: int) -> bool:
        return any(f <= ordinal <= last for f, last in alive.get(path, ()))

    def inside(path: str) -> bool:
        return any(path == n or path.startswith(n + ".") for n in import_names)

    hints = []
    for path, _first, last, (kind, canonical, _deprecated) in internal_rows:
        ordinal = last + 1
        if ordinal >= len(versions) or at(path, ordinal):
            continue
        version = versions[ordinal]
        if canonical and canonical != path and kind != "getattr":
            if not inside(canonical):
                hints.append((path, canonical, MOVED_CONFIDENCE, f"{path} was an import of {canonical}"))
                continue
            if at(canonical, ordinal):
                evidence = f"{path} was an import of {canonical}, which {version} still has"
                hints.append((path, canonical, STILL_PUBLIC_CONFIDENCE, evidence))
                continue
        leaf = path.rpartition(".")[2]
        others = {
            other
            for other, first, end, is_module in defines.get(leaf, ())
            if other != path and first <= ordinal <= end and is_module == (kind == "module")
        }
        if len(others) == 1:
            other = others.pop()
            hints.append(
                (path, other, MOVED_CONFIDENCE, f"{path} is gone in {version}, where {other} defines {leaf}")
            )
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


def _state(rec: SymbolRecord) -> tuple:
    """A record as a span's columns: `(kind, canonical, params, deprecated, deprecated_params)`."""
    return (
        rec.kind,
        rec.canonical,
        None if rec.params is None else json.dumps(rec.params),
        rec.deprecated,
        None if rec.deprecated_params is None else json.dumps(rec.deprecated_params, sort_keys=True),
    )


def _record(path: str, row: Mapping) -> SymbolRecord:
    """A span's columns as a record of `path`."""
    return SymbolRecord(
        path,
        row["kind"],
        row["canonical"],
        None if row["params"] is None else json.loads(row["params"]),
        row["deprecated"],
        None if row["deprecated_params"] is None else json.loads(row["deprecated_params"]),
    )


def own_member_rows(
    span_rows: Sequence[tuple[str, int, int, tuple]],
    class_rows: Sequence[tuple[str, int, int, tuple]],
    member_rows: Sequence[tuple[tuple[str, str], int, int, tuple]],
) -> list[tuple[str, int, int, tuple]]:
    """For a package stored with own members (DT-DATA-16): each class's own members as span rows under each path
    of the class, its public paths and its label, for what checks names against the package's rows (mining a
    hint's target, applying a curated hint). Inherited members aren't repeated under subclasses."""
    homes: dict[str, list[tuple[str, int, int]]] = defaultdict(list)  # canonical class -> (path, first, last)
    for path, first, last, state in span_rows:
        if state[0] == "class" and state[1]:
            homes[state[1]].append((path, first, last))
    for cls, first, last, (_mro, label) in class_rows:
        if not any(p == label and f <= first and last <= end for p, f, end in homes[cls]):
            homes[cls].append((label, first, last))
    out = []
    for (cls, name), first, last, state in member_rows:
        if state[0] == HIDDEN:
            continue
        for path, f, end in homes.get(cls, ()):
            if max(first, f) <= min(last, end):
                out.append((f"{path}.{name}", max(first, f), min(last, end), state))
    return out


def member_lookup(
    span_rows: Sequence[tuple[str, int, int, tuple]],
    class_rows: Sequence[tuple[str, int, int, tuple]],
    member_rows: Sequence[tuple[tuple[str, str], int, int, tuple]],
) -> Callable[[str], tuple[int, str, str] | None]:
    """For a package stored with own members (DT-DATA-16): a class member's last ordinal and object, as a build that
    stores every member under every class has them, from the class at each release and its MRO, and the class that
    defines it then. For checking a hint's target: `predict` on langchain's `BaseLanguageModel` says to use
    `invoke`, which it inherits."""
    # A class's paths, public ones and its label -> (first, last, canonical class)
    classes: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for path, first, last, state in span_rows:
        if state[0] == "class" and state[1]:
            classes[path].append((first, last, state[1]))
    mros: dict[str, list[tuple[int, int, list[str]]]] = defaultdict(list)
    for cls, first, last, (mro, label) in class_rows:
        mros[cls].append((first, last, json.loads(mro) if mro else []))
        classes[label].append((first, last, cls))
    members: dict[tuple[str, str], list[tuple[int, int, tuple]]] = defaultdict(list)
    for key, first, last, state in member_rows:
        members[key].append((first, last, state))

    def resolve(cls: str, name: str, o: int) -> tuple[tuple, str] | None:
        mro = next((m for f, end, m in mros.get(cls, ()) if f <= o <= end), [])
        for owner in [cls, *mro]:
            state = next((st for f, end, st in members.get((owner, name), ()) if f <= o <= end), None)
            if state is not None:
                return None if state[0] == HIDDEN else (state, owner)
        return None

    def lookup(path: str) -> tuple[int, str, str] | None:
        parent, _, name = path.rpartition(".")
        best: tuple[int, str, str] | None = None
        for first, last, cls in classes.get(parent, ()):
            for o in range(last, max(first, best[0] + 1 if best else first) - 1, -1):
                if (found := resolve(cls, name, o)) is not None:
                    best = (o, found[0][1] or path, found[1])
                    break
        return best

    return lookup


def member_heirs(
    span_rows: Sequence[tuple[str, int, int, tuple]],
    class_rows: Sequence[tuple[str, int, int, tuple]],
    lookup: Callable[[str], tuple[int, str, str] | None],
) -> Callable[[str], list[str]]:
    """For a package stored with own members (DT-DATA-49): given a member under a path of the class that defines
    it, the same member under the public paths of the classes that inherit it, as a build that stores every member
    under every class lists them. For naming a hint's target by its shortest public path."""
    owner_of: dict[str, set[str]] = defaultdict(set)  # a class's path (public or label) -> canonical classes
    paths_of: dict[str, set[str]] = defaultdict(set)  # canonical class -> its public paths
    for path, _first, _last, state in span_rows:
        if state[0] == "class" and state[1]:
            owner_of[path].add(state[1])
            paths_of[state[1]].add(path)
    heirs_of: dict[str, set[str]] = defaultdict(set)  # canonical class -> classes with it in their MRO
    for cls, _first, _last, (mro, label) in class_rows:
        owner_of[label].add(cls)
        for base in json.loads(mro) if mro else ():
            heirs_of[base].add(cls)

    @cache
    def heirs(path: str) -> list[str]:
        parent, _, name = path.rpartition(".")
        out = []
        for defining in owner_of.get(parent, ()):
            for heir in sorted(heirs_of.get(defining, ())):
                for home in sorted(paths_of.get(heir, ())):
                    found = lookup(f"{home}.{name}")
                    if found is not None and found[2] == defining:
                        out.append(f"{home}.{name}")
        return out

    return heirs


def _at_label(
    hint: CuratedHint,
    lookup: Callable[[str], tuple[int, str, str] | None],
    label_of: Mapping[str, str],
    rows: set[str],
) -> CuratedHint:
    """A curated hint on an inherited class member, moved to the member of its defining class's label (DT-DATA-16).
    One on a path the package's rows have stays: chromadb's `EmbeddingFunction.__call__(texts)` hint was moved to
    the class's later label, which never took `texts`, and was lost (DT-DATA-49)."""
    func, paren, param = hint.path.partition("(")
    if func in rows:
        return hint
    found = lookup(func)
    if found is None or found[2] not in label_of:
        return hint
    return replace(hint, path=f"{label_of[found[2]]}.{func.rpartition('.')[2]}{paren}{param}")


def _extend(
    open_spans: dict[str, tuple[int, int, tuple]], rows: list[tuple], path: str, state: tuple, i: int
) -> None:
    """Extend `path`'s open span into release `i` if its state is unchanged, else close it and open another."""
    cur = open_spans.get(path)
    if cur and cur[2] == state and cur[1] == i - 1:
        open_spans[path] = (cur[0], i, state)
        return
    if cur:
        rows.append((path, *cur))
    open_spans[path] = (i, i, state)


def _close(open_spans: dict[str, tuple[int, int, tuple]], rows: list[tuple], i: int) -> None:
    """Close the spans of paths release `i` doesn't have."""
    for path in [p for p, (_f, last, _s) in open_spans.items() if last < i]:
        rows.append((path, *open_spans.pop(path)))


@dataclass(frozen=True)
class RebuildStats:
    package: str
    releases: int
    spans: int
    events: int
    replacements: int
    internal: int = 0
    members: int = 0  # own members' spans, for a package stored with them (DT-DATA-16)


class IndexStore:
    def __init__(self, conn: sqlite3.Connection, path: Path | None = None):
        self.conn = conn
        self.path = path  # the file, for a store opened from one
        self.conn.row_factory = sqlite3.Row
        self._internal: bool | None = None  # whether the index has internal_spans (DT-DATA-22)
        self._internal_members: bool | None = None  # whether it has internal_members (DT-DATA-46)
        self._own: frozenset[str] | None = None  # packages stored with own members (DT-DATA-16)
        self._members: OrderedDict[tuple[str, str], tuple[list[dict], list[tuple[int, int, str]]]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def open(cls, path: Path, *, readonly: bool = True) -> IndexStore:
        if readonly:
            # as_uri() percent-encodes, so a Windows path or one with `?`, `#` or `%` stays one path.
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
            return cls(conn, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        store = cls(conn, path)
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
            # Columns added since an index file was created (each build adds to the published index). Readers
            # never need them, so the schema version doesn't change.
            spans = {r[1] for r in self.conn.execute("PRAGMA table_info(spans)")}
            if "deprecated_params" not in spans:
                self.conn.execute("ALTER TABLE spans ADD COLUMN deprecated_params TEXT")
            releases = {r[1] for r in self.conn.execute("PRAGMA table_info(releases)")}
            if "requires" not in releases:
                self.conn.execute("ALTER TABLE releases ADD COLUMN requires TEXT")
            key = [
                r[1]
                for r in sorted(self.conn.execute("PRAGMA table_info(members)"), key=lambda r: r[5])
                if r[5]
            ]
            if key == ["package", "cls", "name", "first_ordinal"]:  # keyed by class first, before DT-DATA-49
                self.conn.execute("ALTER TABLE members RENAME TO members_by_class")
                self.conn.executescript(_schema_sql())
                self.conn.execute("INSERT INTO members SELECT * FROM members_by_class")
                self.conn.execute("DROP TABLE members_by_class")
            self._stamp_schema()

    def _stamp_schema(self) -> None:
        """Version 2 while some package is stored with own members (DT-DATA-16), else 1."""
        own = self.conn.execute("SELECT 1 FROM classes LIMIT 1").fetchone() is not None
        version = OWN_MEMBERS_SCHEMA_VERSION if own else SCHEMA_VERSION
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (version,))

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
        internal_rows: list[tuple] = []
        open_internal: dict[str, tuple[int, int, tuple]] = {}
        # For a package stored with own members (DT-DATA-16): canonical class -> (first, last, (mro, label)), and
        # (canonical class, name) -> (first, last, state).
        class_rows: list[tuple] = []
        open_classes: dict[str, tuple[int, int, tuple]] = {}
        member_rows: list[tuple] = []
        open_members: dict[tuple[str, str], tuple[int, int, tuple]] = {}
        internal_member_rows: list[tuple] = []  # (canonical class, name) -> (first, last, (kind, canonical))
        open_internal_members: dict[tuple[str, str], tuple[int, int, tuple]] = {}
        events: list[tuple[int, int | None, ChangeEvent]] = []
        prev: ApiSnapshot | None = None
        for item in ordered:
            snap = item.load() if isinstance(item, LazySnapshot) else item
            if snap is None:
                fingerprint = None  # the rows no longer match the inputs it describes
                continue
            # Every release of a package stored one way (DT-DATA-49): a release carried from rows written the other
            # way, by an index from before a package switched, would otherwise read as removing every member.
            snap = snap.with_own_members() if spec.own_members else snap.without_own_members()
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
                _extend(open_spans, span_rows, path, _state(rec), i)
            _close(open_spans, span_rows, i)
            if snap.own_members:
                for cls, label in labels(snap).items():
                    crec = snap.classes[cls]
                    mro = None if crec.mro is None else json.dumps(crec.mro)
                    _extend(open_classes, class_rows, cls, (mro, label), i)  # type: ignore[arg-type]
                    for member, mrec in crec.members.items():
                        _extend(open_members, member_rows, (cls, member), _state(mrec), i)  # type: ignore[arg-type]
                    for member, mrec in crec.internal.items():
                        key = (cls, member)
                        _extend(
                            open_internal_members, internal_member_rows, key, (mrec.kind, mrec.canonical), i
                        )  # type: ignore[arg-type]
            _close(open_classes, class_rows, i)  # type: ignore[arg-type]
            _close(open_members, member_rows, i)  # type: ignore[arg-type]
            _close(open_internal_members, internal_member_rows, i)  # type: ignore[arg-type]
            for path, rec in snap.internal.items():
                _extend(open_internal, internal_rows, path, (rec.kind, rec.canonical, rec.deprecated), i)
            _close(open_internal, internal_rows, i)
            if prev is None:
                events.extend((0, None, ev) for ev in initial_events(snap))
            else:
                events.extend((i, i - 1, ev) for ev in diff_snapshots(prev, snap))
            prev = snap
        span_rows.extend((path, *cur) for path, cur in open_spans.items())
        internal_rows.extend((path, *cur) for path, cur in open_internal.items())
        class_rows.extend((cls, *cur) for cls, cur in open_classes.items())
        member_rows.extend((key, *cur) for key, cur in open_members.items())
        internal_member_rows.extend((key, *cur) for key, cur in open_internal_members.items())
        # Names hints are checked against: a package stored with own members has its members in other rows.
        name_rows = (
            [*span_rows, *own_member_rows(span_rows, class_rows, member_rows)] if class_rows else span_rows
        )
        lookup = member_lookup(span_rows, class_rows, member_rows) if class_rows else None
        heirs = member_heirs(span_rows, class_rows, lookup) if lookup is not None else None

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
                for path, _first, last, state in name_rows
            ),
            ((path, first, last, state[3]) for path, first, last, state in name_rows if state[3]),
            (path for path, _first, _last, state in name_rows if state[0] == "attribute"),
            (lambda path: (found[0], found[1]) if (found := lookup(path)) else None) if lookup else None,
            heirs,
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
        for path, target, conf, evidence in internal_moves(
            span_rows, internal_rows, versions, spec.import_names
        ):
            add_repl(path, target, conf, "moved_heuristic", evidence)
        hints = hints_for(name) if curated is None else curated
        # With own members, a member's hints from the library's text are at its class's label (DT-DATA-16), and so
        # is a curated hint on a member a class inherits, which `name_rows` lists only under the defining class.
        label_of = {cls: label for cls, _first, _last, (_mro, label) in class_rows}
        if lookup is not None:
            rows = {path for path, _f, _l, _s in name_rows}
            hints = [_at_label(hint, lookup, label_of, rows) for hint in hints]
        relabel = {
            path: label_of[state[1]]
            for path, _f, _l, state in span_rows
            if state[0] == "class" and state[1] in label_of
        }

        def stated(path: str, target: str) -> float:
            func, paren, param = path.partition("(")
            parent, _, leaf = func.rpartition(".")
            at_label = f"{relabel[parent]}.{leaf}{paren}{param}" if parent in relabel else path
            return max(repl.get((path, target), (0.0,))[0], repl.get((at_label, target), (0.0,))[0])

        for path, target, evidence in curated_rows(name_rows, hints):
            # The library's own text already says it: keep that, so what DepTime finds by itself stays visible.
            if stated(path, target) < STRONG_HINT:
                add_repl(path, target, CURATED_CONFIDENCE, "curated", evidence)

        with self.conn:
            for table, col in (
                ("assembly", "package"),
                ("internal_members", "package"),
                ("members", "package"),
                ("classes", "package"),
                ("internal_spans", "package"),
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
                "INSERT INTO internal_spans(package, path, first_ordinal, last_ordinal, kind, canonical, deprecated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(name, path, first, last, *state) for path, first, last, state in internal_rows],
            )
            self.conn.executemany(
                "INSERT INTO classes(package, cls, first_ordinal, last_ordinal, mro, label) VALUES (?, ?, ?, ?, ?, ?)",
                [(name, cls, first, last, *state) for cls, first, last, state in class_rows],
            )
            self.conn.executemany(
                "INSERT INTO members(package, cls, name, first_ordinal, last_ordinal, kind, canonical, params,"
                " deprecated, deprecated_params) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (name, cls, member, first, last, *state)
                    for (cls, member), first, last, state in member_rows
                ],
            )
            self.conn.executemany(
                "INSERT INTO internal_members(package, cls, name, first_ordinal, last_ordinal, kind, canonical)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (name, cls, member, first, last, *state)
                    for (cls, member), first, last, state in internal_member_rows
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
            self._stamp_schema()
        with self._lock:
            self._own = None
            self._internal = self._internal_members = None
            self._members.clear()
        return RebuildStats(
            name,
            len(release_rows),
            len(span_rows),
            len(events),
            len(repl),
            len(internal_rows),
            len(member_rows),
        )

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
        internal = {}
        if self.has_internal():
            rows = self.conn.execute(
                "SELECT path, kind, canonical, deprecated FROM internal_spans "
                "WHERE package = ? AND first_ordinal <= ? AND last_ordinal >= ?",
                (spec.name, rel["ordinal"], rel["ordinal"]),
            ).fetchall()
            internal = {
                r["path"]: SymbolRecord(r["path"], r["kind"], r["canonical"], None, r["deprecated"])
                for r in rows
            }
        errors = ["analysis problem recorded by an earlier run"] * rel["error_count"]
        requires = rel["requires"] if "requires" in rel.keys() else None  # noqa: SIM118
        classes: dict[str, ClassRecord] = {}
        if self.stores_own_members(spec.name):
            for r in self.conn.execute(
                "SELECT cls, mro FROM classes WHERE package = ? AND first_ordinal <= ? AND last_ordinal >= ?",
                (spec.name, rel["ordinal"], rel["ordinal"]),
            ):
                classes[r["cls"]] = ClassRecord(r["cls"], None if r["mro"] is None else json.loads(r["mro"]))
            for r in self.conn.execute(
                "SELECT * FROM members WHERE package = ? AND first_ordinal <= ? AND last_ordinal >= ?",
                (spec.name, rel["ordinal"], rel["ordinal"]),
            ):
                member = f"{r['cls']}.{r['name']}"
                rec = SymbolRecord(member, HIDDEN) if r["kind"] == HIDDEN else _record(member, r)
                classes[r["cls"]].members[r["name"]] = rec
            if self.has_internal_members():
                for r in self.conn.execute(
                    "SELECT cls, name, kind, canonical FROM internal_members"
                    " WHERE package = ? AND first_ordinal <= ? AND last_ordinal >= ?",
                    (spec.name, rel["ordinal"], rel["ordinal"]),
                ):
                    member = f"{r['cls']}.{r['name']}"
                    classes[r["cls"]].internal[r["name"]] = SymbolRecord(member, r["kind"], r["canonical"])
        return ApiSnapshot(
            spec.name,
            version,
            list(spec.import_names),
            symbols,
            errors,
            requires=None if requires is None else json.loads(requires),
            internal=internal,
            own_members=bool(classes),
            classes=classes,
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

    def _own_packages(self) -> frozenset[str]:
        """The packages stored with their classes' own members (DT-DATA-16)."""
        if self._own is None:
            try:
                own = frozenset(r[0] for r in self.conn.execute("SELECT DISTINCT package FROM classes"))
            except sqlite3.OperationalError:  # an index from before DT-DATA-16
                own = frozenset()
            self._own = own
        return self._own

    def stores_own_members(self, name: str) -> bool:
        return name in self._own_packages()

    def _resolved(self, name: str, path: str) -> tuple[list[dict], list[tuple[int, int, str]]]:
        """For a class member of a package stored with own members (DT-DATA-16): its spans, shaped like rows of
        `spans`, from the class at each release, then each class in its MRO, as Python looks a name up; and which
        class defines it, over which releases, as `(first, last, canonical class)`. Empty for any other path."""
        parent, _, leaf = path.rpartition(".")
        if not parent or not self.stores_own_members(name):
            return [], []
        key = (name, path)
        with self._lock:
            if (hit := self._members.get(key)) is not None:
                self._members.move_to_end(key)
                return hit
        owners = self.conn.execute(
            "SELECT first_ordinal, last_ordinal, canonical FROM spans WHERE package = ? AND path = ? AND kind = 'class'",
            (name, parent),
        ).fetchall()
        found: dict[int, tuple[tuple, str]] = {}  # ordinal -> (state, defining class)
        if owners:
            mros: dict[str, list[tuple[int, int, list[str]]]] = {}
            for cls in {r["canonical"] for r in owners}:
                rows = self.conn.execute(
                    "SELECT first_ordinal, last_ordinal, mro FROM classes WHERE package = ? AND cls = ?",
                    (name, cls),
                )
                mros[cls] = [(r[0], r[1], json.loads(r[2]) if r[2] else []) for r in rows]
            candidates = sorted(
                set(mros) | {b for spans in mros.values() for _f, _l, mro in spans for b in mro}
            )
            members: dict[str, list[tuple[int, int, tuple | None]]] = defaultdict(list)
            for r in self.conn.execute(
                "SELECT cls, first_ordinal, last_ordinal, kind, canonical, params, deprecated, deprecated_params"
                f" FROM members WHERE package = ? AND name = ? AND cls IN ({','.join('?' * len(candidates))})",
                (name, leaf, *candidates),
            ):
                state = None if r["kind"] == HIDDEN else tuple(r)[3:]
                members[r["cls"]].append((r["first_ordinal"], r["last_ordinal"], state))
            for span in owners:
                cls = span["canonical"]
                for o in range(span["first_ordinal"], span["last_ordinal"] + 1):
                    mro = next((m for f, end, m in mros.get(cls, ()) if f <= o <= end), [])
                    for owner in [cls, *mro]:
                        hit = next((st for f, end, st in members.get(owner, ()) if f <= o <= end), ())
                        if hit != ():
                            if hit is not None:
                                found[o] = (hit, owner)
                            break
        spans: list[dict] = []
        defined: list[tuple[int, int, str]] = []
        for o in sorted(found):
            state, owner = found[o]
            last = spans[-1] if spans else None
            if last and last["last_ordinal"] == o - 1 and tuple(last[c] for c in _STATE_COLUMNS) == state:
                last["last_ordinal"] = o
            else:
                spans.append(
                    {"package": name, "path": path, "tail": leaf, "first_ordinal": o, "last_ordinal": o}
                    | dict(zip(_STATE_COLUMNS, state, strict=True))
                )
            if defined and defined[-1][1] == o - 1 and defined[-1][2] == owner:
                defined[-1] = (defined[-1][0], o, owner)
            else:
                defined.append((o, o, owner))
        result = (spans, defined)
        with self._lock:
            self._members[key] = result
            if len(self._members) > MEMBER_CACHE_SIZE:
                self._members.popitem(last=False)
        return result

    def spans(self, name: str, path: str) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM spans WHERE package = ? AND path = ? ORDER BY first_ordinal", (name, path)
        ).fetchall()
        member = self._resolved(name, path)[0]
        return sorted([*rows, *member], key=lambda r: r["first_ordinal"]) if member else rows  # type: ignore[list-item]

    def span_at(self, name: str, path: str, ordinal: int) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM spans WHERE package = ? AND path = ? AND first_ordinal <= ? AND last_ordinal >= ?",
            (name, path, ordinal, ordinal),
        ).fetchone()
        if row is None:
            spans = self._resolved(name, path)[0]
            row = next((r for r in spans if r["first_ordinal"] <= ordinal <= r["last_ordinal"]), None)
        return row

    def has_internal(self) -> bool:
        """Whether the index records internal names (DT-DATA-22); older ones don't have the table."""
        if self._internal is None:
            self._internal = (
                self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'internal_spans'"
                ).fetchone()
                is not None
            )
        return self._internal

    def has_internal_members(self) -> bool:
        """Whether the index records class members that aren't public API (DT-DATA-46)."""
        if self._internal_members is None:
            self._internal_members = (
                self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'internal_members'"
                ).fetchone()
                is not None
            )
        return self._internal_members

    def internal_spans(self, name: str, path: str) -> list[sqlite3.Row]:
        """`path`'s spans as a name that isn't public API, each with `internal` = 1: a module's name (DT-DATA-22),
        or a class's private member or a private class's member (DT-DATA-46), with `member_of` = the class's path."""
        if not self.has_internal():
            return []
        rows = self.conn.execute(
            "SELECT *, 1 AS internal FROM internal_spans WHERE package = ? AND path = ? ORDER BY first_ordinal",
            (name, path),
        ).fetchall()
        members = self._internal_member_spans(name, path)
        return sorted([*rows, *members], key=lambda r: r["first_ordinal"]) if members else rows  # type: ignore[list-item]

    def internal_span_at(self, name: str, path: str, ordinal: int) -> sqlite3.Row | None:
        if not self.has_internal():
            return None
        row = self.conn.execute(
            "SELECT *, 1 AS internal FROM internal_spans"
            " WHERE package = ? AND path = ? AND first_ordinal <= ? AND last_ordinal >= ?",
            (name, path, ordinal, ordinal),
        ).fetchone()
        if row is None:
            spans = self._internal_member_spans(name, path)
            row = next((r for r in spans if r["first_ordinal"] <= ordinal <= r["last_ordinal"]), None)
        return row

    def records_internal_members(self, name: str, classes: set[str]) -> bool:
        """Whether the index has the members that aren't public API of every class in `classes` (DT-DATA-46)."""
        if not classes or not self.has_internal_members() or not self.stores_own_members(name):
            return False
        return all(
            self.conn.execute(
                "SELECT 1 FROM classes WHERE package = ? AND cls = ? LIMIT 1", (name, cls)
            ).fetchone()
            for cls in classes
        )

    def find_internal_classes(self, name: str, suffix: str, limit: int = 20) -> list[tuple[str, str]]:
        """(package, path) of the classes that aren't public API whose path is or ends with `.suffix` (DT-DATA-46).
        It reads every internal name of the package, so it's for a private class name, which public paths rarely
        have."""
        if not self.has_internal():
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT package, path, path = canonical AS defined FROM internal_spans"
            " WHERE package = ? AND kind = 'class' AND (path = ? OR substr(path, -?) = ?)"
            " ORDER BY length(path), path LIMIT ?",
            (name, suffix, len(suffix) + 1, "." + suffix, limit),
        ).fetchall()
        # Where it's defined, rather than a module that imports it (`weaviate.client._Collections`).
        defined = [r for r in rows if r["defined"]] or rows
        return list(dict.fromkeys((r["package"], r["path"]) for r in defined))

    def _internal_member_spans(self, name: str, path: str) -> list[dict]:
        """For `<class path>.<name>` in a package stored with own members: the spans in which the name, looked up
        as Python does through the class and its MRO, is a member that isn't public API (DT-DATA-46). The class's
        path can be public or internal: a public class's private member, or a private class's member, which a
        public base can define. A public class's public member is `_resolved`'s."""
        parent, _, leaf = path.rpartition(".")
        if not parent or not self.has_internal_members() or not self.stores_own_members(name):
            return []
        key = (name, "\0internal\0" + path)
        with self._lock:
            if (hit := self._members.get(key)) is not None:
                self._members.move_to_end(key)
                return hit[0]
        owners = [
            (r["first_ordinal"], r["last_ordinal"], r["canonical"], False)
            for r in self.conn.execute(
                "SELECT first_ordinal, last_ordinal, canonical FROM spans"
                " WHERE package = ? AND path = ? AND kind = 'class'",
                (name, parent),
            )
        ] + [
            (r["first_ordinal"], r["last_ordinal"], r["canonical"], True)
            for r in self.conn.execute(
                "SELECT first_ordinal, last_ordinal, canonical FROM internal_spans"
                " WHERE package = ? AND path = ? AND kind = 'class'",
                (name, parent),
            )
        ]
        found: dict[int, tuple] = {}  # ordinal -> (kind, canonical)
        if owners:
            mros: dict[str, list[tuple[int, int, list[str]]]] = {}
            for cls in {c for _f, _l, c, _i in owners if c}:
                rows = self.conn.execute(
                    "SELECT first_ordinal, last_ordinal, mro FROM classes WHERE package = ? AND cls = ?",
                    (name, cls),
                )
                mros[cls] = [(r[0], r[1], json.loads(r[2]) if r[2] else []) for r in rows]
            candidates = sorted(
                set(mros) | {b for spans in mros.values() for _f, _l, mro in spans for b in mro}
            )
            marks = ",".join("?" * len(candidates))
            public: dict[str, list[tuple[int, int, tuple | None]]] = defaultdict(list)
            for r in self.conn.execute(
                f"SELECT cls, first_ordinal, last_ordinal, kind, canonical FROM members"
                f" WHERE package = ? AND name = ? AND cls IN ({marks})",
                (name, leaf, *candidates),
            ):
                public[r["cls"]].append(
                    (r[1], r[2], None if r["kind"] == HIDDEN else (r["kind"], r["canonical"]))
                )
            internal: dict[str, list[tuple[int, int, tuple | None]]] = defaultdict(list)
            for r in self.conn.execute(
                f"SELECT cls, first_ordinal, last_ordinal, kind, canonical FROM internal_members"
                f" WHERE package = ? AND name = ? AND cls IN ({marks})",
                (name, leaf, *candidates),
            ):
                internal[r["cls"]].append(
                    (r[1], r[2], None if r["kind"] == HIDDEN else (r["kind"], r["canonical"]))
                )
            for first, last, cls, private_owner in owners:
                for o in range(first, last + 1):
                    mro = next((m for f, end, m in mros.get(cls, ()) if f <= o <= end), [])
                    for owner in [cls, *mro]:
                        hit = next((st for f, end, st in public.get(owner, ()) if f <= o <= end), ())
                        if hit != ():
                            if hit is not None and private_owner:  # a public name, but of a private class
                                found[o] = hit
                            break
                        hit = next((st for f, end, st in internal.get(owner, ()) if f <= o <= end), ())
                        if hit != ():
                            if hit is not None:
                                found[o] = hit
                            break
        spans: list[dict] = []
        for o in sorted(found):
            last = spans[-1] if spans else None
            if last and last["last_ordinal"] == o - 1 and (last["kind"], last["canonical"]) == found[o]:
                last["last_ordinal"] = o
            else:
                kind, canonical = found[o]
                spans.append(
                    {"package": name, "path": path, "first_ordinal": o, "last_ordinal": o, "kind": kind}
                    | {"canonical": canonical, "deprecated": None, "internal": 1, "member_of": parent}
                )
        with self._lock:
            self._members[key] = (spans, [])
            if len(self._members) > MEMBER_CACHE_SIZE:
                self._members.popitem(last=False)
        return spans

    def events(self, name: str, path: str) -> list[sqlite3.Row]:
        # `+ordinal`: ordering by the bare column made SQLite read the package's events through events_by_ordinal
        # and filter on path, a scan of every event the package has (4.5 ms a call, hundreds of calls when the
        # error's owner is searched in every package).
        rows = self.conn.execute(
            "SELECT * FROM events WHERE package = ? AND path = ? ORDER BY +ordinal, kind", (name, path)
        ).fetchall()
        member = self._resolved(name, path)[0]
        if not member:
            return rows
        # A class member of a package stored with own members (DT-DATA-16): the events stored at its path are its
        # defining class's, so they're worked out from its states, release by release, as an index build does.
        direct = self.conn.execute(
            "SELECT * FROM spans WHERE package = ? AND path = ?", (name, path)
        ).fetchall()
        # A state only changes where a span starts or ends, so only those releases are compared: one record per
        # span, not one per release (fastapi's `__init__` spans 100 releases; an argument error took 65 ms).
        runs = [(r["first_ordinal"], r["last_ordinal"], _record(path, r)) for r in [*direct, *member]]
        at: dict[int, SymbolRecord] = {}
        for first, last, rec in runs:
            at[first] = at[last] = rec
        count = self.conn.execute("SELECT COUNT(*) FROM releases WHERE package = ?", (name,)).fetchone()[0]
        bounds = {o for first, last, _rec in runs for o in (first, last + 1) if o < count}
        out: list[dict] = []
        for o in sorted(bounds):
            if o == 0:
                found, prev = initial_path_events(path, at[0]), None
            else:
                found, prev = path_events(path, at.get(o - 1), at.get(o)), o - 1
            out += [
                {"package": name, "path": path, "ordinal": o, "prev_ordinal": prev, "kind": ev.kind}
                | {"detail": json.dumps(ev.detail, sort_keys=True)}
                for ev in found
            ]
        out += [dict(r) for r in rows if r["kind"] == "moved"]  # pairs module-level paths, from the build
        return sorted(out, key=lambda r: (r["ordinal"], r["kind"]))  # type: ignore[return-value]

    def _labels(self, name: str, path: str) -> list[str]:
        """For a class member of a package stored with own members, where its defining classes keep their
        members' events and hints (DT-DATA-16): each one's label and the member's name, `path` itself left out.
        Every label a class had up to the last release it defines the member in: a deprecation is stored once, at
        the label of the release that brings it, and sqlalchemy's `Select` was `sqlalchemy.sql.Select` in 1.4, so
        2.0's `sqlalchemy.Select.column` lost its hint (DT-DATA-49)."""
        _spans, defined = self._resolved(name, path)
        leaf = path.rpartition(".")[2]
        out: dict[str, None] = {}
        for _first, last, cls in defined:
            for r in self.conn.execute(
                "SELECT label FROM classes WHERE package = ? AND cls = ? AND first_ordinal <= ?",
                (name, cls, last),
            ):
                out[f"{r['label']}.{leaf}"] = None
        out.pop(path, None)
        return list(out)

    def _event_paths(self, name: str, paths: list[str], after: int, upto: int) -> list[str]:
        """`paths` and, for a package stored with own members (DT-DATA-16), where their members' events are:
        a member's defining classes' labels, and for a class, its own label and those of its MRO, in (after, upto]."""
        out = dict.fromkeys(paths)
        for path in paths:
            out.update(dict.fromkeys(self._labels(name, path)))
            for span in self.conn.execute(
                "SELECT canonical FROM spans WHERE package = ? AND path = ? AND kind = 'class'"
                " AND first_ordinal <= ? AND last_ordinal > ?",
                (name, path, upto, after),
            ):
                classes = [span["canonical"]]
                for r in self.conn.execute(
                    "SELECT mro FROM classes WHERE package = ? AND cls = ? AND first_ordinal <= ? AND last_ordinal > ?",
                    (name, span["canonical"], upto, after),
                ):
                    classes += json.loads(r["mro"]) if r["mro"] else []
                for cls in dict.fromkeys(classes):
                    for r in self.conn.execute(
                        "SELECT label FROM classes WHERE package = ? AND cls = ?"
                        " AND first_ordinal <= ? AND last_ordinal > ?",
                        (name, cls, upto, after),
                    ):
                        out[r["label"]] = None
        return list(out)

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
        if paths and self.stores_own_members(name):
            paths = self._event_paths(name, paths, after_ordinal, upto_ordinal)
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
        rows = self.conn.execute(
            "SELECT * FROM replacements WHERE package = ? AND path = ? ORDER BY confidence DESC, target",
            (name, path),
        ).fetchall()
        func, paren, param = path.partition("(")
        labels = self._labels(name, func)
        if not labels:
            return rows
        # A class member of a package stored with own members (DT-DATA-16): its defining class's hints too.
        best: dict[str, Mapping] = {r["target"]: r for r in rows}
        for label in labels:
            for r in self.conn.execute(
                "SELECT * FROM replacements WHERE package = ? AND path = ?", (name, f"{label}{paren}{param}")
            ):
                target = (
                    r["target"] if r["source"] == "curated" else self._sibling(name, r["target"], label, func)
                )
                if target not in best or best[target]["confidence"] < r["confidence"]:
                    best[target] = dict(r) | {"path": path, "target": target}
        return sorted(best.values(), key=lambda r: (-r["confidence"], r["target"]))  # type: ignore[return-value]

    def _sibling(self, name: str, target: str, label: str, path: str) -> str:
        """A hint's target on the defining class's member `label`, named as a member of `path`'s class, as a build
        that stores every member under every class names it: when `path`'s class has a member of that name and the
        target is the defining class's member of that name (`get_relevant_documents` on a retriever subclass says to
        use `invoke`, which it inherits from `BaseRetriever`) or the same object."""
        defining, parent = label.rpartition(".")[0], path.rpartition(".")[0]
        owner, _, leaf = target.rpartition(".")
        if not owner or "(" in target or f"{parent}.{leaf}" == target:
            return target
        sibling = f"{parent}.{leaf}"
        mine = self.spans(name, sibling)
        if not mine:
            return target
        if owner == defining:
            return sibling
        objects = {r["canonical"] for r in self.spans(name, target)}
        return sibling if objects & {r["canonical"] for r in mine} else target

    def param_replacements(self, name: str, func_path: str) -> list[sqlite3.Row]:
        prefix = func_path + "("
        rows = self.conn.execute(
            "SELECT * FROM replacements WHERE package = ? AND substr(path, 1, ?) = ? ORDER BY path, confidence DESC",
            (name, len(prefix), prefix),
        ).fetchall()
        labels = self._labels(name, func_path)
        if not labels:
            return rows
        best: dict[tuple[str, str], Mapping] = {(r["path"], r["target"]): r for r in rows}
        for label in labels:
            for r in self.conn.execute(
                "SELECT * FROM replacements WHERE package = ? AND substr(path, 1, ?) = ?",
                (name, len(label) + 1, label + "("),
            ):
                key = (func_path + r["path"][len(label) :], r["target"])
                if key not in best or best[key]["confidence"] < r["confidence"]:
                    best[key] = dict(r) | {"path": key[0]}
        return sorted(best.values(), key=lambda r: (r["path"], -r["confidence"]))  # type: ignore[return-value]

    def _suffix_query(self, suffix: str, packages: Sequence[str] | None) -> tuple[str, list]:
        """A query for `package, path` of the public paths equal to or ending with `.suffix`, in any version.

        Rows are found by tail, and each costs a read from the table. A member's own tail can match tens of
        thousands of rows (61,023 for `__init__`), so a dotted suffix is matched on its parent's tail (38 for
        `Client`) and the member is then looked up by its path (DT-CORE-16): `Client.__init__` took 1.3 s cold.
        The suffix as a whole path is looked up by path too, since a top-level name such as `google.genai`'s
        `google` has no row of its own. Callers apply DISTINCT outside: inside, it made SQLite read every span of
        the package in path order rather than use the tail index.

        A package stored with own members (DT-DATA-16) has a class's members in `members`, its own and those of
        its MRO. A bare name is matched there at the label of the class defining it.
        """
        in_packages = f" AND p.package IN ({','.join('?' * len(packages))})" if packages is not None else ""
        # Only the packages stored with own members: SQLite otherwise read every member row for a package without
        # any (22 ms for sqlalchemy's bare `get`).
        own = sorted(self._own_packages() if packages is None else self._own_packages() & set(packages))
        in_own = f" AND p.package IN ({','.join('?' * len(own))})"
        parent, _, leaf = suffix.rpartition(".")
        if not parent:
            sql = (
                f"SELECT p.package AS package, p.path AS path FROM spans p WHERE p.tail = ?{in_packages}"
                " AND (p.path = ? OR substr(p.path, -?) = ?)"
            )
            params = [suffix, *(packages or ()), suffix, len(suffix) + 1, "." + suffix]
            if own:
                # UNION ALL: a UNION made SQLite read every span of the package in path order, not by tail.
                sql += (
                    " UNION ALL SELECT m.package, c.label || '.' || m.name FROM members m JOIN classes c"
                    " ON c.package = m.package AND c.cls = m.cls"
                    " AND c.first_ordinal <= m.last_ordinal AND c.last_ordinal >= m.first_ordinal"
                    f" WHERE m.package IN ({','.join('?' * len(own))}) AND m.name = ? AND m.kind != ?"
                )
                params += [*own, suffix, HIDDEN]
            return sql, params
        sql = (
            "SELECT p.package AS package, p.path || '.' || ? AS path FROM spans p"
            f" WHERE p.tail = ?{in_packages} AND (p.path = ? OR substr(p.path, -?) = ?)"
            " AND EXISTS (SELECT 1 FROM spans s WHERE s.package = p.package AND s.path = p.path || '.' || ?)"
            " UNION SELECT p.package, p.path FROM spans p"
            f" WHERE p.package IN (SELECT name FROM packages){in_packages} AND p.path = ?"
        )
        ptail = parent.rsplit(".", 1)[-1]
        tail_args = [ptail, *(packages or ()), parent, len(parent) + 1, "." + parent]
        params = [leaf, *tail_args, leaf, *(packages or ()), suffix]
        if own:
            # The class's own member, or a member of a class in its MRO, in a release that has all three.
            sql += (
                " UNION SELECT p.package, p.path || '.' || ? FROM spans p"
                f" WHERE p.tail = ?{in_own} AND (p.path = ? OR substr(p.path, -?) = ?) AND p.kind = 'class'"
                " AND EXISTS (SELECT 1 FROM members m WHERE m.package = p.package AND m.cls = p.canonical"
                " AND m.name = ? AND m.kind != ? AND m.first_ordinal <= p.last_ordinal"
                " AND m.last_ordinal >= p.first_ordinal"
                # CROSS JOIN keeps this order: otherwise SQLite read every member of the package first.
                " UNION ALL SELECT 1 FROM classes c CROSS JOIN json_each(c.mro) j CROSS JOIN members m"
                " WHERE c.package = p.package AND c.cls = p.canonical AND c.first_ordinal <= p.last_ordinal"
                " AND c.last_ordinal >= p.first_ordinal AND m.package = p.package AND m.cls = j.value"
                " AND m.name = ? AND m.kind != ? AND max(p.first_ordinal, c.first_ordinal, m.first_ordinal)"
                " <= min(p.last_ordinal, c.last_ordinal, m.last_ordinal))"
            )
            params += [leaf, ptail, *own, parent, len(parent) + 1, "." + parent, leaf, HIDDEN, leaf, HIDDEN]
        return sql, params

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
        pairs = [(r["package"], r["path"]) for r in rows]
        if "." in suffix or not self._own_packages() & set(packages):
            return pairs
        # A bare name is matched at the label of the class defining it, which for a base no public path names
        # is its canonical path: `httpx._client.BaseClient.build_request`. Name it under its public subclasses.
        out: dict[tuple[str, str], None] = {}
        for package, path in pairs:
            owner = path.rpartition(".")[0]
            if (
                not owner
                or self.conn.execute(
                    "SELECT 1 FROM spans WHERE package = ? AND path = ? LIMIT 1", (package, owner)
                ).fetchone()
            ):
                if self.spans(package, path):  # the label and the member in one release
                    out[(package, path)] = None
                continue
            for r in self.conn.execute(
                "SELECT DISTINCT c.label FROM classes c WHERE c.package = ? AND c.mro LIKE ?"
                " AND EXISTS (SELECT 1 FROM spans p WHERE p.package = c.package AND p.path = c.label"
                " AND p.kind = 'class') ORDER BY length(c.label), c.label LIMIT 3",
                (package, f'%"{owner}"%'),
            ):
                if self.spans(package, heir := f"{r['label']}.{suffix}"):
                    out[(package, heir)] = None
        return sorted(out, key=lambda pp: (len(pp[1]), pp[1]))[:limit]
