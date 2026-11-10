"""A package stored with its classes' own members (DT-DATA-16): every answer the same as storing every member under
every class, from far fewer rows."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from deptime.config import Coverage, PackageSpec, ServedRule, VersionPolicy
from deptime.index.distribution import IndexIncompatible, _validate
from deptime.index.store import OWN_MEMBERS_SCHEMA_VERSION, SCHEMA_VERSION, IndexStore
from deptime.ingest.curated import CuratedHint
from deptime.ingest.snapshot import ApiSnapshot, SymbolRecord, build_snapshot
from deptime.models import SymbolQuery
from deptime.query import SEVERITY, DepTime

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


# DT-DATA-45: sqlalchemy 1.4's `types.py` imported `NullType` and left it out of `__all__`; 2.0 re-exports it with
# `as`. `helper` is imported in both and public in neither.
REEXPORT = {
    "1.0": {
        "sa/__init__.py": "",
        "sa/sqltypes.py": "class NullType:\n    def compare(self, other):\n        pass\n\n\ndef helper():\n    pass\n",
        "sa/types.py": (
            "from sa.sqltypes import NullType, helper\n__all__ = ['Other']\n\n\nclass Other:\n    pass\n"
        ),
    },
    "2.0": {
        "sa/__init__.py": "",
        "sa/sqltypes.py": "class NullType:\n    def compare(self, other):\n        pass\n\n\ndef helper():\n    pass\n",
        "sa/types.py": (
            "from sa.sqltypes import NullType as NullType\nfrom sa.sqltypes import helper\n\n\nclass Other:\n    pass\n"
        ),
    },
}
SA = PackageSpec(name="sa", import_names=("sa",), tier="B")


def _snapshots(tmp_path: Path, releases: dict, package: str, served=()) -> list[ApiSnapshot]:
    snaps = []
    for version, files in releases.items():
        root = tmp_path / version
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        snaps.append(build_snapshot(package, version, [package], root, own_members=True, served=served))
    return snaps


def test_a_name_a_module_binds_counts_public_where_another_release_has_it_public(tmp_path):
    first, second = _snapshots(tmp_path, REEXPORT, "sa")
    assert "sa.types.NullType" in first.bound and "sa.types.NullType" in second.symbols
    store = IndexStore.memory()
    store.rebuild_package(SA, [first, second])
    spans = store.spans("sa", "sa.types.NullType")
    assert [(r["first_ordinal"], r["last_ordinal"], r["kind"]) for r in spans] == [(0, 1, "class")]
    assert not [
        e for e in store.events("sa", "sa.types.NullType") if e["kind"] == "added" and e["ordinal"] == 1
    ]
    assert store.internal_spans("sa", "sa.types.NullType") == []  # public now, so not also internal
    # Never public anywhere: it stays an internal name.
    assert store.spans("sa", "sa.types.helper") == []
    assert store.internal_spans("sa", "sa.types.helper")


# DT-DATA-52: transformers 5 serves `BertTokenizerFast` as `BertTokenizer` from `_LazyModule.__getattr__`, and the
# legacy module `tokenization_utils` as `tokenization_utils_sentencepiece`; the rules hold where `when` matches.
SERVING = {
    "4.0": {
        "tok/__init__.py": (
            "from tok.old_utils import Base\nfrom tok.fast import BertTokenizerFast\nfrom tok.slow import BertTokenizer\n"
        ),
        "tok/old_utils.py": "class Base:\n    def encode(self, text):\n        pass\n",
        "tok/fast.py": (
            "class BertTokenizerFast:\n"
            "    def encode(self, text):\n        pass\n"
            "    def fast_only(self):\n        pass\n"
        ),
        "tok/slow.py": "class BertTokenizer:\n    def encode(self, text):\n        pass\n",
        "tok/lazy.py": "def __getattr__(name):\n    raise AttributeError(name)\n",
    },
    "5.0": {
        "tok/__init__.py": (
            "from tok.new_utils import Base\nfrom tok.slow import BertTokenizer\n"
            "_LEGACY_MODULE_ALIASES = {'old_utils': 'new_utils'}\n"
        ),
        "tok/new_utils.py": "class Base:\n    def encode(self, text):\n        pass\n",
        "tok/slow.py": (
            "class BertTokenizer:\n    def encode(self, text):\n        pass\n\n\n"
            "class XLMTokenizer:\n    pass\n"
        ),
        "tok/lazy.py": (
            "def __getattr__(name):\n"
            "    if name.endswith('TokenizerFast'):\n"
            "        return globals()[name[:-4]]\n"
            "    raise AttributeError(name)\n"
        ),
    },
}
SERVED_RULES = (
    ServedRule(
        file="tok/lazy.py",
        when=r"endswith\('TokenizerFast'\)",
        modules=("tok",),
        ending="Tokenizer",
        add="Fast",
    ),
    ServedRule(file="tok/__init__.py", when="'new_utils'", module="tok.old_utils", serves="tok.new_utils"),
)
TOK = PackageSpec(name="tok", import_names=("tok",), tier="A", served=SERVED_RULES)


def test_names_a_package_serves_count_public_where_another_release_has_them_public(tmp_path):
    old, new = _snapshots(tmp_path, SERVING, "tok", SERVED_RULES)
    assert old.bound == {}  # neither `when` matches 4.0, and its names are real there
    assert (new.bound["tok.BertTokenizerFast"].kind, new.bound["tok.BertTokenizerFast"].canonical) == (
        "class",
        "tok.slow.BertTokenizer",
    )
    assert new.bound["tok.old_utils"].canonical == "tok.new_utils"
    assert new.bound["tok.old_utils.Base"].canonical == "tok.new_utils.Base"
    assert new.served == [r.as_dict() for r in SERVED_RULES]
    store = IndexStore.memory()
    store.rebuild_package(TOK, [old, new])
    for path in ("tok.BertTokenizerFast", "tok.old_utils", "tok.old_utils.Base"):
        spans = store.spans("tok", path)
        assert [o for r in spans for o in range(r["first_ordinal"], r["last_ordinal"] + 1)] == [0, 1], path
    # Served, but never public API: `XLMTokenizerFast` never existed as a name of its own.
    assert store.spans("tok", "tok.XLMTokenizerFast") == []
    # The served class's members are compared with the old class's under the old label: `encode` stays.
    rows, _total = store.events_between("tok", 0, 1, 100, severity=SEVERITY)
    removed = {e["path"] for e in rows if e["kind"] == "removed"}
    assert removed == {"tok.BertTokenizerFast.fast_only", "tok.fast", "tok.fast.BertTokenizerFast"}
    dt = DepTime(store, Coverage(POLICY, (TOK,)))
    (check,) = dt.check_symbols("tok", "5.0", [SymbolQuery(path="tok.BertTokenizerFast.encode")]).results
    assert check.status == "present"


# DT-DATA-58: transformers 5's `_LazyModule.__getattr__` serves a tokenizer name it can't find through
# `SLOW_TO_FAST_CONVERTERS`: `ElectraTokenizer` and `ElectraTokenizerFast` as the tokenizer named after the key's
# converter (`BertConverter` -> `BertTokenizer`), from the module asked or the top level, else another key's.
CONVERTING = {
    "4.0": {
        "tok/__init__.py": (
            "from tok.electra import ElectraTokenizer, ElectraTokenizerFast\nfrom tok.slow import BertTokenizer\n"
            "from tok.x import XTokenizer\nfrom tok.fnet import FNetTokenizer\n"
        ),
        "tok/electra.py": "class ElectraTokenizer:\n    pass\n\n\nclass ElectraTokenizerFast:\n    pass\n",
        "tok/slow.py": "class BertTokenizer:\n    pass\n",
        "tok/x.py": "class XTokenizer:\n    pass\n",
        "tok/fnet.py": "class FNetTokenizer:\n    pass\n",
        "tok/models/__init__.py": "",
        "tok/models/electra/__init__.py": "from tok.electra import ElectraTokenizer\n",
        "tok/lazy.py": "def __getattr__(name):\n    raise AttributeError(name)\n",
    },
    "5.0": {
        "tok/__init__.py": "from tok.slow import BertTokenizer\nfrom tok.y import YTokenizer\n",
        "tok/slow.py": "class BertTokenizer:\n    pass\n",
        "tok/y.py": "class YTokenizer:\n    pass\n",
        "tok/models/__init__.py": "",
        "tok/models/electra/__init__.py": "",
        "tok/convert.py": (
            "SLOW_TO_FAST_CONVERTERS = {\n"
            "    'BertTokenizer': BertConverter,\n"
            "    'ElectraTokenizer': BertConverter,\n"
            "    'FNetTokenizer': AlbertConverter,\n"
            "    'XTokenizer': OddConverter,\n"
            "    'YTokenizer': OddConverter,\n"
            "}\n"
        ),
        "tok/lazy.py": (
            "def __getattr__(name):\n"
            "    from tok.convert import SLOW_TO_FAST_CONVERTERS\n"
            "    raise AttributeError(name)\n"
        ),
    },
}
CONVERTER_RULE = ServedRule(
    file="tok/lazy.py",
    when="SLOW_TO_FAST_CONVERTERS",
    modules=("tok", "tok.models.{model}"),
    table="tok/convert.py:SLOW_TO_FAST_CONVERTERS",
    value="{base}Converter",
    target="{base}Tokenizer",
    add="Fast",
)


def test_names_served_through_a_literal_table_follow_the_tokenizer_its_value_names(tmp_path):
    old, new = _snapshots(tmp_path, CONVERTING, "tok", (CONVERTER_RULE,))
    assert old.bound == {}  # `when` doesn't match 4.0
    canonical = {path: rec.canonical for path, rec in new.bound.items()}
    for path in ("tok.ElectraTokenizer", "tok.ElectraTokenizerFast", "tok.models.electra.ElectraTokenizer"):
        assert canonical[path] == "tok.slow.BertTokenizer", path
    # No `OddTokenizer`: the other key with the same converter.
    assert canonical["tok.XTokenizer"] == "tok.y.YTokenizer"
    # No `AlbertTokenizer` and no other key with `AlbertConverter`: not served.
    assert "tok.FNetTokenizer" not in canonical
    assert "tok.BertTokenizer" not in new.bound  # bound by the module itself
    spec = PackageSpec(name="tok", import_names=("tok",), tier="A", served=(CONVERTER_RULE,))
    store = IndexStore.memory()
    store.rebuild_package(spec, [old, new])
    for path in ("tok.ElectraTokenizer", "tok.ElectraTokenizerFast", "tok.models.electra.ElectraTokenizer"):
        spans = store.spans("tok", path)
        assert [o for r in spans for o in range(r["first_ordinal"], r["last_ordinal"] + 1)] == [0, 1], path
    assert [(r["first_ordinal"], r["last_ordinal"]) for r in store.spans("tok", "tok.FNetTokenizer")] == [
        (0, 0)
    ]
    # Served, but never public API there.
    assert store.spans("tok", "tok.models.electra.ElectraTokenizerFast") == []
    assert store.spans("tok", "tok.YTokenizerFast") == []


def test_a_parameter_hint_written_obj_dot_x_is_the_classs_method(tmp_path):
    # DT-CORE-44: "Use obj.ffill() or obj.bfill() instead" for `NDFrame.fillna(method)` gives `NDFrame.ffill`.
    releases = {
        "2.2": {
            "frm/__init__.py": "from frm.generic import NDFrame\n",
            "frm/generic.py": (
                "import warnings\n\n\nclass NDFrame:\n"
                "    def fillna(self, value=None, method=None):\n"
                "        if method is not None:\n"
                "            warnings.warn(\n"
                "                f\"{type(self).__name__}.fillna with 'method' is deprecated and will raise in a future \"\n"
                '                "version. Use obj.ffill() or obj.bfill() instead.",\n'
                "                FutureWarning,\n"
                "            )\n\n"
                "    def ffill(self):\n        pass\n\n    def bfill(self):\n        pass\n"
            ),
        },
    }
    spec = PackageSpec(name="frm", import_names=("frm",), tier="B")
    store = IndexStore.memory()
    store.rebuild_package(spec, _snapshots(tmp_path, releases, "frm"))
    rows = [(r["target"], r["confidence"]) for r in store.replacements("frm", "frm.NDFrame.fillna(method)")]
    assert rows == [("frm.NDFrame.ffill", 0.8), ("frm.NDFrame.bfill", 0.79)]


def test_a_private_member_of_a_class_no_package_has_is_looked_up_in_internal_members(tmp_path):
    # DT-CORE-42: gplearn's `SymbolicRegressor.fit` calls `self._validate_data`, which scikit-learn 1.7 removed from
    # `BaseEstimator`; the error names gplearn's class, which no covered package has.
    releases = {
        "1.6": {
            "est/__init__.py": "from est.base import BaseEstimator, Other\n",
            "est/base.py": (
                "class BaseEstimator:\n    def _validate_data(self, X, y=None):\n        pass\n\n"
                "    def _more_tags(self):\n        pass\n\n\n"
                "class Other:\n    def _more_tags(self):\n        pass\n\n    def _check(self):\n        pass\n"
            ),
        },
        "1.7": {
            "est/__init__.py": "from est.base import BaseEstimator, Other\n",
            "est/base.py": "class BaseEstimator:\n    pass\n\n\nclass Other:\n    def _check(self):\n        pass\n",
        },
    }
    spec = PackageSpec(name="est", import_names=("est",), tier="B")
    store = IndexStore.memory()
    store.rebuild_package(spec, _snapshots(tmp_path, releases, "est"))
    dt = DepTime(store, Coverage(POLICY, (spec,)))

    def explain(member: str, installed: dict[str, str]) -> list:
        tb = f"AttributeError: 'SymbolicRegressor' object has no attribute '{member}'"
        return dt.explain_error(tb, installed).findings

    (f,) = explain("_validate_data", {"est": "1.7"})
    assert (f.package, f.symbol, f.issue, f.last_version_with) == (
        "est",
        "est.BaseEstimator._validate_data",
        "symbol_removed",
        "1.6",
    )
    assert (
        f.internal
        and f.confidence == "medium"
        and "only `est.BaseEstimator` had `_validate_data`" in f.explanation
    )
    assert explain("_more_tags", {"est": "1.7"}) == []  # two classes had it: which one is a guess
    assert explain("_check", {"est": "1.7"}) == []  # still there
    assert explain("_validate_data", {}) == []  # no installed release to compare with


def test_a_name_imported_from_the_standard_library_reads_present_and_internal(tmp_path):
    # DT-DATA-61: pydantic's `networks` imports `IPv4Address` from `ipaddress`, and code uses it from there; a name no
    # import explains still reads missing, and names for annotations aren't recorded.
    releases = {
        "2.0": {
            "net/__init__.py": "from net.networks import IPvAnyAddress\n",
            "net/networks.py": (
                "import os\nfrom collections.abc import Mapping\nfrom ipaddress import IPv4Address\n"
                "from typing import Any\nfrom typing_extensions import Literal\n\n\n"
                "class IPvAnyAddress:\n    pass\n\n\n__all__ = ['IPvAnyAddress']\n"
            ),
        }
    }
    (snap,) = _snapshots(tmp_path, releases, "net")
    internal = {p: (r.kind, r.canonical) for p, r in snap.internal.items() if p.startswith("net.networks.")}
    assert internal == {"net.networks.IPv4Address": ("alias", "ipaddress.IPv4Address")}
    spec = PackageSpec(name="net", import_names=("net",), tier="A")
    store = IndexStore.memory()
    store.rebuild_package(spec, [snap])
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    queries = [
        SymbolQuery(path="net.networks.IPv4Address", positional=1),
        SymbolQuery(path="net.networks.Nope"),
    ]
    found, missing = dt.check_symbols("net", "2.0", queries).results
    assert (found.status, found.internal) == ("present", True)
    assert found.hint == "present, but not public API: net.networks imports it from ipaddress for its own use"
    assert missing.status == "missing"


# DT-DATA-60: pydantic 2 keeps v1's modules as `__getattr__ = getattr_migration(__name__)`, which serves the names
# `pydantic/_migration.py`'s tables list as `module:name`: `MOVED_IN_V2` with a warning, `DEPRECATED_MOVED_IN_V2`
# as the deprecated object (2.0 spreads a comprehension into it), `REDIRECT_TO_V1` from `pydantic.v1` with a warning.
MIGRATING = {
    "1.0": {
        "pyd/__init__.py": "from pyd.error_wrappers import ValidationError\nfrom pyd.tools import parse_obj_as\n",
        "pyd/error_wrappers.py": "class ValidationError(ValueError):\n    pass\n",
        "pyd/tools.py": "def parse_obj_as(type_, obj):\n    pass\n",
        "pyd/utils.py": (
            "def to_camel(s):\n    pass\n\n\ndef deep_update(mapping, *updates):\n    pass\n\n\n"
            "def lenient(cls, other):\n    pass\n\n\ndef real():\n    pass\n"
        ),
    },
    "2.0": {
        "pyd/__init__.py": "from pyd.core import ValidationError\n",
        "pyd/core.py": "class ValidationError(ValueError):\n    pass\n",
        "pyd/alias_generators.py": "def to_pascal(snake):\n    pass\n",
        "pyd/deprecated/__init__.py": "",
        "pyd/deprecated/tools.py": (
            "import warnings\n\n\ndef parse_obj_as(type_, obj):\n"
            "    warnings.warn('parse_obj_as is deprecated. Use TypeAdapter instead.', DeprecationWarning)\n"
        ),
        "pyd/v1/__init__.py": "from pyd.v1 import utils\n",
        "pyd/v1/utils.py": "def deep_update(mapping, *updates):\n    pass\n\n\ndef lenient(cls, other):\n    pass\n",
        "pyd/error_wrappers.py": "from ._migration import getattr_migration\n\n__getattr__ = getattr_migration(__name__)\n",
        "pyd/tools.py": "from ._migration import getattr_migration\n\n__getattr__ = getattr_migration(__name__)\n",
        "pyd/utils.py": (
            "from ._migration import getattr_migration\n\n\ndef real():\n    pass\n\n\n"
            "__getattr__ = getattr_migration(__name__)\n"
        ),
        "pyd/_migration.py": (
            "MOVED_IN_V2 = {\n"
            "    'pyd.error_wrappers:ValidationError': 'pyd:ValidationError',\n"
            "    'pyd.utils:to_camel': 'pyd.alias_generators:to_pascal',\n"
            "    'pyd.utils:real': 'pyd.alias_generators:to_pascal',\n"
            "    'pyd.gone:Thing': 'pyd:ValidationError',\n"
            "    'pyd.utils:nowhere': 'pyd.alias_generators:missing',\n"
            "}\n"
            "DEPRECATED_MOVED_IN_V2 = {\n"
            "    'pyd.tools:parse_obj_as': 'pyd.deprecated.tools:parse_obj_as',\n"
            "    **{f'pyd.utils:{obj}': f'pyd.v1.utils:{obj}' for obj in ('lenient',)},\n"
            "}\n"
            "REDIRECT_TO_V1 = {f'pyd.utils:{obj}': f'pyd.v1.utils:{obj}' for obj in ('deep_update',)}\n"
        ),
    },
}
MIGRATION_RULES = (
    ServedRule(
        file="pyd/_migration.py",
        when=r"(?m)^MOVED_IN_V2\b",
        table="pyd/_migration.py:MOVED_IN_V2",
        message="`{key}` has been moved to `{value}`.",
    ),
    ServedRule(
        file="pyd/_migration.py",
        when=r"(?m)^DEPRECATED_MOVED_IN_V2\b",
        table="pyd/_migration.py:DEPRECATED_MOVED_IN_V2",
    ),
    ServedRule(
        file="pyd/_migration.py",
        when=r"(?m)^REDIRECT_TO_V1\b",
        table="pyd/_migration.py:REDIRECT_TO_V1",
        message="`{key}` has been removed. We are importing from `{value}` instead.",
    ),
)
PYD = PackageSpec(name="pyd", import_names=("pyd",), tier="A", served=MIGRATION_RULES)


def test_names_served_through_a_table_of_import_paths_read_present_and_deprecated(tmp_path):
    old, new = _snapshots(tmp_path, MIGRATING, "pyd", MIGRATION_RULES)
    assert old.bound == {}  # no `_migration.py` in 1.0
    bound = {path: (rec.canonical, rec.deprecated) for path, rec in new.bound.items()}
    assert bound["pyd.error_wrappers.ValidationError"] == (
        "pyd.core.ValidationError",
        "`pyd.error_wrappers:ValidationError` has been moved to `pyd:ValidationError`.",
    )
    assert bound["pyd.utils.to_camel"][0] == "pyd.alias_generators.to_pascal"
    assert bound["pyd.tools.parse_obj_as"] == (
        "pyd.deprecated.tools.parse_obj_as",
        "parse_obj_as is deprecated. Use TypeAdapter instead.",
    )
    assert bound["pyd.utils.lenient"] == ("pyd.v1.utils.lenient", None)  # spread in, without a warning
    assert bound["pyd.utils.deep_update"] == (
        "pyd.v1.utils.deep_update",
        "`pyd.utils:deep_update` has been removed. We are importing from `pyd.v1.utils:deep_update` instead.",
    )
    # A name the module binds never reaches `__getattr__`; no module, or nothing at the target: not served.
    assert "pyd.utils.real" not in new.bound and new.symbols["pyd.utils.real"].canonical == "pyd.utils.real"
    assert not {"pyd.gone.Thing", "pyd.utils.nowhere"} & new.bound.keys()
    store = IndexStore.memory()
    store.rebuild_package(PYD, [old, new])
    for path in ("pyd.error_wrappers.ValidationError", "pyd.utils.to_camel", "pyd.utils.deep_update"):
        spans = store.spans("pyd", path)
        assert [o for r in spans for o in range(r["first_ordinal"], r["last_ordinal"] + 1)] == [0, 1], path
    rows, _total = store.events_between("pyd", 0, 1, 100, severity=SEVERITY)
    assert not {e["path"] for e in rows if e["kind"] == "removed"} & {
        "pyd.error_wrappers.ValidationError",
        "pyd.utils.to_camel",
        "pyd.utils.deep_update",
        "pyd.tools.parse_obj_as",
    }
    dt = DepTime(store, Coverage(POLICY, (PYD,)))
    (check,) = dt.check_symbols(
        "pyd", "2.0", [SymbolQuery(path="pyd.error_wrappers.ValidationError")]
    ).results
    assert check.status == "present"
    assert check.deprecated == "`pyd.error_wrappers:ValidationError` has been moved to `pyd:ValidationError`."
    hints = {
        (r["path"], r["target"]) for r in store.replacements("pyd", "pyd.error_wrappers.ValidationError")
    }
    assert hints == {("pyd.error_wrappers.ValidationError", "pyd.ValidationError")}


@pytest.mark.parametrize(
    "fields",
    [
        {"table": "pyd/_migration.py:MOVED", "value": "{x}", "target": "{x}"},  # import paths take no others
        {"table": "pyd/_migration.py:MOVED", "add": "Fast"},
        {"table": "pyd/_migration.py", "message": "m"},  # no name
        {"modules": ("tok",), "ending": "T", "add": "F", "message": "m"},  # a message without a table
        {"modules": ("tok",), "table": "tok/c.py:T", "value": "{b}C", "target": "{b}T", "message": "m"},
    ],
)
def test_a_served_rule_with_a_table_of_import_paths_takes_only_a_message(fields):
    with pytest.raises(ValueError):
        ServedRule(file="pyd/_migration.py", when="x", **fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"modules": ("tok",), "table": "tok/convert.py:TABLE", "value": "{base}C"},  # no target
        {"modules": ("tok",), "table": "tok/convert.py:TABLE", "value": "{base}C", "target": "{other}T"},
        {"modules": ("tok",), "table": "tok/convert.py", "value": "{base}C", "target": "{base}T"},  # no name
        {"module": "tok.a", "serves": "tok.b", "table": "tok/convert.py:TABLE", "value": "C", "target": "T"},
        {"modules": ("tok",), "ending": "T", "add": "F", "value": "{base}C", "target": "{base}T"},  # no table
    ],
)
def test_a_served_rule_with_a_table_needs_its_fields(fields):
    with pytest.raises(ValueError):
        ServedRule(file="tok/lazy.py", when="x", **fields)


# DT-CORE-40: transformers 4.54's `AutoModelForVision2Seq` warns only from its classmethods that the class is
# deprecated; and pydantic-ai's `MCPServerHTTP` says to use `MCPServerSSE`, which is deprecated in turn and goes too.
WARNS = "import warnings\n\n\n"
CHAINED = {
    "1.0": {
        "tp/__init__.py": "from tp.models import Auto2Seq, AutoText, HTTPServer, SSEServer, Toolset\n",
        "tp/models.py": WARNS
        + "class AutoText:\n    pass\n\n\nclass Toolset:\n    pass\n\n\n"
        + "class Auto2Seq:\n    @classmethod\n    def from_pretrained(cls, name):\n        return cls()\n\n\n"
        + "class HTTPServer:\n    def __init__(self, url):\n        pass\n\n\n"
        + "class SSEServer:\n    def __init__(self, url):\n        pass\n",
    },
    "2.0": {
        "tp/__init__.py": "from tp.models import Auto2Seq, AutoText, HTTPServer, SSEServer, Toolset\n",
        "tp/models.py": WARNS
        + "class AutoText:\n    pass\n\n\nclass Toolset:\n    pass\n\n\n"
        + "class Auto2Seq:\n    @classmethod\n    def from_pretrained(cls, name):\n"
        + "        warnings.warn('The class `Auto2Seq` is deprecated and will be removed in v3. Please use"
        + " `AutoText` instead.', FutureWarning)\n        return cls()\n\n\n"
        + "class HTTPServer:\n    def __init__(self, url):\n"
        + "        warnings.warn('The `HTTPServer` class is deprecated, use `SSEServer` instead.', DeprecationWarning)\n\n\n"
        + "class SSEServer:\n    def __init__(self, url):\n"
        + "        warnings.warn('`SSEServer` is deprecated and will be removed in v3. Use `Toolset` instead.',"
        + " DeprecationWarning)\n",
    },
    "3.0": {
        "tp/__init__.py": "from tp.models import AutoText, Toolset\n",
        "tp/models.py": "class AutoText:\n    pass\n\n\nclass Toolset:\n    pass\n",
    },
}
TP = PackageSpec(name="tp", import_names=("tp",), tier="A")


def test_a_class_deprecated_in_its_members_words_gets_their_replacement_and_a_gone_target_is_followed(
    tmp_path,
):
    store = IndexStore.memory()
    store.rebuild_package(TP, _snapshots(tmp_path, CHAINED, "tp"))
    assert [r["target"] for r in store.replacements("tp", "tp.Auto2Seq")] == ["tp.AutoText"]
    assert [r["target"] for r in store.replacements("tp", "tp.HTTPServer")] == ["tp.SSEServer"]
    dt = DepTime(store, Coverage(POLICY, (TP,)))
    (old,) = dt.explain_error(
        "ImportError: cannot import name 'HTTPServer' from 'tp' (/x/tp/__init__.py)", {"tp": "3.0"}
    ).findings
    assert [r.target for r in old.replacements] == ["tp.Toolset", "tp.SSEServer"]
    assert old.replacements[0].evidence.startswith(
        "Followed from tp.SSEServer, which this release doesn't have:"
    )
    # 2.0 still has `SSEServer`: nothing to follow.
    (check,) = dt.check_symbols("tp", "3.0", [SymbolQuery(path="tp.HTTPServer")]).results
    assert check.status == "missing" and check.hint.endswith("; try tp.Toolset")
