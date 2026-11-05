import shutil
from datetime import date

from deptime.config import Coverage, PackageSpec, VersionPolicy
from deptime.index.store import IndexStore
from deptime.ingest.snapshot import ApiSnapshot, SymbolRecord
from deptime.models import SymbolQuery
from deptime.query import DepTime, parse_installed
from tests.conftest import HTTPX
from tests.test_tracebacks import HTTPX_TB

POLICY = VersionPolicy(
    since=date(2023, 1, 1), recent_months=12, include_prereleases=False, include_yanked=False
)


def test_explain_removed_kwarg_with_exact_installed_version(dt):
    res = dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"})
    assert res.signal.kind == "unexpected_kwarg"
    top = res.findings[0]
    assert top.symbol == "httpx.Client.__init__(proxies)"
    assert top.issue == "param_removed"
    # 0.26.0 is the earliest indexed release, so the deprecation's true start is unknown: "<=0.26.0".
    assert (top.last_version_with, top.first_version_without, top.deprecated_in) == (
        "0.27.2",
        "0.28.1",
        "<=0.26.0",
    )
    assert "in or before 0.26.0" in top.explanation
    assert top.pin_to_restore == "httpx<=0.27.2"
    assert {r.target for r in top.replacements} == {"proxy", "mounts"}
    assert top.confidence == "high"


def test_explain_unindexed_version_reports_the_window(dt):
    # 0.28.0 is not indexed: nearest lower (0.27.2) still accepts `proxies`, next indexed (0.28.1) does not.
    res = dt.explain_error(HTTPX_TB, pip_freeze="httpx==0.28.0\nidna==3.10\n")
    top = res.findings[0]
    assert (top.issue, top.resolved_version, top.confidence) == ("param_removed", "0.27.2", "medium")
    assert (top.last_version_with, top.first_version_without) == ("0.27.2", "0.28.1")
    assert "window" in top.explanation


NEW_THING_TB = "ImportError: cannot import name 'NewThing' from 'httpx' (/app/x.py)"


def test_an_addition_is_reported_between_two_indexed_releases(dt):
    # DT-CORE-18: 0.26.0 lacks NewThing and 0.27.2, the next indexed release, has it. Releases may lie in between,
    # so the answer can't say it was added in 0.27.2.
    f = dt.explain_error(NEW_THING_TB, {"httpx": "0.26.0"}).findings[0]
    assert (f.issue, f.last_version_without, f.first_version_with, f.pin_to_restore, f.confidence) == (
        "symbol_not_yet_added",
        "0.26.0",
        "0.27.2",
        "httpx>=0.27.2",
        "high",
    )
    assert "between httpx 0.26.0 and 0.27.2" in f.explanation
    assert "first appears" not in f.explanation
    # Removal fields stay empty on an addition.
    assert (f.last_version_with, f.first_version_without) == (None, None)


def test_an_addition_in_the_window_of_an_unindexed_installed_version(dt):
    # 0.27.0 isn't indexed: it resolves to 0.26.0, which lacks NewThing, and 0.27.2 has it, so 0.27.0 may too.
    f = dt.explain_error(NEW_THING_TB, {"httpx": "0.27.0"}).findings[0]
    assert (f.issue, f.resolved_version, f.confidence) == ("symbol_not_yet_added", "0.26.0", "medium")
    assert (f.last_version_without, f.first_version_with, f.pin_to_restore) == (
        "0.26.0",
        "0.27.2",
        "httpx>=0.27.2",
    )
    assert "your 0.27.0 is in that window and not indexed" in f.explanation


def test_a_parameter_addition_is_reported_between_two_indexed_releases():
    spec = PackageSpec(name="grow", import_names=("grow",), tier="B")
    mod = SymbolRecord("grow", "module", "grow")
    unknown = SymbolRecord("grow.run", "alias", "_grow.run")

    def run(*params: list[dict]) -> SymbolRecord:
        return SymbolRecord("grow.run", "function", "grow.run", list(params))

    def explain(snaps: list[tuple[str, SymbolRecord]], installed: str):
        store = IndexStore.memory()
        store.rebuild_package(
            spec, [ApiSnapshot("grow", v, ["grow"], {"grow": mod, "grow.run": s}) for v, s in snaps]
        )
        tb = "TypeError: run() got an unexpected keyword argument 'retries'"
        return DepTime(store, Coverage(POLICY, (spec,))).explain_error(tb, {"grow": installed}).findings

    old, new = run(_param("url")), run(_param("url"), _param("retries"))
    [f] = explain([("1.0", old), ("1.2", old), ("2.0", new)], "1.0")
    assert (f.symbol, f.issue, f.last_version_without, f.first_version_with, f.pin_to_restore) == (
        "grow.run(retries)",
        "param_not_yet_added",
        "1.2",
        "2.0",
        "grow>=2.0",
    )
    assert "between grow 1.2 and 2.0" in f.explanation
    # 1.5 isn't indexed and lies between 1.2 (without) and 2.0 (with).
    [f] = explain([("1.0", old), ("1.2", old), ("2.0", new)], "1.5")
    assert (f.issue, f.confidence, f.last_version_without, f.first_version_with) == (
        "param_not_yet_added",
        "medium",
        "1.2",
        "2.0",
    )
    assert "your 1.5 is in that window and not indexed" in f.explanation
    # 1.2's signature is unknown, so the last release shown without `retries` is 1.0.
    [f] = explain([("1.0", old), ("1.2", unknown), ("2.0", new)], "1.0")
    assert (f.last_version_without, f.first_version_with) == ("1.0", "2.0")
    assert "between grow 1.0 and 2.0" in f.explanation
    # Named but positional-only at the installed release: not an addition, whatever later releases do.
    positional = run(_param("url"), _param("retries", "positional-only", None))
    assert explain([("1.0", positional), ("2.0", new)], "1.0") == []


def test_explain_removed_symbol_with_replacement(dt):
    res = dt.explain_error(
        "ImportError: cannot import name 'OldThing' from 'httpx' (/v/site-packages/httpx/__init__.py)",
        {"httpx": "0.28.1"},
    )
    f = res.findings[0]
    assert (f.symbol, f.issue, f.last_version_with, f.first_version_without) == (
        "httpx.OldThing",
        "symbol_removed",
        "0.26.0",
        "0.27.2",
    )
    assert f.replacements[0].target == "httpx.NewThing"


def test_explain_never_existed(dt):
    res = dt.explain_error("AttributeError: module 'httpx' has no attribute 'Clientt'", {"httpx": "0.28.1"})
    f = res.findings[0]
    assert (f.issue, f.confidence) == ("never_existed", "medium")
    # DT-CORE-17: the index starts at a release, so a name removed before it is indistinguishable from a typo.
    assert "If it was removed before 0.26.0, the earliest release DepTime indexes" in f.explanation

    kw = "TypeError: Client.__init__() got an unexpected keyword argument 'nope'"
    f = dt.explain_error(kw, {"httpx": "0.28.1"}).findings[0]
    assert f.issue == "never_existed"
    assert "If it was removed before 0.26.0, the earliest release DepTime indexes" in f.explanation


def test_never_existed_on_an_owner_with_getattr_is_low_confidence():
    # DT-CORE-17: numpy served np.float from a module __getattr__, which a static index can't see.
    spec = PackageSpec(name="lazy", import_names=("lazy",), tier="B")
    mod = SymbolRecord("lazy", "module", "lazy")
    hook = SymbolRecord(
        "lazy.__getattr__", "function", "lazy.__getattr__", [_param("name", "positional only", None)]
    )
    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot("lazy", "1.0", ["lazy"], {"lazy": mod}),
            ApiSnapshot("lazy", "2.0", ["lazy"], {"lazy": mod, "lazy.__getattr__": hook}),
        ],
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "AttributeError: module 'lazy' has no attribute 'float'"

    f = dt.explain_error(tb, {"lazy": "2.0"}).findings[0]
    assert (f.issue, f.confidence) == ("never_existed", "low")
    assert "lazy defines `__getattr__` at 2.0" in f.explanation
    # Without the hook at the installed release, it's the ordinary answer.
    f = dt.explain_error(tb, {"lazy": "1.0"}).findings[0]
    assert (f.issue, f.confidence) == ("never_existed", "medium")
    assert "__getattr__" not in f.explanation


def test_no_parameter_verdict_without_a_signature():
    # DT-CORE-20: an unresolved alias has no signature (pytest.skip before `_pytest` was loaded), so a
    # kwarg error against it says nothing about whether the parameter existed.
    spec = PackageSpec(name="wrap", import_names=("wrap",), tier="B")
    mod = SymbolRecord("wrap", "module", "wrap")
    func = SymbolRecord("wrap.skip", "function", "_wrap.skip", [_param("reason"), _param("msg")])
    alias = SymbolRecord("wrap.skip", "alias", "_wrap.skip")
    tb = "TypeError: skip() got an unexpected keyword argument 'msg'"

    def explain(snaps: list[tuple[str, SymbolRecord]], installed: str) -> list:
        store = IndexStore.memory()
        store.rebuild_package(
            spec, [ApiSnapshot("wrap", v, ["wrap"], {"wrap": mod, "wrap.skip": s}) for v, s in snaps]
        )
        return DepTime(store, Coverage(POLICY, (spec,))).explain_error(tb, {"wrap": installed}).findings

    # No release has a signature: not "never accepted".
    assert explain([("1.0", alias), ("2.0", alias)], "2.0") == []
    # 2.0 rejects `msg`, but 1.0's signature is unknown, so "never accepted" still can't be shown.
    without = SymbolRecord("wrap.skip", "function", "_wrap.skip", [_param("reason")])
    assert explain([("1.0", alias), ("2.0", without)], "2.0") == []
    assert explain([("1.0", func), ("2.0", without)], "2.0")[0].issue == "param_removed"
    # The installed release has none: not "removed" either, although 1.0 accepted `msg`.
    assert explain([("1.0", func), ("2.0", alias)], "2.0") == []
    # 1.5 isn't indexed and 1.0 accepts `msg`; an unknown signature at 2.0 doesn't close the window.
    assert explain([("1.0", func), ("2.0", alias)], "1.5") == []


def test_kwargs_catch_all_does_not_accept_a_parameter_once_declared_by_name():
    # DT-CORE-05: pydantic 2's Field(**extra) raises "`regex` is removed"; pydantic 1 declared `regex`.
    spec = PackageSpec(name="pyd", import_names=("pyd",), tier="B")
    mod = SymbolRecord("pyd", "module", "pyd")
    extra = _param("extra", "variadic keyword", None)

    def explain(*params: list[dict]) -> list:
        snaps = [
            ApiSnapshot(
                "pyd",
                f"{i}.0",
                ["pyd"],
                {"pyd": mod, "pyd.Field": SymbolRecord("pyd.Field", "function", "pyd.Field", p)},
            )
            for i, p in enumerate(params, start=1)
        ]
        store = IndexStore.memory()
        store.rebuild_package(spec, snaps)
        tb = "TypeError: Field() got an unexpected keyword argument 'regex'"
        return (
            DepTime(store, Coverage(POLICY, (spec,))).explain_error(tb, {"pyd": f"{len(params)}.0"}).findings
        )

    f = explain([_param("default"), _param("regex")], [_param("default"), extra])[0]
    assert (f.issue, f.last_version_with, f.first_version_without) == ("param_removed", "1.0", "2.0")
    # Never declared by name: the catch-all may well accept it, so no verdict.
    assert explain([_param("default"), extra], [_param("default"), extra]) == []


def test_explain_reports_uncovered_packages(dt):
    res = dt.explain_error("ImportError: cannot import name 'x' from 'somepkg.sub' (/a.py)")
    assert res.findings == [] and res.not_covered == ["somepkg"]


def test_symbol_history(dt):
    h = dt.symbol_history("httpx", "Client.__init__")
    assert h.path == "httpx.Client.__init__" and h.ever_seen and h.present_in_latest
    kinds = {(e.kind, e.version, e.detail.get("param")) for e in h.events}
    assert ("param_removed", "0.28.1", "proxies") in kinds
    assert ("param_deprecated", "0.26.0", "proxies") in kinds
    assert any("proxies" in r.target for r in h.replacements)


def test_check_symbols(dt):
    res = dt.check_symbols(
        "httpx",
        "0.28.1",
        [SymbolQuery(path="httpx.Client", kwargs=["proxies", "proxy"]), SymbolQuery(path="OldThing")],
    )
    client, old = res.results
    assert (client.status, client.rejected_kwargs) == ("present", ["proxies"])
    assert old.status == "missing" and "0.26.0" in old.hint and "httpx.NewThing" in old.hint


def test_diff_versions_orders_by_severity_and_filters(dt):
    res = dt.diff_versions("httpx", "0.26.0", "0.28.1")
    assert res.events[0].kind == "removed"
    only_client = dt.diff_versions("httpx", "0.26.0", used_symbols=["httpx.Client"])
    assert {e.path for e in only_client.events} == {"httpx.Client.__init__"}


def test_diff_from_date(dt):
    res = dt.diff_versions("httpx", "2024-09-01")
    assert res.from_version == "0.27.2" and res.to_version == "0.28.1"


def test_coverage_report(dt):
    cov = dt.coverage_report()
    assert cov.packages[0].package == "httpx" and cov.packages[0].versions_indexed == 3
    assert cov.configured_but_not_indexed == ["google-genai"]


def test_parse_installed_accepts_freeze_list_and_dict():
    got = parse_installed(
        {"Google_GenAI": "==1.2.0"},
        "httpx==0.28.1\nrequests[socks]==2.32.3\nPackage    Version\npydantic   2.14.1\n",
    )
    assert got == {"google-genai": "1.2.0", "httpx": "0.28.1", "requests": "2.32.3", "pydantic": "2.14.1"}


def _big_diff_store() -> DepTime:
    """2.0 adds 2,500 functions and removes one; the removal sorts after every added path."""
    spec = PackageSpec(name="bigpkg", import_names=("bigpkg",), tier="B")
    base = {"bigpkg": SymbolRecord("bigpkg", "module", "bigpkg")}
    old = {
        **base,
        "bigpkg.zzz_removed": SymbolRecord("bigpkg.zzz_removed", "function", "bigpkg.zzz_removed", []),
    }
    new = dict(base)
    for i in range(2500):
        path = f"bigpkg.f{i:04d}"
        new[path] = SymbolRecord(path, "function", path, [])
    store = IndexStore.memory()
    store.rebuild_package(
        spec, [ApiSnapshot("bigpkg", "1.0", ["bigpkg"], old), ApiSnapshot("bigpkg", "2.0", ["bigpkg"], new)]
    )
    return DepTime(store, Coverage(POLICY, (spec,)))


def test_diff_versions_never_hides_breaking_changes_behind_added_events():
    # Q-01: filtering and the severity sort used to run on the first 2,000 rows only.
    dt = _big_diff_store()
    res = dt.diff_versions("bigpkg", "1.0", "2.0")
    assert [(e.path, e.kind) for e in res.events] == [("bigpkg.zzz_removed", "removed")]
    assert res.truncated is False
    used = dt.diff_versions("bigpkg", "1.0", "2.0", used_symbols=["zzz_removed"])
    assert [e.path for e in used.events] == ["bigpkg.zzz_removed"]
    everything = dt.diff_versions("bigpkg", "1.0", "2.0", include_added=True, limit=500)
    assert everything.events[0].kind == "removed"  # most severe first, even past 2,000 added rows
    assert len(everything.events) == 500 and everything.truncated is True


def test_explain_error_without_an_index_says_it_is_downloading(coverage):
    # Q-07: with no index yet, httpx must not be reported as "not covered".
    res = DepTime(None, coverage).explain_error(HTTPX_TB, {"httpx": "0.28.1"})
    assert res.signal.kind == "unexpected_kwarg"
    assert res.findings == [] and res.not_covered == []
    assert "no index yet" in res.note


def test_index_downloaded_by_another_process_is_picked_up(tmp_path, index_file, coverage):
    # D-01: a server that started without an index opens one that appears on disk later.
    path = tmp_path / "home" / "index.sqlite"
    path.parent.mkdir()
    dt = DepTime(None, coverage, path)
    assert "no index yet" in dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).note
    path.write_bytes(b"not a database")  # a broken file is ignored, not fatal
    assert dt.store is None
    path.unlink()
    shutil.copy(index_file, path)
    res = dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"})
    assert res.findings[0].symbol == "httpx.Client.__init__(proxies)"


def test_low_confidence_replacements_are_not_stated_as_advice():
    # Q-03: a 0.6 hint must not become "Use … instead." (DT-CORE-23 reads Q-03's own text, "use keyword
    # arguments", as prose: no hint at all.)
    spec = PackageSpec(name="p5", import_names=("p5",), tier="B")
    old_dep = "Passing positional arguments is deprecated; use p5.f with keyword arguments"
    param_dep = "The 'old' argument is deprecated; see 'docs_page'."
    v1 = {
        "p5": SymbolRecord("p5", "module", "p5"),
        "p5.old": SymbolRecord("p5.old", "function", "p5.old", [], old_dep),
        "p5.f": SymbolRecord("p5.f", "function", "p5.f", [_param("old")], param_dep),
    }
    v2 = {"p5": v1["p5"], "p5.f": SymbolRecord("p5.f", "function", "p5.f", [])}
    store = IndexStore.memory()
    store.rebuild_package(spec, [ApiSnapshot("p5", "1.0", ["p5"], v1), ApiSnapshot("p5", "2.0", ["p5"], v2)])
    dt = DepTime(store, Coverage(POLICY, (spec,)))

    sym = dt.explain_error("ImportError: cannot import name 'old' from 'p5' (/x.py)", {"p5": "2.0"}).findings[
        0
    ]
    assert sym.issue == "symbol_removed" and "p5.f" not in sym.explanation
    assert [(r.target, r.confidence) for r in sym.replacements] == [
        ("p5.f", 0.6)
    ]  # still listed, with its score

    par = dt.explain_error("TypeError: f() got an unexpected keyword argument 'old'", {"p5": "2.0"}).findings[
        0
    ]
    assert par.issue == "param_removed" and "Use `docs_page`" not in par.explanation
    assert "Pin p5<=1.0 or update the call." in par.explanation
    assert [r.target for r in par.replacements] == ["docs_page"]

    check = dt.check_symbols("p5", "2.0", [SymbolQuery(path="old")]).results[0]
    assert check.status == "missing" and "p5.f" not in check.hint


def test_a_parameter_renamed_in_case_is_the_replacement():
    # DT-CORE-24: mcp 2.0 renamed `ServerSession.elicit(requestedSchema)` to `requested_schema` with no
    # deprecation text; the same-release removal and addition say what to pass instead.
    spec = PackageSpec(name="p6", import_names=("p6",), tier="B")
    mod = SymbolRecord("p6", "module", "p6")

    def release(version: str, param: str) -> ApiSnapshot:
        f = SymbolRecord("p6.elicit", "function", "p6.elicit", [_param(param, "positional or keyword")])
        return ApiSnapshot("p6", version, ["p6"], {"p6": mod, "p6.elicit": f})

    store = IndexStore.memory()
    store.rebuild_package(spec, [release("1.0", "requestedSchema"), release("2.0", "requested_schema")])
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "TypeError: elicit() got an unexpected keyword argument 'requestedSchema'"
    par = dt.explain_error(tb, {"p6": "2.0"}).findings[0]
    assert par.issue == "param_removed" and par.symbol == "p6.elicit(requestedSchema)"
    assert [(r.target, r.confidence, r.source) for r in par.replacements] == [
        ("requested_schema", 0.8, "moved_heuristic")
    ]
    assert "Use `requested_schema` instead, or pin p6<=1.0." in par.explanation
    assert "requestedSchema" in par.replacements[0].evidence


def test_a_removed_name_points_to_the_path_that_still_has_its_object():
    # DT-CORE-22: numpy 2.0 dropped `numpy.lib.arraysetops.union1d`, but `numpy.union1d` is the same function;
    # pydantic 2.0 dropped `pydantic.ConstrainedBytes`, which `pydantic.v1` still serves (as a copy).
    spec = PackageSpec(name="p7", import_names=("p7",), tier="B")

    def rec(path: str, canonical: str, kind: str = "function", deprecated: str | None = None) -> SymbolRecord:
        return SymbolRecord(path, kind, canonical, [] if kind == "function" else None, deprecated)

    modules = {
        p: rec(p, p, "module")
        for p in [
            "p7",
            "p7.lib",
            "p7.lib.sets",
            "p7.matlib",
            "p7.types",
            "p7.v1",
            "p7.chains",
            "p7.chains.qa",
            "p7.chains.sum",
            "p7.chains.refine",
            "p7.v1.types",
        ]
    }
    both = {
        "p7.union1d": rec("p7.union1d", "p7._impl.union1d"),
        "p7.matlib.union1d": rec("p7.matlib.union1d", "p7._impl.union1d"),
        "p7.lib.old_name": rec("p7.lib.old_name", "p7._impl.old_name"),
        "p7.renamed": rec("p7.renamed", "p7._impl.renamed"),
        "p7.Model": rec("p7.Model", "p7._impl.Model", "class"),
        "p7.Other": rec("p7.Other", "p7._impl.Other", "class"),
        "p7.Other.dict": rec("p7.Other.dict", "p7._impl.Base.dict"),
        "p7.chains.sum.Refine": rec("p7.chains.sum.Refine", "p7.chains.refine.Refine", "class"),
        "p7.chains.refine.Refine": rec("p7.chains.refine.Refine", "p7.chains.refine.Refine", "class"),
    }
    v1 = {
        **modules,
        **both,
        "p7.lib.sets.union1d": rec("p7.lib.sets.union1d", "p7._impl.union1d"),
        "p7.old_name": rec(
            "p7.old_name", "p7._impl.old_name", deprecated="old_name is deprecated; use p7.renamed instead."
        ),
        "p7.Model.dict": rec("p7.Model.dict", "p7._impl.Base.dict"),
        "p7.chains.qa.Refine": rec("p7.chains.qa.Refine", "p7.chains.refine.Refine", "class"),
        "p7.ConstrainedBytes": rec("p7.ConstrainedBytes", "p7.types.ConstrainedBytes", "class"),
        "p7.types.ConstrainedBytes": rec("p7.types.ConstrainedBytes", "p7.types.ConstrainedBytes", "class"),
        "p7.v1.ConstrainedBytes": rec("p7.v1.ConstrainedBytes", "p7.types.ConstrainedBytes", "class"),
        "p7.v1.types.ConstrainedBytes": rec(
            "p7.v1.types.ConstrainedBytes", "p7.types.ConstrainedBytes", "class"
        ),
    }
    v2 = {
        **modules,
        **both,
        "p7.v1.ConstrainedBytes": rec("p7.v1.ConstrainedBytes", "p7.v1.types.ConstrainedBytes", "class"),
        "p7.v1.types.ConstrainedBytes": rec(
            "p7.v1.types.ConstrainedBytes", "p7.v1.types.ConstrainedBytes", "class"
        ),
    }
    store = IndexStore.memory()
    store.rebuild_package(spec, [ApiSnapshot("p7", "1.0", ["p7"], v1), ApiSnapshot("p7", "2.0", ["p7"], v2)])

    def hints(path: str) -> list[tuple[str, float, str]]:
        return [(r["target"], r["confidence"], r["source"]) for r in store.replacements("p7", path)]

    # The same object, still public: the shortest path, not `p7.matlib.union1d`.
    assert hints("p7.lib.sets.union1d") == [("p7.union1d", 0.75, "moved_heuristic")]
    # The module that defines it, over a shorter path that only imports it (langchain's `chains.summarize`).
    assert hints("p7.chains.qa.Refine") == [("p7.chains.refine.Refine", 0.75, "moved_heuristic")]
    # What held the object until this release: weaker, since `p7.v1.ConstrainedBytes` is now another object.
    assert hints("p7.ConstrainedBytes") == [("p7.v1.ConstrainedBytes", 0.6, "moved_heuristic")]
    assert hints("p7.types.ConstrainedBytes") == [("p7.v1.ConstrainedBytes", 0.6, "moved_heuristic")]
    # Not for a class's member (an inherited one is shared by every subclass), nor next to a stronger hint.
    assert hints("p7.Model.dict") == []
    assert [t for t, c, _ in hints("p7.old_name") if c >= 0.75] == ["p7.renamed"]
    assert "p7.lib.old_name" not in [t for t, _, _ in hints("p7.old_name")]

    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "ImportError: cannot import name 'union1d' from 'p7.lib.sets' (/x.py)"
    sym = dt.explain_error(tb, {"p7": "2.0"}).findings[0]
    assert sym.issue == "symbol_removed" and "Use p7.union1d instead." in sym.explanation
    assert sym.replacements[0].evidence == (
        "p7.lib.sets.union1d was removed in 2.0; the same object is still public as p7.union1d"
    )


def _param(name: str, kind: str = "keyword-only", default: str | None = "None") -> dict:
    return {"name": name, "kind": kind, "default": default, "annotation": None}


def _fp(*defaults: str | None) -> DepTime:
    """fp.f across releases 1.0, 2.0, …: no `x` where the entry is "absent", else f(*, x=<default>)."""
    spec = PackageSpec(name="fp", import_names=("fp",), tier="B")
    snaps = []
    for i, default in enumerate(defaults, start=1):
        params = [] if default == "absent" else [_param("x", default=default)]
        f = SymbolRecord("fp.f", "function", "fp.f", params)
        snaps.append(
            ApiSnapshot("fp", f"{i}.0", ["fp"], {"fp": SymbolRecord("fp", "module", "fp"), "fp.f": f})
        )
    store = IndexStore.memory()
    store.rebuild_package(spec, snaps)
    return DepTime(store, Coverage(POLICY, (spec,)))


MISSING_X = "TypeError: f() missing 1 required keyword-only argument: 'x'"


def test_missing_argument_after_a_default_was_removed():
    # I-10: f(*, x=None) in 2.0 -> f(*, x) in 3.0.
    dt = _fp("absent", "None", None)
    f = dt.explain_error(MISSING_X, {"fp": "3.0"}).findings[0]
    assert (f.issue, f.last_version_with, f.first_version_without, f.pin_to_restore) == (
        "param_now_required",
        "2.0",
        "3.0",
        "fp<=2.0",
    )
    assert "had a default in 2.0" in f.explanation
    assert [(e.kind, e.version) for e in dt.diff_versions("fp", "2.0").events] == [
        ("param_now_required", "3.0")
    ]


def test_missing_argument_is_not_blamed_on_a_requirement_that_was_lifted():
    # Q-17: f() 1.0 -> f(*, x) 2.0 -> f(*, x=None) 3.0. At 3.0 `x` is optional, so no downgrade advice.
    dt = _fp("absent", None, "None")
    assert dt.explain_error(MISSING_X, {"fp": "3.0"}).findings == []
    still = dt.explain_error(MISSING_X, {"fp": "2.0"}).findings[0]
    assert (still.issue, still.pin_to_restore) == ("param_now_required", "fp<=1.0")


OPENAI = PackageSpec(name="openai", import_names=("openai",), tier="A")
OPENAI_INTERNAL_TB = """\
Traceback (most recent call last):
  File "/app/main.py", line 3, in <module>
    client = OpenAI()
  File "/venv/lib/python3.12/site-packages/openai/_client.py", line 123, in __init__
    super().__init__(
  File "/venv/lib/python3.12/site-packages/openai/_base_client.py", line 857, in __init__
    self._client = http_client or SyncHttpxClientWrapper(
  File "/venv/lib/python3.12/site-packages/openai/_base_client.py", line 755, in __init__
    super().__init__(**kwargs)
TypeError: Client.__init__() got an unexpected keyword argument 'proxies'
"""


def test_error_raised_inside_a_dependency_finds_the_called_package(store):
    # Q-18: openai calls httpx.Client(proxies=...); the innermost frame is openai's, but httpx changed.
    dt = DepTime(store, Coverage(POLICY, (HTTPX, OPENAI)))
    installed = {"httpx": "0.28.1", "openai": "1.55.0"}

    # A) openai configured but not indexed
    res = dt.explain_error(OPENAI_INTERNAL_TB, installed)
    top = res.findings[0]
    assert (top.package, top.symbol, top.issue, top.pin_to_restore) == (
        "httpx",
        "httpx.Client.__init__(proxies)",
        "param_removed",
        "httpx<=0.27.2",
    )
    assert top.confidence == "high" and res.not_covered == ["openai"]
    assert "inside openai's code" in res.note

    # B) openai indexed too, with its own `Client` alias whose __init__ never took `proxies`
    sym = {
        p: SymbolRecord(p, k, c, params)
        for p, k, c, params in [
            ("openai", "module", "openai", None),
            ("openai.Client", "class", "openai._client.OpenAI", None),
            ("openai.Client.__init__", "function", "openai._client.OpenAI.__init__", [_param("api_key")]),
        ]
    }
    store.rebuild_package(OPENAI, [ApiSnapshot("openai", "1.55.0", ["openai"], sym)])
    res = dt.explain_error(OPENAI_INTERNAL_TB, installed)
    assert [(f.package, f.issue) for f in res.findings] == [("httpx", "param_removed")]

    # httpx missing from installed_versions: still found, from the latest indexed release, at lower confidence
    res = dt.explain_error(OPENAI_INTERNAL_TB, {"openai": "1.55.0"})
    assert [(f.package, f.issue, f.confidence) for f in res.findings] == [("httpx", "param_removed", "low")]


def _rec(path: str, kind: str = "attribute", params: list[dict] | None = None) -> SymbolRecord:
    return SymbolRecord(path, kind, path, params)


def _dt(specs_and_snaps: list[tuple[PackageSpec, list[ApiSnapshot]]], extra=()) -> DepTime:
    store = IndexStore.memory()
    for spec, snaps in specs_and_snaps:
        store.rebuild_package(spec, snaps)
    return DepTime(store, Coverage(POLICY, tuple(s for s, _ in specs_and_snaps) + tuple(extra)))


NUMPY = PackageSpec(name="numpy", import_names=("numpy",), tier="B")


def _numpy() -> DepTime:
    old = {p: _rec(p) for p in ("numpy.NaN", "numpy.nan", "numpy.obj2sctype")} | {
        "numpy.dtype": _rec("numpy.dtype", "class")
    }
    old |= {
        "numpy.ndarray": _rec("numpy.ndarray", "class"),
        "numpy.ndarray.itemset": _rec("numpy.ndarray.itemset", "function", []),
    }
    new = {
        p: r for p, r in old.items() if p not in ("numpy.NaN", "numpy.obj2sctype", "numpy.ndarray.itemset")
    }
    mod = {"numpy": _rec("numpy", "module")}
    return _dt(
        [
            (
                NUMPY,
                [
                    ApiSnapshot("numpy", "1.26.4", ["numpy"], mod | old),
                    ApiSnapshot("numpy", "2.0.0", ["numpy"], mod | new),
                ],
            )
        ]
    )


def test_a_replacement_named_by_the_error_message_is_checked_against_the_index():
    # DT-CORE-06: numpy 2's own message names the fix; `np.` is how numpy's messages spell `numpy.`.
    dt = _numpy()
    tb = "AttributeError: `np.NaN` was removed in the NumPy 2.0 release. Use `np.nan` instead.. Did you mean: 'nan'?"
    f = dt.explain_error(tb, {"numpy": "2.0.0"}).findings[0]
    assert (f.symbol, f.issue) == ("numpy.NaN", "symbol_removed")
    assert [(r.target, r.confidence, r.source) for r in f.replacements] == [
        ("numpy.nan", 0.85, "error_message")
    ]
    assert f.replacements[0].evidence.startswith(
        "`np.NaN` was removed in the NumPy 2.0 release. Use `np.nan`"
    )
    assert "Use numpy.nan instead." in f.explanation
    # Not a path in the index: never offered, so text in a traceback can't become advice on its own.
    for msg in ("Use `np.evil` instead.", "Use `os.system` instead."):
        f = dt.explain_error(
            f"AttributeError: `np.NaN` was removed in the NumPy 2.0 release. {msg}", {"numpy": "2.0.0"}
        ).findings[0]
        assert (f.issue, f.replacements) == ("symbol_removed", [])
    # The head of an expression isn't the replacement, though `numpy.dtype` is in the index.
    tb = "AttributeError: `np.obj2sctype` was removed in the NumPy 2.0 release. Use `np.dtype(obj).type` instead."
    f = dt.explain_error(tb, {"numpy": "2.0.0"}).findings[0]
    assert (f.symbol, f.replacements) == ("numpy.obj2sctype", [])
    # "Use `arr[index] = value` instead" names no symbol (the miner reads `arr`).
    tb = "AttributeError: `itemset` was removed from the ndarray class in NumPy 2.0. Use `arr[index] = value` instead."
    f = dt.explain_error(tb, {"numpy": "2.0.0"}).findings[0]
    assert (f.symbol, f.issue, f.replacements) == ("numpy.ndarray.itemset", "symbol_removed", [])


def test_an_error_message_hint_and_an_indexed_hint_for_the_same_target_are_one(dt):
    tb = "AttributeError: module 'httpx' has no attribute 'OldThing'. Use httpx.NewThing instead."
    f = dt.explain_error(tb, {"httpx": "0.28.1"}).findings[0]
    assert [(r.target, r.source) for r in f.replacements] == [("httpx.NewThing", "error_message")]


def test_a_parameter_named_by_the_error_message_must_be_a_parameter():
    # DT-CORE-06: pydantic 2's `removed-kwargs` error, "`regex` is removed. use `pattern` instead".
    spec = PackageSpec(name="pyd", import_names=("pyd",), tier="B")
    mod = {"pyd": _rec("pyd", "module")}
    v1 = mod | {"pyd.Field": _rec("pyd.Field", "function", [_param("default"), _param("regex")])}
    extra = _param("extra", "variadic keyword", None)
    v2 = mod | {"pyd.Field": _rec("pyd.Field", "function", [_param("default"), _param("pattern"), extra])}
    dt = _dt([(spec, [ApiSnapshot("pyd", "1.0", ["pyd"], v1), ApiSnapshot("pyd", "2.0", ["pyd"], v2)])])

    def explain(use: str):
        tb = f"pydantic.errors.PydanticUserError: `regex` is removed. use `{use}` instead"
        return dt.explain_error(tb, {"pyd": "2.0"}).findings[0]

    f = explain("pattern")
    assert (f.symbol, f.issue) == ("pyd.Field(regex)", "param_removed")
    assert [(r.target, r.source) for r in f.replacements] == [("pattern", "error_message")]
    assert "Use `pattern` instead" in f.explanation
    assert explain("default").replacements[0].target == "default"  # any parameter the function has
    for use in ("nonexistent", "pyd.Field", "regex"):
        assert explain(use).replacements == []


def test_a_name_moved_to_another_package_is_offered_when_that_package_has_it():
    # DT-CORE-06: "`BaseSettings` has been moved to the `pydantic-settings` package."
    pyd = PackageSpec(name="pydantic", import_names=("pydantic",), tier="A")
    settings = PackageSpec(name="pydantic-settings", import_names=("pydantic_settings",), tier="A")
    mod = {"pydantic": _rec("pydantic", "module")}
    pyd_snaps = [
        ApiSnapshot(
            "pydantic",
            "1.10.0",
            ["pydantic"],
            mod | {"pydantic.BaseSettings": _rec("pydantic.BaseSettings", "class")},
        ),
        ApiSnapshot("pydantic", "2.0", ["pydantic"], mod),
    ]
    settings_snap = ApiSnapshot(
        "pydantic-settings",
        "2.0.0",
        ["pydantic_settings"],
        {
            "pydantic_settings": _rec("pydantic_settings", "module"),
            "pydantic_settings.BaseSettings": _rec("pydantic_settings.BaseSettings", "class"),
        },
    )
    tb = (
        "pydantic.errors.PydanticImportError: `BaseSettings` has been moved to the `pydantic-settings` package. "
        "See https://docs.pydantic.dev/2.6/migration/#basesettings-has-moved-to-pydantic-settings for more details."
    )
    f = (
        _dt([(pyd, pyd_snaps), (settings, [settings_snap])])
        .explain_error(tb, {"pydantic": "2.0"})
        .findings[0]
    )
    assert (f.symbol, f.issue) == ("pydantic.BaseSettings", "symbol_removed")
    assert [(r.target, r.source) for r in f.replacements] == [
        ("pydantic_settings.BaseSettings", "error_message")
    ]
    assert "Use pydantic_settings.BaseSettings instead." in f.explanation
    # Configured but not indexed: nothing to check the name against, so no hint.
    f = _dt([(pyd, pyd_snaps)], extra=[settings]).explain_error(tb, {"pydantic": "2.0"}).findings[0]
    assert (f.issue, f.replacements) == ("symbol_removed", [])


def _constraints() -> tuple[PackageSpec, list[ApiSnapshot]]:
    # DT-DATA-42: django 5.1 deprecates CheckConstraint(check=...) for `condition`; the class itself isn't deprecated.
    spec = PackageSpec(name="dj", import_names=("dj",), tier="B")
    msg = "CheckConstraint.check is deprecated in favor of `.condition`."

    def snap(version: str, check: bool) -> ApiSnapshot:
        params = [_param("name"), _param("condition")] + ([_param("check")] if check else [])
        init = SymbolRecord(
            "dj.CheckConstraint.__init__",
            "function",
            "dj.c.CheckConstraint.__init__",
            params,
            None,
            {"check": msg} if check else None,
        )
        cls = SymbolRecord("dj.CheckConstraint", "class", "dj.c.CheckConstraint")
        return ApiSnapshot(
            "dj", version, ["dj"], {"dj": SymbolRecord("dj", "module", "dj"), cls.path: cls, init.path: init}
        )

    return spec, [snap("5.0", False), snap("5.1", True), snap("6.0", False)]


def test_argument_deprecations_round_trip_and_reach_answers(tmp_path):
    spec, snaps = _constraints()
    store = IndexStore.memory()
    store.rebuild_package(spec, snaps)
    assert store.snapshot_at(spec, "5.1").symbols == snaps[1].symbols
    dt = DepTime(store, Coverage(POLICY, (spec,)))

    f = dt.explain_error(
        "TypeError: CheckConstraint.__init__() got an unexpected keyword argument 'check'", {"dj": "6.0"}
    )
    assert (f.findings[0].symbol, f.findings[0].deprecated_in) == (
        "dj.CheckConstraint.__init__(check)",
        "5.1",
    )
    assert [r.target for r in f.findings[0].replacements] == ["condition"]

    passing = dt.check_symbols(
        "dj", "5.1", [SymbolQuery(path="dj.CheckConstraint", kwargs=["check"])]
    ).results[0]
    assert passing.deprecated == "CheckConstraint.check is deprecated in favor of `.condition`."
    other = dt.check_symbols(
        "dj", "5.1", [SymbolQuery(path="dj.CheckConstraint", kwargs=["condition"])]
    ).results[0]
    assert other.deprecated is None


def test_an_argument_a_decorator_renamed_is_accepted_until_the_decorator_goes(tmp_path):
    # DT-DATA-37: polars 0.19.11 renamed `Series.shift(periods)` to `n` with
    # `@deprecate_renamed_parameter("periods", "n")`, which accepts `periods` until 1.0 drops the decorator. The
    # signature lost `periods` at the rename, so it read "removed in 0.19.11".
    spec = PackageSpec(name="pl", import_names=("pl",), tier="B")
    msg = "`periods` is deprecated. Use `n` instead."

    def snap(version: str, params: list[str], renamed: bool) -> ApiSnapshot:
        shift = SymbolRecord(
            "pl.Series.shift",
            "function",
            "pl.series.Series.shift",
            [_param(p) for p in params],
            None,
            {"periods": msg} if renamed else None,
        )
        cls = SymbolRecord("pl.Series", "class", "pl.series.Series")
        return ApiSnapshot(
            "pl",
            version,
            ["pl"],
            {"pl": SymbolRecord("pl", "module", "pl"), cls.path: cls, shift.path: shift},
        )

    store = IndexStore.memory()
    releases = [("0.19.10", ["periods"], False), ("0.19.11", ["n"], True), ("0.20.31", ["n"], True)]
    store.rebuild_package(spec, [snap(*r) for r in [*releases, ("1.0.0", ["n"], False)]])
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "TypeError: Series.shift() got an unexpected keyword argument 'periods'"
    f = dt.explain_error(tb, {"pl": "1.0.0"}).findings[0]
    assert (f.issue, f.last_version_with, f.first_version_without, f.deprecated_in) == (
        "param_removed",
        "0.20.31",
        "1.0.0",
        "0.19.11",
    )
    assert [r.target for r in f.replacements] == ["n"]
    # Still accepted while the decorator is there: no finding, and check_symbols doesn't reject it.
    assert dt.explain_error(tb, {"pl": "0.20.31"}).findings == []
    check = dt.check_symbols(
        "pl", "0.20.31", [SymbolQuery(path="pl.Series.shift", kwargs=["periods"])]
    ).results[0]
    assert (check.rejected_kwargs, check.deprecated) == ([], msg)


def test_an_index_from_before_argument_deprecations_still_opens(tmp_path):
    # Released clients read the published index; the nightly adds to it. Neither may trip over the new column.
    spec, snaps = _constraints()
    path = tmp_path / "index.sqlite"
    store = IndexStore.open(path, readonly=False)
    store.rebuild_package(spec, snaps)
    store.conn.execute("ALTER TABLE spans DROP COLUMN deprecated_params")
    store.close()
    old = IndexStore.open(path)
    dt = DepTime(old, Coverage(POLICY, (spec,)))
    assert (
        dt.check_symbols("dj", "5.1", [SymbolQuery(path="dj.CheckConstraint", kwargs=["check"])])
        .results[0]
        .status
        == "present"
    )
    old.close()
    again = IndexStore.open(path, readonly=False)
    again.rebuild_package(spec, snaps)
    assert again.snapshot_at(spec, "5.1").symbols == snaps[1].symbols


def test_an_indexed_hint_names_a_path_the_index_has():
    # DT-CORE-12: "Please use `prod` instead" means numpy.prod, not the builtin; a target the index contradicts
    # stays listed with its evidence but isn't stated as advice.
    mod = {"numpy": _rec("numpy", "module")}
    product = SymbolRecord(
        "numpy.product",
        "function",
        "numpy.core.fromnumeric.product",
        [],
        "`product` is deprecated as of NumPy 1.25.0, and will be removed in NumPy 2.0. Please use `prod` instead.",
    )
    typo = SymbolRecord(
        "numpy.alltrue",
        "function",
        "numpy.alltrue",
        [],
        "`alltrue` is deprecated. Use `all_true_now` instead.",
    )
    prod = _rec("numpy.prod", "function", [])
    dt = _dt(
        [
            (
                NUMPY,
                [
                    ApiSnapshot(
                        "numpy",
                        "1.25.2",
                        ["numpy"],
                        mod | {"numpy.prod": prod, "numpy.product": product, "numpy.alltrue": typo},
                    ),
                    ApiSnapshot("numpy", "2.0.0", ["numpy"], mod | {"numpy.prod": prod}),
                ],
            )
        ]
    )
    tb = "ImportError: cannot import name 'product' from 'numpy' (/x.py)"
    f = dt.explain_error(tb, {"numpy": "2.0.0"}).findings[0]
    assert [(r.target, r.confidence) for r in f.replacements] == [("numpy.prod", 0.85)]
    assert "Use numpy.prod instead." in f.explanation
    assert [(r.target, r.confidence) for r in dt.symbol_history("numpy", "numpy.alltrue").replacements] == [
        ("all_true_now", 0.5)
    ]


def test_a_method_named_with_a_leading_dot_is_on_the_same_class():
    # DT-CORE-23: pandas 1.5's DataFrame.iteritems says "Use .items instead."; in the index and in error text alike.
    pk = PackageSpec(name="pk", import_names=("pk",), tier="B")
    frame = {"pk": _rec("pk", "module"), "pk.Frame": _rec("pk.Frame", "class")}
    frame |= {
        "pk.Frame.items": _rec("pk.Frame.items", "function", []),
        "pk.items": _rec("pk.items", "function", []),
    }
    old = SymbolRecord(
        "pk.Frame.iteritems",
        "function",
        "pk.Frame.iteritems",
        [],
        "iteritems is deprecated. Use .items instead.",
    )
    dt = _dt(
        [
            (
                pk,
                [
                    ApiSnapshot("pk", "1.5", ["pk"], frame | {old.path: old}),
                    ApiSnapshot("pk", "2.0", ["pk"], frame),
                ],
            )
        ]
    )
    assert [(r.target, r.confidence) for r in dt.symbol_history("pk", "pk.Frame.iteritems").replacements] == [
        ("pk.Frame.items", 0.85)
    ]
    tb = "AttributeError: 'Frame' object has no attribute 'iteritems'. Use .items instead."
    f = dt.explain_error(tb, {"pk": "2.0"}).findings[0]
    assert f.symbol == "pk.Frame.iteritems"
    assert [(r.target, r.source) for r in f.replacements] == [("pk.Frame.items", "error_message")]


def test_a_member_of_an_attribute_is_not_contradicted():
    # DT-DATA-31: matplotlib 3.7's `cm.register_cmap` says to use `matplotlib.colormaps.register(name)`; `colormaps`
    # is an instance the package binds, so the index has no members under it to check.
    mpl = PackageSpec(name="matplotlib", import_names=("matplotlib",), tier="B")
    mod = {
        "matplotlib": _rec("matplotlib", "module"),
        "matplotlib.cm": _rec("matplotlib.cm", "module"),
        "matplotlib.colormaps": _rec("matplotlib.colormaps", "attribute"),
    }
    old = SymbolRecord(
        "matplotlib.cm.register_cmap",
        "function",
        "matplotlib.cm.register_cmap",
        [],
        "register_cmap was deprecated in 3.7. Use `matplotlib.colormaps.register(name)` instead.",
    )
    dt = _dt(
        [
            (
                mpl,
                [
                    ApiSnapshot("matplotlib", "3.7.0", ["matplotlib"], mod | {old.path: old}),
                    ApiSnapshot("matplotlib", "3.9.0", ["matplotlib"], mod),
                ],
            )
        ]
    )
    history = dt.symbol_history("matplotlib", "matplotlib.cm.register_cmap")
    assert [(r.target, r.confidence) for r in history.replacements] == [
        ("matplotlib.colormaps.register", 0.85)
    ]
