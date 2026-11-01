"""Index publishing + download for local mode, fully offline via httpx.MockTransport."""

import gzip
import hashlib
import json
import lzma
from dataclasses import replace
from datetime import timedelta

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
    assert (manifest.packages, manifest.releases, manifest.schema_version) == (1, 3, "1")
    assert json.loads((out / "manifest.json").read_text())["sha256"] == manifest.sha256
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    assert result.changed and result.path == cfg.index_path
    dt = DepTime(IndexStore.open(cfg.index_path), coverage)
    assert dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0].issue == "param_removed"
    assert dist.read_local_meta(cfg.index_path)["manifest"]["sha256"] == manifest.sha256


def test_checksum_mismatch_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = cfg.index_path.read_bytes()
    with _client(out, corrupt=True) as client, pytest.raises(ValueError, match="checksum"):
        dist.download(cfg.index_url, cfg.index_path, client)
    assert cfg.index_path.read_bytes() == before
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
    dt = DepTime(IndexStore.open(cfg.index_path), coverage)
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
    before = cfg.index_path.read_bytes()
    calls: list[str] = []
    with _client(out, corrupt=True, calls=calls) as client, pytest.raises(ValueError, match="checksum"):
        dist.download(cfg.index_url, cfg.index_path, client)
    assert calls[-1] == "https://idx.test/index.sqlite.xz"  # no second download of the .gz
    assert cfg.index_path.read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_a_truncated_xz_with_a_matching_checksum_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = cfg.index_path.read_bytes()
    xz = out / dist.XZ_ASSET
    xz.write_bytes(xz.read_bytes()[: xz.stat().st_size // 2])
    xz_sha256 = hashlib.sha256(xz.read_bytes()).hexdigest()
    _edit_manifest(out, sha256="0" * 64, xz_sha256=xz_sha256, xz_size=xz.stat().st_size)  # a new build
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert result.changed is False and "keeping the current one" in result.message, result.message
    assert "EOFError" in result.message  # failed while decompressing, after the checksum passed
    assert cfg.index_path.read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_an_xz_above_the_size_cap_is_refused_before_downloading(tmp_path, settings, monkeypatch):
    out, manifest = _published(tmp_path)
    monkeypatch.setattr(dist, "MAX_DOWNLOAD_BYTES", manifest.xz_size - 1)
    calls: list[str] = []
    with _client(out, calls=calls) as client, pytest.raises(ValueError, match="cap"):
        dist.download("https://idx.test", tmp_path / "home" / "index.sqlite", client)
    assert calls == ["https://idx.test/manifest.json"]
