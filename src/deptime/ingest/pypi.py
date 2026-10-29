"""PyPI access via the JSON Simple API (PEP 691/700), version selection, and safe artifact extraction."""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version

from deptime import __version__
from deptime.config import VersionPolicy

SIMPLE_URL = "https://pypi.org/simple/{name}/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
USER_AGENT = f"deptime/{__version__} (+https://github.com/; API history indexer)"
MAX_ARTIFACT_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class ReleaseFile:
    filename: str
    url: str
    sha256: str | None
    upload_time: datetime | None
    yanked: bool
    packagetype: str  # "wheel" | "sdist"
    pure_python: bool = False


@dataclass(frozen=True)
class Release:
    version: Version
    files: tuple[ReleaseFile, ...]

    @property
    def uploaded_at(self) -> datetime | None:
        times = [f.upload_time for f in self.files if f.upload_time]
        return min(times) if times else None

    @property
    def yanked(self) -> bool:
        return bool(self.files) and all(f.yanked for f in self.files)


def http_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_simple_index(payload: dict) -> list[Release]:
    """Group the files of a PEP 691 JSON project page into releases."""
    project = canonicalize_name(payload.get("name", ""))
    by_version: dict[Version, list[ReleaseFile]] = defaultdict(list)
    for f in payload.get("files", []):
        filename: str = f["filename"]
        try:
            if filename.endswith(".whl"):
                name, version, _build, tags = parse_wheel_filename(filename)
                pure = any(t.abi == "none" and t.platform == "any" for t in tags)
                ptype = "wheel"
            elif filename.endswith((".tar.gz", ".zip")):
                name, version = parse_sdist_filename(filename)
                pure, ptype = False, "sdist"
            else:
                continue
        except (InvalidWheelFilename, InvalidSdistFilename):
            continue
        if project and canonicalize_name(name) != project:
            continue
        by_version[version].append(
            ReleaseFile(
                filename=filename,
                url=f["url"],
                sha256=(f.get("hashes") or {}).get("sha256"),
                upload_time=_parse_time(f.get("upload-time")),
                yanked=bool(f.get("yanked")),
                packagetype=ptype,
                pure_python=pure,
            )
        )
    return sorted((Release(v, tuple(fs)) for v, fs in by_version.items()), key=lambda r: r.version)


def fetch_releases(name: str, client: httpx.Client) -> list[Release]:
    resp = client.get(SIMPLE_URL.format(name=canonicalize_name(name)), headers={"Accept": SIMPLE_ACCEPT})
    resp.raise_for_status()
    return parse_simple_index(resp.json())


def _months_before(day: date, months: int) -> date:
    idx = day.year * 12 + (day.month - 1) - months
    year, month = divmod(idx, 12)
    return date(year, month + 1, min(day.day, 28))


def select_versions(
    releases: list[Release], policy: VersionPolicy, today: date | None = None
) -> list[Release]:
    """Apply the coverage policy (see deptime/coverage.toml) and return releases in version order."""
    today = today or datetime.now(UTC).date()
    candidates = [
        r
        for r in releases
        if (policy.include_prereleases or not (r.version.is_prerelease or r.version.is_devrelease))
        and (policy.include_yanked or not r.yanked)
    ]
    chosen: dict[Version, Release] = {}

    series: dict[tuple[int, int], list[Release]] = defaultdict(list)
    for r in candidates:
        series[(r.version.major, r.version.minor)].append(r)
    for rs in series.values():
        rs.sort(key=lambda r: r.version)
        if any(r.uploaded_at and r.uploaded_at.date() >= policy.since for r in rs):
            for r in rs if policy.whole_series else (rs[0], rs[-1]):
                chosen[r.version] = r

    recent_cutoff = _months_before(today, policy.recent_months)
    for r in candidates:
        if r.uploaded_at and r.uploaded_at.date() >= recent_cutoff:
            chosen[r.version] = r
    return [chosen[v] for v in sorted(chosen)]


def pick_file(release: Release) -> ReleaseFile | None:
    """Prefer a pure-Python wheel, then any wheel (has .py/.pyi), then the sdist."""
    files = [f for f in release.files if not f.yanked] or list(release.files)
    pure = [f for f in files if f.packagetype == "wheel" and f.pure_python]
    if pure:
        return pure[0]
    wheels = sorted(
        (f for f in files if f.packagetype == "wheel"),
        key=lambda f: ("manylinux" not in f.filename, f.filename),
    )
    if wheels:
        return wheels[0]
    sdists = [f for f in files if f.packagetype == "sdist"]
    return sdists[0] if sdists else None


def download(file: ReleaseFile, dest: Path, client: httpx.Client) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (not file.sha256 or hashlib.sha256(dest.read_bytes()).hexdigest() == file.sha256):
        return dest
    digest, size = hashlib.sha256(), 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", file.url) as resp, tmp.open("wb") as out:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise ValueError(f"{file.filename} exceeds {MAX_ARTIFACT_BYTES} bytes")
            digest.update(chunk)
            out.write(chunk)
    if file.sha256 and digest.hexdigest() != file.sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"sha256 mismatch for {file.filename}")
    tmp.replace(dest)
    return dest


def extract(artifact: Path, dest: Path) -> Path:
    """Extract a wheel or sdist into `dest` and return the directory to use as a griffe search path."""
    if dest.exists() and any(dest.iterdir()):
        return _search_root(dest, artifact)
    dest.mkdir(parents=True, exist_ok=True)
    if artifact.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(artifact) as zf:
            root = dest.resolve()
            for member in zf.infolist():
                target = (dest / member.filename).resolve()
                if not target.is_relative_to(root):  # zip-slip guard
                    raise ValueError(f"unsafe path in {artifact.name}: {member.filename}")
            zf.extractall(dest)
    else:
        with tarfile.open(artifact) as tf:
            tf.extractall(dest, filter="data")  # PEP 706 filter rejects traversal/links outside dest
    return _search_root(dest, artifact)


def _search_root(dest: Path, artifact: Path) -> Path:
    if artifact.suffix == ".whl":
        return dest
    # sdist: <name>-<version>/ possibly with a src/ layout
    tops = [p for p in dest.iterdir() if p.is_dir()]
    top = tops[0] if len(tops) == 1 else dest
    return top / "src" if (top / "src").is_dir() else top
