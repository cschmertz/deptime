"""Prebuilt index distribution: CI packs and publishes it; local installs download it.

Publish (CI, .github/workflows/build-index.yml):
    deptime index pack --out dist/      -> dist/index.sqlite.xz + dist/index.sqlite.gz + dist/manifest.json
    gh release upload index ... --clobber   (rolling pre-release tagged `index`; the manifest goes last)
Consume (users): `deptime index pull`, or automatically in the background when `deptime mcp` / `deptime serve`
start. Stable URLs: <index_url>/manifest.json, <index_url>/index.sqlite.xz and <index_url>/index.sqlite.gz.

The same file is published twice. The `.xz` is about 37% smaller and is what this client downloads when the
manifest lists it (DT-DATA-35). The `.gz` is for released clients, which read only the manifest keys they know
(`sha256` and `size` describe the `.gz`), and for a Python built without `lzma`.

Safety rules:
- the download is verified against the manifest's sha256 for that file, decompressed to a temp file, validated,
  then atomically renamed into place; a failed download never damages the current index;
- the new file gets a name of its own (`index-<sha>.sqlite`), and the metadata file names it, so no file a
  server holds open is ever replaced (Windows forbids it; DT-OSS-08, see `location.py`);
- an index built locally with `deptime ingest` carries no download metadata and is never replaced automatically.
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING

import httpx

from deptime import __version__
from deptime.config import Settings
from deptime.index.location import (
    active_index,
    meta_path,
    open_index,
    read_local_meta,
    remove_stale,
    versioned_name,
)
from deptime.index.store import SCHEMA_VERSION, IndexStore

__all__ = ["active_index", "meta_path", "open_index", "read_local_meta", "remove_stale"]

try:
    import lzma
except ImportError:  # CPython built without liblzma: publish and download the .gz only
    lzma = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from deptime.query import DepTime

INDEX_ASSET = "index.sqlite.gz"
XZ_ASSET = "index.sqlite.xz"
MANIFEST_ASSET = "manifest.json"
MAX_DOWNLOAD_BYTES = 2 * 1024**3
XZ_PRESET = 6  # 9 saves ~1% for a 64 MB decompression dictionary


class IndexIncompatible(RuntimeError):
    """The published index was built for a different deptime schema."""


@dataclass(frozen=True)
class Manifest:
    schema_version: str
    built_at: str
    sha256: str  # of index.sqlite.gz
    size: int  # bytes of index.sqlite.gz
    packages: int
    releases: int
    deptime_version: str
    xz_sha256: str | None = None  # of index.sqlite.xz; absent in manifests published before DT-DATA-35
    xz_size: int | None = None  # bytes of index.sqlite.xz

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        return cls(**{name: data[name] for name in cls.__dataclass_fields__ if name in data})


@dataclass(frozen=True)
class EnsureResult:
    path: Path | None  # usable index, if any
    changed: bool  # a new file was installed
    message: str


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)  # never stdout: in stdio mode stdout is the MCP channel


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_client() -> httpx.Client:
    return httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": f"deptime/{__version__}"})


# ---------------------------------------------------------------- local metadata
def _replace(src: Path, dst: Path, attempts: int = 20) -> None:
    """os.replace, retried briefly: on Windows it fails while another process is reading `dst`."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def _temp_file(folder: Path, prefix: str, suffix: str) -> Path:
    """A new empty file of this process's own, closed (an open one can't be renamed on Windows)."""
    fd, name = tempfile.mkstemp(dir=folder, prefix=prefix, suffix=suffix)
    os.close(fd)
    return Path(name)


def _write_local_meta(index_path: Path, payload: dict) -> None:
    target = meta_path(index_path)
    tmp = _temp_file(target.parent, f"{target.name}.", ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def adopt_downloaded(index_path: Path) -> None:
    """Before `deptime ingest --into-downloaded` writes to `index_path`: move the downloaded file the metadata
    names there, so the build starts from it (CI's publish job pulls the published index, then ingests)."""
    active = active_index(index_path)
    if active != index_path:
        _replace(active, index_path)


def mark_locally_built(index_path: Path) -> None:
    """Called after `deptime ingest`: the index is now the user's own and must not be auto-replaced."""
    meta_path(index_path).unlink(missing_ok=True)


# ---------------------------------------------------------------- publish side
def pack(index_path: Path, out_dir: Path) -> Manifest:
    """Snapshot (SQLite backup API), VACUUM, compress (xz and gzip), and describe the index for publishing."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "index.sqlite.tmp"
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{index_path.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("VACUUM")
        meta = dict(dst.execute("SELECT key, value FROM meta").fetchall())
        packages = dst.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
        releases = dst.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    finally:
        dst.close()
        src.close()
    gz_path, xz_path = out_dir / INDEX_ASSET, out_dir / XZ_ASSET
    with tmp.open("rb") as raw, gzip.GzipFile(gz_path, "wb", compresslevel=9, mtime=0) as gz:
        shutil.copyfileobj(raw, gz, 1 << 20)
    xz_path.unlink(missing_ok=True)
    if lzma is not None:
        with tmp.open("rb") as raw, lzma.open(xz_path, "wb", preset=XZ_PRESET) as xz:
            shutil.copyfileobj(raw, xz, 1 << 20)
    else:
        _stderr("deptime: this Python has no lzma module; packing the .gz only")
    tmp.unlink()
    has_xz = xz_path.exists()
    manifest = Manifest(
        schema_version=meta.get("schema_version", "?"),
        built_at=meta.get("built_at", _now().isoformat()),
        sha256=_sha256(gz_path),
        size=gz_path.stat().st_size,
        packages=packages,
        releases=releases,
        deptime_version=__version__,
        xz_sha256=_sha256(xz_path) if has_xz else None,
        xz_size=xz_path.stat().st_size if has_xz else None,
    )
    (out_dir / MANIFEST_ASSET).write_text(json.dumps(asdict(manifest), indent=2) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------- consume side
def fetch_manifest(base_url: str, client: httpx.Client) -> Manifest:
    resp = client.get(f"{base_url}/{MANIFEST_ASSET}")
    resp.raise_for_status()
    return Manifest.from_dict(resp.json())


def _validate(path: Path) -> None:
    try:
        store = IndexStore.open(path)
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"downloaded index is not a SQLite database: {exc}") from exc
    try:
        row = store.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None or row["value"] != SCHEMA_VERSION:
            raise IndexIncompatible(
                f"downloaded index has schema {row['value'] if row else '?'}, expected {SCHEMA_VERSION}"
            )
        store.conn.execute("SELECT 1 FROM packages LIMIT 1").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"downloaded index is unreadable: {exc}") from exc
    finally:
        store.close()


def _asset(manifest: Manifest) -> tuple[str, str, int, Callable[[Path], IO[bytes]]]:
    """Which file to download: the .xz when the manifest lists it and this Python can read it, else the .gz."""
    if lzma is not None and manifest.xz_sha256 and manifest.xz_size is not None:
        return XZ_ASSET, manifest.xz_sha256, manifest.xz_size, lzma.open
    return INDEX_ASSET, manifest.sha256, manifest.size, gzip.open


def download(
    base_url: str, dest: Path, client: httpx.Client, *, manifest: Manifest | None = None
) -> Manifest:
    """Download, verify, validate and atomically install the published index at `dest`."""
    manifest = manifest or fetch_manifest(base_url, client)
    if manifest.schema_version != SCHEMA_VERSION:
        raise IndexIncompatible(
            f"the published index uses schema {manifest.schema_version}; deptime {__version__} reads schema "
            f"{SCHEMA_VERSION}. Upgrade deptime (for example `uvx deptime@latest`)."
        )
    asset, expected_sha256, expected_size, opener = _asset(manifest)
    if expected_size > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"published index is {expected_size} bytes, above the {MAX_DOWNLOAD_BYTES}-byte cap")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Named after the build, so it is a new file no process has open; temp names are this process's own.
    target = dest.with_name(versioned_name(dest, manifest.sha256))
    packed_tmp = _temp_file(dest.parent, f"{dest.name}.download-", Path(asset).suffix)
    db_tmp = _temp_file(dest.parent, f"{dest.name}.download-", ".sqlite")
    try:
        digest, size = hashlib.sha256(), 0
        with client.stream("GET", f"{base_url}/{asset}") as resp, packed_tmp.open("wb") as out:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("index download exceeded the size cap")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("index checksum mismatch; refusing to install it")
        with opener(packed_tmp) as packed, db_tmp.open("wb") as out:
            shutil.copyfileobj(packed, out, 1 << 20)
        _validate(db_tmp)
        if not target.exists():  # else another process installed this build already
            os.replace(db_tmp, target)
        now = _now().isoformat()
        _write_local_meta(
            dest,
            {
                "manifest": asdict(manifest),
                "asset": asset,
                "source_url": base_url,
                "downloaded_at": now,
                "checked_at": now,
                "file": target.name,  # the file to open (DT-OSS-08); clients before it ignore this key
            },
        )
        remove_stale(dest)  # what an open server still holds is retried later
        return manifest
    finally:
        packed_tmp.unlink(missing_ok=True)
        db_tmp.unlink(missing_ok=True)


def ensure_index(
    settings: Settings, *, client: httpx.Client | None = None, now: datetime | None = None
) -> EnsureResult:
    """Make sure a usable index exists; refresh a *downloaded* one when it is older than the max age."""
    dest = settings.index_path
    current = active_index(dest)  # the downloaded file the metadata names, else `dest` (DT-OSS-08)
    exists = current.exists()
    if settings.offline:
        return EnsureResult(
            current if exists else None,
            False,
            "offline mode" if exists else "offline mode and no local index",
        )
    local = read_local_meta(dest) if exists else None
    if exists and local is None:
        return EnsureResult(current, False, "using a locally built index (never auto-replaced)")
    now = now or _now()
    if exists and local:
        checked = datetime.fromisoformat(local.get("checked_at") or local["downloaded_at"])
        if now - checked < timedelta(hours=settings.index_max_age_hours):
            return EnsureResult(current, False, "index is fresh")
    own_client = client is None
    client = client or http_client()
    try:
        manifest = fetch_manifest(settings.index_url, client)
        if exists and local and local.get("manifest", {}).get("sha256") == manifest.sha256:
            _write_local_meta(dest, {**local, "checked_at": now.isoformat()})
            return EnsureResult(current, False, "index is up to date")
        download(settings.index_url, dest, client, manifest=manifest)
        verb = "updated" if exists else "downloaded"
        return EnsureResult(
            active_index(dest), True, f"index {verb}: {manifest.packages} packages, built {manifest.built_at}"
        )
    except Exception as exc:  # network, HTTP, checksum, schema: keep whatever works
        reason = f"{type(exc).__name__}: {exc}"
        if exists:
            return EnsureResult(
                current, False, f"could not refresh the index ({reason}); keeping the current one"
            )
        return EnsureResult(
            None, False, f"could not download the index ({reason}); run `deptime index pull` when online"
        )
    finally:
        if own_client:
            client.close()


# Seconds between attempts while there is no index at all (DT-OSS-10); the last one repeats.
RETRY_DELAYS = (30, 60, 120, 300, 600, 1800)
# Seconds a replaced store stays open for requests that already hold it, before it is closed and its file deleted.
CLOSE_GRACE_SECONDS = 60.0


def _retire(store: IndexStore, index_path: Path, grace: float) -> None:
    """Close a store the server swapped out, after `grace`, then delete the files nothing names (DT-OSS-08)."""

    def run() -> None:
        store.close()
        remove_stale(index_path)

    timer = threading.Timer(grace, run)
    timer.daemon = True
    timer.start()


def _in_words(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


def start_background_refresh(
    dt: DepTime,
    settings: Settings,
    *,
    log: Callable[[str], None] = _stderr,
    client: httpx.Client | None = None,
    delays: Sequence[float] = RETRY_DELAYS,
    stop: threading.Event | None = None,
    grace: float = CLOSE_GRACE_SECONDS,
) -> threading.Thread:
    """Ensure/refresh the index without delaying server start-up; hot-swap the store when a new file lands.

    While there is no index, the download is retried with backoff until one lands (or another process puts
    one on disk), and answers say why there is none (DT-OSS-10). `stop` ends the retries.
    """
    stop = stop or threading.Event()

    def run() -> None:
        for attempt in itertools.count():
            result = ensure_index(settings, client=client)
            if result.path is not None:
                old = dt.store
                # Also when another process switched to a newer file while this one kept the old open.
                if result.changed or old is None or old.path != result.path:
                    dt.set_store(IndexStore.open(result.path))
                    if old is not None and old.path is not None:
                        _retire(old, settings.index_path, grace)
                else:
                    remove_stale(settings.index_path)  # files a server held at the last switch
                dt.index_problem = None
                if result.changed:
                    log(f"deptime: {result.message}")
                return
            log(f"deptime: {result.message}")
            if settings.offline:
                dt.index_problem = (
                    "DEPTIME_OFFLINE is set, so it isn't downloaded; run `deptime index pull` once online."
                )
                return
            delay = delays[min(attempt, len(delays) - 1)]
            dt.index_problem = (
                f"{result.message}. DepTime is retrying in the background in {_in_words(delay)}."
            )
            if stop.wait(delay):
                return
            if dt.store is not None:  # another process downloaded it
                dt.index_problem = None
                return

    thread = threading.Thread(target=run, name="deptime-index-refresh", daemon=True)
    thread.start()
    return thread
