"""Offline end-to-end: real wheel zips served through httpx.MockTransport -> pipeline -> index -> explain_error."""

import hashlib
import io
import zipfile
from datetime import date

import httpx

from deptime import cli
from deptime.config import Coverage, PackageSpec, VersionPolicy
from deptime.index.store import IndexStore
from deptime.ingest.pipeline import IngestReport, ingest_package, snapshot_path
from deptime.ingest.snapshot import ApiSnapshot
from deptime.query import DepTime

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


def _simple_index() -> dict:
    files = []
    for i, (name, data) in enumerate(WHEELS.items()):
        files.append(
            {
                "filename": name,
                "url": f"https://files.example/{name}",
                "hashes": {"sha256": hashlib.sha256(data).hexdigest()},
                "upload-time": f"2026-0{i + 1}-01T00:00:00Z",
                "yanked": False,
            }
        )
    return {"name": "fakepkg", "files": files, "meta": {"api-version": "1.4"}}


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
