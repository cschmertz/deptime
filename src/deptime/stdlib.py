"""Which standard-library modules each Python version has, from typeshed's stdlib/VERSIONS (DT-DATA-06), and the
names inside them whose presence changes between Python 3.9 and 3.15, from typeshed's stubs (DT-DATA-47).

`No module named 'distutils'` on Python 3.12 is a version change like a library's, but the index only holds
distributions from PyPI. Both tables ship with deptime (`stdlib_versions.txt`, `stdlib_names.txt`, built by
tools/stdlib_names.py) and are read at query time.
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
# The names table covers these minor versions: 3.9 from an older typeshed commit, 3.10-3.15 from the current one.
NAMES_FIRST, NAMES_LAST = 9, 15
NAME_LINE_RE = re.compile(
    r"^(?P<module>[A-Za-z_][\w.]*) (?P<name>[A-Za-z_]\w*) (?P<ranges>\d+-\d*(?:,\d+-\d*)*)"
    r"(?: (?P<moved>[A-Za-z_][\w.]*))?$"
)


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


@dataclass(frozen=True)
class StdlibName:
    module: str
    name: str
    # The minor versions of 3.x that have it, as (first, last) runs; last None while it's still there. A run starting
    # at NAMES_FIRST may start earlier.
    ranges: tuple[tuple[int, int | None], ...]
    moved: str | None  # another public module's path for the same object, in the first version without it

    def has(self, minor: int) -> bool:
        return any(a <= minor and (b is None or minor <= b) for a, b in self.ranges)

    @property
    def path(self) -> str:
        return f"{self.module}.{self.name}"

    def last_before(self, minor: int) -> int | None:
        """The last minor version before `minor` that has it."""
        return max(
            (min(b if b is not None else minor - 1, minor - 1) for a, b in self.ranges if a < minor),
            default=None,
        )

    def first_after(self, minor: int) -> int | None:
        """The first minor version after `minor` that has it."""
        return min((max(a, minor + 1) for a, b in self.ranges if b is None or b > minor), default=None)


@cache
def _names() -> dict[tuple[str, str], StdlibName]:
    text = resources.files("deptime").joinpath("stdlib_names.txt").read_text(encoding="utf-8")
    out: dict[tuple[str, str], StdlibName] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        m = NAME_LINE_RE.match(line)
        if m is None:
            raise ValueError(f"stdlib_names.txt: unreadable line {line!r}")
        ranges = []
        for run in m.group("ranges").split(","):
            first, _, last = run.partition("-")
            ranges.append((int(first), int(last) if last else None))
        out[m.group("module"), m.group("name")] = StdlibName(
            m.group("module"), m.group("name"), tuple(ranges), m.group("moved")
        )
    return out


def lookup_name(module: str, name: str) -> StdlibName | None:
    """`module.name` when its presence changes between Python 3.9 and 3.15; None for a name every version with the
    module has, and for one typeshed doesn't list."""
    return _names().get((module, name))
