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


def _warned(
    version: str, deprecated_params: dict[str, str] | None, deprecated: str | None = None
) -> ApiSnapshot:
    params = [
        {"name": n, "kind": "keyword-only", "default": "None", "annotation": None}
        for n in ("check", "condition")
    ]
    rec = SymbolRecord("p.C.__init__", "function", "p.C.__init__", params, deprecated, deprecated_params)
    return ApiSnapshot("p", version, ["p"], {rec.path: rec})


def test_a_name_added_already_deprecated_is_a_deprecation():
    # DT-CORE-23: matplotlib 3.9.1 restored `cm.get_cmap` deprecated; a new re-export of a deprecated function
    # carries its warnings. Neither had an event, so neither had a hint or a deprecation date.
    old = ApiSnapshot("p", "1.0", ["p"], {"p": SymbolRecord("p", "module", "p")})
    gone = SymbolRecord("p.get_cmap", "function", "p.get_cmap", [], "Use `p.colormaps` instead.")
    warned = _warned("2.0", {"check": "`check` is deprecated in favor of `.condition`."})
    new = ApiSnapshot("p", "2.0", ["p"], {**old.symbols, **warned.symbols, gone.path: gone})
    k = {(e.path, e.kind, e.detail.get("param")) for e in diff_snapshots(old, new)}
    assert {
        ("p.get_cmap", "added", None),
        ("p.get_cmap", "deprecated", None),
        ("p.C.__init__", "added", None),
        ("p.C.__init__", "param_deprecated", "check"),
    } <= k


def test_argument_deprecations_are_events_of_their_own():
    # DT-DATA-42: a warning that deprecates one argument is an event for that argument, with its hint, and the
    # function stays undeprecated.
    msg = "CheckConstraint.check is deprecated in favor of `.condition`."
    first = initial_events(_warned("5.1", {"check": msg}))
    assert [(e.kind, e.detail["param"], e.detail["alternatives"]) for e in first] == [
        ("param_deprecated", "check", [{"target": "condition", "confidence": 0.8}])
    ]
    assert diff_snapshots(_warned("5.1", {"check": msg}), _warned("5.2", {"check": msg})) == []  # unchanged
    later = diff_snapshots(
        _warned("5.1", {"check": msg}), _warned("5.2", {"check": msg}, "C() is deprecated.")
    )
    assert [(e.kind, e.detail.get("param")) for e in later] == [("deprecated", None)]  # only what is new


def _sig(version: str, *params: tuple[str, str]) -> ApiSnapshot:
    rec = SymbolRecord(
        "p.f",
        "function",
        "p.f",
        [{"name": n, "kind": k, "default": "None", "annotation": None} for n, k in params],
    )
    return ApiSnapshot("p", version, ["p"], {rec.path: rec})


def _renames(old: ApiSnapshot, new: ApiSnapshot) -> dict[str, str | None]:
    return {
        e.detail["param"]: e.detail.get("renamed_to")
        for e in diff_snapshots(old, new)
        if e.kind == "param_removed"
    }


def test_a_parameter_renamed_in_case_or_underscores_is_a_rename():
    # DT-CORE-24: mcp 2.0 renamed `ServerSession.elicit(requestedSchema)` to `requested_schema` without a
    # deprecation, and gradio 4.0 renamed `_js` to `js`: the removal and the addition are one rename.
    kw, pos, only = "keyword-only", "positional or keyword", "positional-only"
    old = _sig(
        "1",
        ("requestedSchema", pos),
        ("_js", kw),
        ("id", kw),
        ("obj", kw),  # made private: `_obj` is no advice
        ("name", pos),  # `__name` is positional-only by convention (PEP 484)
        ("__value", pos),  # nobody passed it by name
        ("first", pos),  # now positional-only: pass it by position, not as `First`
        ("a", only),
        ("kwargs", "variadic keyword"),
        ("fooBar", kw),  # two candidates: no evidence
    )
    new = _sig(
        "2",
        ("requested_schema", pos),
        ("js", kw),
        ("id_", kw),
        ("_obj", kw),
        ("__name", pos),
        ("value", pos),
        ("First", only),
        ("A", pos),
        ("_kwargs", "variadic keyword"),
        ("foo_bar", kw),
        ("foobar", kw),
    )
    assert _renames(old, new) == {
        "requestedSchema": "requested_schema",
        "_js": "js",
        "id": "id_",
        "obj": None,
        "name": None,
        "__value": None,
        "first": None,
        "a": None,
        "kwargs": None,
        "fooBar": None,
    }
    removed = next(e for e in diff_snapshots(old, new) if e.detail.get("param") == "obj")
    assert "renamed_to" not in removed.detail  # other events keep their detail as it was
