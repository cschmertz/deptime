"""Offline end-to-end: real wheel zips served through httpx.MockTransport -> pipeline -> index -> explain_error."""

import ast
import hashlib
import io
import sys
import weakref
import zipfile
from datetime import UTC, date, datetime
from importlib import resources

import httpx

from deptime import cli
from deptime.config import Coverage, PackageSpec, VersionPolicy
from deptime.index.store import IndexStore
from deptime.ingest import pipeline
from deptime.ingest.pipeline import IngestReport, coverage_summary, ingest_package, snapshot_path
from deptime.ingest.snapshot import ApiSnapshot, LazySnapshot, SymbolRecord
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


def test_cached_snapshot_with_other_namespace_skips_is_rebuilt(tmp_path):
    """DT-DATA-23: a coverage.toml edit to `namespace_skip` changes what is loaded, like `import_names`."""
    store = IndexStore.memory()
    _full_ingest(store, tmp_path)
    skipping = PackageSpec(
        name="fakepkg", import_names=("fakepkg",), tier="B", namespace_skip=("fakepkg.vendor",)
    )
    with _client() as client:
        report = ingest_package(skipping, POLICY, store, tmp_path, client=client, today=TODAY, log=_quiet)
    assert report.cached == [] and report.built == ["1.0.0", "2.0.0"]
    assert ApiSnapshot.load(snapshot_path(tmp_path, "fakepkg", "1.0.0")).namespace_skip == ["fakepkg.vendor"]


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


def test_ingest_appends_a_coverage_summary(tmp_path, monkeypatch):
    """DT-DATA-01: CI passes $GITHUB_STEP_SUMMARY, so a run's failures show on the workflow's page."""
    _cli_home(tmp_path, monkeypatch)
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n")
    assert cli.main(["ingest", "httpx", "--into-downloaded", "--summary", str(summary)]) == 1
    text = summary.read_text()
    assert text.startswith("earlier step\n") and "| httpx |" in text and "PyPI unavailable" in text


def test_coverage_summary_lists_failures_and_packages_below_the_bar():
    """DT-DATA-01: at least 90% of each package's selected releases are indexed; failures are listed."""
    full = IngestReport("full", selected=["1", "2"], cached=["1", "2"])
    kept = IngestReport("kept", selected=["1", "2"], cached=["1"], carried=["2"], failed={"2": "boom"})
    low = IngestReport(
        "low",
        selected=[str(v) for v in range(10)],
        cached=[str(v) for v in range(8)],
        failed={"8": "no cached snapshot (cached-only run)", "9": "ValueError: a | b\nsecond line"},
    )
    down = IngestReport("down", failed={"*": "PyPI unavailable (ConnectError); used 0 cached snapshots"})
    text = coverage_summary([full, kept, low, down])

    assert "12 of 14 selected releases indexed" in text  # `kept` still serves 2 from the existing index
    assert "Below it: low." in text and "| low | 8 of 10 (80.0%) **below 90%** |" in text
    assert "| kept | 2 of 2 (100.0%) |" in text and "`2`: boom" in text
    assert "| full |" not in text  # only packages with something to report get a row
    assert "| down | not counted |" in text and "PyPI unavailable" in text
    assert "a \\| b second line" in text  # one table cell: pipes escaped, newlines folded
    assert "snapshots job" in text  # where to find why a release had no snapshot


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


def _rows(store: IndexStore) -> dict[str, list]:
    """Every table's rows, without the times they were written."""
    columns = {"packages": "name, import_names, tier", "releases": "*", "spans": "*", "events": "*"}
    columns |= {"replacements": "*", "assembly": "*"}
    return {
        table: sorted(map(tuple, store.conn.execute(f"SELECT {cols} FROM {table}")), key=repr)
        for table, cols in columns.items()
    }


def _assemble(store: IndexStore, cache_dir) -> IngestReport:
    """What CI's publish job runs: assemble from cached snapshots only."""
    with _client(serve_files=False) as client:
        return ingest_package(
            SPEC, POLICY, store, cache_dir, client=client, today=TODAY, cached_only=True, log=_quiet
        )


def test_unchanged_package_keeps_its_rows(tmp_path):
    """DT-DATA-41: the nightly assembles only the packages whose snapshots or assembly code changed."""
    store = IndexStore.memory()
    _full_ingest(store, tmp_path)
    before = _rows(store)
    assert before["assembly"]  # the rows' fingerprint is stored with them

    report = _assemble(store, tmp_path)
    assert report.unchanged and report.stats is None
    assert report.cached == ["1.0.0", "2.0.0"] and report.failed == {}
    assert _rows(store) == before

    fresh = IndexStore.memory()  # the rows kept are the rows a full assembly writes
    assert _assemble(fresh, tmp_path).stats.releases == 2
    assert _rows(fresh) == before


def test_changed_snapshot_or_assembly_code_rebuilds_the_package(tmp_path, monkeypatch):
    store = IndexStore.memory()
    _full_ingest(store, tmp_path)
    path = snapshot_path(tmp_path, "fakepkg", "2.0.0")
    snap = ApiSnapshot.load(path)
    snap.symbols["fakepkg.added"] = SymbolRecord(
        "fakepkg.added", "function", "fakepkg._client.added", [], None
    )
    snap.save(path)

    report = _assemble(store, tmp_path)
    assert not report.unchanged and report.stats.releases == 2
    assert store.spans("fakepkg", "fakepkg.added")
    fresh = IndexStore.memory()
    _assemble(fresh, tmp_path)
    assert _rows(store) == _rows(fresh)

    monkeypatch.setattr(pipeline, "assembly_code_digest", lambda: "after an edit to diff.py")
    assert _assemble(store, tmp_path).stats is not None
    assert _assemble(store, tmp_path).unchanged


def test_rows_not_assembled_from_snapshots_alone_are_always_rebuilt(tmp_path):
    """Any doubt rebuilds: a release carried over from the index, or a `--versions` run, stores no fingerprint."""
    store = IndexStore.memory()
    _full_ingest(store, tmp_path / "a")
    with _client() as client:
        ingest_package(SPEC, POLICY, store, tmp_path / "a", versions=["2.0.0"], client=client, log=_quiet)
    assert _assemble(store, tmp_path / "a").stats is not None

    snapshot_path(tmp_path / "a", "fakepkg", "2.0.0").unlink()  # its per-package job failed tonight
    for _ in range(2):
        report = _assemble(store, tmp_path / "a")
        assert report.carried == ["2.0.0"] and report.stats.releases == 2


def test_assembly_digest_covers_the_code_that_assembles():
    """An import the assembly code adds must join ASSEMBLY_SOURCES, or an edit to it would keep stale rows."""
    covered = {
        "deptime." + source.removesuffix(".py").replace("/", ".")
        for source in pipeline.ASSEMBLY_SOURCES
        if source.endswith(".py")
    }
    # Only their results reach the fingerprint: the selected releases, their files and dates, the package spec.
    inputs_only = {"deptime.config", "deptime.ingest.pypi"}
    allowed = covered | inputs_only
    root = resources.files("deptime")
    for module in sorted(covered):
        tree = ast.parse(root.joinpath(module.removeprefix("deptime.").replace(".", "/") + ".py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [a.name for a in node.names if a.name.split(".")[0] == "deptime"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "deptime":
                assert node.level == 0, module
                # `from deptime.ingest import pypi` imports a module; `from deptime.config import X`, names.
                if node.module in allowed:
                    continue
                imported = [f"{node.module}.{a.name}" for a in node.names]
            else:
                continue
            for name in imported:
                assert name in allowed, f"{module} imports {name}"
    assert "index/schema.sql" in pipeline.ASSEMBLY_SOURCES


def test_snapshots_only_leaves_the_index_alone(tmp_path):
    """CI's per-package jobs only build snapshots; the publish job assembles them (DT-DATA-41)."""
    store = IndexStore.memory()
    with _client() as client:
        report = ingest_package(
            SPEC, POLICY, store, tmp_path, client=client, today=TODAY, assemble=False, log=_quiet
        )
    assert report.built == ["1.0.0", "2.0.0"] and report.stats is None
    assert _versions(store) == [] and snapshot_path(tmp_path, "fakepkg", "2.0.0").exists()


def test_ingest_cli_snapshots_only_and_kept_rows(tmp_path, monkeypatch):
    meta, _calls = _cli_home(tmp_path, monkeypatch)
    seen: dict = {}

    def unchanged(spec, *args, **kwargs):
        seen.update(kwargs)
        return IngestReport(spec.name, selected=["1"], cached=["1"], unchanged=True)

    monkeypatch.setattr("deptime.ingest.pipeline.ingest_package", unchanged)
    assert cli.main(["ingest", "httpx", "--into-downloaded", "--snapshots-only"]) == 0
    assert seen["assemble"] is False
    assert not meta.exists()  # kept rows still stamp `built_at`: the index is no longer the downloaded file


def test_coverage_summary_counts_unchanged_packages():
    same = IngestReport("same", selected=["1"], cached=["1"], unchanged=True)
    text = coverage_summary([same, IngestReport("new", selected=["1", "2"], cached=["1", "2"])])
    assert "1 of 2 packages were unchanged since the index was last assembled and kept their rows." in text


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
        "wrapped_slim-2.0.0-py3-none-any.whl": _zip(
            {
                "wrapped/__init__.py": "class New:\n    pass\n",
                "wrapped_slim-2.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: wrapped-slim\n"
                "Version: 2.0.0\nRequires-Dist: httpx>=0.27\n",
            }
        )
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
    # The analysed code's requirements (DT-DATA-14): the pinned distribution's; 1.0.0's wheel has no metadata.
    requires = {"1.0.0": None, "2.0.0": '["httpx>=0.27"]'}
    assert {r["version"]: r["requires"] for r in store.releases("wrapped")} == requires
    assert ApiSnapshot.load(snapshot_path(tmp_path, "wrapped", "2.0.0")).requires == ["httpx>=0.27"]
    assert [p for p in tmp_path.rglob("*") if p.is_file() and "snapshots" not in p.parts] == []

    # CI's publish job assembles from cached snapshots and records the same source files.
    fresh = IndexStore.memory()
    with _wrapped_client() as client:
        again = ingest_package(
            WRAPPED, POLICY, fresh, tmp_path, client=client, today=TODAY, cached_only=True, log=_quiet
        )
    assert again.cached == ["1.0.0", "2.0.0"]
    assert {r["version"]: r["source_file"] for r in fresh.releases("wrapped")} == expected
    assert {r["version"]: r["requires"] for r in fresh.releases("wrapped")} == requires


def test_a_release_yanked_after_it_was_indexed_is_dropped(tmp_path):
    """DT-DATA-04: yanked on PyPI after a run indexed it, a release leaves the index at the next rebuild, from
    the snapshot cache too (CI's assembly), and isn't kept as if its build had failed."""
    store = IndexStore.memory()
    with _client() as client:
        ingest_package(SPEC, POLICY, store, tmp_path, client=client, today=TODAY, log=_quiet)
    assert [r["version"] for r in store.releases("fakepkg")] == ["1.0.0", "2.0.0"]

    def yanked(request: httpx.Request) -> httpx.Response:
        page = _simple_index()
        for f in page["files"]:
            f["yanked"] = f["filename"].startswith("fakepkg-2.0.0") and "broken build"
        return httpx.Response(200, json=page)

    for cached_only in (True, False):
        with httpx.Client(transport=httpx.MockTransport(yanked)) as client:
            report = ingest_package(
                SPEC, POLICY, store, tmp_path, client=client, today=TODAY, cached_only=cached_only, log=_quiet
            )
        assert report.selected == ["1.0.0"] and report.failed == {} and report.carried == []
        assert [r["version"] for r in store.releases("fakepkg")] == ["1.0.0"]


def test_the_summary_says_which_releases_are_past_the_freshness_bound():
    """DT-DATA-04: a selected release is indexed within 24 h of its upload."""
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    fresh = IngestReport(
        "fresh",
        selected=["1", "2"],
        cached=["1", "2"],
        uploaded={"1": "2026-10-01T00:00:00+00:00", "2": "2026-11-05T09:00:00+00:00"},
    )
    text = coverage_summary([fresh], now=now)
    assert (
        "Freshness (bound: 24 h from upload): Every selected release uploaded more than 24 h before this run is "
        "indexed, up to each package's newest. The newest indexed release is fresh 2, uploaded 3 h before it."
        in text
    )
    late = IngestReport(
        "late",
        selected=["1", "2", "3"],
        cached=["1"],
        failed={"2": "boom", "3": "no cached snapshot (cached-only run)"},
        uploaded={
            "1": "2026-10-01T00:00:00+00:00",
            "2": "2026-11-04T02:00:00+00:00",
            "3": "2026-11-05T11:00:00+00:00",
        },
    )
    text = coverage_summary([fresh, late], now=now)
    # Release 3 failed too, but is within the bound.
    assert (
        "**1 release newer than its package's newest indexed one was uploaded more than 24 h before this run**: "
        "late 2 (34 h)." in text
    )
    # An old release that never builds (dspy 0.1.5) isn't a freshness miss once a newer one is indexed.
    old = IngestReport(
        "old",
        selected=["0.1", "2.0"],
        cached=["2.0"],
        failed={"0.1": "no module"},
        uploaded={"0.1": "2024-01-01T00:00:00+00:00", "2.0": "2026-10-01T00:00:00+00:00"},
    )
    assert "Every selected release uploaded more than 24 h" in coverage_summary([fresh, old], now=now)
