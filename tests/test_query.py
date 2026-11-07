import shutil
from datetime import date

from packaging.requirements import Requirement

from deptime.config import Coverage, PackageSpec, VersionPolicy, load_coverage
from deptime.index.store import IndexStore
from deptime.ingest.curated import CuratedHint
from deptime.ingest.snapshot import ApiSnapshot, ClassRecord, SymbolRecord
from deptime.models import SymbolQuery
from deptime.query import DepTime, parse_installed
from tests.conftest import HTTPX, RELEASED_AT, SNAPSHOTS
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


def test_explain_a_wrapped_error_explains_the_cause_and_says_so(dt):
    # S3 (DT-SPIKE-03): `raise RuntimeError(...) from exc` around the httpx break.
    tb = (
        HTTPX_TB
        + "\nThe above exception was the direct cause of the following exception:\n\n"
        + "Traceback (most recent call last):\n"
        + '  File "/home/u/app/main.py", line 9, in <module>\n'
        + '    raise RuntimeError("could not start the service") from exc\n'
        + "RuntimeError: could not start the service\n"
    )
    res = dt.explain_error(tb, {"httpx": "0.28.1"})
    assert (res.signal.kind, res.signal.exception_type) == ("unexpected_kwarg", "TypeError")
    assert res.findings[0].symbol == "httpx.Client.__init__(proxies)"
    assert "The last exception, RuntimeError, was raised from or while handling this TypeError" in res.note


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


def test_a_submodule_imported_as_a_name_is_a_removed_module():
    # DT-QA-01 cases: `from urllib3.contrib import appengine` and `instructor.client` name a submodule.
    spec = PackageSpec(name="pkgm", import_names=("pkgm",), tier="B")
    mod = SymbolRecord("pkgm", "module", "pkgm")
    sub = SymbolRecord("pkgm.sub", "module", "pkgm.sub")
    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot("pkgm", "1.0", ["pkgm"], {"pkgm": mod, "pkgm.sub": sub}),
            ApiSnapshot("pkgm", "2.0", ["pkgm"], {"pkgm": mod}),
        ],
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    for tb in (
        "ImportError: cannot import name 'sub' from 'pkgm' (/venv/lib/python3.12/site-packages/pkgm/__init__.py)",
        "AttributeError: module 'pkgm' has no attribute 'sub'",
    ):
        f = dt.explain_error(tb, {"pkgm": "2.0"}).findings[0]
        assert (f.symbol, f.issue, f.last_version_with) == ("pkgm.sub", "module_removed", "1.0")


def test_a_module_that_became_a_name_is_a_removed_module():
    # DT-CORE-35: fastmcp 3.2 turned the module `fastmcp.tools.tool` into the `tool` function its package exports, so
    # `No module named 'fastmcp.tools.tool'` in 4.0 read "exists in the installed version".
    spec = PackageSpec(name="pkgn", import_names=("pkgn",), tier="B")
    mod = SymbolRecord("pkgn", "module", "pkgn")
    sub = SymbolRecord("pkgn.tool", "module", "pkgn.tool")
    func = SymbolRecord("pkgn.tool", "function", "pkgn.base.tool", [])
    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot("pkgn", "1.0", ["pkgn"], {"pkgn": mod, "pkgn.tool": sub}),
            ApiSnapshot("pkgn", "2.0", ["pkgn"], {"pkgn": mod, "pkgn.tool": func}),
            ApiSnapshot("pkgn", "3.0", ["pkgn"], {"pkgn": mod, "pkgn.tool": func}),
        ],
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "  File \"app.py\", line 1, in <module>\n    from pkgn.tool import Thing\nModuleNotFoundError: No module named 'pkgn.tool'"
    f = dt.explain_error(tb, {"pkgn": "3.0"}).findings[0]
    assert (f.symbol, f.issue, f.last_version_with, f.first_version_without, f.pin_to_restore) == (
        "pkgn.tool",
        "module_removed",
        "1.0",
        "2.0",
        "pkgn<=1.0",
    )
    assert f.confidence == "medium"  # a `sys.modules` alias may keep it importable for longer (DT-DATA-52)
    assert "the function `pkgn.base.tool`" in f.explanation
    # Still a module at the installed release: nothing changed.
    assert dt.explain_error(tb, {"pkgn": "1.0"}).findings[0].issue == "present_in_installed_version"
    # The name read as an attribute is there: not an import of a module.
    f = dt.explain_error("AttributeError: module 'pkgn' has no attribute 'tool'", {"pkgn": "3.0"}).findings[0]
    assert f.issue == "present_in_installed_version"


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


def _parents_store() -> DepTime:
    """1.0 -> 2.0: the module `par.old` goes (its class `Old` re-exported as `par.Old` stays), `par.new` arrives, and
    `par.sub` turns from a module into a class that keeps the name `x`."""
    spec = PackageSpec(name="par", import_names=("par",), tier="B", own_members=False)
    rec = lambda path, kind: SymbolRecord(path, kind, path, [] if kind == "function" else None)  # noqa: E731
    both = {
        "par": rec("par", "module"),
        "par.Old": SymbolRecord("par.Old", "class", "par.old.Old"),
        "par.Old.go": SymbolRecord("par.Old.go", "function", "par.old.Old.go", []),
        "par.sub.x": rec("par.sub.x", "function"),
    }
    old = {
        **both,
        "par.old": rec("par.old", "module"),
        "par.old.Old": rec("par.old.Old", "class"),
        "par.old.Old.go": rec("par.old.Old.go", "function"),
        "par.old.helper": rec("par.old.helper", "function"),
        "par.sub": rec("par.sub", "module"),
    }
    new = {
        **both,
        "par.new": rec("par.new", "module"),
        "par.new.Fresh": rec("par.new.Fresh", "class"),
        "par.new.Fresh.go": rec("par.new.Fresh.go", "function"),
        "par.sub": rec("par.sub", "class"),
    }
    store = IndexStore.memory()
    store.rebuild_package(
        spec, [ApiSnapshot("par", "1.0", ["par"], old), ApiSnapshot("par", "2.0", ["par"], new)]
    )
    return DepTime(store, Coverage(POLICY, (spec,)))


def test_diff_versions_reports_a_used_paths_removed_parent():
    # DT-CORE-33: used_symbols kept events on a path and below, never above, so a member whose module or class went
    # (mcp 2.0's `mcp.server.fastmcp.FastMCP.tool`, langchain-community's `Qdrant.from_documents`) had no event.
    dt = _parents_store()
    used = ["par.old.Old.go", "par.old.helper", "par.Old.go", "par.sub.x", "par.new.Fresh.go"]
    res = dt.diff_versions("par", "1.0", "2.0", used_symbols=used)
    by_path = {(e.path, e.kind): e for e in res.events}
    # The outermost parent that went, once, with every used path it takes; not also the class inside it.
    assert by_path["par.old", "removed"].affects == ["par.old.Old.go", "par.old.helper"]
    assert ("par.old.Old", "removed") not in by_path
    # A path that stays across its parent's change (a re-export; a module turned class that keeps the name) isn't taken.
    assert all("par.Old.go" not in e.affects and "par.sub.x" not in e.affects for e in res.events)
    assert ("par.sub", "kind_changed") not in by_path
    # Events on the used paths themselves have no `affects`; `added` stays out unless asked for.
    assert by_path["par.old.helper", "removed"].affects == []
    assert not [e for e in res.events if e.kind == "added"]
    assert res.truncated is False and len(res.events) == len({(e.path, e.kind) for e in res.events})
    # Read back from 2.0 to 1.0 (code written for 2.0 on 1.0): the parent added in the range says 1.0 lacks the path.
    added = dt.diff_versions("par", "1.0", "2.0", used_symbols=used, include_added=True)
    assert [(e.path, e.affects) for e in added.events if e.affects and e.kind == "added"] == [
        ("par.new", ["par.new.Fresh.go"])
    ]
    one = dt.diff_versions("par", "1.0", "2.0", used_symbols=used, limit=1)
    assert len(one.events) == 1 and one.truncated is True
    # Asked for the parent too: its event is the same one, not a second copy.
    both = dt.diff_versions("par", "1.0", "2.0", used_symbols=["par.old", "par.old.helper"])
    assert [(e.path, e.affects) for e in both.events if e.path == "par.old"] == [
        ("par.old", ["par.old.helper"])
    ]


def _runtime_stores() -> DepTime:
    """`app` 1.0 (requires `lib>=1,<2`): `app.mark` an instance, `app.status` the module `lib.status` from another
    covered distribution, `app.plt.style` the module `app.style` imported under another path, `app.Kit` a class;
    `lib` 1.0 and 2.0, where 2.0 drops `lib.status.OK`."""

    def rec(path: str, kind: str, canonical: str | None = None) -> SymbolRecord:
        return SymbolRecord(path, kind, canonical or path, [] if kind == "function" else None)

    app = PackageSpec(name="app", import_names=("app",), tier="B", own_members=False)
    lib = PackageSpec(name="lib", import_names=("lib",), tier="B", own_members=False)
    store = IndexStore.memory()
    symbols = {
        "app": rec("app", "module"),
        "app.mark": rec("app.mark", "attribute", "app._marks.MARK_GEN"),
        "app.status": rec("app.status", "alias", "lib.status"),
        "app.style": rec("app.style", "module"),
        "app.style.context": rec("app.style.context", "function"),
        "app.plt": rec("app.plt", "module"),
        "app.plt.style": rec("app.plt.style", "module", "app.style"),
        "app.Kit": rec("app.Kit", "class"),
        "app.Kit.go": rec("app.Kit.go", "function"),
    }
    store.rebuild_package(app, [ApiSnapshot("app", "1.0", ["app"], symbols, requires=["lib<2,>=1"])])
    status = {"lib": rec("lib", "module"), "lib.status": rec("lib.status", "module")}
    store.rebuild_package(
        lib,
        [
            ApiSnapshot(
                "lib", "1.0", ["lib"], {**status, "lib.status.OK": rec("lib.status.OK", "attribute")}
            ),
            ApiSnapshot("lib", "2.0", ["lib"], status),
        ],
    )
    return DepTime(store, Coverage(POLICY, (app, lib)))


def test_check_symbols_says_unknown_where_the_index_cant_see(monkeypatch):
    # DT-CORE-32: all 138 of check_symbols' false alarms on the 2026-11 holdout were `missing`, 77 of them paths through
    # an object the index can't look inside (`pytest.mark.parametrize`, `django.conf.settings.configure`).
    dt = _runtime_stores()
    ask = lambda path: dt.check_symbols("app", "1.0", [SymbolQuery(path=path)]).results[0]  # noqa: E731
    got = ask("app.mark.parametrize")
    assert got.status == "unknown" and got.hint.startswith("app.mark is an attribute (`app._marks.MARK_GEN`)")
    assert ask("app.mark.parametrize.with_args").status == "unknown"
    # A module or class lists its names: still `missing`.
    assert ask("app.nothing").status == "missing" and ask("app.Kit.gone").status == "missing"
    # An alias into another covered distribution: checked there, at the newest release this one allows (lib 1.0).
    ok = ask("app.status.OK")
    assert ok.status == "present" and "`lib.status.OK` in lib 1.0" in ok.hint
    assert ask("app.status.NOPE").status == "missing"
    # A module imported under another path: its own names.
    style = ask("app.plt.style.context")
    assert (style.path, style.status) == ("app.plt.style.context", "present")
    assert ask("app.plt.style.nothing").status == "missing"
    # Not required by this release, or not covered: unknown, and says which.
    monkeypatch.setattr(dt.store, "requirement", lambda *a: None)
    assert "doesn't require" in ask("app.status.OK").hint
    dt.coverage = Coverage(POLICY, (dt.coverage.packages[0],))
    nope = ask("app.status.OK")
    assert nope.status == "unknown" and "which DepTime doesn't index (lib)" in nope.hint


def _format19_stores() -> DepTime:
    """`web` 1.0 (own members; requires `base-dist>=1` and `web-extra`): `App` inherits from `base_dist.core.Base`
    (covered), `Color` from `enum.Enum`, `Far` from `faraway.Thing` (not covered), `Plain` from nothing outside;
    `Configure.Generative` is the class `web.Inner`; `web` is a namespace package. `base-dist` 1.0 has `Base.mount`."""

    def rec(path: str, kind: str, canonical: str | None = None) -> SymbolRecord:
        return SymbolRecord(path, kind, canonical or path, [] if kind == "function" else None)

    web = PackageSpec(name="web", import_names=("web",), tier="B", own_members=True)
    base = PackageSpec(name="base-dist", import_names=("base_dist",), tier="B", own_members=True)
    classes = {
        "web.App": ClassRecord("web.App", [], external=["base_dist.core.Base"]),
        "web.Color": ClassRecord("web.Color", [], external=["enum.Enum"]),
        "web.Far": ClassRecord("web.Far", [], external=["faraway.Thing"]),
        "web.Plain": ClassRecord("web.Plain", []),
        "web.Configure": ClassRecord(
            "web.Configure", [], {"Generative": rec("web.Configure.Generative", "class", "web.Inner")}
        ),
        "web.Inner": ClassRecord("web.Inner", [], {"cohere": rec("web.Inner.cohere", "function")}),
    }
    symbols = {"web": rec("web", "module"), **{c: rec(c, "class") for c in classes}}
    snap = ApiSnapshot(
        "web",
        "1.0",
        ["web"],
        symbols,
        requires=["base-dist>=1", "web-extra"],
        own_members=True,
        classes=classes,
        namespaces=["web"],
    )
    store = IndexStore.memory()
    store.rebuild_package(web, [snap])
    based = {
        "base_dist.core.Base": ClassRecord(
            "base_dist.core.Base", [], {"mount": rec("base_dist.core.Base.mount", "function")}
        )
    }
    store.rebuild_package(
        base,
        [
            ApiSnapshot(
                "base-dist",
                "1.0",
                ["base_dist"],
                {
                    "base_dist": rec("base_dist", "module"),
                    "base_dist.core": rec("base_dist.core", "module"),
                    "base_dist.core.Base": rec("base_dist.core.Base", "class"),
                },
                own_members=True,
                classes=based,
            )
        ],
    )
    return DepTime(store, Coverage(POLICY, (web, base)))


def test_check_symbols_looks_in_bases_from_other_distributions_and_namespaces():
    # DT-CORE-32 shapes 2-4: fastapi's `FastAPI.mount` is starlette's, langgraph is a namespace package that
    # langgraph-checkpoint installs into, weaviate's `Configure.Generative` is a class.
    dt = _format19_stores()
    ask = lambda path: dt.check_symbols("web", "1.0", [SymbolQuery(path=path)]).results[0]  # noqa: E731
    mount = ask("web.App.mount")
    assert mount.status == "present" and mount.hint.startswith("inherited from `base_dist.core.Base`")
    assert ask("web.App.nothing").status == "missing"  # the base is covered and lacks it
    color = ask("web.Color.value")
    assert (color.status, color.hint) == ("present", "inherited from `enum.Enum`, in the standard library")
    assert ask("web.Color.nothing").status == "missing"
    far = ask("web.Far.anything")
    assert far.status == "unknown" and "`faraway.Thing`" in far.hint
    assert ask("web.Plain.nothing").status == "missing"
    assert ask("web.Configure.Generative.cohere").status == "present"
    assert ask("web.Configure.Generative.nothing").status == "missing"
    ns = ask("web.store.memory")
    assert ns.status == "unknown" and "namespace package" in ns.hint and "web-extra" in ns.hint


def test_diff_versions_keeps_a_member_the_class_still_inherits_from_another_distribution():
    # DT-CORE-32: sqlmodel 0.0.12 dropped its own `Session.get`; sqlalchemy's `Session.get` stays, so the use works.
    dt = _format19_stores()
    web = dt.coverage.packages[0]
    own = SymbolRecord("web.App.mount", "function", "web.App.mount", [])
    first = dt.store.snapshot_at(web, "1.0")
    first.classes["web.App"].members["mount"] = own
    second = dt.store.snapshot_at(web, "1.0")
    second.version = "2.0"
    dt.store.rebuild_package(web, [first, second])
    stored = dt.diff_versions("web", "1.0", "2.0")
    assert ("web.App.mount", "removed") in {
        (e.path, e.kind) for e in stored.events
    }  # the class's own member went
    used = dt.diff_versions("web", "1.0", "2.0", used_symbols=["web.App.mount"])
    assert used.events == [] and used.truncated is False


def test_diff_versions_reports_a_used_class_constructor_at_the_used_path():
    # 2026-11 holdout: `fastmcp.client.transports.StdioTransport(log_file=...)` on 2.12; the `log_file` event was at
    # the class's label, `fastmcp.client.StdioTransport.__init__`, so a caller matching its own path saw nothing.
    spec = PackageSpec(name="tp", import_names=("tp",), tier="B", own_members=True)

    def snap(version: str, params: list[dict]) -> ApiSnapshot:
        init = SymbolRecord("tp.impl.Thing.__init__", "function", "tp.impl.Thing.__init__", params)
        symbols = {
            "tp": SymbolRecord("tp", "module", "tp"),
            "tp.Thing": SymbolRecord("tp.Thing", "class", "tp.impl.Thing"),
            "tp.impl": SymbolRecord("tp.impl", "module", "tp.impl"),
            "tp.impl.Thing": SymbolRecord("tp.impl.Thing", "class", "tp.impl.Thing"),
        }
        classes = {"tp.impl.Thing": ClassRecord("tp.impl.Thing", [], {"__init__": init})}
        return ApiSnapshot("tp", version, ["tp"], symbols, own_members=True, classes=classes)

    store = IndexStore.memory()
    store.rebuild_package(
        spec, [snap("1.0", [_param("cmd")]), snap("2.0", [_param("cmd"), _param("log_file")])]
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    res = dt.diff_versions("tp", "1.0", "2.0", used_symbols=["tp.impl.Thing"])
    assert [(e.path, e.kind, e.detail["param"]) for e in res.events] == [
        ("tp.impl.Thing.__init__", "param_added", "log_file")
    ]


def test_a_sys_modules_alias_extends_the_last_release_a_module_imports():
    # DT-DATA-52: fastmcp's `tools/tool.py` went in 3.2, but `sys.modules` kept `fastmcp.tools.tool` importable
    # (as `fastmcp.tools.base`) through 3.4, and 4.0 left only the `tool` function.
    spec = PackageSpec(name="pkga", import_names=("pkga",), tier="B")
    mod = SymbolRecord("pkga", "module", "pkga")
    sub = SymbolRecord("pkga.tool", "module", "pkga.tool")
    func = SymbolRecord("pkga.tool", "function", "pkga.base.tool", [])
    snaps = [
        ApiSnapshot("pkga", "1.0", ["pkga"], {"pkga": mod, "pkga.tool": sub}),
        ApiSnapshot(
            "pkga",
            "2.0",
            ["pkga"],
            {"pkga": mod, "pkga.tool": func},
            module_aliases={"pkga.tool": "pkga.base"},
        ),
        ApiSnapshot("pkga", "3.0", ["pkga"], {"pkga": mod, "pkga.tool": func}),
    ]
    store = IndexStore.memory()
    store.rebuild_package(spec, snaps)
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "ModuleNotFoundError: No module named 'pkga.tool'"
    f = dt.explain_error(tb, {"pkga": "3.0"}).findings[0]
    assert (f.issue, f.last_version_with, f.first_version_without) == ("module_removed", "2.0", "3.0")
    assert "through a `sys.modules` alias" in f.explanation
    assert store.snapshot_at(spec, "2.0").module_aliases == {"pkga.tool": "pkga.base"}


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


def curated_store() -> tuple[IndexStore, PackageSpec]:
    """p8.Client is removed in 2.0 with no deprecation text; p8.connect(timeout) loses its argument."""
    spec = PackageSpec(name="p8", import_names=("p8",), tier="B")
    mod = SymbolRecord("p8", "module", "p8")
    v1 = {
        "p8": mod,
        "p8.Old": SymbolRecord(
            "p8.Old", "class", "p8.Old", None, "Old is deprecated, use p8.Session instead."
        ),
        "p8.Client": SymbolRecord("p8.Client", "class", "p8.Client"),
        "p8.connect": SymbolRecord("p8.connect", "function", "p8.connect", [_param("timeout")]),
        # The same object under other paths: a re-export and a subclass's inherited method.
        "p8.compat.Client": SymbolRecord("p8.compat.Client", "class", "p8.Client"),
        "p8.Base.find_by_id": SymbolRecord("p8.Base.find_by_id", "function", "p8.Base.find_by_id", []),
        "p8.Chrome.find_by_id": SymbolRecord("p8.Chrome.find_by_id", "function", "p8.Base.find_by_id", []),
    }
    v2 = {
        "p8": mod,
        "p8.Session": SymbolRecord("p8.Session", "class", "p8.Session"),
        "p8.connect": SymbolRecord("p8.connect", "function", "p8.connect", [_param("timeouts")]),
    }
    url = "https://example.org/p8/migration#v2"
    curated = [
        CuratedHint("p8", "p8.Client", "p8.Session", "`Client` is replaced by `Session`.", url),
        CuratedHint("p8", "p8.connect(timeout)", "timeouts", "Pass `timeouts` instead of `timeout`.", url),
        CuratedHint("p8", "p8.Client", "q8.Session", "Or use the `q8` package's `Session`.", url),
        CuratedHint("p8", "p8.Missing", "p8.Session", "Not a name p8 ever had.", url),
        CuratedHint("p8", "p8.connect(retries)", "attempts", "Not an argument p8.connect ever had.", url),
        CuratedHint("p8", "p8.Base.find_by_id", "p8.Base.find", "Use `find` instead.", url),
        CuratedHint("p8", "p8.Old", "p8.Session", "`Old` is replaced by `Session`.", url),
    ]
    store = IndexStore.memory()
    store.rebuild_package(
        spec, [ApiSnapshot("p8", "1.0", ["p8"], v1), ApiSnapshot("p8", "2.0", ["p8"], v2)], curated=curated
    )
    return store, spec


def test_curated_hints_enter_the_index_with_their_source():
    # DT-DATA-08: a curated hint from a migration guide, for a name the index has, at 0.9 with its quote and URL;
    # its target may be another package's (DT-DATA-07). One for a name or argument the package never had is
    # left out.
    store, spec = curated_store()

    def hints(path: str) -> list[tuple[str, float, str]]:
        return [(r["target"], r["confidence"], r["source"]) for r in store.replacements("p8", path)]

    assert hints("p8.Client") == [("p8.Session", 0.9, "curated"), ("q8.Session", 0.9, "curated")]
    assert hints("p8.connect(timeout)") == [("timeouts", 0.9, "curated")]
    assert hints("p8.Missing") == [] and hints("p8.connect(retries)") == []
    # A pair the library's own text already gives keeps that source.
    assert hints("p8.Old") == [("p8.Session", 0.85, "deprecation_message")]
    # Every path of the same object gets it: a re-export, an inherited method.
    assert hints("p8.compat.Client") == hints("p8.Client")
    assert hints("p8.Chrome.find_by_id") == [("p8.Base.find", 0.9, "curated")]

    dt = DepTime(store, Coverage(POLICY, (spec,)))
    sym = dt.explain_error(
        "ImportError: cannot import name 'Client' from 'p8' (/x.py)", {"p8": "2.0"}
    ).findings[0]
    assert sym.issue == "symbol_removed" and "Use p8.Session" in sym.explanation
    assert sym.replacements[0].source == "curated"
    assert (
        sym.replacements[0].evidence
        == "`Client` is replaced by `Session`. (https://example.org/p8/migration#v2)"
    )
    par = dt.explain_error("TypeError: connect() got an unexpected keyword argument 'timeout'", {"p8": "2.0"})
    assert [(r.target, r.source) for r in par.findings[0].replacements] == [("timeouts", "curated")]


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


WEAVIATE = PackageSpec(name="weaviate-client", import_names=("weaviate",), tier="B")
OPENAI_REQUIRES = ["anyio<5,>=3.5.0", "httpx<1,>=0.23.0", 'numpy>=1; extra == "datalib"']
USER_UNDER_PYTEST_TB = """\
Traceback (most recent call last):
  File "/venv/lib/python3.12/site-packages/_pytest/python.py", line 159, in pytest_pyfunc_call
    result = testfunction(**testargs)
  File "/app/tests/test_api.py", line 8, in test_get
    client = Client(proxies=PROXIES)
TypeError: Client.__init__() got an unexpected keyword argument 'proxies'
"""


def _weaviate_snap(version: str, *, proxies: bool) -> ApiSnapshot:
    init = [_param("url"), *([_param("proxies")] if proxies else [])]
    sym = {
        p: SymbolRecord(p, k, p, params)
        for p, k, params in [
            ("weaviate", "module", None),
            ("weaviate.Client", "class", None),
            ("weaviate.Client.__init__", "function", init),
        ]
    }
    return ApiSnapshot("weaviate-client", version, ["weaviate"], sym)


def _openai_snap(version: str, requires: list[str] | None) -> ApiSnapshot:
    sym = {"openai": SymbolRecord("openai", "module", "openai")}
    return ApiSnapshot("openai", version, ["openai"], sym, requires=requires)


def test_the_packages_the_raising_code_requires_are_searched_first(store):
    # DT-CORE-16: weaviate-client also removed `Client(proxies)`, but openai, whose code raised, doesn't require it.
    store.rebuild_package(
        WEAVIATE, [_weaviate_snap("4.9.6", proxies=True), _weaviate_snap("4.10.0", proxies=False)]
    )
    # An earlier openai release that required weaviate-client (made up): the installed release's requirements count.
    store.rebuild_package(
        OPENAI, [_openai_snap("1.40.0", ["weaviate-client>=4"]), _openai_snap("1.55.0", OPENAI_REQUIRES)]
    )
    dt = DepTime(store, Coverage(POLICY, (HTTPX, OPENAI, WEAVIATE)))
    everything = {"openai": "1.55.0", "httpx": "0.28.1", "weaviate-client": "4.10.0"}

    res = dt.explain_error(OPENAI_INTERNAL_TB, everything)
    assert [(f.package, f.issue, f.confidence) for f in res.findings] == [("httpx", "param_removed", "high")]
    assert "the change is in httpx, which it lists in its requirements." in res.note
    # A requirement outranks being installed, since installed_versions may list only some packages ...
    res = dt.explain_error(OPENAI_INTERNAL_TB, {"openai": "1.55.0", "weaviate-client": "4.10.0"})
    assert [(f.package, f.confidence) for f in res.findings] == [("httpx", "low")]
    # ... and without versions, any indexed openai release's requirements count.
    assert sorted(f.package for f in dt.explain_error(OPENAI_INTERNAL_TB).findings) == [
        "httpx",
        "weaviate-client",
    ]

    # A requirement only ranks: when openai's are unknown, every package with the change is reported.
    store.rebuild_package(OPENAI, [_openai_snap("1.55.0", None)])
    res = dt.explain_error(OPENAI_INTERNAL_TB, everything)
    assert sorted(f.package for f in res.findings) == ["httpx", "weaviate-client"]
    assert "httpx, weaviate-client, which it calls." in res.note
    # Raised in the user's own code under pytest's frames: pytest isn't the caller.
    res = dt.explain_error(USER_UNDER_PYTEST_TB)
    assert sorted(f.package for f in res.findings) == ["httpx", "weaviate-client"]
    assert "made inside" not in res.note


def test_requirements_round_trip_and_an_index_without_them_still_opens(tmp_path):
    # DT-DATA-14: each release's Requires-Dist, NULL when unknown; released clients never read the column.
    snaps = [
        ApiSnapshot("openai", "1.0.0", ["openai"], {}, requires=OPENAI_REQUIRES),
        ApiSnapshot("openai", "1.1.0", ["openai"], {}),
        ApiSnapshot("openai", "1.2.0", ["openai"], {}, requires=["httpx>=0.23", "Jiter<1"]),
    ]
    path = tmp_path / "index.sqlite"
    store = IndexStore.open(path, readonly=False)
    store.rebuild_package(OPENAI, snaps)
    assert store.declared_dependencies("openai", 0) == {"anyio", "httpx", "numpy"}
    assert store.declared_dependencies("openai", 2) == {"httpx", "jiter"}
    union = {"anyio", "httpx", "numpy", "jiter"}
    assert store.declared_dependencies("openai", 1) == store.declared_dependencies("openai") == union
    assert store.declared_dependencies("httpx") == set()
    assert [store.snapshot_at(OPENAI, v).requires for v in ("1.0.0", "1.1.0")] == [OPENAI_REQUIRES, None]

    store.conn.execute("ALTER TABLE releases DROP COLUMN requires")
    store.close()
    old = IndexStore.open(path)
    assert old.declared_dependencies("openai") == set() and old.snapshot_at(OPENAI, "1.0.0").requires is None
    old.close()
    again = IndexStore.open(path, readonly=False)  # the nightly adds to the published index
    again.rebuild_package(OPENAI, snaps)
    assert again.declared_dependencies("openai", 2) == {"httpx", "jiter"}


GENAI = PackageSpec(name="google-genai", import_names=("google.genai",), tier="A")


def test_suffix_lookups_find_members_and_paths_whose_parent_has_no_row(store):
    # A dotted suffix is matched through its parent's tail (DT-CORE-16); `google`, above `google.genai`, has no row.
    sym = {
        p: SymbolRecord(p, k, p, params)
        for p, k, params in [
            ("google.genai", "module", None),
            ("google.genai.Client", "class", None),
            ("google.genai.Client.__init__", "function", [_param("api_key")]),
        ]
    }
    store.rebuild_package(GENAI, [ApiSnapshot("google-genai", "1.0.0", ["google.genai"], sym)])
    assert store.packages_with_suffix("Client.__init__") == ["google-genai", "httpx"]
    assert store.find_by_suffix(["httpx", "google-genai"], "Client.__init__") == [
        ("httpx", "httpx.Client.__init__"),
        ("google-genai", "google.genai.Client.__init__"),
    ]
    assert store.find_by_suffix(["httpx", "google-genai"], "Client.__init__", limit=1) == [
        ("httpx", "httpx.Client.__init__")
    ]
    assert store.packages_with_suffix("google.genai") == ["google-genai"]
    assert store.find_by_suffix(["google-genai"], "google.genai") == [("google-genai", "google.genai")]
    assert store.packages_with_suffix("genai.Client.__init__") == ["google-genai"]
    assert store.packages_with_suffix("Client") == ["google-genai", "httpx"]
    assert store.packages_with_suffix("Client.missing") == []
    assert store.find_by_suffix(["httpx"], "google.genai") == []


class _Recorder:
    """Records each statement an IndexStore runs, so a test can ask SQLite how it plans them."""

    def __init__(self, conn):
        self.conn, self.calls = conn, []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.conn.execute(sql, params)

    def plans(self) -> list[str]:
        out = [
            " | ".join(r[3] for r in self.conn.execute("EXPLAIN QUERY PLAN " + s, p)) for s, p in self.calls
        ]
        self.calls.clear()
        return out


def test_lookups_by_path_and_suffix_use_their_indexes(store):
    # DT-CORE-16: each wrong plan read every row of a package; on the published index the search of every package
    # for an error's owner took up to a second. Without ANALYZE statistics, SQLite's choice follows the query text.
    real = store.conn
    store.stores_own_members("httpx")  # read once per store (DT-DATA-16), not per lookup
    rec = store.conn = _Recorder(real)
    try:
        # httpx is stored with own members (DT-DATA-49): a member's events come from its class's rows, each read
        # by its key.
        store.events("httpx", "httpx.Client.__init__")
        plans = rec.plans()
        assert "events_by_path" in plans[0] and all(p.startswith("SEARCH") for p in plans), plans
        # A bare name across every package: one lookup per package, each by its index.
        store.packages_with_suffix("Client")
        plans = rec.plans()[1:]  # after the list of packages
        assert plans and all("SCAN spans" not in p and "SCAN m" not in p for p in plans), plans
        assert all(
            "spans_by_tail (tail=? AND package=?)" in p or "PRIMARY KEY (package=? AND name=?)" in p
            for p in plans
        ), plans
        for packages, suffix in [
            (["httpx"], "Client"),
            (["httpx"], "Client.__init__"),
            (None, "Client.__init__"),
        ]:
            if packages is None:
                store.packages_with_suffix(suffix)
            else:
                store.find_by_suffix(packages, suffix)
            # A bare name's results are each checked by path (DT-DATA-49).
            plan, *checks = rec.plans()
            assert all("(package=? AND path=?)" in c for c in checks), checks
            assert "USING INDEX spans_by_tail" in plan, (suffix, plan)
            assert "spans_1 (package=?)" not in plan and "SCAN spans" not in plan, (suffix, plan)
            # Members by name: never every member row of a package.
            assert "SEARCH m USING PRIMARY KEY (package=?)" not in plan, (suffix, plan)
    finally:
        store.conn = real


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
    assert store.snapshot_at(spec, "5.1").expanded() == snaps[1].symbols
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
    assert again.snapshot_at(spec, "5.1").expanded() == snaps[1].symbols


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


def _internal_store() -> DepTime:
    """pymongo/qdrant/instructor/sqlmodel shapes (DT-DATA-22), as internal rows of package `ipkg`."""
    spec = PackageSpec(name="ipkg", import_names=("ipkg",), tier="B")

    def mod(path: str) -> SymbolRecord:
        return SymbolRecord(path, "module", path)

    def snap(version: str, public: list[SymbolRecord], internal: list[SymbolRecord]) -> ApiSnapshot:
        base = [mod("ipkg"), mod("ipkg.cursor"), mod("ipkg.core"), mod("ipkg.response"), mod("ipkg.shared")]
        return ApiSnapshot(
            "ipkg",
            version,
            ["ipkg"],
            {r.path: r for r in base + public},
            internal={r.path: r for r in internal},
        )

    handle = SymbolRecord("ipkg.response.handle", "function", "ipkg.response.handle", [])
    opts = SymbolRecord("ipkg.cursor._OPTS", "attribute", "ipkg.cursor._OPTS")
    opts_moved = SymbolRecord("ipkg.shared._OPTS", "attribute", "ipkg.shared._OPTS")
    handle_import = SymbolRecord("ipkg.core.handle", "function", "ipkg.response.handle")
    conn = SymbolRecord("ipkg.core.AsyncConnection", "alias", "sqlalchemy.ext.asyncio.engine.AsyncConnection")
    models_public = SymbolRecord("ipkg.core.MODELS", "attribute", "ipkg.shared.MODELS")
    models_import = SymbolRecord("ipkg.core.MODELS", "attribute", "ipkg.shared.MODELS")
    models = SymbolRecord("ipkg.shared.MODELS", "attribute", "ipkg.shared.MODELS")
    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            # 1.0: MODELS is public at ipkg.core; 2.0: only imported there; 3.0: gone from ipkg.core.
            snap("1.0", [handle, models_public, models], [opts, handle_import, conn]),
            snap("2.0", [handle, models], [opts, handle_import, conn, models_import]),
            snap("3.0", [handle, models], [opts_moved]),
        ],
    )
    return DepTime(store, Coverage(POLICY, (spec,)))


def test_a_removed_internal_name_is_removed_not_never_existed():
    # DT-DATA-22: motor 3.5.1 imports pymongo 4.8's `pymongo.cursor._QUERY_OPTIONS`; 4.9 defines it elsewhere.
    dt = _internal_store()
    tb = "ImportError: cannot import name '_OPTS' from 'ipkg.cursor' (/venv/lib/python3.12/site-packages/ipkg/cursor.py)"
    f = dt.explain_error(tb, {"ipkg": "3.0"}).findings[0]
    assert (f.symbol, f.issue, f.last_version_with, f.first_version_without) == (
        "ipkg.cursor._OPTS",
        "symbol_removed",
        "2.0",
        "3.0",
    )
    assert f.internal and f.pin_to_restore == "ipkg<=2.0"
    assert "not public API" in f.explanation
    # The same name is defined in exactly one other module once it's gone here.
    assert [(r.target, r.confidence, r.source) for r in f.replacements] == [
        ("ipkg.shared._OPTS", 0.6, "moved_heuristic")
    ]
    # Present in the installed release: the error has another cause.
    f = dt.explain_error(tb, {"ipkg": "2.0"}).findings[0]
    assert (f.issue, f.internal) == ("present_in_installed_version", True)


def test_a_removed_import_points_to_the_object_it_imported():
    dt = _internal_store()
    # instructor's `core.patch.handle_response_model`: an import whose object is still public where it's defined.
    f = dt.explain_error(
        "AttributeError: module 'ipkg.core' has no attribute 'handle'", {"ipkg": "3.0"}
    ).findings[0]
    assert (f.issue, f.last_version_with, f.internal) == ("symbol_removed", "2.0", True)
    assert [(r.target, r.confidence) for r in f.replacements] == [("ipkg.response.handle", 0.75)]
    assert "Use ipkg.response.handle instead." in f.explanation
    # sqlmodel's `AsyncConnection`, imported from sqlalchemy: that's where it is defined.
    tb = "ImportError: cannot import name 'AsyncConnection' from 'ipkg.core'"
    f = dt.explain_error(tb, {"ipkg": "3.0"}).findings[0]
    assert (f.issue, f.last_version_with) == ("symbol_removed", "2.0")
    assert [(r.target, r.confidence) for r in f.replacements] == [
        ("sqlalchemy.ext.asyncio.engine.AsyncConnection", 0.6)
    ]


def test_a_name_public_then_internal_is_removed_when_it_goes_from_both():
    # qdrant-client: `qdrant_fastembed.IDF_EMBEDDING_MODELS` stopped being public in 1.14 but was importable to 1.18.
    dt = _internal_store()
    f = dt.explain_error(
        "ImportError: cannot import name 'MODELS' from 'ipkg.core'", {"ipkg": "3.0"}
    ).findings[0]
    assert (f.issue, f.last_version_with, f.first_version_without, f.internal) == (
        "symbol_removed",
        "2.0",
        "3.0",
        True,
    )
    assert "It was public API up to ipkg 1.0." in f.explanation
    assert [r.target for r in f.replacements] == ["ipkg.shared.MODELS"]


def test_check_and_history_know_internal_names():
    dt = _internal_store()
    r = dt.check_symbols("ipkg", "2.0", [SymbolQuery(path="ipkg.cursor._OPTS")]).results[0]
    assert (r.status, r.internal) == ("present", True)
    r = dt.check_symbols("ipkg", "3.0", [SymbolQuery(path="ipkg.cursor._OPTS")]).results[0]
    assert (r.status, r.internal) == ("missing", True)
    assert r.hint.startswith("seen in 1.0..2.0")
    h = dt.symbol_history("ipkg", "ipkg.cursor._OPTS")
    assert (h.ever_seen, h.first_seen, h.last_seen, h.present_in_latest, h.internal) == (
        True,
        "1.0",
        "2.0",
        False,
        True,
    )
    assert "not public API" in h.note


def test_an_index_without_internal_rows_still_answers():
    # Indexes built before DT-DATA-22 have no internal_spans table.
    dt = _internal_store()
    dt.store.conn.execute("DROP TABLE internal_spans")
    f = dt.explain_error(
        "ImportError: cannot import name '_OPTS' from 'ipkg.cursor'", {"ipkg": "3.0"}
    ).findings[0]
    assert (f.issue, f.internal) == ("never_existed", False)


def test_a_private_member_of_a_class_is_not_claimed_never_to_have_existed():
    # weaviate's `_Collections._get_all`: class members that aren't public aren't indexed, so DepTime can't tell.
    spec = PackageSpec(name="cpkg", import_names=("cpkg",), tier="B")
    snap = ApiSnapshot(
        "cpkg",
        "1.0",
        ["cpkg"],
        {"cpkg": SymbolRecord("cpkg", "module", "cpkg")},
        internal={"cpkg._Things": SymbolRecord("cpkg._Things", "class", "cpkg._Things")},
    )
    store = IndexStore.memory()
    store.rebuild_package(spec, [snap])
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    f = dt.explain_error(
        "AttributeError: module 'cpkg' has no attribute '_Things'", {"cpkg": "1.0"}
    ).findings[0]
    assert (f.issue, f.internal) == ("present_in_installed_version", True)
    f = dt._symbol_finding(spec, "cpkg._Things._get_all", *store.resolve_version("cpkg", "1.0"), "1.0")
    assert (f.issue, f.confidence) == ("never_existed", "low")
    assert "members of classes" in f.explanation


def test_an_internal_name_moved_to_one_of_two_modules_gets_no_hint():
    spec = PackageSpec(name="apkg", import_names=("apkg",), tier="B")
    mods = {p: SymbolRecord(p, "module", p) for p in ("apkg", "apkg.a", "apkg.b", "apkg.c")}

    def rec(path: str) -> SymbolRecord:
        return SymbolRecord(path, "function", path)

    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot("apkg", "1.0", ["apkg"], mods, internal={"apkg.a._f": rec("apkg.a._f")}),
            ApiSnapshot(
                "apkg", "2.0", ["apkg"], mods, internal={p: rec(p) for p in ("apkg.b._f", "apkg.c._f")}
            ),
        ],
    )
    assert store.replacements("apkg", "apkg.a._f") == []
    # A release carried over from the index keeps its internal names.
    again = store.snapshot_at(spec, "2.0")
    assert (
        set(again.internal) == {"apkg.b._f", "apkg.c._f"} and again.internal["apkg.b._f"].kind == "function"
    )


def test_internal_moves_and_deprecations_follow_the_rows():
    spec = PackageSpec(name="dpkg", import_names=("dpkg",), tier="B")
    mods = {p: SymbolRecord(p, "module", p) for p in ("dpkg", "dpkg.core", "dpkg.a", "dpkg.b", "dpkg.c")}

    def rec(path: str, canonical: str | None = None, kind: str = "attribute") -> SymbolRecord:
        return SymbolRecord(path, kind, canonical or path)

    stack = SymbolRecord("dpkg._stack", "getattr", "dpkg.__getattr__")
    warned = SymbolRecord("dpkg._stack", "getattr", "dpkg.__getattr__", None, "'_stack' is deprecated.")
    releases = {
        "1.0": [rec("dpkg.core._x", "dpkg.a._x"), rec("dpkg.a._x"), rec("dpkg.core._y"), stack],
        # flask 2.3: the module __getattr__ now warns.
        "2.0": [
            rec("dpkg.core._x", "dpkg.a._x"),
            rec("dpkg.a._x"),
            rec("dpkg.core._y", "dpkg.b._y"),
            rec("dpkg.b._y"),
            warned,
        ],
        # The imported object is gone too: the one module that now defines `_x` is the hint, not the old target.
        "3.0": [rec("dpkg.c._x"), rec("dpkg.core._y", "dpkg.b._y"), rec("dpkg.b._y")],
    }
    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot("dpkg", v, ["dpkg"], mods, internal={r.path: r for r in recs})
            for v, recs in releases.items()
        ],
    )
    assert [(r["target"], r["confidence"]) for r in store.replacements("dpkg", "dpkg.core._x")] == [
        ("dpkg.c._x", 0.6)
    ]
    # `_y` changed from a definition to an import but never went away: no hint.
    assert store.replacements("dpkg", "dpkg.core._y") == []
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    f = dt.explain_error("ImportError: cannot import name '_stack' from 'dpkg'", {"dpkg": "3.0"}).findings[0]
    assert (f.issue, f.last_version_with, f.deprecated_in, f.deprecation_message) == (
        "symbol_removed",
        "2.0",
        "2.0",
        "'_stack' is deprecated.",
    )
    assert "`__getattr__` serves the private name `_stack`" in f.explanation


def test_an_inherited_member_is_named_on_the_class_that_defines_it():
    # DT-CORE-28: Django 4.0 removed `HttpRequest.is_ajax()`; the error names the subclass, `WSGIRequest`.
    spec = PackageSpec(name="dj", import_names=("dj",), tier="B")
    canonical = "dj.http.request.HttpRequest.is_ajax"
    mods = {
        p: SymbolRecord(p, "module", p)
        for p in ("dj", "dj.http", "dj.http.request", "dj.core", "dj.core.wsgi")
    }
    classes = {
        p: SymbolRecord(p, "class", c)
        for p, c in (
            ("dj.http.HttpRequest", "dj.http.request.HttpRequest"),
            ("dj.http.request.HttpRequest", "dj.http.request.HttpRequest"),
            ("dj.core.wsgi.WSGIRequest", "dj.core.wsgi.WSGIRequest"),
        )
    }
    members = {
        p: SymbolRecord(p, "function", canonical, [])
        for p in (
            "dj.http.HttpRequest.is_ajax",
            "dj.http.request.HttpRequest.is_ajax",
            "dj.core.wsgi.WSGIRequest.is_ajax",
        )
    }
    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot("dj", "3.2", ["dj"], {**mods, **classes, **members}),
            ApiSnapshot("dj", "4.0", ["dj"], {**mods, **classes}),
        ],
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "AttributeError: 'WSGIRequest' object has no attribute 'is_ajax'"
    findings = dt.explain_error(tb, {"dj": "4.0"}).findings
    assert [(f.symbol, f.issue, f.last_version_with) for f in findings] == [
        ("dj.http.HttpRequest.is_ajax", "symbol_removed", "3.2")
    ]
    assert findings[0].explanation.startswith("WSGIRequest inherits is_ajax from HttpRequest.")
    # A member from a mixin deeper in the package than the class users import stays on that class.
    mixin = "dj.core.mixins.FastMixin.vector_params"
    store.rebuild_package(
        spec,
        [
            ApiSnapshot(
                "dj",
                "3.2",
                ["dj"],
                {
                    **mods,
                    **classes,
                    **members,
                    "dj.Client": SymbolRecord("dj.Client", "class", "dj.core.client.Client"),
                    "dj.Client.vector_params": SymbolRecord("dj.Client.vector_params", "function", mixin, []),
                    mixin: SymbolRecord(mixin, "function", mixin, []),
                },
            ),
            ApiSnapshot("dj", "4.0", ["dj"], {**mods, **classes}),
        ],
    )
    f = dt.explain_error(
        "AttributeError: 'Client' object has no attribute 'vector_params'", {"dj": "4.0"}
    ).findings[0]
    assert (f.symbol, f.issue) == ("dj.Client.vector_params", "symbol_removed")
    assert "inherits" not in f.explanation
    # A member the subclass defines itself stays on the subclass.
    assert (
        dt.explain_error("AttributeError: 'HttpRequest' object has no attribute 'is_ajax'", {"dj": "4.0"})
        .findings[0]
        .symbol
        == "dj.http.HttpRequest.is_ajax"
    )


def test_an_argument_forwarded_to_the_parent_is_found_on_the_called_class():
    # DT-CORE-27: gradio 6 took `theme` out of Blocks.__init__, which passes **kwargs on to BlockContext.__init__.
    from tests.test_tracebacks import GRADIO_TB

    spec = PackageSpec(name="gradio", import_names=("gradio",), tier="B")
    base = {
        "gradio": SymbolRecord("gradio", "module", "gradio"),
        "gradio.Blocks": SymbolRecord("gradio.Blocks", "class", "gradio.Blocks"),
        "gradio.BlockContext": SymbolRecord("gradio.BlockContext", "class", "gradio.BlockContext"),
        "gradio.BlockContext.__init__": SymbolRecord(
            "gradio.BlockContext.__init__", "function", "gradio.BlockContext.__init__", [_param("render")]
        ),
    }

    def blocks(*params: str) -> SymbolRecord:
        kwargs = {"name": "kwargs", "kind": "variadic keyword", "default": None, "annotation": None}
        return SymbolRecord(
            "gradio.Blocks.__init__", "function", "gradio.Blocks.__init__", [*map(_param, params), kwargs]
        )

    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot(
                "gradio", "5.50.0", ["gradio"], {**base, "gradio.Blocks.__init__": blocks("theme", "title")}
            ),
            ApiSnapshot("gradio", "6.0.0", ["gradio"], {**base, "gradio.Blocks.__init__": blocks("title")}),
        ],
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    f = dt.explain_error(GRADIO_TB, {"gradio": "6.0.0"}).findings[0]
    assert (f.symbol, f.issue, f.last_version_with) == (
        "gradio.Blocks.__init__(theme)",
        "param_removed",
        "5.50.0",
    )


def _aliased(**kw) -> DepTime:
    """The synthetic httpx index, with coverage that gives httpx another distribution name or a rival (DT-CORE-03)."""
    store = IndexStore.memory()
    store.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    return DepTime(
        store, Coverage(POLICY, (PackageSpec(name="httpx", import_names=("httpx",), tier="B", **kw),))
    )


def test_a_former_distribution_name_gives_the_installed_version():
    # pinecone-client 3.2.2 is pinecone 3.2.2; pinecone-client 6.0.0 is a stub that only raises "renamed".
    dt = _aliased(installed_as=(Requirement("httpx-client<1"),))
    res = dt.explain_error(HTTPX_TB, pip_freeze="httpx-client==0.28.1\n")
    top = res.findings[0]
    assert (top.issue, top.installed_version, top.resolved_version) == ("param_removed", "0.28.1", "0.28.1")
    assert "httpx-client 0.28.1 is read as httpx 0.28.1" in res.note
    outside = dt.explain_error(HTTPX_TB, {"httpx-client": "6.0.0"})
    assert outside.findings[0].installed_version is None and "read as" not in (outside.note or "")
    # The covered name wins when both are listed.
    both = dt.explain_error(HTTPX_TB, {"httpx-client": "0.26.0", "httpx": "0.28.1"})
    assert both.findings[0].installed_version == "0.28.1" and both.note is None


def test_a_former_distribution_name_points_the_other_tools_at_the_covered_one():
    dt = _aliased(installed_as=(Requirement("httpx-client<1"),))
    res = dt.check_symbols("httpx_client", "0.28.1", [SymbolQuery(path="Client")])
    assert res.results[0].status == "not_indexed"
    assert res.note == (
        "httpx-client (httpx-client<1) installs the code DepTime indexes as httpx, with the same version numbers; "
        'call again with package="httpx".'
    )


def test_a_shared_import_name_is_not_explained_by_the_covered_package_when_a_rival_is_installed():
    # `agents` is openai-agents', TensorFlow Agents' (`agents`) and ai-agents'.
    dt = _aliased(shares_import_name_with=("httpx-rival",))
    res = dt.explain_error(HTTPX_TB, {"httpx-rival": "1.0"})
    # Not even as the called package (DT-CORE-16): httpx's history is another library's.
    assert res.findings == [] and res.not_covered == ["httpx-rival"]
    assert "`httpx` here is httpx-rival's, which DepTime does not cover" in res.note
    module = dt.explain_error(NEW_THING_TB, {"httpx-rival": "1.0"})
    assert module.findings == [] and module.not_covered == ["httpx-rival"]


def test_a_shared_import_name_that_a_full_freeze_leaves_out_is_not_covered():
    dt = _aliased(shares_import_name_with=("httpx-rival",))
    res = dt.explain_error(HTTPX_TB, pip_freeze="idna==3.10\n")
    assert res.findings == [] and res.not_covered == ["httpx"]
    assert "pip_freeze lists no httpx, so `httpx` comes from another distribution" in res.note
    # A dict can list only some packages, so it settles nothing by leaving httpx out.
    partial = dt.explain_error(HTTPX_TB, {"idna": "3.10"})
    assert partial.findings[0].issue == "param_removed"
    assert "`httpx` is also the import name of httpx-rival; this answer assumes httpx" in partial.note


def test_a_shared_import_name_with_the_covered_package_installed():
    dt = _aliased(shares_import_name_with=("httpx-rival",))
    alone = dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"})
    assert alone.findings[0].installed_version == "0.28.1" and alone.note is None
    both = dt.explain_error(HTTPX_TB, {"httpx": "0.28.1", "httpx-rival": "1.0"})
    assert both.findings[0].issue == "param_removed"
    assert "httpx and httpx-rival are both installed, and both install `httpx`" in both.note


def test_the_shipped_coverage_names_the_collisions():
    cov = load_coverage()
    assert cov.get("openai-agents").shares_import_name_with == ("agents", "ai-agents")
    assert cov.installed_under("pinecone-client").name == "pinecone"
    typer = cov.get("typer")
    assert typer.alias_version({"typer-slim": "0.21.1"}) == ("typer-slim", "0.21.1")
    assert typer.alias_version({"typer-slim": "0.22.0"}) is None  # an empty wheel requiring typer from 0.22.0
    assert typer.alias_version({"typer-slim": "not a version"}) is None


def test_an_installed_release_newer_than_the_index_is_a_window_with_no_upper_end(dt):
    # DT-DATA-04: httpx 0.29.0 came out after the index was built. Only the error shows a change after 0.28.1.
    f = dt.explain_error(NEW_THING_TB, {"httpx": "0.29.0"}).findings[0]
    assert (f.issue, f.last_version_with, f.first_version_without, f.pin_to_restore, f.confidence) == (
        "symbol_removed",
        "0.28.1",
        None,
        "httpx<=0.28.1",
        "medium",
    )
    built = dt.store.built_at()[:10]
    assert f.explanation.startswith(
        f"httpx.NewThing disappears after httpx 0.28.1, the newest release the index has (built {built}): your "
        "0.29.0 is newer and not indexed yet. If no newer release changed it, the error has another cause"
    )
    kwarg = dt.explain_error(
        "TypeError: Client.__init__() got an unexpected keyword argument 'timeout'", {"httpx": "0.29.0"}
    ).findings[0]
    assert (kwarg.issue, kwarg.last_version_with, kwarg.confidence) == ("param_removed", "0.28.1", "medium")
    # A change the index shows is answered as before.
    proxies = dt.explain_error(HTTPX_TB, {"httpx": "0.29.0"}).findings[0]
    assert (proxies.issue, proxies.last_version_with, proxies.first_version_without) == (
        "param_removed",
        "0.27.2",
        "0.28.1",
    )


def test_a_name_no_indexed_release_has_is_low_when_the_installed_release_is_newer(dt):
    f = dt.explain_error(
        "ImportError: cannot import name 'BrandNew' from 'httpx'", {"httpx": "0.29.0"}
    ).findings[0]
    assert (f.issue, f.confidence) == ("never_existed", "low")
    assert (
        "Your 0.29.0 is newer than 0.28.1, the newest release the index has, so DepTime can't tell"
        in f.explanation
    )
    assert (
        dt.explain_error("ImportError: cannot import name 'BrandNew' from 'httpx'", {"httpx": "0.28.1"})
        .findings[0]
        .confidence
        == "medium"
    )
    res = dt.check_symbols("httpx", "0.29.0", [SymbolQuery(path="BrandNew")])
    assert res.note.endswith(
        "your 0.29.0 is newer and not indexed yet, so names added after 0.28.1 read missing."
    )
    assert dt.check_symbols("httpx", "0.27.5", [SymbolQuery(path="Client")]).note == (
        "0.27.5 is not indexed; answered for the nearest earlier indexed release 0.27.2."
    )


def test_coverage_gives_the_newest_release_s_upload_date(dt):
    entry = dt.coverage_report().packages[0]
    assert (entry.latest_version, entry.latest_released_at) == ("0.28.1", "2024-12-06T00:00:00+00:00")


def test_a_finding_carries_both_signatures_and_its_release_pages(dt):
    # DT-CORE-29: what an agent would otherwise look up: the signature on each side and where to read the change.
    f = dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0]
    assert f.symbol == "httpx.Client.__init__(proxies)"
    assert "proxies" in [p.name for p in f.signature_before]
    assert [p.name for p in f.signature_after] == ["auth", "proxy", "mounts", "timeout"]
    assert [(link.title, link.url) for link in f.links] == [
        ("httpx 0.27.2 on PyPI", "https://pypi.org/project/httpx/0.27.2/"),
        ("httpx 0.28.1 on PyPI", "https://pypi.org/project/httpx/0.28.1/"),
    ]
    # "Use 'proxy' or 'mounts' instead": the library names two, so neither is the fix.
    assert [r.target for r in f.replacements] == ["proxy", "mounts"] and f.fix is None


def test_a_fix_is_a_replacement_the_index_shows_at_the_installed_release():
    # DT-CORE-29: never a hint the installed release can't use, and never written text.
    spec = PackageSpec(name="p8", import_names=("p8",), tier="B")
    mod = SymbolRecord("p8", "module", "p8")
    new = SymbolRecord("p8.New", "class", "p8._impl.New")
    init = SymbolRecord("p8.New.__init__", "function", "p8._impl.New.__init__", [_param("size")])
    old = SymbolRecord("p8.Old", "class", "p8._impl.Old", None, "Old is deprecated, use p8.New instead.")
    snaps = [
        ApiSnapshot("p8", "1.0", ["p8"], {"p8": mod, "p8.Old": old, "p8.New": new, "p8.New.__init__": init}),
        ApiSnapshot("p8", "2.0", ["p8"], {"p8": mod}),
        ApiSnapshot("p8", "3.0", ["p8"], {"p8": mod, "p8.New": new, "p8.New.__init__": init}),
    ]
    store = IndexStore.memory()
    store.rebuild_package(spec, snaps)
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    tb = "ImportError: cannot import name 'Old' from 'p8'"
    at2 = dt.explain_error(tb, {"p8": "2.0"}).findings[0]
    assert at2.issue == "symbol_removed" and at2.replacements[0].target == "p8.New"
    assert at2.fix is None  # 2.0 has no `p8.New`
    fix = dt.explain_error(tb, {"p8": "3.0"}).findings[0].fix
    assert (fix.kind, fix.old, fix.new, fix.verified_at, fix.same_object, fix.source) == (
        "path",
        "p8.Old",
        "p8.New",
        "3.0",
        False,
        "deprecation_message",
    )
    assert [p.name for p in fix.signature] == ["size"] and "use p8.New" in fix.evidence
    assert dt.explain_error(tb).findings[0].fix.verified_at == "3.0"  # no installed version: the newest


def test_the_same_object_or_a_renamed_argument_is_a_drop_in_fix():
    # DT-CORE-29 with DT-CORE-22 and DT-CORE-24: a path that still has the object, and a parameter that only changed
    # case, need no other change.
    spec = PackageSpec(name="p9", import_names=("p9",), tier="B")
    mods = {p: SymbolRecord(p, "module", p) for p in ("p9", "p9.lib", "p9.lib.sets")}
    union = SymbolRecord("p9.union1d", "function", "p9._impl.union1d", [])

    def release(version: str, param: str, old_path: bool) -> ApiSnapshot:
        f = SymbolRecord("p9.elicit", "function", "p9.elicit", [_param(param, "positional or keyword")])
        symbols = {**mods, "p9.union1d": union, "p9.elicit": f}
        if old_path:
            symbols["p9.lib.sets.union1d"] = SymbolRecord(
                "p9.lib.sets.union1d", "function", "p9._impl.union1d", []
            )
        return ApiSnapshot("p9", version, ["p9"], symbols)

    store = IndexStore.memory()
    store.rebuild_package(
        spec, [release("1.0", "requestedSchema", True), release("2.0", "requested_schema", False)]
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    moved = dt.explain_error(
        "AttributeError: module 'p9.lib.sets' has no attribute 'union1d'", {"p9": "2.0"}
    ).findings[0]
    assert (moved.fix.kind, moved.fix.new, moved.fix.same_object, moved.fix.confidence) == (
        "path",
        "p9.union1d",
        True,
        0.75,
    )
    renamed = dt.explain_error(
        "TypeError: elicit() got an unexpected keyword argument 'requestedSchema'", {"p9": "2.0"}
    ).findings[0]
    assert (renamed.fix.kind, renamed.fix.old, renamed.fix.new, renamed.fix.same_object) == (
        "argument",
        "requestedSchema",
        "requested_schema",
        True,
    )


def test_the_changelog_link_comes_from_the_release_metadata():
    # DT-CORE-29: the project's own Project-URL entries, never a guessed URL; untrusted, so checked again on the way out.
    from dataclasses import replace

    urls = {"Homepage": "https://example.org", "Docs: Changelog": "https://example.org/changes"}
    snaps = [
        SNAPSHOTS[0],
        replace(SNAPSHOTS[1], project_urls={"Changelog": "javascript:alert(1)"}),
        replace(SNAPSHOTS[2], project_urls=urls),
    ]
    store = IndexStore.memory()
    store.rebuild_package(HTTPX, snaps, RELEASED_AT)
    assert store.snapshot_at(HTTPX, "0.28.1").project_urls == urls
    dt = DepTime(store, Coverage(POLICY, (HTTPX,)))
    f = dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0]
    assert (f.links[-1].title, f.links[-1].url) == ("httpx Docs: Changelog", "https://example.org/changes")
    # 0.27.2's entry isn't a web page, so it gives no link; 0.26.0 has none and takes the nearest later release's.
    assert (
        store.project_urls("httpx", 1)
        == store.project_urls("httpx", 0)
        == {"Changelog": "javascript:alert(1)"}
    )
    assert dt._changelog("httpx", 1) is None and dt._changelog("httpx", 0) is None


def test_a_package_deptime_doesnt_index_points_to_coverage_and_the_request_form(dt):
    # DT-OSS-11: say what is covered and how to ask, not just "not covered".
    res = dt.explain_error("ImportError: cannot import name 'Sessionx' from 'requests_toolbelt'\n")
    assert res.findings == [] and res.not_covered == ["requests_toolbelt"]
    assert "the `coverage` tool lists the packages it does" in res.note
    assert "issues/new?template=package-request.yml" in res.note
    # A standard module isn't a package to ask for.
    std = dt.explain_error(
        "ImportError: cannot import name 'Mappings' from 'collections' (/usr/lib/python3.10/x.py)"
    )
    assert "package-request" not in std.note
    assert "package-request.yml" in dt.symbol_history("requests", "requests.get").note


def test_a_finding_says_why_it_has_its_confidence(dt):
    # DT-CORE-30: the factors behind `confidence`, as a field an agent can read.
    exact = dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0]
    assert exact.confidence_factors == [
        "installed_version_indexed",
        "module_static",
        "public_name",
        "replacement_from_library",
    ]
    unknown = dt.explain_error(HTTPX_TB).findings[0]
    assert unknown.confidence == "low" and unknown.confidence_factors[0] == "installed_version_unknown"
    ahead = dt.explain_error(HTTPX_TB, {"httpx": "0.28.9"}).findings[0]
    assert ahead.confidence_factors[0] == "installed_version_after_newest"


def test_a_module_getattr_is_a_confidence_factor():
    spec = PackageSpec(name="p10", import_names=("p10",), tier="B")
    mod = SymbolRecord("p10", "module", "p10")
    hook = SymbolRecord(
        "p10.__getattr__", "function", "p10.__getattr__", [_param("name", "positional or keyword")]
    )
    old = SymbolRecord("p10.Old", "class", "p10._impl.Old")
    store = IndexStore.memory()
    store.rebuild_package(
        spec,
        [
            ApiSnapshot("p10", "1.0", ["p10"], {"p10": mod, "p10.__getattr__": hook, "p10.Old": old}),
            ApiSnapshot("p10", "2.0", ["p10"], {"p10": mod, "p10.__getattr__": hook}),
        ],
    )
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    f = dt.explain_error("ImportError: cannot import name 'Old' from 'p10'", {"p10": "2.0"}).findings[0]
    assert f.issue == "symbol_removed" and "module_has_getattr" in f.confidence_factors


def test_min_confidence_and_suppressions_are_counted_never_silent(store, coverage, tmp_path):
    from deptime import suppressions

    dt = DepTime(store, coverage)
    low = dt.explain_error(HTTPX_TB, min_confidence="medium")
    assert low.findings == [] and low.below_min_confidence == 1
    assert "1 finding below medium confidence left out (min_confidence)." in low.note
    assert dt.explain_error(HTTPX_TB, min_confidence="low").findings  # low is the floor: everything stays
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.deptime.suppress]]\npackage = "HTTPX"\npath = "httpx.Client"\nkind = "param_removed"\n'
        'reason = "we pass mounts"\n\n[[tool.deptime.suppress]]\npath = "no.package"\n'
    )
    loaded = suppressions.load(tmp_path)
    assert loaded == (suppressions.Suppression("httpx", "httpx.Client", "param_removed", "we pass mounts"),)
    quiet = DepTime(store, coverage, suppressions=loaded)
    res = quiet.explain_error(HTTPX_TB, {"httpx": "0.28.1"})
    assert res.findings == [] and res.suppressed == 1
    assert "1 finding left out by this project's suppressions" in res.note
    assert "No version-history explanation found" not in res.note
    diff = quiet.diff_versions("httpx", "0.26.0")
    assert diff.suppressed >= 1 and all(
        not e.path.startswith("httpx.Client.") or e.kind != "param_removed" for e in diff.events
    )
    assert "left out by this project's suppressions" in diff.note
    history = quiet.symbol_history("httpx", "httpx.Client.__init__")
    assert history.suppressed == 1 and all(e.kind != "param_removed" for e in history.events)
    # deptime.toml wins over pyproject.toml; nothing to read is nothing suppressed.
    (tmp_path / "deptime.toml").write_text('[[suppress]]\npackage = "numpy"\n')
    assert suppressions.load(tmp_path) == (suppressions.Suppression("numpy"),)
    assert suppressions.load(tmp_path / "missing") == ()


def test_too_many_positional_arguments_finds_the_parameter_that_left_the_positions():
    # DT-CORE-09: "takes 1 positional argument but 2 were given" after a parameter became keyword-only, or went.
    spec = PackageSpec(name="p11", import_names=("p11",), tier="B")
    mod = SymbolRecord("p11", "module", "p11")
    pk, kw = "positional or keyword", "keyword-only"

    def release(version: str, new: bool) -> ApiSnapshot:
        connect = [_param("host", pk, None), _param("port", kw if new else pk, "0")]
        init = [_param("url", pk, None), _param("timeout", kw if new else pk, "5")]
        send = [_param("data", pk, None)] + ([] if new else [_param("flags", pk, "0")])
        symbols = {
            "p11": mod,
            "p11.connect": SymbolRecord("p11.connect", "function", "p11.connect", connect),
            "p11.Client": SymbolRecord("p11.Client", "class", "p11.Client"),
            "p11.Client.__init__": SymbolRecord(
                "p11.Client.__init__", "function", "p11.Client.__init__", init
            ),
            "p11.send": SymbolRecord("p11.send", "function", "p11.send", send),
        }
        return ApiSnapshot("p11", version, ["p11"], symbols)

    store = IndexStore.memory()
    store.rebuild_package(spec, [release("1.0", False), release("1.1", False), release("2.0", True)])
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    f = dt.explain_error(
        "TypeError: connect() takes 1 positional argument but 2 were given", {"p11": "2.0"}
    ).findings[0]
    assert (f.symbol, f.issue, f.last_version_with, f.first_version_without, f.pin_to_restore) == (
        "p11.connect(port)",
        "param_kind_changed",
        "1.1",
        "2.0",
        "p11<=1.1",
    )
    assert (f.fix.kind, f.fix.new, f.fix.same_object, f.fix.source, f.fix.verified_at) == (
        "keyword",
        "port",
        True,
        "signature",
        "2.0",
    )
    assert f.explanation.startswith("`port` of p11.connect became keyword-only in p11 2.0")
    # A method: Python counts the instance, the index doesn't.
    tb = "TypeError: Client.__init__() takes 2 positional arguments but 3 were given"
    m = dt.explain_error(tb, {"p11": "2.0"}).findings[0]
    assert (m.symbol, m.issue) == ("p11.Client.__init__(timeout)", "param_kind_changed")
    # A positional parameter that's gone reads like the removal it is.
    gone = dt.explain_error("TypeError: send() takes 1 positional argument but 2 were given", {"p11": "2.0"})
    assert (gone.findings[0].symbol, gone.findings[0].issue) == ("p11.send(flags)", "param_removed")
    # Counts the installed signature can't produce, or a release that still takes them: no finding.
    assert (
        dt.explain_error(
            "TypeError: connect() takes 3 positional arguments but 4 were given", {"p11": "2.0"}
        ).findings
        == []
    )
    assert (
        dt.explain_error(
            "TypeError: connect() takes 2 positional arguments but 3 were given", {"p11": "1.1"}
        ).findings
        == []
    )


def test_check_symbols_reads_what_a_snippet_uses(dt):
    # DT-CORE-07: the code itself, read with ast and never run.
    code = (
        "import httpx\n"
        "import os\n"
        "from httpx import OldThing as Old, Client\n"
        "client = httpx.Client(proxies='http://x', timeout=3)\n"
        "client.get('https://example.com')\n"
        "with httpx.Client(auth=None) as c:\n"
        "    c.stream('GET', 'x')\n"
        "httpx.request('GET', 'u', proxies={})\n"
        "fetch = httpx.request\n"
        "os.path.join('a', 'b')\n"
    )
    res = dt.check_symbols("httpx", "0.28.1", code=code)
    got = {r.path: (r.status, r.rejected_kwargs) for r in res.results}
    assert got == {
        "httpx": ("present", []),
        "httpx.OldThing": ("missing", []),
        "httpx.Client": ("present", ["proxies"]),
        "httpx.Client.get": ("present", []),
        "httpx.Client.stream": ("missing", []),
        "httpx.request": ("present", ["proxies"]),
    }
    assert res.note.startswith("6 names of httpx read from the code.")
    # Given symbols come first and merge with the code's; code that doesn't parse says so.
    both = dt.check_symbols(
        "httpx", "0.28.1", [SymbolQuery(path="httpx.NewThing")], code="import httpx.Client"
    )
    assert [r.path for r in both.results] == ["httpx.NewThing", "httpx.Client"]
    assert dt.check_symbols("httpx", "0.28.1", code="def (").note.startswith("The code doesn't parse")


def test_an_attribute_of_what_a_function_returns_isnt_guessed(dt):
    # Only a call the index shows builds a class makes an object whose attributes are checked.
    res = dt.check_symbols("httpx", "0.28.1", code="import httpx\nr = httpx.request('GET', 'u')\nr.json()\n")
    assert [r.path for r in res.results] == ["httpx", "httpx.request"]


def test_a_method_named_without_its_class_says_which_classes_fit():
    # DT-CORE-31 (partly): `sort() got an unexpected keyword argument` names no class.
    spec = PackageSpec(name="p12", import_names=("p12",), tier="B")
    mod = SymbolRecord("p12", "module", "p12")

    def release(version: str, new: bool) -> ApiSnapshot:
        params = [_param("by", "positional or keyword", None)] + (
            [_param("descending", default="False")] if new else []
        )
        symbols = {"p12": mod}
        for cls in ("Frame", "Series"):
            symbols[f"p12.{cls}"] = SymbolRecord(f"p12.{cls}", "class", f"p12.{cls}")
            symbols[f"p12.{cls}.sort"] = SymbolRecord(
                f"p12.{cls}.sort", "function", f"p12.{cls}.sort", params
            )
        return ApiSnapshot("p12", version, ["p12"], symbols)

    store = IndexStore.memory()
    store.rebuild_package(spec, [release("1.0", False), release("2.0", True)])
    dt = DepTime(store, Coverage(POLICY, (spec,)))
    res = dt.explain_error(
        "TypeError: sort() got an unexpected keyword argument 'descending'", {"p12": "1.0"}
    )
    assert {f.symbol for f in res.findings} == {"p12.Frame.sort(descending)", "p12.Series.sort(descending)"}
    assert "names `sort()` without its class, and 2 classes' `sort` changed the same way" in res.note
    one = dt.explain_error(
        "TypeError: Frame.sort() got an unexpected keyword argument 'descending'", {"p12": "1.0"}
    )
    assert one.note is None or "without its class" not in one.note
