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
from deptime.ingest.snapshot import ApiSnapshot, SymbolRecord, build_snapshot
from deptime.models import SymbolQuery
from deptime.query import DepTime

CLASSIC = PackageSpec(name="inh", import_names=("inh",), tier="A", own_members=False)
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
    # A private base: its public names are Sub's members, its private ones internal (DT-DATA-46); its module binds
    # it, so it has an MRO of its own.
    assert first.classes["inh._hidden._Bridge"].mro == []
    assert set(first.classes["inh._hidden._Bridge"].members) == {"get"}
    assert set(first.classes["inh._hidden._Bridge"].internal) == {"_get_all"}


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


def test_a_member_a_class_resolves_through_another_class_is_reported_at_its_label(stores):
    # DT-DATA-49: only names a class defines were compared, so a subclass's inherited name that changed because the
    # subclass's chain of bases changed had no stored event, for diff_versions or for hints.
    own, classic = stores["own"], stores["classic"]
    stored = {(r[0], r[1], r[2]) for r in own.conn.execute("SELECT path, ordinal, kind FROM events")}
    assert ("inh.Sub.mix", 2, "removed") in stored  # Sub stops inheriting Mixin in 3.0
    assert ("inh.Mixin.mix", 2, "removed") not in stored  # Mixin keeps it
    # Not repeated under subclasses when the base that defines it changes: that's the base's own history.
    assert not [
        e for e in stored if e[0].startswith("inh.Sub.") and e[0].endswith((".get", ".fetch", ".old"))
    ]
    severity = {"removed": 0, "param_removed": 1, "deprecated": 2, "added": 9}
    rows, _ = own.events_between("inh", 1, 2, 50, severity=severity, paths=["inh.Sub.mix"])
    assert ("inh.Sub.mix", "removed") in {(r["path"], r["kind"]) for r in rows}
    rows, _ = classic.events_between("inh", 1, 2, 50, severity=severity, paths=["inh.Sub.mix"])
    assert ("inh.Sub.mix", "removed") in {(r["path"], r["kind"]) for r in rows}


# gradio 3 -> 4: `ImageMask` inherited `change` from `Changeable`; in 4.x it subclasses `ImageEditor`, which defines its
# own `change`, and the argument `_js` became `js`.
REHOMED = {
    "1.0": {
        "gr/__init__.py": "from gr.components import ImageMask\n__all__ = ['ImageMask']\n",
        "gr/events.py": "class Changeable:\n    def change(self, fn, _js=None):\n        pass\n",
        "gr/components.py": "from gr.events import Changeable\n\nclass ImageMask(Changeable):\n    pass\n",
    },
    "2.0": {
        "gr/__init__.py": "from gr.components import ImageMask\n__all__ = ['ImageMask']\n",
        "gr/editor.py": "class ImageEditor:\n    def change(self, fn, js=None):\n        pass\n",
        "gr/components.py": "from gr.editor import ImageEditor\n\nclass ImageMask(ImageEditor):\n    pass\n",
    },
}


def test_a_rename_under_a_new_defining_base_gets_its_hint(tmp_path):
    spec = PackageSpec(name="gr", import_names=("gr",), tier="A")
    found = {}
    for own in (False, True):
        snaps = []
        for version, files in REHOMED.items():
            root = tmp_path / f"{own}-{version}"
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            snaps.append(build_snapshot("gr", version, ["gr"], root, own_members=own))
        store = IndexStore.memory()
        store.rebuild_package(spec if own else CLASSIC_GR, snaps)
        found[own] = [(r["path"], r["target"]) for r in store.param_replacements("gr", "gr.ImageMask.change")]
    assert found[True] == found[False] == [("gr.ImageMask.change(_js)", "js")]


CLASSIC_GR = PackageSpec(name="gr", import_names=("gr",), tier="A", own_members=False)


def test_diffing_versions_finds_a_used_members_events_at_its_own_path(stores):
    # DT-CORE-33: a used member's events were its defining class's, at that class's label (`inh._hidden._Bridge.get`),
    # so a caller matching events to the paths it uses found none; the classic store has them at the member's path.
    own, classic = stores["own"], stores["classic"]
    severity = {"removed": 0, "param_removed": 1, "deprecated": 2, "added": 9}
    for paths in (
        ["inh.Sub.get"],
        ["inh.Sub.get", "inh.Sub.mix", "inh.Sub.run"],
        ["inh.Sub.mix", "inh.Base.old"],
    ):
        for after, upto in ((0, 2), (0, 1), (1, 2)):
            got = own.events_between("inh", after, upto, 50, severity=severity, paths=paths)
            want = classic.events_between("inh", after, upto, 50, severity=severity, paths=paths)
            assert sorted((r["path"], r["ordinal"], r["kind"], r["detail"]) for r in got[0]) == sorted(
                (r["path"], r["ordinal"], r["kind"], r["detail"]) for r in want[0]
            ), (paths, after, upto)
            assert got[1] == want[1] == len(got[0]), (paths, after, upto)
    rows, _ = own.events_between("inh", 0, 2, 50, severity=severity, paths=["inh.Sub.get"])
    assert [(r["path"], r["kind"]) for r in rows] == [("inh.Sub.get", "deprecated")]
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
    assert [(e.path, e.kind, e.version) for e in diff.events] == [("inh.Sub.get", "deprecated", "2.0")]


def test_a_suffix_search_reads_members_by_class(stores):
    # Without CROSS JOIN, SQLite read every member of the package before the MRO: 110 ms at p95 on transformers.
    own = stores["own"]
    sql, params = own._suffix_query("Sub.get", ["inh"])
    plan = [r[3] for r in own.conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    seeks = [line for line in plan if line.startswith("SEARCH m ")]
    assert seeks and all("cls=?" in line for line in seeks), plan


def test_an_index_with_members_keyed_by_class_first_is_rekeyed_by_name(built, tmp_path):
    # DT-DATA-49: a bare name (an error's attribute, searched in every package) read every member row of a package
    # when the key began (package, cls). The published index has such a table; ingesting into it rekeys it.
    path = tmp_path / "index.sqlite"
    store = IndexStore.open(path, readonly=False)
    store.rebuild_package(OWN, built["own"])
    rows = sorted(store.conn.execute("SELECT * FROM members").fetchall(), key=tuple)
    with store.conn:
        store.conn.execute("ALTER TABLE members RENAME TO new_members")
        store.conn.execute(
            "CREATE TABLE members (package TEXT NOT NULL, cls TEXT NOT NULL, name TEXT NOT NULL,"
            " first_ordinal INTEGER NOT NULL, last_ordinal INTEGER NOT NULL, kind TEXT NOT NULL, canonical TEXT,"
            " params TEXT, deprecated TEXT, deprecated_params TEXT,"
            " PRIMARY KEY (package, cls, name, first_ordinal)) WITHOUT ROWID"
        )
        store.conn.execute("INSERT INTO members SELECT * FROM new_members")
        store.conn.execute("DROP TABLE new_members")
    store.close()
    reader = IndexStore.open(path)  # a reader leaves the file as it is
    assert reader.find_by_suffix(["inh"], "fetch") == [("inh", "inh.Base.fetch")]
    reader.close()
    again = IndexStore.open(path, readonly=False)
    key = [r[1] for r in sorted(again.conn.execute("PRAGMA table_info(members)"), key=lambda r: r[5]) if r[5]]
    assert key == ["package", "name", "cls", "first_ordinal"]
    assert sorted(again.conn.execute("SELECT * FROM members").fetchall(), key=tuple) == rows
    sql, params = again._suffix_query("fetch", ["inh"])
    plan = " | ".join(r[3] for r in again.conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    assert "SEARCH m USING PRIMARY KEY (package=? AND name=?)" in plan, plan


# weaviate 4.6 -> 4.7: the client's `collections` is a private `_Collections`, which inherited `_get_all` from
# `_CollectionsBase` until 4.7.0 made it a wrapper. `Client._transport` is a public class's private member.
PRIVATE = {
    "1.0": {
        "wv/__init__.py": "from wv.client import Client\n__all__ = ['Client']\n",
        "wv/client.py": (
            "from wv.collections import _Collections\n\n"
            "class Client:\n"
            "    def __init__(self):\n        self.collections = _Collections()\n"
            "    def _transport(self):\n        pass\n"
            "    def __enter__(self):\n        return self\n"
        ),
        "wv/collections.py": (
            "class _CollectionsBase:\n"
            "    def _get_all(self, simple):\n        pass\n"
            "    def list_all(self):\n        pass\n\n"
            "class _Collections(_CollectionsBase):\n"
            "    def get(self, name):\n        pass\n"
        ),
    },
    "2.0": {
        "wv/__init__.py": "from wv.client import Client\n__all__ = ['Client']\n",
        "wv/client.py": (
            "from wv.collections import _Collections\n\n"
            "class Client:\n"
            "    def __init__(self):\n        self.collections = _Collections()\n"
            "    def _transport(self):\n        pass\n"
        ),
        "wv/collections.py": (
            "class _Collections:\n"
            "    def get(self, name):\n        pass\n"
            "    def list_all(self):\n        pass\n"
        ),
    },
}
WV = PackageSpec(name="wv", import_names=("wv",), tier="A")
POLICY = VersionPolicy(
    since=date(2023, 1, 1), recent_months=12, include_prereleases=False, include_yanked=False
)


def _private_store(tmp_path: Path) -> tuple[IndexStore, list[ApiSnapshot]]:
    snaps = []
    for version, files in PRIVATE.items():
        root = tmp_path / version
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        snaps.append(build_snapshot("wv", version, ["wv"], root, own_members=True))
    store = IndexStore.memory()
    store.rebuild_package(WV, snaps)
    return store, snaps


def test_private_members_and_private_classes_are_recorded_as_internal(tmp_path):
    # DT-DATA-46: only a class's public members were recorded, so `_Collections._get_all` read never_existed at low.
    _store, (first, second) = _private_store(tmp_path)
    client = first.classes["wv.client.Client"]
    assert set(client.members) == {"__init__", "collections"}
    assert set(client.internal) == {"_transport"}  # not `__enter__`
    collections = first.classes["wv.collections._Collections"]
    assert collections.mro == ["wv.collections._CollectionsBase"]
    assert collections.members == {} and set(collections.internal) == {"get"}
    assert set(first.classes["wv.collections._CollectionsBase"].internal) == {"_get_all", "list_all"}
    assert second.classes["wv.collections._Collections"].mro == []
    # Kind and canonical path only, as other internal names; nothing changes in the public API.
    assert first.classes["wv.collections._CollectionsBase"].internal["_get_all"] == SymbolRecord(
        "wv.collections._CollectionsBase._get_all", "function", "wv.collections._CollectionsBase._get_all"
    )
    assert {p for p in first.expanded() if p.startswith("wv.Client.")} == {
        "wv.Client.__init__",
        "wv.Client.collections",
    }
    assert again_loaded(first, tmp_path).classes == first.classes


def again_loaded(snap: ApiSnapshot, tmp_path: Path) -> ApiSnapshot:
    snap.save(tmp_path / "snap.json.gz")
    return ApiSnapshot.load(tmp_path / "snap.json.gz")


def test_a_private_classs_inherited_member_reads_removed(tmp_path):
    store, _snaps = _private_store(tmp_path)
    dt = DepTime(store, Coverage(POLICY, (WV,)))
    trace = (
        "Traceback (most recent call last):\n"
        '  File "/app/haystack_integrations/document_stores/weaviate/document_store.py", line 206, in client\n'
        "    self._client.collections._get_all(simple=True)\n"
        "AttributeError: '_Collections' object has no attribute '_get_all'"
    )
    f = dt.explain_error(trace, {"wv": "2.0"}).findings[0]
    assert (f.symbol, f.issue, f.last_version_with, f.internal) == (
        "wv.collections._Collections._get_all",
        "symbol_removed",
        "1.0",
        True,
    )
    assert (
        "wv.collections._Collections is a class that isn't public API" not in f.explanation
    )  # a private name
    # A public name of a private class is internal too, found through the class it inherits it from in 1.0.
    spans = store.internal_spans("wv", "wv.collections._Collections.list_all")
    assert [(r["first_ordinal"], r["last_ordinal"], r["canonical"]) for r in spans] == [
        (0, 0, "wv.collections._CollectionsBase.list_all"),
        (1, 1, "wv.collections._Collections.list_all"),
    ]
    (check,) = dt.check_symbols("wv", "1.0", [SymbolQuery(path="wv.Client._transport")]).results
    assert (check.status, check.internal) == ("present", True)
    assert "`_transport` is a private name" in check.hint


def test_a_classs_private_members_never_read_as_its_public_api(tmp_path):
    store, _snaps = _private_store(tmp_path)
    assert store.spans("wv", "wv.Client._transport") == []
    assert store.find_by_suffix(["wv"], "_transport") == []
    assert store.find_by_suffix(["wv"], "Client._transport") == []
    assert store.packages_with_suffix("_get_all") == []
    # No events: what diff_versions lists is public API.
    rows = store.conn.execute("SELECT path FROM events").fetchall()
    assert not [r["path"] for r in rows if "_get_all" in r["path"] or "_transport" in r["path"]]
    # A member no class it resolves through has stays unknown to the index, at medium confidence.
    dt = DepTime(store, Coverage(POLICY, (WV,)))
    f = dt._symbol_finding(WV, "wv.Client._session", *store.resolve_version("wv", "2.0"), "2.0")
    assert (f.issue, f.confidence) == ("never_existed", "medium")
    assert "doesn't index the private members" not in f.explanation


def test_a_bare_name_defined_on_a_private_base_is_found_under_its_public_subclasses(stores):
    # DT-DATA-49: matched at the defining class's label, `get` came back as `inh._hidden._Bridge.get`, which no
    # release has as a public path, so a finding on it read never_existed.
    own = [p for _pkg, p in stores["own"].find_by_suffix(["inh"], "get")]
    assert own and all(not p.startswith("inh._hidden") for p in own), own
    assert set(own) <= {p for _pkg, p in stores["classic"].find_by_suffix(["inh"], "get")}
