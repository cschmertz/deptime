"""Offline end-to-end: real wheel zips served through httpx.MockTransport -> pipeline -> index -> explain_error."""

import hashlib
import io
import sys
import weakref
import zipfile
from datetime import date

import httpx

from deptime import cli
from deptime.config import Coverage, PackageSpec, VersionPolicy
from deptime.index.store import IndexStore
from deptime.ingest import pipeline
from deptime.ingest.pipeline import IngestReport, ingest_package, snapshot_path
from deptime.ingest.snapshot import ApiSnapshot, LazySnapshot
from deptime.query import DepTime
from tests.conftest import HTTPX, RELEASED_AT, SNAPSHOTS

SPEC = PackageSpec(name="fakepkg", import_names=("fakepkg",), tier="B")
POLICY = VersionPolicy(date(2023, 1, 1), 12, False, False)
V1_INIT = (
    "import warnings\n\n"
    "class Client:\n"
    "    def __init__(self, *, proxy=None, proxies=None):\n"
    "        if proxies:\n"
    "            warnings.warn(\"The 'proxies' argument is deprecated. Use 'proxy' instead.\", DeprecationWarning)\n"
)
V2_INIT = "class Client:\n    def __init__(self, *, proxy=None):\n        pass\n"


def _wheel(init_source: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fakepkg/__init__.py", 'from fakepkg._client import Client\n__all__ = ["Client"]\n')
        zf.writestr("fakepkg/_client.py", init_source)
    return buf.getvalue()


WHEELS = {
    "fakepkg-1.0.0-py3-none-any.whl": _wheel(V1_INIT),
    "fakepkg-2.0.0-py3-none-any.whl": _wheel(V2_INIT),
}


def _project_page(project: str, wheels: dict[str, bytes]) -> dict:
    files = []
    for i, (name, data) in enumerate(wheels.items()):
        files.append(
            {
                "filename": name,
                "url": f"https://files.example/{name}",
                "hashes": {"sha256": hashlib.sha256(data).hexdigest()},
                "upload-time": f"2026-0{i + 1}-01T00:00:00Z",
                "yanked": False,
            }
        )
    return {"name": project, "files": files, "meta": {"api-version": "1.4"}}


def _simple_index() -> dict:
    return _project_page("fakepkg", WHEELS)


def _client(serve_files: bool = True, only: tuple[str, ...] = ()) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://pypi.org/simple/fakepkg/":
            return httpx.Response(200, json=_simple_index())
        name = url.rsplit("/", 1)[-1]
        if serve_files and name in WHEELS and (not only or name in only):
            return httpx.Response(200, content=WHEELS[name])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pipeline_end_to_end(tmp_path):
    store = IndexStore.memory()
    with _client() as client:
        report = ingest_package(
            SPEC, POLICY, store, tmp_path, client=client, today=date(2026, 10, 17), log=lambda _m: None
        )
    assert report.failed == {} and report.built == ["1.0.0", "2.0.0"]
    assert report.stats.releases == 2

    dt = DepTime(store, Coverage(POLICY, (SPEC,)))
    res = dt.explain_error(
        "TypeError: Client.__init__() got an unexpected keyword argument 'proxies'", {"fakepkg": "2.0.0"}
    )
    top = res.findings[0]
    assert (top.symbol, top.issue, top.last_version_with, top.deprecated_in) == (
        "fakepkg.Client.__init__(proxies)",
        "param_removed",
        "1.0.0",
        "<=1.0.0",
    )
    assert [r.target for r in top.replacements] == ["proxy"]

    # Second run hits the snapshot cache: nothing is rebuilt from artifacts.
    with _client(serve_files=False) as client:
        again = ingest_package(
            SPEC, POLICY, store, tmp_path, client=client, today=date(2026, 10, 17), log=lambda _m: None
        )
    assert again.cached == ["1.0.0", "2.0.0"] and again.failed == {}


def test_total_failure_keeps_existing_index(tmp_path):
    store = IndexStore.memory()
    with _client() as client:
        ingest_package(
            SPEC, POLICY, store, tmp_path / "a", client=client, today=date(2026, 10, 17), log=lambda _m: None
        )
    with _client(serve_files=False) as client:  # fresh cache dir + artifacts unavailable
        report = ingest_package(
            SPEC, POLICY, store, tmp_path / "b", client=client, today=date(2026, 10, 17), log=lambda _m: None
        )
    assert set(report.failed) == {"1.0.0", "2.0.0"} and report.stats is None
    assert [r["version"] for r in store.releases("fakepkg")] == ["1.0.0", "2.0.0"]


def test_pypi_outage_rebuilds_from_cached_snapshots(tmp_path):
    """CI builds a fresh index each night; an outage must not drop a package from what gets published."""
    with _client() as client:
        ingest_package(
            SPEC,
            POLICY,
            IndexStore.memory(),
            tmp_path,
            client=client,
            today=date(2026, 10, 17),
            log=lambda _m: None,
        )

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("pypi unreachable")

    fresh = IndexStore.memory()
    with httpx.Client(transport=httpx.MockTransport(down)) as client:
        report = ingest_package(SPEC, POLICY, fresh, tmp_path, client=client, log=lambda _m: None)
    assert "PyPI unavailable" in report.failed["*"]
    assert [r["version"] for r in fresh.releases("fakepkg")] == ["1.0.0", "2.0.0"]


def test_unindexed_package_answers_gracefully():
    store = IndexStore.memory()
    dt = DepTime(store, Coverage(POLICY, (SPEC,)))
    h = dt.symbol_history("fakepkg", "Client")
    assert h.indexed is False and "not indexed" in h.note


TODAY = date(2026, 10, 17)


def _quiet(_message: str) -> None:
    pass


def _versions(store: IndexStore) -> list[str]:
    return [r["version"] for r in store.releases("fakepkg")]


def _full_ingest(store: IndexStore, cache_dir) -> None:
    with _client() as client:
        ingest_package(SPEC, POLICY, store, cache_dir, client=client, today=TODAY, log=_quiet)


def test_failed_retry_keeps_an_indexed_release(tmp_path):
    """I-02: one failed download (and no cached snapshot) must not delete a release that is already indexed."""
    store = IndexStore.memory()
    _full_ingest(store, tmp_path / "a")
    with _client(only=("fakepkg-1.0.0-py3-none-any.whl",)) as client:  # fresh cache; 2.0.0 download fails
        report = ingest_package(SPEC, POLICY, store, tmp_path / "b", client=client, today=TODAY, log=_quiet)
    assert _versions(store) == ["1.0.0", "2.0.0"]
    assert "2.0.0" in report.failed and report.carried == ["2.0.0"]
    res = DepTime(store, Coverage(POLICY, (SPEC,))).explain_error(
        "TypeError: Client.__init__() got an unexpected keyword argument 'proxies'", {"fakepkg": "2.0.0"}
    )
    assert res.findings[0].issue == "param_removed"


def test_cached_snapshot_for_other_import_names_is_rebuilt(tmp_path):
    """A coverage.toml edit to import_names must reach every cached release, or its names look "added" later."""
    store = IndexStore.memory()
    _full_ingest(store, tmp_path)
    wider = PackageSpec(name="fakepkg", import_names=("fakepkg", "fakepkg_hooks"), tier="B")
    with _client() as client:
        report = ingest_package(wider, POLICY, store, tmp_path, client=client, today=TODAY, log=_quiet)
    assert report.cached == [] and report.built == ["1.0.0", "2.0.0"]
    assert ApiSnapshot.load(snapshot_path(tmp_path, "fakepkg", "1.0.0")).import_names == [
        "fakepkg",
        "fakepkg_hooks",
    ]


def test_versions_run_adds_to_indexed_releases(tmp_path):
    """I-02: `ingest --versions 2.0.0` adds 2.0.0; it doesn't narrow the package to it."""
    store = IndexStore.memory()
    _full_ingest(store, tmp_path / "a")
    with _client() as client:
        report = ingest_package(
            SPEC, POLICY, store, tmp_path / "b", versions=["2.0.0"], client=client, log=_quiet
        )
    assert _versions(store) == ["1.0.0", "2.0.0"]
    assert report.built == ["2.0.0"] and report.carried == ["1.0.0"]


def test_snapshot_rebuilt_from_index_matches_cached_snapshot(tmp_path):
    store = IndexStore.memory()
    _full_ingest(store, tmp_path)
    for version in ("1.0.0", "2.0.0"):
        cached = ApiSnapshot.load(snapshot_path(tmp_path, "fakepkg", version))
        rebuilt = store.snapshot_at(SPEC, version)
        assert rebuilt.symbols == cached.symbols and len(rebuilt.errors) == len(cached.errors)


def _cli_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPTIME_HOME", str(tmp_path))
    monkeypatch.delenv("DEPTIME_INDEX_PATH", raising=False)
    monkeypatch.delenv("DEPTIME_CACHE_DIR", raising=False)
    calls = []

    def fake_ingest(spec, *args, **kwargs):  # never touches PyPI; rebuilds nothing
        calls.append(spec.name)
        return IngestReport(spec.name, failed={"*": "PyPI unavailable"})

    monkeypatch.setattr("deptime.ingest.pipeline.ingest_package", fake_ingest)
    meta = tmp_path / "index.sqlite.meta.json"
    meta.write_text("{}")  # what `index pull` leaves next to a downloaded index
    return meta, calls


def test_ingest_refuses_a_downloaded_index(tmp_path, monkeypatch):
    """I-02: ingesting into the downloaded index would silently stop its daily updates."""
    meta, calls = _cli_home(tmp_path, monkeypatch)
    assert cli.main(["ingest", "httpx"]) == 2
    assert calls == [] and meta.exists()


def test_ingest_into_downloaded_keeps_it_downloaded_until_something_is_rebuilt(tmp_path, monkeypatch):
    meta, calls = _cli_home(tmp_path, monkeypatch)
    assert cli.main(["ingest", "httpx", "--into-downloaded"]) == 1
    assert calls == ["httpx"] and meta.exists()


def test_ingest_keeps_only_snapshots_on_disk(tmp_path):
    """Wheels and unpacked sources are deleted once analysed (a cold CI build would run out of disk)."""
    with _client(only=("fakepkg-1.0.0-py3-none-any.whl",)) as client:  # 2.0.0 fails mid-run
        report = ingest_package(
            SPEC, POLICY, IndexStore.memory(), tmp_path, client=client, today=TODAY, log=_quiet
        )
    assert report.built == ["1.0.0"] and "2.0.0" in report.failed
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file() and "snapshots" not in p.parts]
    assert leftovers == []
    assert snapshot_path(tmp_path, "fakepkg", "1.0.0").exists()


def test_ingest_memory_does_not_grow_with_release_count(tmp_path, monkeypatch):
    """A cold CI build died holding 100+ litellm snapshots at once; at most a few may be alive (DT-DATA-03)."""
    wheels = {f"fakepkg-{n}.0.0-py3-none-any.whl": _wheel(V2_INIT) for n in range(1, 7)}
    monkeypatch.setattr(sys.modules[__name__], "WHEELS", wheels)
    refs: list[weakref.ref] = []  # ApiSnapshot is an unhashable dataclass, so no WeakSet
    most = 0

    def track(snap):
        nonlocal most
        if snap is not None:
            refs.append(weakref.ref(snap))
            most = max(most, sum(r() is not None for r in refs))
        return snap

    real_build, real_load = pipeline.build_snapshot, ApiSnapshot.load.__func__
    monkeypatch.setattr(pipeline, "build_snapshot", lambda *a, **k: track(real_build(*a, **k)))
    monkeypatch.setattr(ApiSnapshot, "load", classmethod(lambda cls, path: track(real_load(cls, path))))
    for serve_files in (True, False):  # cold run builds every release; warm run reads them all from the cache
        with _client(serve_files=serve_files) as client:
            report = ingest_package(
                SPEC, POLICY, IndexStore.memory(), tmp_path, client=client, today=TODAY, log=_quiet
            )
        assert report.failed == {} and report.stats.releases == 6
    assert most <= 3


def test_lazy_rebuild_matches_eager_rebuild():
    eager, lazy = IndexStore.memory(), IndexStore.memory()
    eager.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    items = [LazySnapshot(s.version, lambda s=s: s) for s in reversed(SNAPSHOTS)]
    stats = lazy.rebuild_package(HTTPX, [*items, LazySnapshot("9.9.9", lambda: None)], RELEASED_AT)
    assert stats.releases == len(SNAPSHOTS)  # a snapshot that loads as None is skipped
    for table in ("releases", "spans", "events", "replacements"):
        rows = [sorted(map(tuple, s.conn.execute(f"SELECT * FROM {table}")), key=repr) for s in (eager, lazy)]
        assert rows[0] == rows[1], table


def test_cached_only_never_downloads_and_keeps_published_releases(tmp_path):
    """CI's publish job assembles from the per-package jobs' snapshots; a missing one keeps the indexed copy."""
    store = IndexStore.memory()
    with _client() as client:
        ingest_package(SPEC, POLICY, store, tmp_path, client=client, today=TODAY, log=_quiet)
    snapshot_path(tmp_path, "fakepkg", "2.0.0").unlink()  # as if its per-package job had failed
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == "https://pypi.org/simple/fakepkg/":
            return httpx.Response(200, json=_simple_index())
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ingest_package(
            SPEC, POLICY, store, tmp_path, client=client, today=TODAY, cached_only=True, log=_quiet
        )
    assert requested == ["https://pypi.org/simple/fakepkg/"]  # the release list only, no artifacts
    assert report.cached == ["1.0.0"] and "2.0.0" in report.failed
    assert report.carried == ["2.0.0"] and report.stats.releases == 2
    assert {r["version"]: r["source_file"] for r in store.releases("fakepkg")} == {
        "1.0.0": "fakepkg-1.0.0-py3-none-any.whl",
        "2.0.0": "fakepkg-2.0.0-py3-none-any.whl",
    }


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _wrapper(name: str, version: str, *requires: str) -> bytes:
    """A wheel with no code: only metadata that requires other distributions (fastmcp 3.3+, typer-cli)."""
    lines = [f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"]
    lines += [f"Requires-Dist: {r}\n" for r in requires]
    return _zip({f"{name.replace('-', '_')}-{version}.dist-info/METADATA": "".join(lines)})


WRAPPED = PackageSpec(name="wrapped", import_names=("wrapped",), tier="B")
WRAPPED_PROJECTS = {
    "wrapped": {
        "wrapped-1.0.0-py3-none-any.whl": _zip({"wrapped/__init__.py": "class Old:\n    pass\n"}),
        # typer 0.12.0 pins typer-cli (empty too) before typer-slim (the code), both at its own version.
        "wrapped-2.0.0-py3-none-any.whl": _wrapper(
            "wrapped", "2.0.0", "wrapped-cli (==2.0.0)", "wrapped-slim[server]==2.0.0", "rich>=13"
        ),
        # dspy 0.1.5 pins dspy-ai==2.4.5: another version's code isn't this release's API.
        "wrapped-3.0.0-py3-none-any.whl": _wrapper("wrapped", "3.0.0", "wrapped-slim==2.0.0"),
    },
    "wrapped-cli": {
        "wrapped_cli-2.0.0-py3-none-any.whl": _wrapper("wrapped-cli", "2.0.0", "wrapped-slim==2.0.0")
    },
    "wrapped-slim": {
        "wrapped_slim-2.0.0-py3-none-any.whl": _zip({"wrapped/__init__.py": "class New:\n    pass\n"})
    },
}


def _wrapped_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for project, wheels in WRAPPED_PROJECTS.items():
            if url == f"https://pypi.org/simple/{project}/":
                return httpx.Response(200, json=_project_page(project, wheels))
            if (name := url.rsplit("/", 1)[-1]) in wheels:
                return httpx.Response(200, content=wheels[name])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_wrapper_release_is_analysed_from_the_distribution_it_pins(tmp_path):
    """DT-DATA-17: fastmcp 3.3+ is an empty wheel that pins fastmcp-slim at the same version."""
    store = IndexStore.memory()
    with _wrapped_client() as client:
        report = ingest_package(WRAPPED, POLICY, store, tmp_path, client=client, today=TODAY, log=_quiet)
    assert report.built == ["1.0.0", "2.0.0"] and list(report.failed) == ["3.0.0"]
    assert "wrapped.New" in ApiSnapshot.load(snapshot_path(tmp_path, "wrapped", "2.0.0")).symbols
    expected = {"1.0.0": "wrapped-1.0.0-py3-none-any.whl", "2.0.0": "wrapped_slim-2.0.0-py3-none-any.whl"}
    assert {r["version"]: r["source_file"] for r in store.releases("wrapped")} == expected
    assert [p for p in tmp_path.rglob("*") if p.is_file() and "snapshots" not in p.parts] == []

    # CI's publish job assembles from cached snapshots and records the same source files.
    fresh = IndexStore.memory()
    with _wrapped_client() as client:
        again = ingest_package(
            WRAPPED, POLICY, fresh, tmp_path, client=client, today=TODAY, cached_only=True, log=_quiet
        )
    assert again.cached == ["1.0.0", "2.0.0"]
    assert {r["version"]: r["source_file"] for r in fresh.releases("wrapped")} == expected
