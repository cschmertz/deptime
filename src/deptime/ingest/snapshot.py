"""Static API snapshot of one release, extracted with griffe (no code is imported or executed).

A snapshot maps every *public import path* (what users type: `httpx.Client`, not `httpx._client.Client`)
to its kind, canonical definition path, parameters, and deprecation message, if any.
"""

from __future__ import annotations

import ast
import gzip
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import griffe

SNAPSHOT_FORMAT = 4  # bump when extraction changes: cached snapshots in another format are rebuilt
MAX_DEPTH = 12  # guards pathological re-export cycles

DEPRECATED_DECORATOR_RE = re.compile(r"^(?:[\w.]+\.)?deprecated\b(?:\((?P<args>.*)\))?$", re.S)
STRING_LITERAL_RE = re.compile(r"""(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)""")
# One or more adjacent single-line literals (implicit concatenation may span lines via the \s* gap).
ADJACENT_LITERALS_RE = re.compile(r"""(?:f?(["'])(?:\\.|(?!\1).)*\1\s*)+""")
DEPRECATION_WORDS_RE = re.compile(r"deprecat|will be removed|no longer supported|use .+ instead", re.I)
WARNING_CATEGORY_RE = re.compile(
    r"\b(?:DeprecationWarning|PendingDeprecationWarning|FutureWarning|\w*Deprecat\w*Warning)\b"
)
# A shim's warning category that marks a deprecation, including django's RemovedInDjango50Warning.
SHIM_CATEGORY_RE = re.compile(rf"{WARNING_CATEGORY_RE.pattern}|\bRemovedIn\w*Warning\b")
# Builds run on the pinned interpreter (.python-version), so this list doesn't vary between index builds.
# distutils left the stdlib in 3.12; typing_extensions is a backport of it.
STDLIB_MODULES = frozenset(sys.stdlib_module_names) | {"distutils", "typing_extensions"}


@dataclass
class SymbolRecord:
    path: str
    kind: str  # module | class | function | attribute | type alias | alias (unresolved/external) | getattr (module __getattr__)
    canonical: str | None = None
    params: list[dict[str, Any]] | None = None
    deprecated: str | None = None


@dataclass
class ApiSnapshot:
    package: str
    version: str
    import_names: list[str]
    symbols: dict[str, SymbolRecord] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    format: int = SNAPSHOT_FORMAT

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["symbols"] = [asdict(s) for s in self.symbols.values()]
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)

    @classmethod
    def load(cls, path: Path) -> ApiSnapshot:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        symbols = {s["path"]: SymbolRecord(**s) for s in payload.pop("symbols")}
        return cls(symbols=symbols, **payload)


@dataclass(frozen=True)
class LazySnapshot:
    """A release's snapshot, loaded only when an index rebuild reaches it.

    Holding every release of a package at once doesn't fit a CI runner: litellm's 290 releases need ~19 GB
    (DT-DATA-03). `load` may return None when the snapshot turns out to be unavailable.
    """

    version: str
    load: Callable[[], ApiSnapshot | None]


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def _params(func: griffe.Function) -> list[dict[str, Any]]:
    params = list(func.parameters)
    if params and func.parent is not None and func.parent.is_class and params[0].name in {"self", "cls"}:
        params = params[1:]
    return [
        {
            "name": p.name,
            "kind": p.kind.value if p.kind else "positional or keyword",
            "default": _s(p.default),
            "annotation": _s(p.annotation),
        }
        for p in params
    ]


def _literal_text(chunk: str) -> str:
    return "".join(m.group("s") for m in STRING_LITERAL_RE.finditer(chunk))


def deprecation_message(obj: griffe.Object) -> str | None:
    """Best-effort deprecation detection: griffe extensions, @deprecated decorators, warnings in the body."""
    flagged = getattr(obj, "deprecated", None)
    if flagged:
        return flagged if isinstance(flagged, str) else "deprecated"
    for dec in getattr(obj, "decorators", []) or []:
        m = DEPRECATED_DECORATOR_RE.match(str(dec.value))
        if m:
            return (m.group("args") or "deprecated").strip()[:400]
    if not isinstance(obj, griffe.Function):
        return None
    try:
        source = obj.source
    except (OSError, ValueError, AttributeError):
        return None
    if "warn" not in source or not WARNING_CATEGORY_RE.search(source):
        return None
    # Join implicitly-concatenated string literals, keep the ones that read like deprecation notices.
    for m in ADJACENT_LITERALS_RE.finditer(source):
        text = _literal_text(m.group(0))
        if DEPRECATION_WORDS_RE.search(text):
            return text.strip()[:400]
    return "deprecated (warning emitted; message not statically recoverable)"


@dataclass(frozen=True)
class _Names:
    """Exactly `names`, or (`exclude`) every name but them."""

    names: frozenset[str]
    exclude: bool = False

    def __and__(self, other: frozenset[str]) -> _Names:
        return _Names(other - self.names) if self.exclude else _Names(self.names & other)

    def __sub__(self, other: frozenset[str]) -> _Names:
        return _Names(self.names | other, True) if self.exclude else _Names(self.names - other)


def _union(a: _Names | None, b: _Names | None) -> _Names | None:
    if a is None or b is None:
        return a or b
    if a.exclude and b.exclude:
        return _Names(a.names & b.names, True)
    if a.exclude or b.exclude:
        exc, inc = (a, b) if a.exclude else (b, a)
        return _Names(exc.names - inc.names, True)
    return _Names(a.names | b.names)


def _string_keys(node: ast.expr) -> dict[str, str | None] | None:
    """A literal dict, set, list or tuple of strings: key -> the value when it is a string too."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1:
        return _string_keys(node.args[0]) if node.func.id in {"frozenset", "set", "tuple", "list"} else None
    if isinstance(node, ast.Dict):
        if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
            return None
        return {
            k.value: v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None  # type: ignore[union-attr]
            for k, v in zip(node.keys, node.values, strict=True)
        }
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            return None
        return {e.value: None for e in node.elts}  # type: ignore[attr-defined]
    return None


@dataclass
class _Shim:
    module: str
    param: str
    tables: dict[str, dict[str, str | None]]  # module-level literal collections, by name


def _tested(test: ast.expr, shim: _Shim) -> tuple[frozenset[str], bool] | None:
    """The names `test` compares the attribute name with, and whether it holds for them (`==`, `in`) or for
    every other name (`!=`, `not in`). None when the test is about something else."""
    if isinstance(test, ast.BoolOp):
        parts = [_tested(v, shim) for v in test.values]
        positive = isinstance(test.op, ast.Or)  # `a == x or a == y`, `a != x and a != y`
        if any(p is None or p[1] != positive for p in parts):
            return None
        return frozenset().union(*(p[0] for p in parts if p)), positive
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    left, op, right = test.left, test.ops[0], test.comparators[0]
    if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Name) and right.id == shim.param:
        left, right = right, left
    if not (isinstance(left, ast.Name) and left.id == shim.param):
        return None
    if (
        isinstance(op, (ast.Eq, ast.NotEq))
        and isinstance(right, ast.Constant)
        and isinstance(right.value, str)
    ):
        return frozenset({right.value}), isinstance(op, ast.Eq)
    if not isinstance(op, (ast.In, ast.NotIn)):
        return None
    if isinstance(right, ast.Call) and isinstance(right.func, ast.Attribute) and right.func.attr == "keys":
        right = right.func.value
    keys = shim.tables.get(right.id) if isinstance(right, ast.Name) else _string_keys(right)
    return (frozenset(keys), isinstance(op, ast.In)) if keys is not None else None


def _reach(
    stmts: list[ast.stmt], live: _Names, trail: list[ast.stmt], shim: _Shim, served: dict[str, list[ast.stmt]]
) -> _Names | None:
    """Follow `stmts` for the names in `live`; record each name that reaches a `return`, with the statements run
    on the way there. Returns the names that fall through, or None when none do."""
    for st in stmts:
        if isinstance(st, ast.Return):
            if not live.exclude:
                for name in live.names:
                    served.setdefault(name, trail)
            return None
        if isinstance(st, ast.Raise):
            return None
        if isinstance(st, ast.If):
            tested = _tested(st.test, shim)
            if tested is None:  # a condition we can't read: names may get past it, but none is surely served
                after = _union(
                    _reach(st.body, live, trail, shim, {}), _reach(st.orelse, live, trail, shim, {})
                )
            else:
                names, positive = tested
                yes, no = (live & names, live - names) if positive else (live - names, live & names)
                after = _union(
                    _reach(st.body, yes, trail, shim, served), _reach(st.orelse, no, trail, shim, served)
                )
            if after is None:
                return None
            live = after
            if tested is None:
                trail = [
                    *trail,
                    st,
                ]  # its warning may still be the name's deprecation (fastmcp's is conditional)
            continue
        trail = [*trail, st]  # `try`, `with` and `match` bodies aren't followed: their returns don't count
    return live


def _mapped(node: ast.expr, name: str, shim: _Shim) -> str | None:
    """`TABLE[name]` or `TABLE.get(name)`: the string TABLE maps `name` to (a rename's new name)."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Name) and node.slice.id == shim.param:
        table = node.value
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == shim.param
    ):
        table = node.func.value
    else:
        return None
    return shim.tables.get(table.id, {}).get(name) if isinstance(table, ast.Name) else None


def _warning_text(node: ast.expr, name: str, env: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
            parts.append(env.get(v.value.id, "…"))
        else:
            parts.append("…")
    return "".join(parts)


def _warn_call(st: ast.stmt) -> ast.Call | None:
    """A `warn(...)` statement, also inside the conditions `_reach` couldn't read (not in nested functions)."""
    if isinstance(st, ast.If):
        return next(filter(None, map(_warn_call, st.body + st.orelse)), None)
    call = st.value if isinstance(st, ast.Expr) else None
    if not (isinstance(call, ast.Call) and call.args):
        return None
    func = call.func
    named = (isinstance(func, ast.Name) and func.id == "warn") or (
        isinstance(func, ast.Attribute) and func.attr == "warn"
    )
    return call if named else None


def _shim_message(trail: list[ast.stmt], name: str, shim: _Shim) -> str | None:
    """The deprecation warning a shim's branch emits for `name`, with the f-string filled in where the source
    says what goes there (the name itself, the module, a rename table's target)."""
    env = {shim.param: name, "__name__": shim.module}
    for st in trail:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            if (target := _mapped(st.value, name, shim)) is not None:
                env[st.targets[0].id] = target
        elif call := _warn_call(st):
            text = _warning_text(call.args[0], name, env)
            category = (
                call.args[1]
                if len(call.args) > 1
                else next((k.value for k in call.keywords if k.arg == "category"), None)
            )
            category_name = ast.unparse(category) if category is not None else ""
            deprecation = DEPRECATION_WORDS_RE.search(text or "") or SHIM_CATEGORY_RE.search(category_name)
            return text.strip()[:400] if text and deprecation else None
    return None


def _find_getattr(stmts: list[ast.stmt]) -> ast.FunctionDef | None:
    for st in stmts:  # numpy defines it inside a module-level `if`/`else`
        if isinstance(st, ast.FunctionDef) and st.name == "__getattr__":
            return st
        if isinstance(st, (ast.If, ast.Try)) and (found := _find_getattr(st.body + st.orelse)):
            return found
    return None


def _source_tree(mod: griffe.Module) -> ast.Module | None:
    filepath = mod.filepath
    if not isinstance(filepath, Path) or filepath.suffix != ".py":
        return None
    try:
        return ast.parse(filepath.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None


def getattr_served(mod: griffe.Module) -> dict[str, str | None]:
    """Names a module `__getattr__` returns a value for (deprecation shims, lazy imports), each with the
    deprecation message its branch warns with. Read from the source with `ast`; nothing is imported (ADR 0002).

    Only names compared with literals, or looked up in a module-level literal collection, are found. A name
    whose branch raises (numpy 2.0's expired attributes) is not served.
    """
    tree = _source_tree(mod)
    if tree is None:
        return {}
    func = _find_getattr(tree.body)
    args = (func.args.posonlyargs + func.args.args) if func else []
    if func is None or not args:
        return {}
    tables: dict[str, dict[str, str | None]] = {}
    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            target, value = st.targets[0], st.value
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            target, value = st.target, st.value
        else:
            continue
        if isinstance(target, ast.Name) and (keys := _string_keys(value)) is not None:
            tables[target.id] = keys
    shim = _Shim(mod.path, args[0].arg, tables)
    served: dict[str, list[ast.stmt]] = {}
    _reach(func.body, _Names(frozenset(), exclude=True), [], shim, served)
    return {
        name: _shim_message(trail, name, shim)
        for name, trail in sorted(served.items())
        if name.isidentifier()
        and (not name.startswith("_") or (name.startswith("__") and name.endswith("__")))
    }


def foreign_reexports(mod: griffe.Module, *, plain: bool) -> frozenset[str]:
    """Names a module imports from another distribution in a shape that re-exports them (DT-DATA-21):
    `from click.termui import clear as clear`, the explicit re-export convention (typer), and with `plain`,
    `from markupsafe import escape` from outside the standard library (flask 2.2).

    Only top-level statements count: an import under `if TYPE_CHECKING:` or `try:` may not exist at runtime.
    """
    tree = _source_tree(mod)
    names: set[str] = set()
    for st in tree.body if tree is not None else []:
        if isinstance(st, ast.ImportFrom) and st.level == 0 and st.module:
            stdlib = st.module.split(".")[0] in STDLIB_MODULES
            names.update(
                a.name
                for a in st.names
                if a.asname == a.name or (plain and a.asname is None and not stdlib and a.name != "*")
            )
    return frozenset(names)


def _within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + ".")


def _is_test_module(path: str) -> bool:
    """Bundled test suites (`numpy.lib.tests.test_shape_base`) aren't API; `numpy.testing` and `django.test` are."""
    return any(part in {"tests", "conftest"} or part.startswith("test_") for part in path.split("."))


def _resolve(member: griffe.Object | griffe.Alias) -> griffe.Object | None:
    if not member.is_alias:
        return member  # type: ignore[return-value]
    try:
        return member.final_target  # type: ignore[union-attr]
    except (griffe.AliasResolutionError, griffe.CyclicAliasError):
        return None


class _Walker:
    def __init__(self, snapshot: ApiSnapshot, root_prefixes: tuple[str, ...]):
        self.snap = snapshot
        self.roots = root_prefixes
        self.visiting: set[tuple[str, str]] = set()
        self.shims: dict[str, dict[str, str | None]] = {}
        self.foreign: dict[str, frozenset[str]] = {}

    def _internal(self, canonical: str) -> bool:
        return any(canonical == r or canonical.startswith(r + ".") for r in self.roots)

    def record(self, rec: SymbolRecord) -> None:
        self.snap.symbols.setdefault(rec.path, rec)

    def module(self, mod: griffe.Module, public_path: str, depth: int = 0) -> None:
        key = (mod.path, public_path)
        if key in self.visiting or depth > MAX_DEPTH:
            return
        self.visiting.add(key)
        self.record(SymbolRecord(public_path, "module", mod.path))
        reexporter = mod.path if public_path == mod.path and mod.is_init_module else None
        for name, member in list(mod.members.items()):
            if self._public(mod, name, member, reexporter):
                self.member(member, f"{public_path}.{name}", depth, reexporter)
        hook = mod.members.get("__getattr__")
        if hook is not None and not hook.is_alias:
            if mod.path not in self.shims:
                self.shims[mod.path] = getattr_served(mod)
            canonical = f"{mod.path}.__getattr__"
            for name, message in self.shims[mod.path].items():
                if name not in mod.members:  # a real attribute never reaches __getattr__
                    self.record(SymbolRecord(f"{public_path}.{name}", "getattr", canonical, None, message))

    def _public(
        self, mod: griffe.Module, name: str, member: griffe.Object | griffe.Alias, reexporter: str | None
    ) -> bool:
        """griffe's rule, plus what a package's `__init__` imports from the package itself when `__all__` is
        missing (or computed at runtime, as in numpy).

        griffe counts imported names as private, but `from .app import Flask as Flask` is how flask, fastapi,
        click and sqlalchemy expose their API. When an `__init__.pyi` redefines an imported name (numpy 2.0's
        `float64`, `add`), griffe merges the stub and the name is no longer an alias, so the `.py` import counts.
        Names from another distribution count only in a re-exporting shape (`foreign_reexports`).
        """
        try:
            if member.is_public:
                return True
        except (griffe.AliasResolutionError, griffe.CyclicAliasError):
            return False
        source = member.target_path if member.is_alias else mod.imports.get(name)  # type: ignore[union-attr]
        if reexporter is None or source is None or mod.exports or name.startswith("_"):
            return False
        if self._internal(source):
            return True
        if mod.path not in self.foreign:
            # A plain import counts only at the package's top level: a subpackage importing pydantic's
            # `BaseModel` (chromadb.server.fastapi, crewai.flow.runtime) uses it rather than exporting it.
            self.foreign[mod.path] = foreign_reexports(mod, plain=mod.path in self.roots)
        return name in self.foreign[mod.path]

    @staticmethod
    def _walks(module_path: str, public_path: str, reexporter: str | None) -> bool:
        """Walk a module at its own path, and once more where a package's `__init__` re-exports it
        (`numpy.emath`), unless that points back at the package or a parent of it.

        Modules re-exported anywhere else are recorded but not walked: a wildcard import of a module that
        imports its own package (litellm's `from .main import *`) otherwise yields `litellm.litellm.litellm...`.
        """
        if public_path == module_path:
            return True
        return reexporter is not None and not _within(reexporter, module_path)

    def member(
        self,
        member: griffe.Object | griffe.Alias,
        public_path: str,
        depth: int,
        reexporter: str | None = None,
    ) -> None:
        target = _resolve(member)
        if target is None:
            self.record(SymbolRecord(public_path, "alias", getattr(member, "target_path", None)))
            return
        if target.is_module:
            if _is_test_module(target.path):
                return
            if self._internal(target.path) and self._walks(target.path, public_path, reexporter):
                self.module(target, public_path, depth + 1)  # type: ignore[arg-type]
            else:
                self.record(SymbolRecord(public_path, "module", target.path))
        elif target.is_class:
            self.cls(target, public_path)  # type: ignore[arg-type]
        elif target.is_function:
            self.record(
                SymbolRecord(
                    public_path, "function", target.path, _params(target), deprecation_message(target)
                )
            )  # type: ignore[arg-type]
        else:
            self.record(
                SymbolRecord(public_path, target.kind.value, target.path, None, deprecation_message(target))
            )

    def cls(self, cls: griffe.Class, public_path: str) -> None:
        self.record(SymbolRecord(public_path, "class", cls.path, None, deprecation_message(cls)))
        try:
            members = cls.all_members  # includes inherited members when bases resolve
        except Exception as exc:  # griffe raises several resolution errors for external bases
            self.snap.errors.append(f"{cls.path}: inherited members unavailable ({type(exc).__name__})")
            members = cls.members
        for name, member in members.items():
            if name.startswith("_") and name not in {"__init__", "__call__"}:
                continue
            target = _resolve(member)
            if target is None:
                continue
            path = f"{public_path}.{name}"
            if target.is_function:
                self.record(
                    SymbolRecord(path, "function", target.path, _params(target), deprecation_message(target))
                )  # type: ignore[arg-type]
            elif target.is_attribute:
                self.record(SymbolRecord(path, "attribute", target.path, None, None))
            elif target.is_class:
                self.record(SymbolRecord(path, "class", target.path, None, deprecation_message(target)))


def build_snapshot(package: str, version: str, import_names: list[str], search_path: Path) -> ApiSnapshot:
    snap = ApiSnapshot(package=package, version=version, import_names=list(import_names))
    walker = _Walker(snap, tuple(import_names))
    for import_name in import_names:
        try:
            loaded = griffe.load(
                import_name,
                search_paths=[str(search_path)],
                allow_inspection=False,  # never import untrusted code
                try_relative_path=False,
                resolve_aliases=True,
                # None loads only a private sibling (`_pytest` for `pytest`), found in the same wheel because
                # search_paths replaces sys.path; other distributions' names stay aliases.
                resolve_external=None,
            )
        except Exception as exc:  # report and continue: one bad import name must not sink the release
            snap.errors.append(f"load {import_name}: {type(exc).__name__}: {exc}")
            continue
        if isinstance(loaded, griffe.Module):
            walker.module(loaded, import_name)
        else:
            walker.member(loaded, import_name, 0)
    return snap
