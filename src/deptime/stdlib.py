"""Which standard-library modules each Python version has, from typeshed's stdlib/VERSIONS (DT-DATA-06).

`No module named 'distutils'` on Python 3.12 is a version change like a library's, but the index only holds
distributions from PyPI. The table ships with deptime (`stdlib_versions.txt`) and is read at query time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

# The oldest Python typeshed supports; its ranges for older ones "are generally accurate" but not guaranteed. A module
# added by then is taken to be there when the Python version is unknown.
OLDEST_MINOR = 9
LINE_RE = re.compile(r"^(?P<module>[A-Za-z_][\w.]*):\s*3\.(?P<first>\d+)-(?:3\.(?P<last>\d+))?\s*(?:#.*)?$")
VERSION_RE = re.compile(r"^\s*v?3\.(?P<minor>\d{1,2})(?!\d)")


@dataclass(frozen=True)
class StdlibModule:
    module: str  # the listed module: the one asked about, or the parent whose lifetime it shares
    first: int  # minor version that added it (3.<first>)
    last: int | None  # minor version that last has it; None while it is still available

    def has(self, minor: int) -> bool:
        return self.first <= minor and (self.last is None or minor <= self.last)


@cache
def _table() -> dict[str, StdlibModule]:
    text = resources.files("deptime").joinpath("stdlib_versions.txt").read_text(encoding="utf-8")
    out: dict[str, StdlibModule] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if m is None:
            raise ValueError(f"stdlib_versions.txt: unreadable line {line!r}")
        last = m.group("last")
        out[m.group("module")] = StdlibModule(
            m.group("module"), int(m.group("first")), int(last) if last else None
        )
    return out


def lookup(module: str) -> StdlibModule | None:
    """The entry for `module`, or for its longest listed parent (a submodule not listed shares its lifetime)."""
    table = _table()
    parts = module.split(".")
    for n in range(len(parts), 0, -1):
        entry = table.get(".".join(parts[:n]))
        if entry is not None:
            return entry
    return None


def minor_of(version: str | None) -> int | None:
    """3.12 -> 12 for "3.12", "3.12.1", "3.13.0rc2" or "3.13t"; None for anything else."""
    m = VERSION_RE.match(version or "")
    return int(m.group("minor")) if m else None
