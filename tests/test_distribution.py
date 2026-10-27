"""Index publishing + download for local mode, fully offline via httpx.MockTransport."""

import json
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
        if corrupt and name == dist.INDEX_ASSET:
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
