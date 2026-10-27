"""Prebuilt index distribution: CI packs and publishes it; local installs download it.

Publish (CI, .github/workflows/build-index.yml):
    deptime index pack --out dist/                 -> dist/index.sqlite.gz + dist/manifest.json
    gh release upload index dist/* --clobber        (rolling pre-release tagged `index`)
Consume (users): `deptime index pull`, or automatically in the background when `deptime mcp` / `deptime serve`
start. Stable URLs: <index_url>/manifest.json and <index_url>/index.sqlite.gz.

Safety rules:
- the download is verified against the manifest's sha256, decompressed to a temp file, validated, then
  atomically renamed into place; a failed download never damages the current index;
- an index built locally with `deptime ingest` carries no download metadata and is never replaced automatically.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from deptime import __version__
from deptime.config import Settings
from deptime.index.store import SCHEMA_VERSION, IndexStore

if TYPE_CHECKING:
    from deptime.query import DepTime

INDEX_ASSET = "index.sqlite.gz"
MANIFEST_ASSET = "manifest.json"
MAX_DOWNLOAD_BYTES = 2 * 1024**3


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

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        return cls(**{name: data[name] for name in cls.__dataclass_fields__})


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
def meta_path(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".meta.json")


def read_local_meta(index_path: Path) -> dict | None:
    try:
        return json.loads(meta_path(index_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_local_meta(index_path: Path, payload: dict) -> None:
    target = meta_path(index_path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def mark_locally_built(index_path: Path) -> None:
    """Called after `deptime ingest`: the index is now the user's own and must not be auto-replaced."""
    meta_path(index_path).unlink(missing_ok=True)


# ---------------------------------------------------------------- publish side
def pack(index_path: Path, out_dir: Path) -> Manifest:
    """Snapshot (SQLite backup API), VACUUM, gzip, and describe the index for publishing."""
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
    gz_path = out_dir / INDEX_ASSET
    with tmp.open("rb") as raw, gzip.GzipFile(gz_path, "wb", compresslevel=9, mtime=0) as gz:
        shutil.copyfileobj(raw, gz, 1 << 20)
    tmp.unlink()
    manifest = Manifest(
        schema_version=meta.get("schema_version", "?"),
        built_at=meta.get("built_at", _now().isoformat()),
        sha256=_sha256(gz_path),
        size=gz_path.stat().st_size,
        packages=packages,
        releases=releases,
        deptime_version=__version__,
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
    if manifest.size > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"published index is {manifest.size} bytes, above the {MAX_DOWNLOAD_BYTES}-byte cap")
    dest.parent.mkdir(parents=True, exist_ok=True)
    gz_tmp = dest.with_name(dest.name + ".download.gz")
    db_tmp = dest.with_name(dest.name + ".download")
    try:
        digest, size = hashlib.sha256(), 0
        with client.stream("GET", f"{base_url}/{INDEX_ASSET}") as resp, gz_tmp.open("wb") as out:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ValueError("index download exceeded the size cap")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != manifest.sha256:
            raise ValueError("index checksum mismatch; refusing to install it")
        with gzip.open(gz_tmp, "rb") as gz, db_tmp.open("wb") as out:
            shutil.copyfileobj(gz, out, 1 << 20)
        _validate(db_tmp)
        os.replace(db_tmp, dest)
        now = _now().isoformat()
        _write_local_meta(
            dest,
            {"manifest": asdict(manifest), "source_url": base_url, "downloaded_at": now, "checked_at": now},
        )
        return manifest
    finally:
        gz_tmp.unlink(missing_ok=True)
        db_tmp.unlink(missing_ok=True)


def ensure_index(
    settings: Settings, *, client: httpx.Client | None = None, now: datetime | None = None
) -> EnsureResult:
    """Make sure a usable index exists; refresh a *downloaded* one when it is older than the max age."""
    dest = settings.index_path
    exists = dest.exists()
    if settings.offline:
        return EnsureResult(
            dest if exists else None, False, "offline mode" if exists else "offline mode and no local index"
        )
    local = read_local_meta(dest) if exists else None
    if exists and local is None:
        return EnsureResult(dest, False, "using a locally built index (never auto-replaced)")
    now = now or _now()
    if exists and local:
        checked = datetime.fromisoformat(local.get("checked_at") or local["downloaded_at"])
        if now - checked < timedelta(hours=settings.index_max_age_hours):
            return EnsureResult(dest, False, "index is fresh")
    own_client = client is None
    client = client or http_client()
    try:
        manifest = fetch_manifest(settings.index_url, client)
        if exists and local and local.get("manifest", {}).get("sha256") == manifest.sha256:
            _write_local_meta(dest, {**local, "checked_at": now.isoformat()})
            return EnsureResult(dest, False, "index is up to date")
        download(settings.index_url, dest, client, manifest=manifest)
        verb = "updated" if exists else "downloaded"
        return EnsureResult(
            dest, True, f"index {verb}: {manifest.packages} packages, built {manifest.built_at}"
        )
    except Exception as exc:  # network, HTTP, checksum, schema: keep whatever works
        reason = f"{type(exc).__name__}: {exc}"
        if exists:
            return EnsureResult(
                dest, False, f"could not refresh the index ({reason}); keeping the current one"
            )
        return EnsureResult(
            None, False, f"could not download the index ({reason}); run `deptime index pull` when online"
        )
    finally:
        if own_client:
            client.close()


def start_background_refresh(
    dt: DepTime,
    settings: Settings,
    *,
    log: Callable[[str], None] = _stderr,
    client: httpx.Client | None = None,
) -> threading.Thread:
    """Ensure/refresh the index without delaying server start-up; hot-swap the store when a new file lands."""

    def run() -> None:
        result = ensure_index(settings, client=client)
        if result.path is not None and (result.changed or dt.store is None):
            dt.set_store(IndexStore.open(result.path))
        if result.changed or result.path is None:
            log(f"deptime: {result.message}")

    thread = threading.Thread(target=run, name="deptime-index-refresh", daemon=True)
    thread.start()
    return thread
