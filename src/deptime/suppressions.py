"""Changes a project says don't matter to it (DT-CORE-30): suppressions by package, path and change kind.

A project lists them in `deptime.toml`, or under `[tool.deptime]` in `pyproject.toml`, in the directory the local
server runs in (or `DEPTIME_PROJECT_DIR`):

    [[suppress]]                  # in pyproject.toml: [[tool.deptime.suppress]]
    package = "numpy"
    path = "numpy.float_"         # optional: the path and everything under it
    kind = "symbol_removed"       # optional: a finding's issue or an event's kind
    reason = "we pin numpy<2"     # optional, for the people reading the file

They apply only where DepTime runs locally, never in hosted mode, and every answer counts what they left out.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name


@dataclass(frozen=True)
class Suppression:
    package: str  # canonical distribution name
    path: str | None = None
    kind: str | None = None
    reason: str | None = None

    def matches(self, package: str, path: str, kind: str) -> bool:
        if canonicalize_name(package) != self.package:
            return False
        if self.kind is not None and kind != self.kind:
            return False
        if self.path is None:
            return True
        bare = path.partition("(")[0]  # a parameter-level finding: `func.path(param)`
        return path == self.path or bare == self.path or bare.startswith(self.path + ".")


def _entries(project: Path) -> tuple[list[object], Path | None]:
    own = project / "deptime.toml"
    if own.is_file():
        return tomllib.loads(own.read_text(encoding="utf-8")).get("suppress", []), own
    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return data.get("tool", {}).get("deptime", {}).get("suppress", []), pyproject
    return [], None


def load(project: Path | None = None) -> tuple[Suppression, ...]:
    """The project's suppressions; an unreadable file or entry is reported on stderr (stdout belongs to the MCP
    protocol) and skipped, never guessed at."""
    root = project or Path(os.environ.get("DEPTIME_PROJECT_DIR", "").strip() or Path.cwd())
    try:
        entries, source = _entries(root)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"deptime: suppressions not read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return ()
    out = []
    for i, entry in enumerate(entries if isinstance(entries, list) else []):
        fields = entry if isinstance(entry, dict) else {}
        package = fields.get("package")
        values = [fields.get(k) for k in ("path", "kind", "reason")]
        if not isinstance(package, str) or not all(v is None or isinstance(v, str) for v in values):
            print(
                f"deptime: suppression {i + 1} in {source} skipped: it needs a package name", file=sys.stderr
            )
            continue
        out.append(Suppression(canonicalize_name(package), *values))
    return tuple(out)


def note(count: int, what: str) -> str | None:
    if not count:
        return None
    return f"{count} {what}{'' if count == 1 else 's'} left out by this project's suppressions (deptime.toml or pyproject.toml)."
