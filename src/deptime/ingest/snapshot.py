"""Static API snapshot of one release, extracted with griffe (no code is imported or executed).

A snapshot maps every *public import path* (what users type: `httpx.Client`, not `httpx._client.Client`)
to its kind, canonical definition path, parameters, and deprecation message, if any.
"""

from __future__ import annotations

import ast
import copy
import gzip
import json
import re
import string
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import griffe

SNAPSHOT_FORMAT = 7  # bump when extraction changes: cached snapshots in another format are rebuilt
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
    # The artifact analysed: another distribution's for a wrapper release (DT-DATA-17). None in older snapshots.
    source_file: str | None = None

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


def _table_keys(node: ast.expr, tables: dict[str, dict[str, Any]]) -> frozenset[str] | None:
    """A literal collection of strings, or a module-level table's keys: `list(_module_lookup.keys())`."""
    if (literal := _string_keys(node)) is not None:
        return frozenset(literal)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"list", "tuple", "sorted", "set", "frozenset"}
        and len(node.args) == 1
    ):
        node = node.args[0]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "keys":
        node = node.func.value
    return frozenset(tables[node.id]) if isinstance(node, ast.Name) and node.id in tables else None


_TableValue = str | tuple[str | None, ...] | None  # what a table maps a key to, as far as the source says
_MAX_TEXT = 4000  # deprecation messages are cut at 400; longer text isn't worth building
_MAX_ITEMS = 10_000


def _format(template: str, args: list[Any], kwargs: dict[str, Any]) -> str | None:
    """`template.format(*args, **kwargs)` for plain fields (`{}`, `{0}`, `{name}`, `!r`, `!s`, `!a`) whose
    arguments are known strings; None for anything else. A format spec (`{n:>999999999}`) would let untrusted
    source make `str.format` allocate without bound, so none is evaluated."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return None
    out: list[str] = []
    auto = 0
    for literal, field_name, spec, conversion in parsed:
        out.append(literal)
        if field_name is None:
            continue
        if spec or conversion not in {None, "r", "s", "a"}:
            return None
        if field_name == "":
            value, auto = (args[auto] if auto < len(args) else None), auto + 1
        elif field_name.isdecimal():
            value = args[int(field_name)] if int(field_name) < len(args) else None
        elif field_name.isidentifier():
            value = kwargs.get(field_name)
        else:  # `{0.attr}`, `{n[0]}`
            return None
        if not isinstance(value, str):
            return None
        out.append(repr(value) if conversion == "r" else ascii(value) if conversion == "a" else value)
    text = "".join(out)
    return text if len(text) <= _MAX_TEXT else None


def _row(value: Any) -> _TableValue:
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return tuple(v if isinstance(v, str) else None for v in value)
    return None


class _Literals:
    """Module-level names bound to strings and literal sequences, in source order, to read the tables a
    `__getattr__` consults when the module builds them with comprehensions and `str.format` (numpy 1.24-1.26's
    `__deprecated_attrs__`). A value is a string, a tuple of values, or None when the source doesn't say."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def value(self, node: ast.expr, local: dict[str, Any] | None = None) -> Any:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return local[node.id] if local is not None and node.id in local else self.values.get(node.id)
        if isinstance(node, (ast.Tuple, ast.List)):
            if len(node.elts) > _MAX_ITEMS or any(isinstance(e, ast.Starred) for e in node.elts):
                return None
            return tuple(self.value(e, local) for e in node.elts)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    if part.format_spec is not None or part.conversion not in {-1, ord("s")}:
                        return None
                    part = part.value
                text = self.value(part, local)
                if not isinstance(text, str):
                    return None
                parts.append(text)
            text = "".join(parts)
            return text if len(text) <= _MAX_TEXT else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left, local), self.value(node.right, local)
            if isinstance(left, str) and isinstance(right, str) and len(left) + len(right) <= _MAX_TEXT:
                return left + right
            return None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and not any(isinstance(a, ast.Starred) for a in node.args)
            and all(k.arg for k in node.keywords)
            and isinstance(template := self.value(node.func.value, local), str)
        ):
            args = [self.value(a, local) for a in node.args]
            return _format(
                template, args, {k.arg: self.value(k.value, local) for k in node.keywords if k.arg}
            )
        return None

    def bind(self, name: str, node: ast.expr) -> None:
        self.values[name] = self.value(node)

    @staticmethod
    def _unpack(target: ast.expr, item: Any) -> dict[str, Any] | None:
        if isinstance(target, ast.Name):
            return {target.id: item}
        if not (isinstance(target, ast.Tuple) and isinstance(item, tuple) and len(item) == len(target.elts)):
            return None
        local: dict[str, Any] = {}
        for t, v in zip(target.elts, item, strict=True):
            bound = _Literals._unpack(t, v)
            if bound is None:
                return None
            local.update(bound)
        return local

    def table(self, node: ast.expr) -> dict[str, _TableValue] | None:
        """A literal dict, set, list or tuple keyed by strings, or a comprehension over a literal sequence
        (`{n: (alias, _msg.format(n=n, an=an)) for n, alias, an in _type_info}`): key -> what it maps to."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1:
            return self.table(node.args[0]) if node.func.id in {"frozenset", "set", "tuple", "list"} else None
        if isinstance(node, ast.Dict):
            if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
                return None
            return {k.value: _row(self.value(v)) for k, v in zip(node.keys, node.values, strict=True)}  # type: ignore[union-attr]
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
                return None
            return {e.value: None for e in node.elts}  # type: ignore[attr-defined]
        if not isinstance(node, (ast.DictComp, ast.SetComp)) or len(node.generators) != 1:
            return None
        gen = node.generators[0]
        items = self.value(gen.iter)
        if gen.ifs or gen.is_async or not isinstance(items, tuple):
            return None
        out: dict[str, _TableValue] = {}
        for item in items:
            local = self._unpack(gen.target, item)
            key = (
                self.value(node.key if isinstance(node, ast.DictComp) else node.elt, local) if local else None
            )
            if not isinstance(key, str):
                return None
            out[key] = _row(self.value(node.value, local)) if isinstance(node, ast.DictComp) else None
        return out


def _fill_table(tables: dict[str, dict[str, _TableValue]], literals: _Literals, st: ast.stmt) -> bool:
    """Apply `st` if it fills a table in place (`T.update({...})`, `T['k'] = v`, `del T['k']`); False if it
    doesn't. A change the source doesn't spell out leaves the table's keys unknown, so it is dropped."""
    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
        call = st.value
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "update"
            and isinstance(func.value, ast.Name)
            and func.value.id in tables
        ):
            return False
        name = func.value.id
        extra = {} if not call.args else literals.table(call.args[0]) if len(call.args) == 1 else None
        if extra is None or any(k.arg is None for k in call.keywords):  # `T.update(other)`, `T.update(**kw)`
            del tables[name]
        else:
            tables[name].update(extra)
            tables[name].update({k.arg: _row(literals.value(k.value)) for k in call.keywords if k.arg})
        return True
    targets = st.targets if isinstance(st, (ast.Assign, ast.Delete)) else []
    if len(targets) != 1 or not isinstance(sub := targets[0], ast.Subscript):
        return False
    if not (isinstance(sub.value, ast.Name) and sub.value.id in tables):
        return False
    name = sub.value.id
    if not (isinstance(sub.slice, ast.Constant) and isinstance(sub.slice.value, str)):
        del tables[name]
    elif isinstance(st, ast.Assign):
        tables[name][sub.slice.value] = _row(literals.value(st.value))
    else:
        tables[name].pop(sub.slice.value, None)
    return True


@dataclass
class _Shim:
    module: str
    param: str
    tables: dict[str, dict[str, _TableValue]]  # module-level literal collections, by name
    exports: frozenset[str] = frozenset()  # what `__all__` lists, when the source says so statically
    mod: griffe.Module | None = None
    bound: dict[str, str] = field(default_factory=dict)  # module-level imports: local name -> dotted path
    helpers: dict[str, ast.FunctionDef | None] = field(default_factory=dict)

    def helper(self, local: str, bound: dict[str, str]) -> ast.FunctionDef | None:
        """The function a call to `local` runs, when it is defined in this module or imported from one griffe
        loaded (this package's own source)."""
        path = bound.get(local, f"{self.module}.{local}")
        if path not in self.helpers:
            module_path, _, name = path.rpartition(".")
            module = _module_at(self.mod, module_path) if self.mod is not None and module_path else None
            tree = _source_tree(module) if module is not None else None
            body = tree.body if tree is not None else []
            self.helpers[path] = next(
                (st for st in body if isinstance(st, ast.FunctionDef) and st.name == name), None
            )
        return self.helpers[path]


@dataclass
class _Served:
    """What `_reach` finds: names that reach a `return`, each with the statements run on the way there, and
    `return getattr(<module>, name)` catch-alls: (the module's dotted path, names that never get there, trail)."""

    names: dict[str, list[ast.stmt]] = field(default_factory=dict)
    forwards: list[tuple[str, frozenset[str], list[ast.stmt]]] = field(default_factory=list)


def _absolute(module_path: str, is_init: bool, level: int, name: str | None) -> str:
    """The absolute module a `from <level dots><name> import ...` in `module_path` refers to."""
    if level == 0:
        return name or ""
    parts = module_path.split(".")
    parts = parts[: len(parts) - level + (1 if is_init else 0)]
    return ".".join([*parts, name] if name else parts)


def _bind(bound: dict[str, str], st: ast.stmt, module_path: str, is_init: bool) -> None:
    """Add the names an `import` statement binds, each to the dotted path it imports."""
    if isinstance(st, ast.Import):
        for alias in st.names:
            local = alias.asname or alias.name.split(".")[0]
            bound[local] = alias.name if alias.asname else local
    elif isinstance(st, ast.ImportFrom):
        source = _absolute(module_path, is_init, st.level, st.module)
        bound.update({a.asname or a.name: f"{source}.{a.name}" for a in st.names if a.name != "*"})


def _bindings(trail: list[ast.stmt], shim: _Shim) -> dict[str, str]:
    """Import bindings in scope at the end of `trail`: the module's, then the function's own imports."""
    bound = dict(shim.bound)
    is_init = shim.mod is not None and shim.mod.is_init_module
    for st in trail:
        _bind(bound, st, shim.module, is_init)
    return bound


def _forwarded(value: ast.expr, trail: list[ast.stmt], shim: _Shim) -> str | None:
    """The module a `return getattr(<module>, name[, default])` forwards to, also through a variable the trail
    assigned that call to (numpy 2.0's `ret = getattr(numeric, attr_name, sentinel)` ... `return ret`)."""
    if isinstance(value, ast.Name):
        assigned = [
            st.value
            for st in trail
            if isinstance(st, ast.Assign)
            and len(st.targets) == 1
            and isinstance(st.targets[0], ast.Name)
            and st.targets[0].id == value.id
        ]
        if not assigned:
            return None
        value = assigned[-1]
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "getattr"
        and len(value.args) in {2, 3}
        and not value.keywords
        and isinstance(value.args[0], ast.Name)
        and isinstance(value.args[1], ast.Name)
        and value.args[1].id == shim.param
    ):
        return None
    return _bindings(trail, shim).get(value.args[0].id)


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


def _lookup(st: ast.Try, shim: _Shim) -> tuple[frozenset[str], ast.ExceptHandler] | None:
    """`try: x = TABLE[name]` (or `val, msg = TABLE[name]`) `except KeyError: ...`: the table's keys go on to the
    `else` and past the `try` (numpy 1.24-1.26's `__deprecated_attrs__`, django 6.1's `core.mail`, pydantic-ai's
    lazy exports), every other name runs the handler."""
    if len(st.body) != 1 or len(st.handlers) != 1 or st.finalbody:
        return None
    assign, handler = st.body[0], st.handlers[0]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and isinstance(assign.targets[0], (ast.Name, ast.Tuple))
        and isinstance(value := assign.value, ast.Subscript)
        and isinstance(value.value, ast.Name)
        and value.value.id in shim.tables
        and isinstance(value.slice, ast.Name)
        and value.slice.id == shim.param
        and isinstance(handler.type, ast.Name)
        and handler.type.id in {"KeyError", "LookupError"}
    ):
        return None
    return frozenset(shim.tables[value.value.id]), handler


def _raises_when_called(value: ast.expr | None, trail: list[ast.stmt]) -> bool:
    """`return f` for a function the branch defines that does nothing but raise: numpy 1.x warns about its
    expired financial functions and hands back such a dummy, so the name is gone."""
    if not isinstance(value, ast.Name):
        return False
    func = next(
        (
            s
            for s in reversed(trail)
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == value.id
        ),
        None,
    )
    if func is None:
        return False
    body = func.body[1:] if _is_docstring(func.body[0]) else func.body
    return len(body) == 1 and isinstance(body[0], ast.Raise)


def _reach(
    stmts: list[ast.stmt], live: _Names, trail: list[ast.stmt], shim: _Shim, served: _Served
) -> _Names | None:
    """Follow `stmts` for the names in `live`; record each name that reaches a `return`, with the statements run
    on the way there. Returns the names that fall through, or None when none do.

    A `return` that every name but a few reaches serves the names `__all__` lists (langchain 0.3.4's `chains`
    lists its lazy imports only as `list(_module_lookup.keys())`), and, when it returns `getattr(<module>, name)`
    for an imported module, that module's names (numpy 2's `numpy.core.*`); other names it may serve are
    unknowable."""
    for st in stmts:
        if isinstance(st, ast.Return):
            if _raises_when_called(st.value, trail):
                return None
            if live.exclude and st.value is not None and (target := _forwarded(st.value, trail, shim)):
                served.forwards.append((target, live.names, trail))
            for name in shim.exports - live.names if live.exclude else live.names:
                served.names.setdefault(name, trail)
            return None
        if isinstance(st, ast.Raise):
            return None
        if isinstance(st, ast.If):
            tested = _tested(st.test, shim)
            if tested is None:  # a condition we can't read: names may get past it, but none is surely served
                after = _union(
                    _reach(st.body, live, trail, shim, _Served()),
                    _reach(st.orelse, live, trail, shim, _Served()),
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
        if isinstance(st, ast.Try) and (lookup := _lookup(st, shim)) is not None:
            keys, handler = lookup
            after = _union(
                _reach([*st.body, *st.orelse], live & keys, trail, shim, served),
                _reach(handler.body, live - keys, trail, shim, served),
            )
            if after is None:
                return None
            live = after
            continue
        # Other `try`, `with` and `match` bodies aren't followed: their returns don't count.
        trail = [*trail, st]
    return live


def _mapped(node: ast.expr, name: str, shim: _Shim) -> _TableValue:
    """`TABLE[name]` or `TABLE.get(name)`: what TABLE maps `name` to (a rename's new name, a message, or a row
    such as numpy's `(value, message)`)."""
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
    if isinstance(node, ast.Name):  # `warn(msg)` with `msg` looked up in a table (django 6.1's `core.mail`)
        return env.get(node.id)
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


def _deprecation_text(call: ast.Call, env: dict[str, str]) -> str | None:
    """A `warn(...)` call's message, when the text or the category says it is a deprecation."""
    text = _warning_text(call.args[0], "", env)
    category = (
        call.args[1]
        if len(call.args) > 1
        else next((k.value for k in call.keywords if k.arg == "category"), None)
    )
    category_name = ast.unparse(category) if category is not None else ""
    deprecation = DEPRECATION_WORDS_RE.search(text or "") or SHIM_CATEGORY_RE.search(category_name)
    return text.strip()[:400] if text and deprecation else None


_UNKNOWN = "…"  # a value the source doesn't say statically


class _Unreadable(Exception):
    """A helper takes a branch on something its arguments don't decide."""


def _text_value(node: ast.expr | None, env: dict[str, str | None]) -> str | None:
    """A string literal, an f-string or a bound name as text; None for `None`; `_UNKNOWN` for anything else."""
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str)):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id, _UNKNOWN)
    text = _warning_text(node, "", _known(env)) if node is not None else None
    return _UNKNOWN if text is None else text


def _known(env: dict[str, str | None]) -> dict[str, str]:
    return {k: v for k, v in env.items() if v is not None}


def _helper_warning(call: ast.Call, helper: ast.FunctionDef, env: dict[str, str | None]) -> str | None:
    """The deprecation warning a helper's body emits, with its parameters bound to `call`'s arguments: numpy 2's
    `_raise_warning(attr_name, "numeric")`. Follows string assignments and `if <param> is (not) None:`; any other
    branch, or a warning in a nested call, gives nothing."""
    params = helper.args.posonlyargs + helper.args.args
    names = [p.arg for p in params]
    defaults = dict(zip(names[len(names) - len(helper.args.defaults) :], helper.args.defaults, strict=True))
    keywords = {k.arg: k.value for k in call.keywords if k.arg}
    local: dict[str, str | None] = {}
    for i, name in enumerate(names):
        node = call.args[i] if i < len(call.args) else keywords.get(name, defaults.get(name))
        local[name] = _text_value(node, env) if node is not None else _UNKNOWN

    def run(stmts: list[ast.stmt]) -> str | None:
        for st in stmts:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                local[st.targets[0].id] = _text_value(st.value, local)
            elif isinstance(st, ast.If):
                test = st.test
                if not (
                    isinstance(test, ast.Compare)
                    and len(test.ops) == 1
                    and isinstance(test.ops[0], (ast.Is, ast.IsNot))
                    and isinstance(test.left, ast.Name)
                    and local.get(test.left.id, _UNKNOWN) != _UNKNOWN
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value is None
                ):
                    raise _Unreadable
                holds = (local[test.left.id] is None) == isinstance(test.ops[0], ast.Is)
                if (text := run(st.body if holds else st.orelse)) is not None:
                    return text
            elif (warn := _warn_call(st)) is not None:
                return _deprecation_text(warn, _known(local))
        return None

    try:
        return run(helper.body)
    except _Unreadable:
        return None


def _shim_message(trail: list[ast.stmt], name: str, shim: _Shim) -> str | None:
    """The deprecation warning a shim's branch emits for `name`, with the f-string filled in where the source
    says what goes there (the name itself, the module, a rename table's target). A call to a helper of the same
    package whose body warns counts as that warning."""
    env = {shim.param: name, "__name__": shim.module}
    for st in trail:
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            target, mapped = st.targets[0], _mapped(st.value, name, shim)
            if isinstance(target, ast.Name) and isinstance(mapped, str):
                env[target.id] = mapped
            elif (
                isinstance(target, ast.Tuple)
                and isinstance(mapped, tuple)
                and len(mapped) == len(target.elts)
            ):
                env.update(
                    {
                        t.id: v
                        for t, v in zip(target.elts, mapped, strict=True)
                        if isinstance(t, ast.Name) and v
                    }
                )
        elif call := _warn_call(st):
            return _deprecation_text(call, env)
        elif (
            isinstance(st, ast.Expr)
            and isinstance(st.value, ast.Call)
            and isinstance(st.value.func, ast.Name)
            and (helper := shim.helper(st.value.func.id, _bindings(trail, shim)))
            and (text := _helper_warning(st.value, helper, dict(env))) is not None
        ):
            return text
    return None


def _find_getattr(stmts: list[ast.stmt]) -> tuple[ast.FunctionDef, list[ast.stmt]] | None:
    """The module's `__getattr__`, and the module-level statements in its scope: the top level, with the block
    it is defined in spliced in (numpy 1.x defines it, and the tables it reads, inside an `if`/`else`)."""
    for i, st in enumerate(stmts):
        if isinstance(st, ast.FunctionDef) and st.name == "__getattr__":
            return st, stmts
        if isinstance(st, (ast.If, ast.Try)):
            for block in (st.body, st.orelse):
                if found := _find_getattr(block):
                    return found[0], [*stmts[:i], *found[1], *stmts[i + 1 :]]
    return None


def _source_tree(mod: griffe.Module) -> ast.Module | None:
    filepath = mod.filepath
    if not isinstance(filepath, Path) or filepath.suffix != ".py":
        return None
    try:
        return ast.parse(filepath.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None


@dataclass
class _Forward:
    """A catch-all `return getattr(<module>, name)`: every public name of `module` but `excluded` is served."""

    module: str
    excluded: frozenset[str]
    trail: list[ast.stmt]
    shim: _Shim

    def message(self, name: str) -> str | None:
        return _shim_message(self.trail, name, self.shim)


@dataclass
class _GetattrShim:
    """`names`: each served name with its deprecation message. `bound`: what the `.py` binds itself, so never
    reaches `__getattr__` (None: unknowable, after a star import). `copied`: names a loop binds through
    `globals()[n] = ...`, which griffe can't see. `deleted`: names the module binds and then `del`s, which griffe
    still lists (numpy 1.25's `import math` ... `del math`)."""

    names: dict[str, str | None] = field(default_factory=dict)
    forwards: list[_Forward] = field(default_factory=list)
    bound: frozenset[str] | None = None
    copied: frozenset[str] = frozenset()
    deleted: frozenset[str] = frozenset()


def _copies_into_globals(loop: ast.For) -> bool:
    return isinstance(loop.target, ast.Name) and any(
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "globals"
        for node in ast.walk(loop)
    )


def _module_names(body: list[ast.stmt]) -> tuple[frozenset[str] | None, frozenset[str]]:
    """The names a module's top level binds (None after a star import: unknowable), and the literal names a loop
    copies in through `globals()` (numpy 2's `numpy.core.multiarray`, for old pickles)."""
    bound: set[str] = set()
    copied: set[str] = set()
    star = False
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(st.name)
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            star |= any(a.name == "*" for a in st.names)
            bound.update(a.asname or a.name.split(".")[0] for a in st.names if a.name != "*")
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            bound.update(n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
        elif (
            isinstance(st, ast.For)
            and _copies_into_globals(st)
            and (keys := _string_keys(st.iter)) is not None
        ):
            copied.update(keys)
        if isinstance(st, (ast.If, ast.Try, ast.With)):
            nested = [*st.body, *getattr(st, "orelse", []), *getattr(st, "finalbody", [])]
            nested += [s for h in getattr(st, "handlers", []) for s in h.body]
            inner, inner_copied = _module_names(nested)
            star |= inner is None
            bound |= inner or set()
            copied |= inner_copied
    return (None if star else frozenset(bound)), frozenset(copied)


def getattr_served(mod: griffe.Module) -> _GetattrShim:
    """Names a module `__getattr__` returns a value for (deprecation shims, lazy imports), each with the
    deprecation message its branch warns with, and the modules it forwards every other name to. Read from the
    source with `ast`; nothing is imported (ADR 0002).

    Only names compared with literals, looked up in a module-level table (a literal, or a comprehension over one,
    filled in place with `.update()` and `T[k] = v`), or listed in a static `__all__` are found. A name whose
    branch raises (numpy 2.0's expired attributes), or returns a dummy that raises, is not served.
    """
    tree = _source_tree(mod)
    if tree is None:
        return _GetattrShim()
    found = _find_getattr(tree.body)
    if found is None:
        return _GetattrShim()
    func, scope = found
    args = func.args.posonlyargs + func.args.args
    if not args:
        return _GetattrShim()
    tables: dict[str, dict[str, _TableValue]] = {}
    exports: frozenset[str] = frozenset()
    bound: dict[str, str] = {}
    deleted: set[str] = set()
    literals = _Literals()
    for st in scope:
        _bind(bound, st, mod.path, mod.is_init_module)
        if _fill_table(tables, literals, st):
            continue
        if isinstance(st, ast.Delete):
            deleted.update(t.id for t in st.targets if isinstance(t, ast.Name))
            continue
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            target, value = st.targets[0], st.value
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            target, value = st.target, st.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        deleted.discard(target.id)
        if target.id == "__all__":
            exports = _table_keys(value, tables) or frozenset()
        elif (keys := literals.table(value)) is not None:
            tables[target.id] = keys
        else:
            tables.pop(target.id, None)
        literals.bind(target.id, value)
    shim = _Shim(mod.path, args[0].arg, tables, exports, mod, bound)
    served = _Served()
    _reach(func.body, _Names(frozenset(), exclude=True), [], shim, served)
    names = {
        name: _shim_message(trail, name, shim)
        for name, trail in sorted(served.names.items())
        if name.isidentifier()
        and (not name.startswith("_") or (name.startswith("__") and name.endswith("__")))
    }
    if not served.forwards:
        return _GetattrShim(names, deleted=frozenset(deleted))
    own, copied = _module_names(tree.body)
    forwards = [_Forward(target, excluded, trail, shim) for target, excluded, trail in served.forwards]
    return _GetattrShim(names, forwards, own, copied, frozenset(deleted))


@dataclass(frozen=True)
class _Imports:
    """How a module's top-level `from x import y` statements bind names. An import under `if TYPE_CHECKING:` or
    `try:` may not exist at runtime, so only top-level statements count."""

    redundant: frozenset[str]  # `from x import y as y`, the explicit re-export convention
    plain: frozenset[str]  # `from x import y`, with x outside the standard library
    imports_only: bool  # nothing but imports and a docstring, like sqlalchemy 1.4's `schema.py`


def _is_docstring(st: ast.stmt) -> bool:
    return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


def module_imports(mod: griffe.Module) -> _Imports:
    tree = _source_tree(mod)
    body = tree.body if tree is not None else []
    redundant: set[str] = set()
    plain: set[str] = set()
    for st in body:
        if isinstance(st, ast.ImportFrom):
            stdlib = st.level == 0 and (st.module or "").split(".")[0] in STDLIB_MODULES
            redundant.update(a.name for a in st.names if a.asname == a.name)
            plain.update(a.name for a in st.names if a.asname is None and a.name != "*" and not stdlib)
    imports_only = bool(body) and all(
        isinstance(st, (ast.Import, ast.ImportFrom)) or _is_docstring(st) for st in body
    )
    return _Imports(frozenset(redundant), frozenset(plain), imports_only)


# `__all__` built by code rather than written out: a method call, `+=`, or a value that isn't a literal.
_ALL_IN_CODE_RE = re.compile(r"__all__\s*(?:\.\s*\w+\s*\(|\+=|=\s*(?![\s\[(=]))")


@dataclass(frozen=True)
class _Exports:
    names: frozenset[str] | None  # None: the source never binds `__all__`
    stars: tuple[str, ...]  # modules the source star-imports, in order


def _module_at(mod: griffe.Module, path: str) -> griffe.Module | None:
    try:
        found = _resolve(mod.modules_collection.get_member(path))
    except (KeyError, ValueError, griffe.AliasResolutionError, griffe.CyclicAliasError):
        return None
    return found if found is not None and found.is_module else None  # type: ignore[return-value]


def _appends_module_all(func: ast.FunctionDef) -> bool:
    """numpy's `extend_all(module)`: a one-argument helper that appends that module's `__all__` to `__all__`."""
    args = func.args.posonlyargs + func.args.args
    if len(args) != 1:
        return False
    param = args[0].arg
    appends = reads = False
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (
                isinstance(owner, ast.Name)
                and owner.id == "__all__"
                and node.func.attr in {"append", "extend"}
            ):
                appends = True
        if isinstance(node, ast.Attribute) and node.attr == "__all__":
            reads |= isinstance(node.value, ast.Name) and node.value.id == param
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            reads |= (
                len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == param
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "__all__"
            )
    return appends and reads


class _RuntimeAll:
    """The names a module's `.py` source surely puts in `__all__` at runtime, read with `ast` (DT-DATA-25).

    griffe reads a literal `__all__` and `+=` of literals or of another module's `__all__`, but a stub's bare
    `__all__: list[str]` replaces that with an empty list, and `.extend()`, `.append()`, helpers and computed
    values aren't read at all. numpy builds its `__all__` with all of these, so `numpy.product` (1.25 to 1.26) and
    most of `numpy.core` were missing. This follows literals, `+`, `|`, `+=`, `.extend/.append/.insert/.remove`,
    `list/set/sorted/tuple(...)`, `.copy()`, `[:]`, another module's `__all__` (`fields.__all__`, or django's
    `from .fields import __all__ as fields_all`) and a helper like numpy's `extend_all(module)`. Anything else adds
    nothing, so the result is a lower bound: callers add these names to what is already public and never take
    any away. django's `__all__ = aggregates_all + fields_all + ...` left `django.db.models.CharField` missing.
    """

    def __init__(self) -> None:
        self.cache: dict[str, _Exports] = {}
        self.active: set[str] = set()

    @staticmethod
    def needed(mod: griffe.Module) -> bool:
        """Whether griffe may have misread `mod.__all__`: a stub replaced it, or code builds it."""
        path = mod.filepath
        if not isinstance(path, Path) or path.suffix != ".py":
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "__all__" in text and (path.with_suffix(".pyi").exists() or bool(_ALL_IN_CODE_RE.search(text)))

    def of(self, mod: griffe.Module) -> _Exports:
        if mod.path in self.cache:
            return self.cache[mod.path]
        if mod.path in self.active:
            return _Exports(frozenset(), ())  # an import cycle: assume nothing
        self.active.add(mod.path)
        try:
            tree = _source_tree(mod)
            found = _AllReader(self, mod).read(tree.body) if tree is not None else _Exports(None, ())
        finally:
            self.active.discard(mod.path)
        self.cache[mod.path] = found
        return found


class _AllReader:
    def __init__(self, runtime: _RuntimeAll, mod: griffe.Module):
        self.runtime = runtime
        self.mod = mod
        self.bound: dict[str, str] = {}  # local name -> the dotted path an import bound it to
        self.lists: dict[str, set[str]] = {}  # module-level names bound to a list of strings
        self.helpers: set[str] = set()
        self.names: set[str] | None = None
        self.stars: list[str] = []

    def read(self, body: list[ast.stmt]) -> _Exports:
        self.block(body)
        return _Exports(None if self.names is None else frozenset(self.names), tuple(self.stars))

    def block(self, body: list[ast.stmt]) -> None:
        for st in body:
            self.statement(st)

    def statement(self, st: ast.stmt) -> None:
        if isinstance(st, ast.If):  # numpy builds `__all__` in the `else:` of `if __NUMPY_SETUP__:`
            before = None if self.names is None else set(self.names)
            self.block(st.body)
            after_body, self.names = self.names, None if before is None else set(before)
            self.block(st.orelse)
            if after_body != before and self.names != before:  # both branches change it: either may run
                self.names = (after_body or set()) & (self.names or set())
            elif after_body != before:
                self.names = after_body
        elif isinstance(st, ast.Try):
            self.block(st.body + st.orelse + st.finalbody)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            self.block(st.body)
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            _bind(self.bound, st, self.mod.path, self.mod.is_init_module)
            if isinstance(st, ast.ImportFrom) and any(a.name == "*" for a in st.names):
                self.stars.append(_absolute(self.mod.path, self.mod.is_init_module, st.level, st.module))
        elif isinstance(st, ast.FunctionDef) and _appends_module_all(st):
            self.helpers.add(st.name)
        elif isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None:
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    self.names = self.value(st.value)
                elif isinstance(target, ast.Name):
                    self.lists[target.id] = self.value(st.value)
        elif isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name) and st.target.id == "__all__":
            if isinstance(st.op, (ast.Add, ast.BitOr)):
                self.names = (self.names or set()) | self.value(st.value)
            else:
                self.names = set()
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            self.call(st.value)
        elif isinstance(st, (ast.For, ast.While, ast.Delete)):
            # A loop may add names we can't read (fine for a lower bound), but one that removes them isn't.
            touches = any(isinstance(node, ast.Name) and node.id == "__all__" for node in ast.walk(st))
            if touches and self.names is not None and not self._only_appends(st):
                self.names = set()

    @staticmethod
    def _only_appends(st: ast.stmt) -> bool:
        return not isinstance(st, ast.Delete) and not any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "__all__"
            and n.func.attr not in {"append", "extend", "insert"}
            for n in ast.walk(st)
        )

    def call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "__all__"
        ):
            names = self.names if self.names is not None else set()
            if func.attr == "extend" and len(node.args) == 1:
                names |= self.value(node.args[0])
            elif func.attr in {"append", "insert"} and node.args:
                names |= self.value(node.args[-1]) if isinstance(node.args[-1], ast.Constant) else set()
            elif func.attr in {"remove", "discard"} and len(node.args) == 1:
                names -= self.value(node.args[0])
            elif func.attr in {"pop", "clear"}:
                names = set()
            self.names = names
        elif isinstance(func, ast.Name) and func.id in self.helpers and len(node.args) == 1:
            self.names = (self.names or set()) | self.value(
                ast.Attribute(node.args[0], "__all__", ast.Load())
            )

    def module_of(self, node: ast.expr) -> griffe.Module | None:
        if isinstance(node, ast.Name) and node.id in self.bound:
            return _module_at(self.mod, self.bound[node.id])
        if isinstance(node, ast.Attribute) and (base := self.module_of(node.value)) is not None:
            return _module_at(self.mod, f"{base.path}.{node.attr}")
        return None

    def value(self, node: ast.expr) -> set[str]:
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return set().union(*(self.value(e.value if isinstance(e, ast.Starred) else e) for e in node.elts))
        if isinstance(node, ast.Name):
            if node.id == "__all__":
                return set(self.names or ())
            bound = self.bound.get(node.id, "")
            if bound.endswith(".__all__"):  # `from .aggregates import __all__ as aggregates_all` (django)
                source = _module_at(self.mod, bound.removesuffix(".__all__"))
                return set(self.runtime.of(source).names or ()) if source is not None else set()
            return set(self.lists.get(node.id, ()))
        if isinstance(node, ast.Attribute) and node.attr == "__all__":
            source = self.module_of(node.value)
            return set(self.runtime.of(source).names or ()) if source is not None else set()
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
            return self.value(node.left) | self.value(node.right)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            return self.value(node.value) if node.slice.lower is None and node.slice.upper is None else set()
        if isinstance(node, ast.Call):
            func = node.func
            conversion = {"list", "set", "sorted", "tuple", "frozenset"}
            if (
                isinstance(func, ast.Name)
                and func.id in conversion
                and len(node.args) == 1
                and not node.keywords
            ):
                return self.value(node.args[0])
            if isinstance(func, ast.Attribute) and func.attr == "copy" and not node.args:
                return self.value(func.value)
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "__all__"
            ):
                return self.value(ast.Attribute(node.args[0], "__all__", ast.Load()))
        return set()


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
        self.imports: dict[str, _Imports] = {}
        self.runtime_all = _RuntimeAll()
        self.exported: dict[str, frozenset[str]] = {}

    def _internal(self, canonical: str) -> bool:
        return any(canonical == r or canonical.startswith(r + ".") for r in self.roots)

    def record(self, rec: SymbolRecord) -> None:
        self.snap.symbols.setdefault(rec.path, rec)

    def _exported(self, mod: griffe.Module) -> frozenset[str]:
        """Names `mod`'s runtime `__all__` lists, where griffe may have read it wrong (DT-DATA-25)."""
        if mod.path not in self.exported:
            names = self.runtime_all.of(mod).names if _RuntimeAll.needed(mod) else None
            self.exported[mod.path] = frozenset(n for n in names or () if n.isidentifier())
        return self.exported[mod.path]

    def _star_exports(self, mod: griffe.Module, name: str) -> bool:
        """Whether `from mod import *` binds `name`."""
        names = self.runtime_all.of(mod).names
        if names is not None:
            return name in names
        return not name.startswith("_") and name in mod.members  # without `__all__`, imports count too

    def _from_stars(
        self, mod: griffe.Module, name: str, seen: frozenset[str] = frozenset()
    ) -> griffe.Object | griffe.Alias | str | None:
        """What `mod`'s star imports bind `name` to, the last one first, as Python does: the member griffe has,
        or for a star import of code griffe can't read (compiled, or another distribution) that module's path."""
        unread = None
        for source in reversed(self.runtime_all.of(mod).stars):
            target = _module_at(mod, source)
            if target is None:
                unread = unread or f"{source}.{name}"
                continue
            if target.path in seen or not self._star_exports(target, name):
                continue
            member = target.members.get(name)
            if member is not None:
                return member
            found = self._from_stars(target, name, seen | {mod.path})
            if found is not None:
                return found
        return unread

    def module(self, mod: griffe.Module, public_path: str, depth: int = 0, forwarded: bool = False) -> None:
        key = (mod.path, public_path)
        if key in self.visiting or depth > MAX_DEPTH:
            return
        self.visiting.add(key)
        self.record(SymbolRecord(public_path, "module", mod.path))
        # A module another one forwards to is public as at its own path, but its submodules aren't walked again.
        reexporter = mod.path if (public_path == mod.path or forwarded) and mod.is_init_module else None
        walk_from = None if forwarded else reexporter
        exported = self._exported(mod)
        for name, member in list(mod.members.items()):
            if member.is_alias and member.wildcard:  # type: ignore[union-attr]  # a star import griffe couldn't expand
                continue
            if name in exported or self._public(mod, name, member, reexporter):
                self.member(member, f"{public_path}.{name}", depth, walk_from)
        for name in sorted(exported - mod.members.keys()):
            found = self._from_stars(mod, name)
            if isinstance(found, str):
                self.record(SymbolRecord(f"{public_path}.{name}", "alias", found))
            elif found is not None:
                self.member(found, f"{public_path}.{name}", depth, walk_from)
        hook = mod.members.get("__getattr__")
        if hook is not None and not hook.is_alias:
            if mod.path not in self.shims:
                self.shims[mod.path] = getattr_served(mod)
            shim = self.shims[mod.path]
            for forward in shim.forwards:
                self.forward(mod, public_path, depth, forward, shim)
            canonical = f"{mod.path}.__getattr__"
            for name, message in shim.names.items():
                # A real attribute never reaches __getattr__; one the module deletes again does.
                if name not in mod.members or name in shim.deleted:
                    self.record(SymbolRecord(f"{public_path}.{name}", "getattr", canonical, None, message))

    def forward(
        self, mod: griffe.Module, public_path: str, depth: int, forward: _Forward, shim: _GetattrShim
    ) -> None:
        """Record what a `return getattr(<module>, name)` catch-all serves: that module's public names, walked at
        `mod`'s path with their own kinds and signatures, each with the shim's warning (DT-DATA-25: numpy 2.0
        renamed `numpy.core` to `numpy._core`, and 1,198 names under it looked removed).

        Names the `.py` binds itself, and its submodules, never reach `__getattr__`. A name only its stub declares
        does, and gets the warning too (numpy 2.5's `numpy/core/numeric.pyi` star-imports `numpy._core.numeric`).
        A forwarded class's members aren't recorded: griffe resolves numpy's private `_core` stubs differently
        from one release to the next (an alias cycle on `ndarray` from 2.2, scalar types stubbed only from 2.2 and
        2.4), and ~3,800 members came and went with it.
        """
        target = _module_at(mod, forward.module)
        if target is None or target.path == mod.path:
            return  # compiled (`numpy._core._multiarray_umath`), another distribution, or itself
        sub = copy.copy(self)  # shares the caches and `visiting`; collects into a snapshot of its own
        sub.snap = ApiSnapshot(self.snap.package, self.snap.version, self.snap.import_names)
        sub.module(target, public_path, depth + 1, forwarded=True)
        self.snap.errors.extend(sub.snap.errors)
        real = shim.bound if shim.bound is not None else frozenset(mod.members)
        real |= {n for n, m in mod.members.items() if not m.is_alias and m.is_module}
        prefix = f"{public_path}."
        for path, rec in sub.snap.symbols.items():
            if not path.startswith(prefix):
                continue
            name, _, below = path.removeprefix(prefix).partition(".")
            if below or name in forward.excluded or (name in real and name not in shim.copied):
                continue
            message = None if name in shim.copied else forward.message(name)
            existing = self.snap.symbols.get(path)
            if existing is None:
                self.record(replace(rec, deprecated=rec.deprecated or message))
            elif message and existing.deprecated is None:
                self.snap.symbols[path] = replace(existing, deprecated=message)

    def _public(
        self, mod: griffe.Module, name: str, member: griffe.Object | griffe.Alias, reexporter: str | None
    ) -> bool:
        """griffe's rule, plus imports that re-export when `__all__` is missing (or computed at runtime, as in
        numpy).

        griffe counts imported names as private, but `from .app import Flask as Flask` is how flask, fastapi,
        click and sqlalchemy expose their API. When an `__init__.pyi` redefines an imported name (numpy 2.0's
        `float64`, `add`), griffe merges the stub and the name is no longer an alias, so the `.py` import counts.

        In any module, `from x import y as y` re-exports (typer's click API, sqlalchemy 2's `schema.py`), and so
        does every import of a module that is nothing but imports (DT-DATA-24). Otherwise a plain module's
        imports are its implementation. A package's `__init__` also re-exports what it imports from the package
        itself, and its top-level `__init__` what it imports from another distribution (DT-DATA-21: flask 2.2's
        markupsafe `escape`); a subpackage importing pydantic's `BaseModel` (chromadb.server.fastapi) uses it.
        """
        try:
            if member.is_public:
                return True
        except (griffe.AliasResolutionError, griffe.CyclicAliasError):
            return False
        source = member.target_path if member.is_alias else mod.imports.get(name)  # type: ignore[union-attr]
        if source is None or mod.exports or name.startswith("_"):
            return False
        if mod.path not in self.imports:
            self.imports[mod.path] = module_imports(mod)
        shapes = self.imports[mod.path]
        if name in shapes.redundant or (shapes.imports_only and name in shapes.plain):
            return True
        if reexporter is None:
            return False
        return self._internal(source) or (mod.path in self.roots and name in shapes.plain)

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


def _in_type_checking_else(node: ast.AST) -> bool:
    """Whether `node` sits in the `else:` of an `if TYPE_CHECKING:`, the branch that runs."""
    child, parent = node, getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, ast.If) and ast.unparse(parent.test) in {
            "TYPE_CHECKING",
            "typing.TYPE_CHECKING",
        }:
            return any(child is st for st in parent.orelse)
        child, parent = parent, getattr(parent, "parent", None)
    return False


class _RuntimeElse(griffe.Extension):
    """griffe marks the whole `if TYPE_CHECKING:` statement as type-checking only, `else:` included, and `import *`
    skips names that aren't available at runtime. pydantic 1.x's `StrictBool` and 2.x's `NaiveDatetime`, defined
    for type checkers and again under `else:`, were missing from the package that star-imports them (DT-DATA-26)."""

    def on_instance(self, *, node: ast.AST | griffe.ObjectNode, obj: griffe.Object, **kwargs: Any) -> None:
        if not obj.runtime and isinstance(node, ast.AST) and _in_type_checking_else(node):
            obj.runtime = True

    def on_alias_instance(
        self, *, node: ast.AST | griffe.ObjectNode, alias: griffe.Alias, **kwargs: Any
    ) -> None:
        if not alias.runtime and isinstance(node, ast.AST) and _in_type_checking_else(node):
            alias.runtime = True


_NAMED_KINDS = frozenset(
    {
        griffe.ParameterKind.positional_only,
        griffe.ParameterKind.positional_or_keyword,
        griffe.ParameterKind.keyword_only,
    }
)


def _annotation_union(params: list[griffe.Parameter]) -> str | griffe.Expr | None:
    """The overloads' distinct annotations joined with `|`, as griffe annotates an implementation from its overloads:
    an unannotated `.py` implementation this stub function is merged into gets the annotations it had before."""
    distinct: list[griffe.Expr] = []
    for p in params:
        if isinstance(p.annotation, griffe.Expr) and all(str(p.annotation) != str(a) for a in distinct):
            distinct.append(p.annotation)
    if len(distinct) < 2:
        return distinct[0] if distinct else None
    union: griffe.Expr = distinct[0].modernize()
    for right in distinct[1:]:
        union = griffe.ExprBinOp(left=union, operator="|", right=right.modernize())
    return union


def _merged_parameters(overloads: list[griffe.Function]) -> griffe.Parameters:
    """One signature that accepts every overload's call, as the implementation a stub leaves out must.

    Names keep the order they first appear in. A parameter is required only when every overload requires it, and
    takes both positional and keyword arguments when overloads pass it both ways.
    """
    by_name: dict[str, list[griffe.Parameter]] = {}
    for overload in overloads:
        for param in overload.parameters:
            by_name.setdefault(param.name, []).append(param)
    merged = []
    for name, params in by_name.items():
        named = {p.kind for p in params} & _NAMED_KINDS
        if len(named) == 1:
            kind = next(iter(named))
        elif named:
            kind = griffe.ParameterKind.positional_or_keyword
        else:
            kind = params[0].kind  # `*args` or `**kwargs` in every overload that has it
        required = len(params) == len(overloads) and all(
            p.default is None and p.kind in _NAMED_KINDS for p in params
        )
        default = None
        if not required and kind in _NAMED_KINDS:
            default = next((p.default for p in params if p.default is not None), "...")
        merged.append(
            griffe.Parameter(name, annotation=_annotation_union(params), kind=kind, default=default)
        )
    return griffe.Parameters(*merged)


class _OverloadsOnly(griffe.Extension):
    """griffe keeps `@overload` signatures aside until the implementation follows, and a stub has none: numpy 2's
    `ndarray.reshape`, `astype` and `itemset` never became members, and merging the stub dropped them (DT-DATA-29).
    Each such name becomes a function with the signature `_merged_parameters` gives. It is deprecated when every
    overload is: one deprecated call form isn't the function's deprecation."""

    def on_members(self, *, obj: griffe.Object, **kwargs: Any) -> None:
        if not isinstance(obj, (griffe.Module, griffe.Class)):
            return
        for name, overloads in list(obj.overloads.items()):
            if not overloads or name in obj.members:
                continue
            func = griffe.Function(
                name,
                lineno=overloads[0].lineno,
                endlineno=overloads[-1].endlineno,
                runtime=any(o.runtime for o in overloads),
                docstring=next((o.docstring for o in overloads if o.docstring), None),
                analysis="static",
                parameters=_merged_parameters(overloads),
            )
            messages = [deprecation_message(o) for o in overloads]
            if all(messages):
                func.deprecated = messages[0]
            func.overloads = overloads
            obj.set_member(name, func)
            del obj.overloads[name]


def _clear_griffe_caches() -> None:
    """Empty griffe's module-level memo caches. They hold loaded objects, and through their parents the whole
    package, for the life of the process: the dataclasses extension's `_dataclass_parameters` kept one package
    tree per release built (DT-DATA-18)."""
    for name, module in list(sys.modules.items()):
        if name == "griffe" or name.startswith("griffe."):
            for value in list(vars(module).values()):
                if not isinstance(value, type) and callable(getattr(value, "cache_clear", None)):
                    value.cache_clear()


def build_snapshot(package: str, version: str, import_names: list[str], search_path: Path) -> ApiSnapshot:
    snap = ApiSnapshot(package=package, version=version, import_names=list(import_names))
    walker = _Walker(snap, tuple(import_names))
    try:
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
                    extensions=griffe.load_extensions(_RuntimeElse(), _OverloadsOnly()),
                )
            except Exception as exc:  # report and continue: one bad import name must not sink the release
                snap.errors.append(f"load {import_name}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(loaded, griffe.Module):
                walker.module(loaded, import_name)
            else:
                walker.member(loaded, import_name, 0)
    finally:
        _clear_griffe_caches()
    return snap
