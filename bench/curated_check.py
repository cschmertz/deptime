"""Check each curated hint (DT-DATA-08) against an index: the names it pairs are real (ADR 0006).

    uv run python bench/curated_check.py [index.sqlite]      # default: the index in DEPTIME_HOME

`path` must be a name its package had in some indexed release, or for `path(param)`, an argument the function
took; for package `python`, a standard module or name some Python version removed (stdlib_versions.txt,
stdlib_names.txt). `target` must be an argument that function takes in some release, a path some covered package has (its own
or another's, by import name), or a standard-library name. A target in a distribution DepTime doesn't cover (flask's "Import them directly from
markupsafe instead") can't be checked here and is listed as such. Exits 1 if any hint fails.
"""

import importlib
import json
import sys
import tomllib
from importlib import resources
from pathlib import Path

from deptime import stdlib
from deptime.config import Settings, load_coverage
from deptime.index.location import active_index
from deptime.index.store import IndexStore

index = Path(sys.argv[1]) if len(sys.argv) > 1 else active_index(Settings.from_env().index_path)
if not index.is_file():
    sys.exit(f"no index at {index}: run `deptime index pull` first")
# Through the store, which looks a class member up through the class's MRO when the index stores each class's
# own members once (DT-DATA-16, DT-DATA-49): such members have no `spans` rows of their own.
store = IndexStore.open(index)
hints = tomllib.loads(resources.files("deptime").joinpath("curated.toml").read_text(encoding="utf-8"))["hint"]
owner = {name: p.name for p in load_coverage().packages for name in p.import_names}


def has(package: str, path: str) -> bool:
    return bool(store.spans(package, path))


def took(package: str, func: str) -> set[str]:
    names: set[str] = set()
    for span in store.spans(package, func):
        names.update(p["name"] for p in json.loads(span["params"] or "[]"))
        names.update(json.loads(span["deprecated_params"] or "{}"))
    return names


def package_of(path: str) -> str | None:
    parts = path.split(".")
    return next(
        (owner[".".join(parts[:i])] for i in range(len(parts), 0, -1) if ".".join(parts[:i]) in owner), None
    )


def in_stdlib(path: str) -> bool:
    parts = path.split(".")
    if parts[0] not in sys.stdlib_module_names:
        return False
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            if not hasattr(obj, attr):
                return False
            obj = getattr(obj, attr)
        return True
    return False


def removed_from_stdlib(path: str) -> bool:
    """A standard module or name some Python version dropped (DT-DATA-06, DT-DATA-47): what a `python` hint is for."""
    entry = stdlib.lookup(path)
    module, _, name = path.rpartition(".")
    named = stdlib.lookup_name(module, name)
    return (entry is not None and entry.module == path and entry.last is not None) or (
        named is not None and named.ranges[-1][1] is not None
    )


bad, unchecked = [], []
for h in hints:
    func, _, param = h["path"].partition("(")
    param = param.removesuffix(")")
    if (
        h["package"] == "python"
    ):  # the standard library's own removals (DT-DATA-48): its tables, not the index
        if not removed_from_stdlib(func):
            bad.append(f"no Python version removed {h['path']}")
    elif not has(h["package"], func) or (param and param not in took(h["package"], func)):
        bad.append(f"{h['package']} never had {h['path']}")
    if param:
        if h["target"] not in took(h["package"], func):
            bad.append(f"{func} never took {h['target']} (for {h['path']})")
    elif pkg := package_of(h["target"]):
        if not has(pkg, h["target"]):
            bad.append(f"{pkg} never had {h['target']} (for {h['path']})")
    elif h["target"].split(".")[0] in sys.stdlib_module_names:
        if not in_stdlib(h["target"]):
            bad.append(f"the standard library has no {h['target']} (for {h['path']})")
    else:
        unchecked.append(f"not checked, outside the index: {h['target']} (for {h['path']})")

print("\n".join(unchecked + bad))
print(f"{len(hints)} curated hints, {len(bad)} problems, {len(unchecked)} not checked ({index})")
sys.exit(1 if bad else 0)
