"""S1 runtime probe (DT-SPIKE-01): list every module-level name a package exposes at runtime.

THIS IMPORTS THE PACKAGE UNDER TEST. It runs only inside the S1 sandbox (`run.py` on a GitHub Actions runner: no
network, a throwaway user, an empty environment), never in the dev venv or on bare metal (invariant 1, ADR 0002).

Stdlib only, and Python 3.9 compatible, because it runs on the Python each sampled release was built for:

  python -I probe.py --roots langchain --package langchain --version 0.3.27 --out names.json

The runtime-public names, chosen to be comparable with the index's public paths (bench/s1/README.md):
- every importable submodule of each root, skipping `_private` segments and bundled test suites;
- in each module, the names in `__all__` when it is defined, otherwise `dir()` minus `_names`, keeping only
  classes, functions and modules defined inside the package (so `os` and `typing.Any` don't count);
- each name confirmed by `getattr()`, which also runs lazy `__getattr__` loaders.

Only exception type names are recorded, never messages or tracebacks (docs/08).
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pkgutil
import signal
import sys
import time
import warnings
from collections import Counter

IMPORT_TIMEOUT_S = 60
MAX_DEPTH = 12


class _Timeout(BaseException):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + ".") for r in roots)


def _skipped(module_path: str) -> bool:
    """Same rule as the index: no `_private` segments, no bundled test suites (snapshot._is_test_module)."""
    parts = module_path.split(".")
    return any(p.startswith("_") or p in {"tests", "conftest"} or p.startswith("test_") for p in parts)


def _guarded(fn, *args):
    """Run `fn` with warnings silenced and a timeout; return (value, exception type name or None)."""
    signal.alarm(IMPORT_TIMEOUT_S)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return fn(*args), None
    except _Timeout:
        return None, "Timeout"
    except BaseException as exc:  # SystemExit, KeyboardInterrupt and friends from import-time code too
        return None, type(exc).__name__
    finally:
        signal.alarm(0)


def import_all(roots: tuple[str, ...]) -> tuple[dict[str, object], dict[str, str]]:
    """Import each root and, breadth first, every public submodule under it."""
    modules: dict[str, object] = {}
    failed: dict[str, str] = {}
    queue = [(r, 0) for r in roots]
    while queue:
        path, depth = queue.pop(0)
        if path in modules or path in failed:
            continue
        mod, err = _guarded(importlib.import_module, path)
        if err is not None:
            failed[path] = err
            continue
        modules[path] = mod
        sub_path = getattr(mod, "__path__", None)
        if sub_path is None or depth >= MAX_DEPTH:
            continue
        children, err = _guarded(
            lambda p, prefix: list(pkgutil.iter_modules(p, prefix)), sub_path, path + "."
        )
        if err is not None:
            failed[path + ".*"] = err
            continue
        for info in sorted(children, key=lambda i: i.name):
            if not _skipped(info.name):
                queue.append((info.name, depth + 1))
    return modules, failed


def _kind(obj: object) -> str:
    if inspect.ismodule(obj):
        return "module"
    if inspect.isclass(obj):
        return "class"
    if inspect.isroutine(obj):
        return "function"
    return "other"


def _origin(obj: object, kind: str, module: str, roots: tuple[str, ...]) -> str:
    """Where the object is defined: `own` (this module), `package` (elsewhere in the package), `external`, or
    `unknown` (constants and instances carry no defining module)."""
    if kind == "module":
        where = getattr(obj, "__name__", None)
    elif kind in {"class", "function"}:
        where = getattr(obj, "__module__", None)
    else:
        return "unknown"
    if not isinstance(where, str):
        return "unknown"
    if where == module:
        return "own"
    return "package" if _within(where, roots) else "external"


def public_names(modules: dict[str, object], roots: tuple[str, ...]) -> tuple[list[list[str]], Counter]:
    rows: list[list[str]] = []
    unresolved: Counter = Counter()
    for path in sorted(modules):
        mod = modules[path]
        stratum = "init" if hasattr(mod, "__path__") else "module"
        exports = getattr(mod, "__all__", None)
        if isinstance(exports, (list, tuple)) and all(isinstance(n, str) for n in exports):
            candidates = [(n, "__all__") for n in dict.fromkeys(exports) if not n.startswith("_")]
        else:
            candidates = [(n, "dir") for n in dir(mod) if not n.startswith("_")]
        for name, how in candidates:
            obj, err = _guarded(getattr, mod, name)
            if err is not None:
                unresolved[err] += 1
                continue
            kind = _kind(obj)
            origin = _origin(obj, kind, path, roots)
            if how == "dir" and (kind == "other" or origin not in {"own", "package"}):
                continue
            rows.append([path, name, stratum, how, kind, origin])
    return rows, unresolved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True, help="import names from coverage.toml")
    ap.add_argument("--package", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    roots = tuple(args.roots)
    signal.signal(signal.SIGALRM, _alarm)
    started = time.monotonic()

    from importlib import metadata

    installed = metadata.version(args.package)
    modules, failed = import_all(roots)
    rows, unresolved = public_names(modules, roots)
    result = {
        "package": args.package,
        "version": args.version,
        "installed_version": installed,
        "python": ".".join(str(n) for n in sys.version_info[:3]),
        "roots": list(roots),
        "modules_imported": len(modules),
        "modules_failed": dict(sorted(failed.items())),
        "unresolved_names": dict(unresolved),
        "seconds": round(time.monotonic() - started, 1),
        "columns": ["module", "name", "stratum", "how", "kind", "origin"],
        "names": rows,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    print(
        f"{args.package} {args.version}: {len(modules)} modules, {len(failed)} failed, {len(rows)} names",
        file=sys.stderr,
    )
    return 0 if installed == args.version else 3


if __name__ == "__main__":
    sys.exit(main())
