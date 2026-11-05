"""Which file holds the index (DT-OSS-08).

A downloaded index lives in a file named after the published build, `index-<sha256[:16]>.sqlite`, and the local
metadata file (`index.sqlite.meta.json`) names it under `file`. A new download is a new file, and switching to
it rewrites only the small metadata file, so nothing ever replaces a file a server has open: Windows refuses that
while any process has it open. Each process opens the file named when it opens one; older files are deleted once
nothing holds them.

Without `file` (an index built locally with `deptime ingest`, or one downloaded by a client before DT-OSS-08) the
index is the configured path itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from deptime.index.store import IndexStore


def meta_path(index_path: Path) -> Path:
    return index_path.with_name(index_path.name + ".meta.json")


def read_local_meta(index_path: Path) -> dict | None:
    try:
        meta = json.loads(meta_path(index_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def versioned_name(index_path: Path, sha256: str) -> str:
    return f"{index_path.stem}-{sha256[:16]}{index_path.suffix}"


def _versioned_re(index_path: Path) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(index_path.stem)}-[0-9a-f]{{16}}{re.escape(index_path.suffix)}")


def active_index(index_path: Path) -> Path:
    """The file to open: the downloaded one the metadata names, if it is there, else `index_path`."""
    meta = read_local_meta(index_path)
    name = meta.get("file") if meta else None
    # Only a file of the versioned shape beside the metadata: the name is read from disk, not trusted as a path.
    if isinstance(name, str) and _versioned_re(index_path).fullmatch(name):
        candidate = index_path.with_name(name)
        if candidate.exists():
            return candidate
    return index_path


def open_index(index_path: Path) -> IndexStore | None:
    path = active_index(index_path)
    return IndexStore.open(path) if path.exists() else None


def remove_stale(index_path: Path) -> list[Path]:
    """Delete the downloaded index files the metadata no longer names, and `index_path` itself once a versioned
    file replaced it. A file still open somewhere (Windows) stays until a later call. Returns what is left."""
    active = active_index(index_path)
    if active == index_path:
        return []
    pattern, left = _versioned_re(index_path), []
    for path in [index_path, *index_path.parent.glob(f"{index_path.stem}-*{index_path.suffix}")]:
        if path == active or not path.exists() or (path != index_path and not pattern.fullmatch(path.name)):
            continue
        try:
            path.unlink()
        except OSError:  # open in a running server (Windows)
            left.append(path)
    return left
