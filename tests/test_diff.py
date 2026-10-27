from deptime.ingest.diff import diff_snapshots, initial_events
from deptime.ingest.snapshot import ApiSnapshot, SymbolRecord
from tests.conftest import SNAPSHOTS


def _kinds(events):
    return {(e.path, e.kind, e.detail.get("param")) for e in events}


def test_initial_events_capture_preexisting_param_deprecation():
    events = initial_events(SNAPSHOTS[0])
    dep = [e for e in events if e.kind == "param_deprecated"]
    assert dep and dep[0].path == "httpx.Client.__init__" and dep[0].detail["param"] == "proxies"
    assert [a["target"] for a in dep[0].detail["alternatives"]] == ["proxy", "mounts"]
    assert any(e.path == "httpx.OldThing" and e.kind == "deprecated" for e in events)


def test_removed_and_added_symbols():
    k = _kinds(diff_snapshots(SNAPSHOTS[0], SNAPSHOTS[1]))
    assert ("httpx.OldThing", "removed", None) in k
    assert ("httpx.NewThing", "added", None) in k


def test_param_removed_between_027_and_028():
    events = diff_snapshots(SNAPSHOTS[1], SNAPSHOTS[2])
    k = _kinds(events)
    assert ("httpx.Client.__init__", "param_removed", "proxies") in k
    assert ("httpx.request", "param_removed", "proxies") in k
    removed = next(e for e in events if e.kind == "param_removed" and e.path == "httpx.Client.__init__")
    assert removed.detail["absorbed_by_var_keyword"] is False


def _f(version: str, **x_param) -> ApiSnapshot:
    params = [{"name": "x", "kind": "keyword-only", "annotation": None, **x_param}]
    rec = SymbolRecord("fp.f", "function", "fp.f", params)
    return ApiSnapshot("fp", version, ["fp"], {"fp.f": rec})


def test_default_removed_or_added_is_a_change():
    # I-10: f(*, x=None) -> f(*, x) is a breaking change even though `x` exists in both releases.
    now_required = diff_snapshots(_f("1.0", default="None"), _f("2.0", default=None))
    assert [(e.kind, e.detail) for e in now_required] == [
        ("param_now_required", {"param": "x", "default": "None"})
    ]
    now_optional = diff_snapshots(_f("2.0", default=None), _f("3.0", default="0"))
    assert [(e.kind, e.detail) for e in now_optional] == [
        ("param_now_optional", {"param": "x", "default": "0"})
    ]
    assert (
        diff_snapshots(_f("1.0", default="None"), _f("2.0", default="1")) == []
    )  # a new default value is not


def _snap(version: str, *records: tuple[str, str]) -> ApiSnapshot:
    return ApiSnapshot("p", version, ["p"], {path: SymbolRecord(path, kind, path) for path, kind in records})


def _moves(old: ApiSnapshot, new: ApiSnapshot) -> set[tuple[str, str]]:
    return {(e.path, e.detail["to"]) for e in diff_snapshots(old, new) if e.kind == "moved"}


def test_moved_never_pairs_class_members_by_leaf():
    # Q-05: `Foo.close` disappearing while `Bar.close` appears is not a move.
    old = _snap("1", ("p", "module"), ("p.Foo", "class"), ("p.Foo.close", "function"))
    new = _snap("2", ("p", "module"), ("p.Foo", "class"), ("p.Bar", "class"), ("p.Bar.close", "function"))
    assert _moves(old, new) == set()


def test_moved_module_level_symbol_to_new_module():
    old = _snap("1", ("p", "module"), ("p.old", "module"), ("p.old.Thing", "class"))
    new = _snap("2", ("p", "module"), ("p.new", "module"), ("p.new.Thing", "class"))
    assert _moves(old, new) == {("p.old.Thing", "p.new.Thing")}


def test_moved_too_many_candidates_is_no_evidence():
    mods = [(f"p.m{i}", "module") for i in range(4)] + [(f"p.m{i}.run", "function") for i in range(4)]
    old = _snap("1", ("p", "module"), ("p.run", "function"))
    new = _snap("2", ("p", "module"), *mods)
    assert _moves(old, new) == set()
