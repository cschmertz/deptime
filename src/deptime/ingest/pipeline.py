"""Ingest one package: select releases -> fetch artifacts -> build/cached snapshots -> rebuild its index rows.

Snapshots are cached as immutable `snapshots/<package>/<version>.json.gz` files so the index can be rebuilt
(new diff logic, schema changes) without re-downloading or re-analysing anything.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date
from functools import cache, partial
from pathlib import Path

import httpx
from packaging.version import Version

from deptime.config import PackageSpec, VersionPolicy
from deptime.index.store import IndexStore, RebuildStats
from deptime.ingest import pypi
from deptime.ingest.snapshot import SNAPSHOT_FORMAT, ApiSnapshot, LazySnapshot, build_snapshot


@dataclass
class IngestReport:
    package: str
    selected: list[str] = field(default_factory=list)
    built: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    # Already indexed and kept, although this run didn't produce it (failed retry, or not in --versions).
    carried: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    stats: RebuildStats | None = None


def snapshot_path(cache_dir: Path, package: str, version: str) -> Path:
    return cache_dir / "snapshots" / package / f"{version}.json.gz"


def _cached_snapshot(cache_dir: Path, spec: PackageSpec, version: str) -> ApiSnapshot | None:
    path = snapshot_path(cache_dir, spec.name, version)
    if not path.exists():
        return None
    snap = ApiSnapshot.load(path)
    # Another extractor's, or built for other import names (a coverage.toml edit): rebuild it.
    current = snap.format == SNAPSHOT_FORMAT and snap.import_names == list(spec.import_names)
    return snap if current else None


def _lazy_cached(cache_dir: Path, spec: PackageSpec, version: str) -> LazySnapshot:
    return LazySnapshot(version, partial(_cached_snapshot, cache_dir, spec, version))


def _carried_snapshot(
    spec: PackageSpec, store: IndexStore, cache_dir: Path, version: str
) -> ApiSnapshot | None:
    return _cached_snapshot(cache_dir, spec, version) or store.snapshot_at(spec, version)


def _carry_forward(
    spec: PackageSpec, store: IndexStore, cache_dir: Path, versions: list[str]
) -> list[LazySnapshot]:
    """Already-indexed releases this run didn't produce: their cached snapshot, else rebuilt from the index.

    They load during `rebuild_package`, which reads them before it rewrites the package's rows.
    """
    return [LazySnapshot(v, partial(_carried_snapshot, spec, store, cache_dir, v)) for v in versions]


def _peak_memory_mb() -> float | None:
    """Peak resident memory of this process so far, for the ingest log (ru_maxrss: KiB on Linux, bytes on macOS)."""
    try:
        import resource
    except ImportError:  # Windows
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def _discard(artifact: Path, extract_dir: Path) -> None:
    """Only the snapshot is kept: wheels and unpacked sources for ~3,500 releases would fill a CI runner's disk."""
    artifact.unlink(missing_ok=True)
    artifact.with_name(artifact.name + ".part").unlink(missing_ok=True)
    shutil.rmtree(extract_dir, ignore_errors=True)


def _fetch(
    dist: str, version: str, file: pypi.ReleaseFile, client: httpx.Client, cache_dir: Path, cleanup: ExitStack
) -> Path:
    """Download and unpack one artifact; `cleanup` deletes both when the snapshot is built."""
    artifact_path = cache_dir / "artifacts" / dist / file.filename
    extract_dir = cache_dir / "extracted" / dist / version
    cleanup.callback(_discard, artifact_path, extract_dir)
    return pypi.extract(pypi.download(file, artifact_path, client), extract_dir)


def _build(
    spec: PackageSpec,
    version: str,
    file: pypi.ReleaseFile,
    client: httpx.Client,
    cache_dir: Path,
    releases_of: Callable[[str], list[pypi.Release]],
) -> ApiSnapshot:
    """Snapshot one release from its artifact.

    A release with no code for its import names is analysed from the first distribution it pins at the same
    version that has it: fastmcp 3.3+ (`fastmcp-slim`), pinecone 2.2.2-5.0.1 (`pinecone-client`), typer
    0.12.0 (`typer-cli`, itself empty, then `typer-slim`) (DT-DATA-17).
    """
    with ExitStack() as cleanup:
        root = _fetch(spec.name, version, file, client, cache_dir, cleanup)
        if not pypi.has_code(root, spec.import_names):
            for dist in pypi.same_version_pins(root, version):
                pinned = next((r for r in releases_of(dist) if r.version == Version(version)), None)
                pinned_file = pypi.pick_file(pinned) if pinned else None
                if pinned_file is None:
                    continue
                pinned_root = _fetch(dist, version, pinned_file, client, cache_dir, cleanup)
                if pypi.has_code(pinned_root, spec.import_names):
                    root, file = pinned_root, pinned_file
                    break
        snap = build_snapshot(spec.name, version, list(spec.import_names), root)
    snap.source_file = file.filename
    return snap


def _previous_release_info(
    indexed: dict[str, sqlite3.Row], versions: list[str]
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Upload dates and artifact names already recorded for `versions`, so a rebuild doesn't lose them."""
    rows = [indexed[v] for v in versions if v in indexed]
    return (
        {r["version"]: r["released_at"] for r in rows},
        {r["version"]: r["source_file"] for r in rows if r["source_file"]},
    )


def ingest_package(
    spec: PackageSpec,
    policy: VersionPolicy,
    store: IndexStore,
    cache_dir: Path,
    *,
    versions: list[str] | None = None,
    client: httpx.Client | None = None,
    today: date | None = None,
    cached_only: bool = False,
    log: Callable[[str], None] = print,
) -> IngestReport:
    """Index `spec`'s selected releases, building any snapshot that isn't cached yet.

    `cached_only` never downloads or analyses a release: one without a cached snapshot is reported as failed,
    so an already-indexed copy is kept. CI's publish job uses it to assemble the index from the snapshots the
    per-package jobs built.
    """
    report = IngestReport(spec.name)
    # Releases already in the index are only dropped on purpose (yanked, or no longer selected by the policy),
    # never because this run failed to download or analyse them.
    indexed = {r["version"]: r for r in store.releases(spec.name)}
    own_client = client is None
    client = client or pypi.http_client()
    try:
        try:
            releases = pypi.fetch_releases(spec.name, client)
        except httpx.HTTPError as exc:
            # PyPI unreachable: rebuild from cached snapshots so a published index never silently loses a package.
            cached = sorted((cache_dir / "snapshots" / spec.name).glob("*.json.gz"))
            report.failed["*"] = (
                f"PyPI unavailable ({type(exc).__name__}); used {len(cached)} cached snapshots"
            )
            log(f"[{spec.name}] {report.failed['*']}")
            snaps = [
                LazySnapshot(p.name.removesuffix(".json.gz"), partial(ApiSnapshot.load, p)) for p in cached
            ]
            report.cached = [s.version for s in snaps]
            carried = _carry_forward(spec, store, cache_dir, [v for v in indexed if v not in report.cached])
            report.carried = [s.version for s in carried]
            if snaps or carried:
                report.stats = store.rebuild_package(
                    spec, snaps + carried, *_previous_release_info(indexed, list(indexed))
                )
            return report
        if versions:
            wanted = set(versions)
            selected = [r for r in releases if str(r.version) in wanted]
            missing = wanted - {str(r.version) for r in selected}
            for v in sorted(missing):
                report.failed[v] = "version not found on PyPI"
        else:
            selected = pypi.select_versions(releases, policy, today)
        report.selected = [str(r.version) for r in selected]
        log(f"[{spec.name}] {len(selected)} releases selected")

        # Snapshots stay on disk until the rebuild streams through them (see LazySnapshot).
        snapshots: list[LazySnapshot] = []
        released_at: dict[str, str | None] = {}
        source_files: dict[str, str] = {}
        releases_of = cache(partial(pypi.fetch_releases, client=client))  # distributions wrappers pin
        for rel in selected:
            version = str(rel.version)
            released_at[version] = rel.uploaded_at.isoformat() if rel.uploaded_at else None
            file = pypi.pick_file(rel)
            # Loaded here only for its format and source file; the rebuild loads it again when it gets there.
            cached = _cached_snapshot(cache_dir, spec, version)
            if cached is not None:
                source = cached.source_file or (file.filename if file else None)
                del cached  # not held while the next release loads (DT-DATA-03)
                snapshots.append(_lazy_cached(cache_dir, spec, version))
                if source:
                    source_files[version] = source
                report.cached.append(version)
                continue
            if cached_only:
                report.failed[version] = "no cached snapshot (cached-only run)"
                continue
            if file is None:
                report.failed[version] = "no wheel or sdist"
                continue
            try:
                snap = _build(spec, version, file, client, cache_dir, releases_of)
            except Exception as exc:  # keep going; the failure is reported and retried next run
                report.failed[version] = f"{type(exc).__name__}: {exc}"
                log(f"[{spec.name}] {version}: FAILED {report.failed[version]}")
                continue
            if not snap.symbols:
                report.failed[version] = "no public symbols extracted: " + "; ".join(snap.errors[:3])
                continue
            snap.save(snapshot_path(cache_dir, spec.name, version))
            snapshots.append(_lazy_cached(cache_dir, spec, version))
            source_files[version] = snap.source_file
            report.built.append(version)
            via = f" from {snap.source_file}" if snap.source_file != file.filename else ""
            log(
                f"[{spec.name}] {version}: {len(snap.symbols)} public symbols{via} ({len(snap.errors)} warnings)"
            )

        if not snapshots:
            # Never replace a working index with nothing (PyPI outage, network failure, bad artifact run).
            log(f"[{spec.name}] no snapshots available; existing index rows left untouched")
            return report
        done = {s.version for s in snapshots}
        if versions:  # explicit versions add to what is indexed
            keep = [v for v in indexed if v not in done]
        else:  # a failed retry keeps the release we already have
            keep = [v for v in report.failed if v in indexed]
            dropped = [v for v in indexed if v not in done and v not in keep]
            if dropped:
                log(
                    f"[{spec.name}] dropping {len(dropped)} releases no longer selected: {', '.join(dropped)}"
                )
        carried = _carry_forward(spec, store, cache_dir, keep)
        report.carried = [s.version for s in carried]
        if carried:
            log(f"[{spec.name}] kept {len(carried)} already-indexed releases: {', '.join(report.carried)}")
        prev_released_at, prev_source_files = _previous_release_info(indexed, report.carried)
        report.stats = store.rebuild_package(
            spec,
            snapshots + carried,
            {**prev_released_at, **released_at},
            {**prev_source_files, **source_files},
        )
        peak = _peak_memory_mb()
        log(f"[{spec.name}] index rebuilt: {report.stats}" + (f"; peak memory {peak:.0f} MB" if peak else ""))
        return report
    finally:
        if own_client:
            client.close()
