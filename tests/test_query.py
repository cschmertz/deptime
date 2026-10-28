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
    # Q-03: a 0.6 hint mined from "use keyword arguments" must not become "Use keyword instead."
    spec = PackageSpec(name="p5", import_names=("p5",), tier="B")
    old_dep = "Passing positional arguments is deprecated; use keyword arguments"
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
    assert sym.issue == "symbol_removed" and "keyword" not in sym.explanation
    assert [(r.target, r.confidence) for r in sym.replacements] == [
        ("keyword", 0.6)
    ]  # still listed, with its score

    par = dt.explain_error("TypeError: f() got an unexpected keyword argument 'old'", {"p5": "2.0"}).findings[
        0
    ]
    assert par.issue == "param_removed" and "Use `docs_page`" not in par.explanation
    assert "Pin p5<=1.0 or update the call." in par.explanation
    assert [r.target for r in par.replacements] == ["docs_page"]

    check = dt.check_symbols("p5", "2.0", [SymbolQuery(path="old")]).results[0]
    assert check.status == "missing" and "keyword" not in check.hint


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
