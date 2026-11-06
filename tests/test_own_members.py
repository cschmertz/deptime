"""A package stored with its classes' own members (DT-DATA-16): every answer the same as storing every member under
every class, from far fewer rows."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from deptime.config import Coverage, PackageSpec, VersionPolicy
from deptime.index.distribution import IndexIncompatible, _validate
from deptime.index.store import OWN_MEMBERS_SCHEMA_VERSION, SCHEMA_VERSION, IndexStore
from deptime.ingest.curated import CuratedHint
from deptime.ingest.snapshot import ApiSnapshot, build_snapshot
from deptime.models import SymbolQuery
from deptime.query import DepTime

CLASSIC = PackageSpec(name="inh", import_names=("inh",), tier="A")
OWN = PackageSpec(name="inh", import_names=("inh",), tier="A", own_members=True)

BRIDGE = (
    "import warnings\n\n"
    "class _Bridge:\n"
    "    def get(self, key):\n"
    "{get_body}"
    "    def _get_all(self):\n"
    "        pass\n"
)
GET_WARNS = '        warnings.warn("get() is deprecated; use fetch() instead", DeprecationWarning)\n'

# 1.0: Sub overrides run and defines own; Other lives in inh.models.
# 2.0: Base.run loses `b` and Sub stops overriding it; own moves to Mixin; Other moves to inh.other but stays
#      inh.Other; _Bridge.get is deprecated; Base gains fetch.
# 3.0: Sub stops inheriting Mixin, so Sub.mix and Sub.own go; Base.old is removed.
RELEASES = {
    "1.0": {
        "inh/__init__.py": (
            "from inh.models import Base, Mixin, Sub, Other\n__all__ = ['Base', 'Mixin', 'Sub', 'Other', 'models']\n"
        ),
        "inh/_hidden.py": BRIDGE.format(get_body="        pass\n"),
        "inh/models.py": (
            "import warnings\n"
            "from inh._hidden import _Bridge\n\n"
            "class Mixin:\n"
            "    def mix(self, a):\n        pass\n\n"
            "class Base(_Bridge):\n"
            "    attr = 3\n"
            "    def __init__(self, x, y=1):\n        pass\n"
            "    def run(self, a, b=2):\n        pass\n"
            "    def old(self):\n"
            '        warnings.warn("old() is deprecated; use run() instead", DeprecationWarning)\n\n'
            "class Sub(Base, Mixin):\n"
            "    def run(self, a, b=2, c=3):\n        pass\n"
            "    def own(self):\n        pass\n\n"
            "class Other:\n"
            "    def go(self):\n        pass\n"
        ),
    },
    "2.0": {
        "inh/__init__.py": (
            "from inh.models import Base, Mixin, Sub\nfrom inh.other import Other\n"
            "__all__ = ['Base', 'Mixin', 'Sub', 'Other', 'models', 'other']\n"
        ),
        "inh/_hidden.py": BRIDGE.format(get_body=GET_WARNS),
        "inh/other.py": "class Other:\n    def go(self):\n        pass\n",
        "inh/models.py": (
            "import warnings\n"
            "from inh._hidden import _Bridge\n\n"
            "class Mixin:\n"
            "    def mix(self, a):\n        pass\n"
            "    def own(self):\n        pass\n\n"
            "class Base(_Bridge):\n"
            "    attr = 3\n"
            "    def __init__(self, x, y=1):\n        pass\n"
            "    def run(self, a):\n        pass\n"
            "    def fetch(self, key):\n        pass\n"
            "    def old(self):\n"
            '        warnings.warn("old() is deprecated; use run() instead", DeprecationWarning)\n\n'
            "class Sub(Base, Mixin):\n"
            "    pass\n"
        ),
    },
    "3.0": {
        "inh/__init__.py": (
            "from inh.models import Base, Mixin, Sub\nfrom inh.other import Other\n"
            "__all__ = ['Base', 'Mixin', 'Sub', 'Other', 'models', 'other']\n"
        ),
        "inh/_hidden.py": BRIDGE.format(get_body=GET_WARNS),
        "inh/other.py": "class Other:\n    def go(self):\n        pass\n",
        "inh/models.py": (
            "from inh._hidden import _Bridge\n\n"
            "class Mixin:\n"
            "    def mix(self, a):\n        pass\n"
            "    def own(self):\n        pass\n\n"
            "class Base(_Bridge):\n"
            "    attr = 3\n"
            "    def __init__(self, x, y=1):\n        pass\n"
            "    def run(self, a):\n        pass\n"
            "    def fetch(self, key):\n        pass\n\n"
            "class Sub(Base):\n"
            "    pass\n"
        ),
    },
}
SPAN = ("first_ordinal", "last_ordinal", "kind", "canonical", "params", "deprecated", "deprecated_params")


def _build(tmp: Path, own: bool) -> list[ApiSnapshot]:
    out = []
    for version, files in RELEASES.items():
        root = tmp / ("own" if own else "classic") / version
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        out.append(build_snapshot("inh", version, ["inh"], root, own_members=own))
    return out


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[ApiSnapshot]]:
    tmp = tmp_path_factory.mktemp("inh")
    return {"classic": _build(tmp, False), "own": _build(tmp, True)}


@pytest.fixture
def stores(built) -> dict[str, IndexStore]:
    out = {}
    for mode, spec in (("classic", CLASSIC), ("own", OWN)):
        out[mode] = IndexStore.memory()
        out[mode].rebuild_package(spec, built[mode])
    return out


def test_a_snapshot_with_own_members_expands_to_the_classic_one(built):
    for classic, own in zip(built["classic"], built["own"], strict=True):
        assert own.own_members and not classic.own_members
        assert own.expanded() == classic.symbols
        assert not any(p.startswith("inh.Sub.") for p in own.symbols)
    first = built["own"][0]
    assert first.classes["inh.models.Sub"].mro == [
        "inh.models.Base",
        "inh._hidden._Bridge",
        "inh.models.Mixin",
    ]
    assert set(first.classes["inh.models.Sub"].members) == {"run", "own"}
    assert first.classes["inh._hidden._Bridge"].mro is None  # recorded only as a base
    assert set(first.classes["inh._hidden._Bridge"].members) == {"get"}  # private names aren't recorded


def test_the_store_answers_every_path_the_same(stores, built):
    classic, own = stores["classic"], stores["own"]
    paths = sorted({p for s in built["classic"] for p in s.symbols})
    assert "inh.Sub.get" in paths and "inh.models.Other.go" in paths
    for path in [*paths, "inh.Sub.nothing", "inh.Sub.get.deeper"]:
        assert [tuple(r[c] for c in SPAN) for r in own.spans("inh", path)] == [
            tuple(r[c] for c in SPAN) for r in classic.spans("inh", path)
        ], path
        for o in range(3):
            a, b = classic.span_at("inh", path, o), own.span_at("inh", path, o)
            assert (a and tuple(a[c] for c in SPAN)) == (b and tuple(b[c] for c in SPAN)), (path, o)
        assert sorted(
            (r["ordinal"], r["prev_ordinal"], r["kind"], r["detail"]) for r in own.events("inh", path)
        ) == sorted(
            (r["ordinal"], r["prev_ordinal"], r["kind"], r["detail"]) for r in classic.events("inh", path)
        )
        assert [(r["path"], r["target"]) for r in own.replacements("inh", path)] == [
            (r["path"], r["target"]) for r in classic.replacements("inh", path)
        ], path
    for suffix in ("Sub.get", "Sub.mix", "Base.run", "Other.go", "Sub.nothing", "Sub"):
        assert own.find_by_suffix(["inh"], suffix) == classic.find_by_suffix(["inh"], suffix), suffix
        assert own.packages_with_suffix(suffix) == classic.packages_with_suffix(suffix), suffix


def test_inherited_members_are_stored_once(stores):
    own, classic = stores["own"], stores["classic"]
    count = lambda s, sql: s.conn.execute(sql).fetchone()[0]  # noqa: E731
    assert count(own, "SELECT COUNT(*) FROM spans WHERE path LIKE 'inh.Sub.%'") == 0
    assert count(classic, "SELECT COUNT(*) FROM spans WHERE path LIKE 'inh.Sub.%'") > 0
    # get's deprecation is stored at its defining class's label, not once per class that inherits it ...
    stored = {r[0] for r in own.conn.execute("SELECT path FROM events WHERE kind = 'deprecated'")}
    assert stored == {"inh._hidden._Bridge.get", "inh.Base.old"}
    # ... and a subclass's history still has it, worked out from its states.
    assert [(r["ordinal"], r["kind"]) for r in own.events("inh", "inh.Sub.get")] == [(1, "deprecated")]
    # get's hint from its deprecation reaches subclasses, named as their member.
    assert [r["target"] for r in own.replacements("inh", "inh.Sub.get")] == ["inh.Sub.fetch"]


def test_a_class_that_moves_keeps_its_label_and_a_method_moved_to_a_base_isnt_removed(stores):
    own = stores["own"]
    stored = {(r[0], r[1]) for r in own.conn.execute("SELECT path, kind FROM events WHERE ordinal = 1")}
    assert ("inh.Other.go", "removed") not in stored and ("inh.Other.go", "added") not in stored
    assert ("inh.Sub.own", "removed") not in stored  # now inherited from Mixin
    assert ("inh.Sub.run", "param_removed") in stored  # Sub stopped overriding run: its signature changed
    assert ("inh.Base.run", "param_removed") in stored


def test_diffing_versions_finds_a_used_members_events_at_its_defining_class(stores):
    own = stores["own"]
    severity = {"removed": 0, "param_removed": 1, "deprecated": 2, "added": 9}
    rows, _ = own.events_between("inh", 0, 2, 50, severity=severity, paths=["inh.Sub.get"])
    assert [(r["path"], r["kind"]) for r in rows] == [("inh._hidden._Bridge.get", "deprecated")]
    rows, _ = own.events_between("inh", 0, 2, 50, severity=severity, paths=["inh.Sub"], include_added=False)
    assert {"inh.Base.old", "inh._hidden._Bridge.get"} <= {r["path"] for r in rows}  # members of its bases


def test_a_curated_hint_on_an_inherited_member_reaches_it(built):
    hint = CuratedHint(
        "inh", "inh.Sub.mix", "inh.Base.run", "Sub.mix is gone; call Base.run", "https://example.org"
    )
    answers = []
    for mode, spec in (("classic", CLASSIC), ("own", OWN)):
        store = IndexStore.memory()
        store.rebuild_package(spec, built[mode], curated=[hint])
        answers.append(
            [(r["path"], r["target"], r["source"]) for r in store.replacements("inh", "inh.Sub.mix")]
        )
    assert answers[0] == answers[1] == [("inh.Sub.mix", "inh.Base.run", "curated")]


def test_snapshot_at_rebuilds_a_release_with_its_classes(stores, built):
    again = stores["own"].snapshot_at(OWN, "2.0")
    assert again.own_members
    assert again.symbols == built["own"][1].symbols
    assert again.classes == built["own"][1].classes


def test_the_schema_version_says_whether_readers_must_resolve_members(stores, tmp_path):
    version = lambda s: s.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]  # noqa: E731
    assert version(stores["classic"]) == SCHEMA_VERSION == "1"
    assert version(stores["own"]) == OWN_MEMBERS_SCHEMA_VERSION == "2"
    path = tmp_path / "index.sqlite"
    store = IndexStore.open(path, readonly=False)
    store.rebuild_package(OWN, [])
    store.close()
    _validate(path)  # this client reads it
    store = IndexStore.open(path, readonly=False)
    with store.conn:
        store.conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    store.close()
    with pytest.raises(IndexIncompatible):
        _validate(path)


def test_the_tools_answer_the_same(stores):
    coverage = Coverage(
        policy=VersionPolicy(
            since=date(2023, 1, 1), recent_months=12, include_prereleases=False, include_yanked=False
        ),
        packages=(OWN,),
    )
    classic, own = DepTime(stores["classic"], coverage), DepTime(stores["own"], coverage)
    frame = '  File "/app/.venv/lib/python3.12/site-packages/inh/models.py", line 1, in run\n'
    for error, installed in [
        ("AttributeError: 'Sub' object has no attribute 'old'", "3.0"),
        ("AttributeError: 'Sub' object has no attribute 'own'", "3.0"),
        ("TypeError: Sub.run() got an unexpected keyword argument 'c'", "2.0"),
        ("TypeError: Base.run() got an unexpected keyword argument 'b'", "2.0"),
    ]:
        tb = f"Traceback (most recent call last):\n{frame}{error}"
        a, b = classic.explain_error(tb, {"inh": installed}), own.explain_error(tb, {"inh": installed})
        assert b.findings and b.model_dump() == a.model_dump(), error
    assert (
        own.explain_error(
            f"Traceback (most recent call last):\n{frame}AttributeError: 'Sub' object has no attribute 'old'",
            {"inh": "3.0"},
        )
        .findings[0]
        .issue
        == "symbol_removed"
    )
    for path in ("Sub.get", "Sub.mix", "Sub.run", "Other.go", "Sub"):
        assert (
            own.symbol_history("inh", path).model_dump() == classic.symbol_history("inh", path).model_dump()
        ), path
    queries = [
        SymbolQuery(path="Sub.run", kwargs=["c"]),
        SymbolQuery(path="Sub.mix"),
        SymbolQuery(path="Sub.get"),
    ]
    for version in RELEASES:
        assert own.check_symbols("inh", version, queries) == classic.check_symbols("inh", version, queries)
    diff = own.diff_versions("inh", "1.0", "3.0", used_symbols=["inh.Sub.get"])
    assert [(e.path, e.kind, e.version) for e in diff.events] == [
        ("inh._hidden._Bridge.get", "deprecated", "2.0")
    ]


def test_a_suffix_search_reads_members_by_class(stores):
    # Without CROSS JOIN, SQLite read every member of the package before the MRO: 110 ms at p95 on transformers.
    own = stores["own"]
    sql, params = own._suffix_query("Sub.get", ["inh"])
    plan = [r[3] for r in own.conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    seeks = [line for line in plan if line.startswith("SEARCH m ")]
    assert seeks and all("cls=?" in line for line in seeks), plan
