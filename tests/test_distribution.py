"""Index publishing + download for local mode, fully offline via httpx.MockTransport."""

import gzip
import hashlib
import json
import lzma
import subprocess
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from deptime.index import distribution as dist
from deptime.index.store import IndexStore
from deptime.query import DepTime
from tests.conftest import HTTPX, RELEASED_AT, SNAPSHOTS
from tests.test_tracebacks import HTTPX_TB


def _published(tmp_path):
    src = tmp_path / "built.sqlite"
    store = IndexStore.open(src, readonly=False)
    store.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    store.close()
    out = tmp_path / "dist"
    return out, dist.pack(src, out)


def _client(dist_dir, *, corrupt=False, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        name = str(request.url).rsplit("/", 1)[-1]
        path = dist_dir / name
        if not path.exists():
            return httpx.Response(404)
        data = path.read_bytes()
        if corrupt and name in (dist.INDEX_ASSET, dist.XZ_ASSET):
            data = data[:-16] + b"\0" * 16
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _local(settings, tmp_path):
    return replace(
        settings, index_path=tmp_path / "home" / "index.sqlite", index_url="https://idx.test", offline=False
    )


def test_pack_then_download_installs_a_working_index(tmp_path, settings, coverage):
    out, manifest = _published(tmp_path)
    assert (manifest.packages, manifest.releases, manifest.schema_version) == (1, 3, "2")
    assert json.loads((out / "manifest.json").read_text())["sha256"] == manifest.sha256
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    assert result.changed and result.path == dist.active_index(cfg.index_path)
    dt = DepTime(IndexStore.open(result.path), coverage)
    assert dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0].issue == "param_removed"
    assert dist.read_local_meta(cfg.index_path)["manifest"]["sha256"] == manifest.sha256


def test_checksum_mismatch_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = dist.active_index(cfg.index_path).read_bytes()
    with _client(out, corrupt=True) as client, pytest.raises(ValueError, match="checksum"):
        dist.download(cfg.index_url, cfg.index_path, client)
    assert dist.active_index(cfg.index_path).read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_incompatible_schema_is_refused(tmp_path, settings):
    out, _ = _published(tmp_path)
    data = json.loads((out / "manifest.json").read_text())
    data["schema_version"] = "999"
    (out / "manifest.json").write_text(json.dumps(data))
    with _client(out) as client:
        result = dist.ensure_index(_local(settings, tmp_path), client=client)
    assert result.path is None and "IndexIncompatible" in result.message


def test_locally_built_index_is_never_fetched_or_replaced(tmp_path, settings):
    cfg = _local(settings, tmp_path)
    store = IndexStore.open(cfg.index_path, readonly=False)
    store.rebuild_package(HTTPX, SNAPSHOTS[:1], RELEASED_AT)
    store.close()
    out, _ = _published(tmp_path)
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        result = dist.ensure_index(cfg, client=client)
    assert result.changed is False and "locally built" in result.message and calls == []


def test_fresh_index_is_not_rechecked_and_stale_one_is(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(cfg, client=client).message == "index is fresh"
    assert calls == []
    later = dist._now() + timedelta(hours=25)
    with _client(out, calls=calls) as client:
        result = dist.ensure_index(cfg, client=client, now=later)
    assert result.message == "index is up to date" and calls == ["https://idx.test/manifest.json"]


def test_offline_mode_never_downloads(tmp_path, settings):
    assert dist.ensure_index(replace(_local(settings, tmp_path), offline=True)).path is None


def test_background_refresh_hot_swaps_the_store(tmp_path, settings, coverage):
    out, _ = _published(tmp_path)
    dt = DepTime(None, coverage)
    assert "no index yet" in dt.symbol_history("httpx", "Client").note
    with _client(out) as client:
        thread = dist.start_background_refresh(
            dt, _local(settings, tmp_path), client=client, log=lambda _m: None
        )
        thread.join(timeout=10)
    assert dt.symbol_history("httpx", "Client").ever_seen


def _flaky_client(dist_dir, failures: int, calls: list[str]):
    """Refuses the first `failures` requests, as a network that isn't up yet would."""
    served = _client(dist_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= failures:
            raise httpx.ConnectError("network is unreachable", request=request)
        return served.send(request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_failed_first_download_is_retried_until_it_lands(tmp_path, settings, coverage):
    """DT-OSS-10: a server started without network gets its index once the network is back, without a restart."""
    out, _ = _published(tmp_path)
    dt, calls, logged = DepTime(None, coverage), [], []
    with _flaky_client(out, 2, calls) as client:
        thread = dist.start_background_refresh(
            dt, _local(settings, tmp_path), client=client, log=logged.append, delays=(0, 0)
        )
        thread.join(timeout=10)
    assert not thread.is_alive() and dt.symbol_history("httpx", "Client").ever_seen
    assert sum("could not download" in m for m in logged) == 2 and dt.index_problem is None


def test_no_index_note_names_the_cause_while_retrying(tmp_path, settings, coverage):
    dt, stop = DepTime(None, coverage), threading.Event()
    with _flaky_client(tmp_path, 10**6, []) as client:
        thread = dist.start_background_refresh(
            dt, _local(settings, tmp_path), client=client, log=lambda _m: None, delays=(60,), stop=stop
        )
        for _ in range(100):
            if dt.index_problem:
                break
            time.sleep(0.05)
        note = dt.symbol_history("httpx", "Client").note
        stop.set()
        thread.join(timeout=10)
    assert "could not download the index (ConnectError: network is unreachable)" in note
    assert "retrying in the background in 1 min" in note
    assert "network is unreachable" in dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).note
    assert not thread.is_alive()


def test_offline_mode_without_an_index_says_so_and_never_retries(tmp_path, settings, coverage):
    dt, calls = DepTime(None, coverage), []
    with _flaky_client(tmp_path, 0, calls) as client:
        thread = dist.start_background_refresh(
            dt, replace(_local(settings, tmp_path), offline=True), client=client, log=lambda _m: None
        )
        thread.join(timeout=10)
    assert calls == [] and not thread.is_alive()
    assert "DEPTIME_OFFLINE" in dt.symbol_history("httpx", "Client").note


# ---------------------------------------------------------------- .xz beside .gz (DT-DATA-35)
def _edit_manifest(out, **changes):
    data = json.loads((out / "manifest.json").read_text())
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    (out / "manifest.json").write_text(json.dumps(data))


def test_pack_writes_xz_beside_gz_and_the_manifest_describes_both(tmp_path):
    out, manifest = _published(tmp_path)
    gz, xz = out / dist.INDEX_ASSET, out / dist.XZ_ASSET
    assert gz.read_bytes() and xz.read_bytes() and not list(out.glob("*.tmp"))
    assert gzip.decompress(gz.read_bytes()) == lzma.decompress(xz.read_bytes())
    data = json.loads((out / "manifest.json").read_text())
    # the keys released clients read keep their meaning: they describe the .gz
    assert data["sha256"] == hashlib.sha256(gz.read_bytes()).hexdigest() and data["size"] == gz.stat().st_size
    assert data["xz_sha256"] == hashlib.sha256(xz.read_bytes()).hexdigest()
    assert data["xz_size"] == xz.stat().st_size
    assert (manifest.xz_sha256, manifest.xz_size) == (data["xz_sha256"], data["xz_size"])


def test_download_prefers_the_xz_when_the_manifest_lists_it(tmp_path, settings, coverage):
    out, manifest = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(cfg, client=client).changed
    assert calls == ["https://idx.test/manifest.json", "https://idx.test/index.sqlite.xz"]
    dt = DepTime(IndexStore.open(dist.active_index(cfg.index_path)), coverage)
    assert dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0].issue == "param_removed"
    meta = dist.read_local_meta(cfg.index_path)
    assert meta["manifest"]["sha256"] == manifest.sha256 and meta["asset"] == dist.XZ_ASSET
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_a_manifest_without_xz_keys_downloads_the_gz(tmp_path, settings):
    out, _ = _published(tmp_path)
    _edit_manifest(out, xz_sha256=None, xz_size=None)  # as published before DT-DATA-35
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(_local(settings, tmp_path), client=client).changed
    assert calls[-1] == "https://idx.test/index.sqlite.gz"


def test_without_lzma_the_client_downloads_the_gz(tmp_path, settings, monkeypatch):
    out, _ = _published(tmp_path)
    monkeypatch.setattr(dist, "lzma", None)  # a Python built without liblzma
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(_local(settings, tmp_path), client=client).changed
    assert calls[-1] == "https://idx.test/index.sqlite.gz"


def test_without_lzma_pack_writes_the_gz_only(tmp_path, monkeypatch):
    monkeypatch.setattr(dist, "lzma", None)
    out, manifest = _published(tmp_path)
    assert (out / dist.INDEX_ASSET).exists() and not (out / dist.XZ_ASSET).exists()
    assert manifest.xz_sha256 is None and manifest.xz_size is None


def test_xz_checksum_mismatch_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = dist.active_index(cfg.index_path).read_bytes()
    calls: list[str] = []
    with _client(out, corrupt=True, calls=calls) as client, pytest.raises(ValueError, match="checksum"):
        dist.download(cfg.index_url, cfg.index_path, client)
    assert calls[-1] == "https://idx.test/index.sqlite.xz"  # no second download of the .gz
    assert dist.active_index(cfg.index_path).read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_a_truncated_xz_with_a_matching_checksum_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = dist.active_index(cfg.index_path).read_bytes()
    xz = out / dist.XZ_ASSET
    xz.write_bytes(xz.read_bytes()[: xz.stat().st_size // 2])
    xz_sha256 = hashlib.sha256(xz.read_bytes()).hexdigest()
    _edit_manifest(out, sha256="0" * 64, xz_sha256=xz_sha256, xz_size=xz.stat().st_size)  # a new build
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert result.changed is False and "keeping the current one" in result.message, result.message
    assert "EOFError" in result.message  # failed while decompressing, after the checksum passed
    assert dist.active_index(cfg.index_path).read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_an_xz_above_the_size_cap_is_refused_before_downloading(tmp_path, settings, monkeypatch):
    out, manifest = _published(tmp_path)
    monkeypatch.setattr(dist, "MAX_DOWNLOAD_BYTES", manifest.xz_size - 1)
    calls: list[str] = []
    with _client(out, calls=calls) as client, pytest.raises(ValueError, match="cap"):
        dist.download("https://idx.test", tmp_path / "home" / "index.sqlite", client)
    assert calls == ["https://idx.test/manifest.json"]


# ---------------------------------------------------------------- a new file per build (DT-OSS-08)
def _republished(tmp_path, out):
    """A second build (one more package) published to the same folder."""
    src = tmp_path / "built2.sqlite"
    store = IndexStore.open(src, readonly=False)
    store.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    store.rebuild_package(
        replace(HTTPX, name="httpx-two", import_names=("httpx_two",)), SNAPSHOTS[:1], RELEASED_AT
    )
    store.close()
    return dist.pack(src, out)


@pytest.fixture
def windows_locks(monkeypatch):
    """Windows semantics on any OS: a file an IndexStore has open can't be replaced or deleted. On Windows itself
    the OS refuses, so nothing is simulated."""
    import os
    import sys
    from pathlib import Path

    from deptime.index import location

    held: dict[Path, int] = {}
    if sys.platform == "win32":
        return held
    real_open, real_close = IndexStore.open.__func__, IndexStore.close
    real_replace, real_unlink = os.replace, Path.unlink

    def tracking_open(cls, path, *, readonly=True):
        store = real_open(cls, path, readonly=readonly)
        held[Path(path).resolve()] = held.get(Path(path).resolve(), 0) + 1
        return store

    def tracking_close(self):
        if self.path is not None:
            held[Path(self.path).resolve()] -= 1
        real_close(self)

    def guarded_replace(src, dst):
        if held.get(Path(dst).resolve()):
            raise PermissionError(13, "The process cannot access the file because it is being used", str(dst))
        real_replace(src, dst)

    def guarded_unlink(self, missing_ok=False):
        if held.get(self.resolve()):
            raise PermissionError(
                13, "The process cannot access the file because it is being used", str(self)
            )
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(IndexStore, "open", classmethod(tracking_open))
    monkeypatch.setattr(IndexStore, "close", tracking_close)
    monkeypatch.setattr(dist.os, "replace", guarded_replace)
    monkeypatch.setattr(location.Path, "unlink", guarded_unlink)
    return held


def test_a_download_gets_a_file_named_after_its_build(tmp_path, settings):
    out, manifest = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    name = f"index-{manifest.sha256[:16]}.sqlite"
    assert (
        result.path == cfg.index_path.with_name(name) and dist.read_local_meta(cfg.index_path)["file"] == name
    )
    assert not cfg.index_path.exists() and dist.remove_stale(cfg.index_path) == []
    # The next build is a new file, and the old one goes once nothing holds it.
    second = _republished(tmp_path, out)
    with _client(out) as client:
        later = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert later.changed and later.path.name == f"index-{second.sha256[:16]}.sqlite"
    assert sorted(p.name for p in cfg.index_path.parent.glob("index*.sqlite")) == [later.path.name]


def test_a_refresh_succeeds_while_a_server_holds_the_old_file_open(
    tmp_path, settings, coverage, windows_locks
):
    """The AC: on Windows the old file can't be replaced or deleted while open. The new build gets its own file,
    another server keeps answering from the old one, and the old file goes once it is closed."""
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        first = dist.ensure_index(cfg, client=client).path
    other_server = IndexStore.open(first)
    _republished(tmp_path, out)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert result.changed and result.path != first and first.exists()  # still open, so still there
    assert DepTime(other_server, coverage).explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings
    assert dist.remove_stale(cfg.index_path) == [first]
    other_server.close()
    assert dist.remove_stale(cfg.index_path) == [] and not first.exists()


def test_the_server_switches_to_a_file_another_process_installed(tmp_path, settings, coverage, windows_locks):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        first = dist.ensure_index(cfg, client=client).path
    dt = DepTime(IndexStore.open(first), coverage, cfg.index_path)
    _republished(tmp_path, out)
    with _client(out) as client:  # another `deptime mcp` refreshed it
        newer = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25)).path
    thread = dist.start_background_refresh(dt, replace(cfg, offline=True), log=lambda _m: None, grace=0)
    thread.join(timeout=10)
    assert dt.store.path == newer and dt.coverage_report().packages[-1].package == "httpx-two"
    for _ in range(100):  # the old store is closed after the grace period, then its file deleted
        if not first.exists():
            break
        time.sleep(0.02)
    assert not first.exists()


def test_an_index_downloaded_before_versioned_files_is_still_used_then_replaced(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    # The layout clients before DT-OSS-08 leave: the file at index_path, metadata without "file".
    dist.active_index(cfg.index_path).rename(cfg.index_path)
    meta = dist.read_local_meta(cfg.index_path)
    del meta["file"]
    dist.meta_path(cfg.index_path).write_text(json.dumps(meta))
    assert dist.active_index(cfg.index_path) == cfg.index_path
    with _client(out) as client:
        assert dist.ensure_index(cfg, client=client).message == "index is fresh"
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert result.message == "index is up to date" and result.path == cfg.index_path
    _republished(tmp_path, out)
    with _client(out) as client:  # checked at +25 h above
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=50))
    assert result.changed and result.path.name.startswith("index-") and not cfg.index_path.exists()


def test_the_metadata_names_only_a_file_beside_it(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    meta = dist.read_local_meta(cfg.index_path)
    elsewhere = tmp_path / "elsewhere.sqlite"
    elsewhere.write_bytes(b"")
    for name in ["../elsewhere.sqlite", str(elsewhere), "index-zz.sqlite", 7]:
        dist.meta_path(cfg.index_path).write_text(json.dumps({**meta, "file": name}))
        assert dist.active_index(cfg.index_path) == cfg.index_path


def test_ingest_into_a_downloaded_index_starts_from_its_file(tmp_path, settings, monkeypatch):
    """CI's publish job pulls the published index, then ingests into it with --into-downloaded."""
    from deptime import cli
    from deptime.ingest.pipeline import IngestReport

    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    monkeypatch.setenv("DEPTIME_HOME", str(cfg.index_path.parent))
    monkeypatch.delenv("DEPTIME_INDEX_PATH", raising=False)
    seen = []

    def fake_ingest(spec, policy, store, *args, **kwargs):
        seen.append([r["name"] for r in store.package_rows()])
        return IngestReport(spec.name, unchanged=True)

    monkeypatch.setattr("deptime.ingest.pipeline.ingest_package", fake_ingest)
    assert cli.main(["ingest", "httpx", "--into-downloaded"]) == 0
    assert seen == [["httpx"]]  # the downloaded rows, not an empty new file
    assert dist.read_local_meta(cfg.index_path) is None and cfg.index_path.exists()
    assert not list(cfg.index_path.parent.glob("index-*.sqlite"))


# ---------------------------------------------------------------- build provenance (DT-OSS-09)
GH_REPO = "acme/deptime"
GH_URL = f"https://github.com/{GH_REPO}/releases/download/index"
WORKFLOW_SAN = f"https://github.com/{GH_REPO}/{dist.BUILD_WORKFLOW}@{dist.BUILD_REF}"


def _gh_result(sha256, statement=None, **cert):
    """The part of `gh attestation verify --format json` output that DepTime reads."""
    certificate = {
        "issuer": dist.OIDC_ISSUER,
        "subjectAlternativeName": WORKFLOW_SAN,
        "sourceRepositoryURI": f"https://github.com/{GH_REPO}",
        "sourceRepositoryRef": dist.BUILD_REF,
        "sourceRepositoryDigest": "0123456789abcdef0123456789abcdef01234567",
        "runnerEnvironment": "github-hosted",
        "runInvocationURI": f"https://github.com/{GH_REPO}/actions/runs/42/attempts/1",
        **cert,
    }
    subject = [{"name": "index.sqlite.xz", "digest": {"sha256": sha256}}]
    result = {
        "signature": {"certificate": certificate},
        "statement": {"predicateType": dist.SLSA_PROVENANCE, "subject": subject, **(statement or {})},
        "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2026-11-07T09:00:00-05:00"}],
    }
    return [{"attestation": {}, "verificationResult": result}]


class FakeGh:
    """Stands in for `gh attestation verify`: records each call, then answers like gh would."""

    def __init__(self):
        self.calls, self.bundles = [], []
        self.returncode, self.stderr = 0, ""
        self.result = _gh_result  # sha256 of the file passed -> verified attestations

    def run(self, args):
        self.calls.append(args)
        bundle = args[args.index("--bundle") + 1] if "--bundle" in args else None
        self.bundles.append(Path(bundle).read_text() if bundle else None)
        if self.returncode:
            return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)
        sha = hashlib.sha256(Path(args[3]).read_bytes()).hexdigest()
        return subprocess.CompletedProcess(args, 0, json.dumps(self.result(sha)), "")


@pytest.fixture
def fake_gh(monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(dist, "_find_gh", lambda: "gh")
    monkeypatch.setattr(dist, "_run_gh", gh.run)
    return gh


def _installed(settings, tmp_path, out):
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    return cfg


@pytest.mark.parametrize(
    "url, repo",
    [
        ("https://github.com/cschmertz/deptime/releases/download/index", "cschmertz/deptime"),
        (f"{GH_URL}/", GH_REPO),
        ("https://idx.test", None),
        ("http://github.com/acme/deptime/releases/download/index", None),
        ("https://github.com/acme/deptime/releases/latest", None),
        ("https://github.com.evil.test/acme/deptime/releases/download/index", None),
    ],
)
def test_the_repository_to_verify_against_comes_from_the_index_url(url, repo):
    assert dist.release_repo(url) == repo


def test_the_default_index_url_verifies_against_this_repository():
    from deptime.config import DEFAULT_INDEX_URL

    assert dist.release_repo(DEFAULT_INDEX_URL) == "cschmertz/deptime"


def test_a_verified_download_is_installed_and_records_its_build(tmp_path, settings, fake_gh):
    out, manifest = _published(tmp_path)
    (out / dist.ATTESTATION_ASSET).write_text('{"mediaType": "bundle"}\n')
    cfg = replace(_local(settings, tmp_path), index_url=GH_URL, verify_index=True)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    assert result.changed and result.message.endswith("build provenance verified")
    (args,) = fake_gh.calls
    assert args[:3] == ["gh", "attestation", "verify"] and args[3].endswith(".xz")
    assert args[args.index("--repo") + 1] == GH_REPO
    assert args[args.index("--signer-workflow") + 1] == f"{GH_REPO}/{dist.BUILD_WORKFLOW}"
    assert fake_gh.bundles == ['{"mediaType": "bundle"}\n']  # the published bundle, so gh needs no login
    meta = dist.read_local_meta(cfg.index_path)
    assert meta["manifest"]["sha256"] == manifest.sha256
    assert meta["provenance"]["repository"] == GH_REPO
    assert meta["provenance"]["run"] == f"https://github.com/{GH_REPO}/actions/runs/42/attempts/1"
    assert meta["provenance"]["commit"].startswith("0123456789ab")
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_without_a_published_bundle_gh_looks_the_attestation_up_itself(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    (args,) = fake_gh.calls
    assert "--bundle" not in args and fake_gh.bundles == [None]
    assert dist.read_local_meta(cfg.index_path)["provenance"]["repository"] == GH_REPO


def test_an_unverified_download_records_no_provenance_and_never_runs_gh(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    assert "provenance" not in dist.read_local_meta(cfg.index_path) and fake_gh.calls == []


@pytest.mark.parametrize(
    "cert, statement",
    [
        (
            {
                "subjectAlternativeName": f"https://github.com/{GH_REPO}/.github/workflows/release.yml@refs/heads/main"
            },
            None,
        ),
        ({"subjectAlternativeName": WORKFLOW_SAN.replace("build-index.yml", "build-index.yml.evil")}, None),
        (
            {
                "subjectAlternativeName": WORKFLOW_SAN.replace("main", "dev"),
                "sourceRepositoryRef": "refs/heads/dev",
            },
            None,
        ),
        (
            {
                "subjectAlternativeName": WORKFLOW_SAN.replace("main", "Main"),
                "sourceRepositoryRef": "refs/heads/Main",
            },
            None,
        ),
        ({"sourceRepositoryRef": "refs/heads/dev"}, None),
        ({"runnerEnvironment": "self-hosted"}, None),
        ({"issuer": "https://accounts.example.test"}, None),
        (
            {
                "sourceRepositoryURI": "https://github.com/evil/deptime",
                "subjectAlternativeName": WORKFLOW_SAN.replace(GH_REPO, "evil/deptime"),
            },
            None,
        ),
        ({}, {"predicateType": "https://example.test/custom/v1"}),
        ({}, {"subject": [{"name": "index.sqlite.xz", "digest": {"sha256": "0" * 64}}]}),
    ],
    ids=[
        "other-workflow",
        "workflow-prefix",
        "other-branch",
        "branch-case",
        "ref-mismatch",
        "self-hosted",
        "other-issuer",
        "other-repo",
        "other-predicate",
        "other-file",
    ],
)
def test_an_attestation_from_another_signer_keeps_the_current_index(
    tmp_path, settings, fake_gh, cert, statement
):
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    before = dist.read_local_meta(cfg.index_path)
    fake_gh.result = lambda sha: _gh_result(sha, statement, **cert)
    with _client(out) as client, pytest.raises(dist.ProvenanceError, match="no attestation for this file"):
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path) == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_an_owner_named_in_another_case_still_verifies(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    url = GH_URL.replace("acme", "ACME")
    with _client(out) as client:
        dist.download(url, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path)["provenance"]["repository"] == GH_REPO


def test_when_gh_rejects_the_file_its_reason_is_given_and_nothing_is_installed(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    fake_gh.returncode, fake_gh.stderr = (
        1,
        'Loaded digest sha256:abc\nError: verifying with issuer "sigstore.dev"\n',
    )
    with (
        _client(out) as client,
        pytest.raises(dist.ProvenanceError, match=r"rejected the index: .*sigstore\.dev"),
    ):
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path) is None
    assert not list(cfg.index_path.parent.glob("index-*.sqlite"))
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_verifying_without_gh_says_how_to_get_it(tmp_path, settings, monkeypatch):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    monkeypatch.setattr(dist, "_find_gh", lambda: None)
    with _client(out) as client, pytest.raises(dist.ProvenanceError, match="GitHub CLI"):
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path) is None


def test_verifying_needs_an_index_served_from_github_releases(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client, pytest.raises(dist.ProvenanceError, match="GitHub repository's releases"):
        dist.download(cfg.index_url, cfg.index_path, client, verify=True)
    assert fake_gh.calls == []


def test_a_refresh_that_must_verify_keeps_the_current_index_when_it_fails(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    before = dist.active_index(cfg.index_path).read_bytes()
    meta = dist.read_local_meta(cfg.index_path)
    dist.meta_path(cfg.index_path).write_text(
        json.dumps(
            {
                **meta,
                "manifest": {**meta["manifest"], "sha256": "old"},
                "checked_at": "2000-01-01T00:00:00+00:00",
            }
        )
    )
    fake_gh.returncode, fake_gh.stderr = 1, "Error: no attestations found\n"
    cfg = replace(cfg, index_url=GH_URL, verify_index=True)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    assert (
        not result.changed
        and "ProvenanceError" in result.message
        and "keeping the current one" in result.message
    )
    assert dist.active_index(cfg.index_path).read_bytes() == before


def test_index_pull_verify_checks_a_current_index_that_was_installed_without_it(
    tmp_path, settings, fake_gh, monkeypatch, capsys
):
    from deptime import cli

    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    monkeypatch.setenv("DEPTIME_HOME", str(cfg.index_path.parent))
    monkeypatch.setenv("DEPTIME_INDEX_URL", GH_URL)
    for name in ("DEPTIME_INDEX_PATH", "DEPTIME_VERIFY_INDEX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dist, "http_client", lambda: _client(out))

    assert cli.main(["index", "pull"]) == 0  # current, and not asked to verify
    assert fake_gh.calls == []
    assert cli.main(["index", "pull", "--verify"]) == 0  # current but never verified: downloaded and checked
    assert len(fake_gh.calls) == 1
    assert "build provenance verified: acme/deptime .github/workflows/build-index.yml@refs/heads/main" in (
        capsys.readouterr().err
    )
    assert cli.main(["index", "pull", "--verify"]) == 0  # verified already
    assert len(fake_gh.calls) == 1 and "up to date" in capsys.readouterr().err

    assert cli.main(["index", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["provenance"]["repository"] == GH_REPO and status["verify_index"] is False

    meta = dist.read_local_meta(cfg.index_path)
    fake_gh.returncode, fake_gh.stderr = 1, "Error: no attestations found\n"
    assert cli.main(["index", "pull", "--verify", "--force"]) == 1
    assert "Nothing was installed; the current index is unchanged." in capsys.readouterr().err
    assert dist.read_local_meta(cfg.index_path) == meta
