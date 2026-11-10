"""Ingest one package: select releases -> fetch artifacts -> build/cached snapshots -> rebuild its index rows.

Snapshots are cached as immutable `snapshots/<package>/<version>.json.gz` files so the index can be rebuilt
(new diff logic, schema changes) without re-downloading or re-analysing anything.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cache, partial
from importlib import resources
from pathlib import Path

import httpx
import packaging
from packaging.version import Version

from deptime.config import PackageSpec, VersionPolicy
from deptime.index.store import IndexStore, RebuildStats
from deptime.ingest import pypi
from deptime.ingest.snapshot import (
    SNAPSHOT_FORMAT,
    ApiSnapshot,
    LazySnapshot,
    build_snapshot,
    external_star_imports,
)


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
    # Its rows were kept: nothing they are assembled from changed since they were written (DT-DATA-41).
    unchanged: bool = False
    # Upload time of each selected release (ISO 8601), for the freshness check (DT-DATA-04).
    uploaded: dict[str, str | None] = field(default_factory=dict)


COVERAGE_BAR = 0.9  # DT-DATA-01: share of each package's selected releases the index must hold
FRESHNESS_HOURS = 24  # DT-DATA-04: a selected release is indexed within this long of its upload
_CACHED_ONLY_MISS = "no cached snapshot (cached-only run)"


def _cell(text: str, limit: int = 200) -> str:
    """Untrusted text (exception messages) as one Markdown table cell."""
    text = " ".join(text.split()).replace("|", "\\|").replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hours_before(now: datetime, uploaded: str | None) -> float | None:
    try:
        return (now - datetime.fromisoformat(uploaded)).total_seconds() / 3600 if uploaded else None
    except (TypeError, ValueError):  # unreadable, or without a timezone
        return None


def _version(v: str) -> Version | None:
    try:
        return Version(v)
    except packaging.version.InvalidVersion:
        return None


def freshness_summary(reports: list[IngestReport], now: datetime) -> str:
    """One paragraph on the freshness bound (DT-DATA-04): the selected releases newer than a package's newest
    indexed one and uploaded more than FRESHNESS_HOURS before `now`, and the newest release that is indexed. An
    older release that can't be analysed is the coverage table's business."""
    late, newest = [], None
    for r in reports:
        done = {*r.cached, *r.built, *r.carried}
        indexed = [x for v in done if (x := _version(v)) is not None]
        top = max(indexed) if indexed else None
        for v in r.selected:
            age = _hours_before(now, r.uploaded.get(v))
            if age is None:
                continue
            if v not in done and age > FRESHNESS_HOURS and (top is None or (_version(v) or top) > top):
                late.append((age, r.package, v))
            elif v in done and (newest is None or age < newest[0]):
                newest = (age, r.package, v)
    if late:
        listed = ", ".join(
            f"{p} {_cell(v, 40)} ({age:.0f} h)" for age, p, v in sorted(late, reverse=True)[:10]
        )
        text = (
            f"**{len(late)} release{'s' if len(late) > 1 else ''} newer than {'their' if len(late) > 1 else 'its'} "
            f"package's newest indexed one {'were' if len(late) > 1 else 'was'} uploaded more than {FRESHNESS_HOURS} h "
            "before this run**: "
            f"{listed}{' and more' if len(late) > 10 else ''}."
        )
    else:
        text = (
            f"Every selected release uploaded more than {FRESHNESS_HOURS} h before this run is indexed, up to each "
            "package's newest."
        )
    if newest is not None:
        text += (
            f" The newest indexed release is {newest[1]} {newest[2]}, uploaded {newest[0]:.0f} h before it."
        )
    return f"Freshness (bound: {FRESHNESS_HOURS} h from upload): {text}"


def coverage_summary(reports: list[IngestReport], max_listed: int = 5, now: datetime | None = None) -> str:
    """A Markdown summary of a run for CI's workflow page: totals and freshness, then a row per package with a
    failure."""
    counted = [r for r in reports if r.selected]
    selected = sum(len(r.selected) for r in counted)
    indexed = {r.package: len(set(r.selected) & {*r.cached, *r.built, *r.carried}) for r in counted}
    total = sum(indexed.values())
    share = f" ({100 * total / selected:.2f}%)" if selected else ""
    below = [r.package for r in counted if indexed[r.package] < COVERAGE_BAR * len(r.selected)]
    verdict = f"Below it: {', '.join(below)}." if below else "Every counted package meets it."
    lines = [
        "### Index coverage",
        "",
        f"{len(reports)} packages: {total} of {selected} selected releases indexed{share}. "
        f"Bar: {COVERAGE_BAR:.0%} of each package's selected releases. {verdict}",
        "",
        freshness_summary(reports, now or datetime.now(UTC)),
    ]
    if unchanged := sum(r.unchanged for r in reports):
        lines += [
            "",
            f"{unchanged} of {len(reports)} packages were unchanged since the index was last assembled "
            "and kept their rows.",
        ]
    rows = []
    for r in reports:
        if not r.failed:
            continue
        if r.selected:
            n = indexed[r.package]
            cell = f"{n} of {len(r.selected)} ({100 * n / len(r.selected):.1f}%)"
            if r.package in below:
                cell += f" **below {COVERAGE_BAR:.0%}**"
        else:
            cell = "not counted"
        failed = sorted(r.failed.items())
        listed = [f"`{_cell(v, 40)}`: {_cell(why)}" for v, why in failed[:max_listed]]
        if len(failed) > max_listed:
            listed.append(f"and {len(failed) - max_listed} more")
        rows.append(f"| {r.package} | {cell} | {'<br>'.join(listed)} |")
    if rows:
        lines += ["", "| Package | Indexed | Not indexed in this run |", "|---|---|---|", *rows]
        if any(why == _CACHED_ONLY_MISS for r in reports for why in r.failed.values()):
            lines += [
                "",
                "A release without a cached snapshot failed to build in its package's snapshots job; "
                "that job's log has the cause.",
            ]
    return "\n".join(lines) + "\n"


def snapshot_path(cache_dir: Path, package: str, version: str) -> Path:
    return cache_dir / "snapshots" / package / f"{version}.json.gz"


def _cached_snapshot(cache_dir: Path, spec: PackageSpec, version: str) -> ApiSnapshot | None:
    path = snapshot_path(cache_dir, spec.name, version)
    if not path.exists():
        return None
    snap = ApiSnapshot.load(path)
    # Another extractor's, or built for other import names or namespace skips (a coverage.toml edit): rebuild it.
    current = (
        snap.format == SNAPSHOT_FORMAT
        and snap.import_names == list(spec.import_names)
        and snap.namespace_skip == list(spec.namespace_skip)
        and snap.own_members == spec.own_members
        and snap.served == [rule.as_dict() for rule in spec.served]
        and snap.internal_names == list(spec.internal_names)
    )
    return snap if current else None


# The code that turns a package's snapshots into its index rows (DT-DATA-41). Editing any of these files changes
# every package's fingerprint, so the next run rebuilds them all rather than keep rows older code wrote.
ASSEMBLY_SOURCES = (
    "curated.toml",
    "index/schema.sql",
    "index/store.py",
    "ingest/curated.py",
    "ingest/diff.py",
    "ingest/pipeline.py",
    "ingest/replacements.py",
    "ingest/snapshot.py",
)


@cache
def assembly_code_digest() -> str:
    digest = hashlib.sha256(f"python {sys.version}; packaging {packaging.__version__}".encode())
    root = resources.files("deptime")
    for source in ASSEMBLY_SOURCES:
        digest.update(f"\0{source}\0".encode())
        digest.update(root.joinpath(source).read_bytes())
    return digest.hexdigest()


def _released_at(rel: pypi.Release) -> str | None:
    return rel.uploaded_at.isoformat() if rel.uploaded_at else None


def assembly_fingerprint(
    spec: PackageSpec, selected: list[pypi.Release], cache_dir: Path, used: set[str]
) -> str:
    """A hash of everything `spec`'s rows are assembled from (DT-DATA-41): the assembly code, the package's
    spec, and for each selected release its upload date, its artifact's name and, if in `used`, the bytes
    of its snapshot file. The files are hashed, not loaded: deciding to keep litellm's rows takes ~1 s."""
    releases = []
    for rel in selected:
        version, file = str(rel.version), pypi.pick_file(rel)
        snap = None
        if version in used:
            with snapshot_path(cache_dir, spec.name, version).open("rb") as fh:
                snap = hashlib.file_digest(fh, "sha256").hexdigest()
        releases.append([version, snap, _released_at(rel), file.filename if file else None])
    spec_inputs = [
        spec.name,
        list(spec.import_names),
        list(spec.namespace_skip),
        spec.tier,
        spec.own_members,
        [rule.as_dict() for rule in spec.served],
        list(spec.internal_names),
    ]
    payload = {"code": assembly_code_digest(), "spec": spec_inputs, "releases": releases}
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


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
    0.12.0 (`typer-cli`, itself empty, then `typer-slim`) (DT-DATA-17); else from one it pins at another
    version, which is what installing the release imports: dspy 0.1.5 ships only `dspy_dummy` and requires
    `dspy-ai==2.4.5`, whose `dspy` it is analysed from (DT-DATA-63). A distribution the analysed code pins at its
    own version that has a module the code star-imports is analysed with it (DT-DATA-56: mcp 2.x's `mcp.types` is
    `from mcp_types import *`, and mcp 2.3.0 requires `mcp-types==2.3.0`).
    """
    with ExitStack() as cleanup:
        root = _fetch(spec.name, version, file, client, cache_dir, cleanup)
        code_version = version  # the version of the distribution whose code is analysed
        pins = pypi.same_version_pins(root, version)
        if not pypi.has_code(root, spec.import_names):
            others = [(d, v) for d, v in pypi.exact_pins(root) if Version(v) != Version(version)]
            for dist, pinned_version in [(d, version) for d in pins] + others:
                pinned_file = _pinned_file(dist, pinned_version, releases_of)
                if pinned_file is None:
                    continue
                pinned_root = _fetch(dist, pinned_version, pinned_file, client, cache_dir, cleanup)
                if pypi.has_code(pinned_root, spec.import_names):
                    root, file, code_version = pinned_root, pinned_file, pinned_version
                    pins = pypi.same_version_pins(root, code_version)
                    break
        siblings: list[tuple[Path, list[str]]] = []
        sibling_files: list[str] = []
        wanted = external_star_imports(root, list(spec.import_names))
        for dist in pins if wanted else ():
            pinned_file = _pinned_file(dist, code_version, releases_of)
            if pinned_file is None or pinned_file.filename == file.filename:
                continue
            pinned_root = _fetch(dist, code_version, pinned_file, client, cache_dir, cleanup)
            names = sorted(m for m in wanted if pypi.has_code(pinned_root, [m]))
            if names:
                siblings.append((pinned_root, names))
                sibling_files.append(pinned_file.filename)
                wanted -= set(names)
            if not wanted:
                break
        snap = build_snapshot(
            spec.name,
            version,
            list(spec.import_names),
            root,
            list(spec.namespace_skip),
            spec.own_members,
            siblings,
            spec.served,
            spec.internal_names,
        )
        # The analysed code's own requirements: a wrapper release's are those of the distribution it pins.
        snap.requires = pypi.requires_dist(root, file.filename)
        snap.project_urls = pypi.project_urls(root, file.filename)
    snap.source_file = file.filename
    snap.sibling_files = sibling_files
    return snap


def _pinned_file(
    dist: str, version: str, releases_of: Callable[[str], list[pypi.Release]]
) -> pypi.ReleaseFile | None:
    """The artifact of `dist`'s release `version`, which a release pins; None when PyPI has none, or no such
    project (another error is raised, so the release is retried next run)."""
    try:
        releases = releases_of(dist)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    pinned = next((r for r in releases if r.version == Version(version)), None)
    return pypi.pick_file(pinned) if pinned else None


def _previous_release_info(
    indexed: dict[str, sqlite3.Row], versions: list[str]
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Upload dates and artifact names already recorded for `versions`, so a rebuild doesn't lose them."""
    rows = [indexed[v] for v in versions if v in indexed]
    return (
        {r["version"]: r["released_at"] for r in rows},
        {r["version"]: r["source_file"] for r in rows if r["source_file"]},
    )


def _on_disk(cache_dir: Path, spec: PackageSpec, versions: list[str]) -> set[str]:
    return {v for v in versions if snapshot_path(cache_dir, spec.name, v).exists()}


def _keep_rows(
    spec: PackageSpec,
    selected: list[pypi.Release],
    store: IndexStore,
    cache_dir: Path,
    indexed: dict[str, sqlite3.Row],
    cached_only: bool,
) -> bool:
    """True when `spec`'s rows were assembled from exactly tonight's inputs (DT-DATA-41).

    Every selected release needs its snapshot on disk, except, in a cached-only run, one that isn't indexed
    either (it failed before too); an indexed one would be carried over from the index, which isn't
    fingerprinted. Snapshots aren't loaded: a cached file of another format changes the fingerprint.
    """
    stored = store.fingerprint(spec.name)
    if stored is None:
        return False
    versions = [str(r.version) for r in selected]
    on_disk = _on_disk(cache_dir, spec, versions)
    missing = [v for v in versions if v not in on_disk]
    if missing and (not cached_only or any(v in indexed for v in missing)):
        return False
    return stored == assembly_fingerprint(spec, selected, cache_dir, on_disk)


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
    assemble: bool = True,
    log: Callable[[str], None] = print,
) -> IngestReport:
    """Index `spec`'s selected releases, building any snapshot that isn't cached yet.

    `cached_only` never downloads or analyses a release: one without a cached snapshot is reported as failed,
    so an already-indexed copy is kept. CI's publish job uses it to assemble the index from the snapshots the
    per-package jobs built. Those jobs pass `assemble=False`: they build snapshots and leave the index alone.
    Rows assembled from the same inputs as tonight's are kept as they are (DT-DATA-41).
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
            if assemble and (snaps or carried):
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
        report.uploaded = {str(r.version): _released_at(r) for r in selected}
        log(f"[{spec.name}] {len(selected)} releases selected")
        if assemble and not versions and _keep_rows(spec, selected, store, cache_dir, indexed, cached_only):
            on_disk = _on_disk(cache_dir, spec, report.selected)
            report.cached = [v for v in report.selected if v in on_disk]
            report.failed = {v: _CACHED_ONLY_MISS for v in report.selected if v not in on_disk}
            report.unchanged = True
            store.stamp_built_at()
            log(f"[{spec.name}] unchanged since its rows were assembled; kept them")
            return report

        # Snapshots stay on disk until the rebuild streams through them (see LazySnapshot).
        snapshots: list[LazySnapshot] = []
        released_at: dict[str, str | None] = {}
        source_files: dict[str, str] = {}
        releases_of = cache(partial(pypi.fetch_releases, client=client))  # distributions wrappers pin
        for rel in selected:
            version = str(rel.version)
            released_at[version] = _released_at(rel)
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
                report.failed[version] = _CACHED_ONLY_MISS
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

        if not assemble:
            return report
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
        # Rows that take anything from the index itself, or only some releases, aren't fingerprinted: the
        # next run rebuilds them.
        fingerprint = None if versions or carried else assembly_fingerprint(spec, selected, cache_dir, done)
        report.stats = store.rebuild_package(
            spec,
            snapshots + carried,
            {**prev_released_at, **released_at},
            {**prev_source_files, **source_files},
            fingerprint,
        )
        peak = _peak_memory_mb()
        log(f"[{spec.name}] index rebuilt: {report.stats}" + (f"; peak memory {peak:.0f} MB" if peak else ""))
        return report
    finally:
        if own_client:
            client.close()
