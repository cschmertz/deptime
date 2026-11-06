"""Build `src/deptime/stdlib_names.txt`: the names inside standard-library modules whose presence changes between
Python 3.9 and 3.15 (DT-DATA-47), read statically from typeshed's stubs. Nothing is imported.

  python tools/stdlib_names.py <typeshed e8ba06f>/stdlib <typeshed e2609bd>/stdlib [--check PYTHON ...] \
      > src/deptime/stdlib_names.txt

typeshed models each Python version it supports with `sys.version_info` checks. The newer commit (the one
`stdlib_versions.txt` comes from) supports 3.10 and later, so 3.9 comes from an older commit that still did; a change
between 3.9 and 3.10 counts only where that older commit's own checks show it, never from the two commits differing.

A stub's public names follow the stub rules (PEP 484, typeshed's CONTRIBUTING): its own definitions, `import x as x`
and `from m import y as y`, a star import's public names, and what `__all__` lists. Platform checks count both ways,
so a name any platform has is there. Each name keeps the definition it comes from, so a removed name whose object
another public module still exports (3.9's `collections.Mapping`, from `_collections_abc`) points there.

`--check` takes CPython interpreters (the standard library only, never an indexed package) and leaves out each name
one of them has in a version the stubs show without it: typeshed drops some deprecated names a module `__getattr__`
still serves (`sqlite3.version` in 3.12 and 3.13) and dates some platform constants late (`os.O_EXEC` on macOS).
The names themselves still come only from the stubs.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

OLD_MINOR = 9  # from the older commit
MINORS = range(10, 16)  # from the newer commit
HEADER = """\
# Names inside standard-library modules whose presence changes between Python 3.9 and 3.15 (DT-DATA-47), read
# statically from typeshed's stubs by tools/stdlib_names.py: 3.10-3.15 from commit
# e2609bd14f7352cb5aa713a8b5c0515295945fd1 (2026-10-27, the one stdlib_versions.txt comes from), and 3.9 from commit
# e8ba06f710d5fe4f9219fafd77abd79a6e3414f4 (2025-08-24), the last before typeshed dropped 3.9; a change between 3.9
# and 3.10 counts only where that commit's own version checks show it.
# typeshed is licensed under the Apache License 2.0: https://github.com/python/typeshed/blob/main/LICENSE
#
# `module name ranges [path]`: the minor versions of 3.x that have the name, `9-10` or `11-` (still there); a range
# starting at 9 may start earlier, and only versions that have the module count. `path` is another public module's
# name for the same object in the first version without it. Names that every version with the module has, private
# names and private modules are left out, and so are names that one of these CPython interpreters has where the
# stubs show none: {checked}.
"""
# Imported to check a name: none has side effects worth running for that, or any outside one platform.
UNCHECKED = {
    "antigravity",
    "this",
    "idlelib",
    "turtle",
    "turtledemo",
    "tkinter",
    "msvcrt",
    "winreg",
    "winsound",
    "nt",
}
PROBE = """
import importlib, json, sys, warnings
out = {}
for path in json.load(sys.stdin):
    module, _, name = path.rpartition(".")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out[path] = hasattr(importlib.import_module(module), name)
    except Exception:
        pass
print(json.dumps([sys.version_info[1], out]))
"""


def _module_of(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _version_test(test: ast.expr, minor: int) -> bool | None:
    """Whether `test` holds on Python 3.<minor>: True, False, or None when it depends on something else (the
    platform, a patch release)."""
    if isinstance(test, ast.BoolOp):
        values = [_version_test(v, minor) for v in test.values]
        if isinstance(test.op, ast.And):
            return False if False in values else (None if None in values else True)
        return True if True in values else (None if None in values else False)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _version_test(test.operand, minor)
        return None if inner is None else not inner
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and ast.unparse(test.left) in {"sys.version_info", "sys.version_info[:2]"}
        and isinstance(test.comparators[0], ast.Tuple)
    ):
        return None
    try:
        wanted = tuple(ast.literal_eval(test.comparators[0]))
    except ValueError:
        return None
    if len(wanted) > 2 and wanted[:2] == (3, minor):
        return None  # a patch release decides
    have, wanted = (3, minor), tuple(wanted[:2])
    op = test.ops[0]
    table = {ast.GtE: have >= wanted, ast.Lt: have < wanted, ast.Gt: have > wanted, ast.LtE: have <= wanted}
    if isinstance(op, ast.Eq):
        return have == wanted
    if isinstance(op, ast.NotEq):
        return have != wanted
    return table.get(type(op))


def _absolute(module: str, is_package: bool, level: int, name: str | None) -> str:
    if level == 0:
        return name or ""
    parts = module.split(".")
    parts = parts[: len(parts) - level + (1 if is_package else 0)]
    return ".".join([*parts, name] if name else parts)


def _type_check_only(st: ast.stmt) -> bool:
    return any(
        ast.unparse(d).rpartition(".")[2] == "type_check_only" for d in getattr(st, "decorator_list", [])
    )


class Stubs:
    def __init__(self, root: Path):
        self.files = {_module_of(root, p): p for p in sorted(root.rglob("*.pyi"))}
        self.memo: dict[tuple[str, int], dict[str, str]] = {}
        self.listed: dict[tuple[str, int], list[str] | None] = {}  # each module's `__all__`, for star imports
        self.active: set[tuple[str, int]] = set()

    def public(self, module: str, minor: int) -> dict[str, str]:
        """`module`'s public names on Python 3.<minor>, each with the definition it comes from."""
        key = (module, minor)
        if key in self.memo:
            return self.memo[key]
        if key in self.active or module not in self.files:
            return {}  # an import cycle, or not a stub module
        self.active.add(key)
        path = self.files[module]
        is_package = path.name == "__init__.pyi"
        bound: dict[str, str] = {}
        exported: set[str] = set()
        listed: list[str] | None = None

        def origin(source: str, name: str) -> str:
            return (
                self.public(source, minor).get(name)
                or self._defined(source, name, minor)
                or f"{source}.{name}"
            )

        def walk(body: list[ast.stmt]) -> None:
            nonlocal listed
            for st in body:
                if isinstance(st, ast.If):
                    holds = _version_test(st.test, minor)
                    if holds is not False:
                        walk(st.body)
                    if holds is not True:
                        walk(st.orelse)
                elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if not _type_check_only(st):
                        bound[st.name] = f"{module}.{st.name}"
                        exported.add(st.name)
                elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                    for t in targets:
                        if not isinstance(t, ast.Name):
                            continue
                        if t.id == "__all__":
                            try:
                                names = [n for n in ast.literal_eval(st.value) if isinstance(n, str)]
                            except ValueError:
                                continue
                            listed = (listed or []) + names if isinstance(st, ast.AugAssign) else names
                        else:
                            bound.setdefault(t.id, f"{module}.{t.id}")
                            exported.add(t.id)
                elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                    call = st.value  # __all__ += [...] written as __all__.extend([...]) / .append("x")
                    func = ast.unparse(call.func)
                    if func in {"__all__.extend", "__all__.append"} and call.args:
                        try:
                            value = ast.literal_eval(call.args[0])
                        except ValueError:
                            continue
                        listed = (listed or []) + ([value] if isinstance(value, str) else list(value))
                elif isinstance(st, ast.Import):
                    for a in st.names:
                        if a.asname and a.asname == a.name:
                            bound[a.asname] = a.name
                            exported.add(a.asname)
                elif isinstance(st, ast.ImportFrom):
                    source = _absolute(module, is_package, st.level, st.module)
                    for a in st.names:
                        if a.name == "*":
                            for name, where in self.star(source, minor).items():
                                bound[name] = where
                                exported.add(name)
                            continue
                        local = a.asname or a.name
                        bound[local] = origin(source, a.name)
                        if a.asname == a.name:
                            exported.add(local)

        walk(ast.parse(path.read_text(encoding="utf-8")).body)
        names = exported | {n for n in listed or () if n in bound}
        result = {n: bound[n] for n in names if not n.startswith("_") and n in bound}
        self.active.discard(key)
        self.memo[key] = result
        self.listed[key] = listed
        return result

    def star(self, module: str, minor: int) -> dict[str, str]:
        """What `from module import *` binds: the names its `__all__` lists, else its public names. 3.9's
        `collections` star-imports `_collections_abc`, whose `__all__` leaves out `dict_items`."""
        names = self.public(module, minor)
        listed = self.listed.get((module, minor))
        return names if listed is None else {n: o for n, o in names.items() if n in listed}

    def _defined(self, module: str, name: str, minor: int) -> str | None:
        """Where a name a module only imports (not re-exported) comes from, for origins."""
        if module not in self.files:
            return None
        for st in ast.walk(ast.parse(self.files[module].read_text(encoding="utf-8"))):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st.name == name:
                return f"{module}.{name}"
        return None


def _shared(a: list[str], b: list[str]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def _ranges(minors: list[int]) -> str:
    out, start, prev = [], None, None
    for m in [*minors, None]:
        if start is not None and (m is None or m != prev + 1):
            out.append(f"{start}-" if prev == MINORS[-1] else f"{start}-{prev}")
            start = None
        if m is not None and start is None:
            start = m
        prev = m
    return ",".join(out)


def _runtime(pythons: list[str], paths: list[str]) -> tuple[list[str], dict[int, dict[str, bool]]]:
    """Which of `paths` each interpreter's standard library has, by minor version."""
    names, seen = [], {}
    wanted = [p for p in paths if p.split(".")[0] not in UNCHECKED]
    for python in pythons:
        done = subprocess.run(
            [python, "-I", "-c", PROBE], input=json.dumps(wanted), capture_output=True, text=True, check=True
        )
        minor, found = json.loads(done.stdout.strip().splitlines()[-1])
        names.append(f"3.{minor}")
        seen[minor] = found
    return names, seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old", type=Path, help="typeshed's stdlib/ at the commit that still models 3.9")
    ap.add_argument("new", type=Path, help="typeshed's stdlib/ at the commit stdlib_versions.txt comes from")
    ap.add_argument(
        "--check", nargs="*", default=[], help="CPython interpreters whose standard library may veto"
    )
    args = ap.parse_args()
    old, new = Stubs(args.old), Stubs(args.new)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from deptime import stdlib  # the module table this one complements

    public_modules = sorted(m for m in new.files if not any(p.startswith("_") for p in m.split(".")))
    lines = []
    for module in public_modules:
        entry = stdlib.lookup(module)
        if entry is None:
            continue
        span = [m for m in [OLD_MINOR, *MINORS] if entry.has(m)]
        views = {m: new.public(module, m) for m in MINORS if m in span}
        if OLD_MINOR in span:
            old9, old10 = old.public(module, OLD_MINOR), old.public(module, 10)
            new10 = views.get(10, {})
            # 3.9 from the older commit only where its own checks change between 3.9 and 3.10.
            nine = {n: o for n, o in new10.items() if (n in old9) == (n in old10)}
            nine |= {n: o for n, o in old9.items() if n not in old10}
            views[OLD_MINOR] = nine
        presence: dict[str, list[int]] = defaultdict(list)
        for m in sorted(views):
            for name in views[m]:
                presence[name].append(m)
        for name, minors in sorted(presence.items()):
            if minors == sorted(views):
                continue
            moved = ""
            gone = next((m for m in sorted(views) if m > minors[0] and m not in minors), None)
            if gone is not None:
                where = views[minors[-1] if minors[-1] < gone else minors[0]][name]
                # The path nearest the old one first: `collections.abc.Mapping`, not typeshed's `typing.Mapping`,
                # which `_collections_abc` re-exports in the stubs but is another object at runtime.
                home = module.split(".")
                others = sorted(
                    (
                        f"{mod}.{n}"
                        for mod in public_modules
                        for n, o in new.public(mod, gone).items()
                        if o == where
                    )
                    if gone >= MINORS[0]
                    else (),
                    key=lambda p: (-_shared(p.split("."), home), p.count("."), len(p), p),
                )
                others = [p for p in others if p != f"{module}.{name}"]
                moved = f" {others[0]}" if others else ""
            lines.append((module, name, minors, f"{module} {name} {_ranges(minors)}{moved}"))
    checked, seen = _runtime(args.check, [f"{m}.{n}" for m, n, _, _ in lines])
    kept = []
    for module, name, minors, line in lines:
        entry = stdlib.lookup(module)
        vetoed = [
            m
            for m, found in seen.items()
            if found.get(f"{module}.{name}") and entry.has(m) and m not in minors
        ]
        if vetoed:
            print(f"left out {module}.{name} ({_ranges(minors)}): present in 3.{vetoed[0]}", file=sys.stderr)
            continue
        kept.append(line)
    header = HEADER.format(checked=", ".join(checked) or "none")
    sys.stdout.write(header + "\n".join(kept) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
