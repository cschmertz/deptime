"""Static API snapshot of one release, extracted with griffe (no code is imported or executed).

A snapshot maps every *public import path* (what users type: `httpx.Client`, not `httpx._client.Client`)
to its kind, canonical definition path, parameters, and deprecation message, if any.
"""

from __future__ import annotations

import ast
import copy
import gzip
import json
import keyword
import os
import re
import string
import sys
import textwrap
import warnings
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import griffe
import griffe._internal.finder as griffe_finder

from deptime.ingest.replacements import (
    DEPRECATION_WORDS_RE,
    deprecates_argument,
    deprecates_name,
    names_argument,
    worded_deprecated,
)

SNAPSHOT_FORMAT = 21  # bump when extraction changes: cached snapshots in another format are rebuilt
MAX_DEPTH = 12  # guards pathological re-export cycles

DEPRECATED_DECORATOR_RE = re.compile(r"^(?:[\w.]+\.)?deprecated\b(?:\(.*\))?$", re.S)
STRING_LITERAL_RE = re.compile(r"""(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)""")
# One or more adjacent single-line literals (implicit concatenation may span lines via the \s* gap).
ADJACENT_LITERALS_RE = re.compile(r"""(?:f?(["'])(?:\\.|(?!\1).)*\1\s*)+""")
WARNING_CATEGORY_RE = re.compile(
    r"\b(?:DeprecationWarning|PendingDeprecationWarning|FutureWarning|\w*Deprecat\w*Warning)\b"
)
# A warning category that marks a deprecation, including django's RemovedInDjango50Warning and marshmallow's
# RemovedInMarshmallow4Warning (DT-DATA-36).
DEPRECATION_CATEGORY_RE = re.compile(rf"{WARNING_CATEGORY_RE.pattern}|\bRemovedIn\w*Warning\b")
# The category of one `warn(...)` call (DT-DATA-42): also subclasses whose names don't end in "Warning", such as
# pydantic's `PydanticDeprecatedSince20` and langgraph's `LangGraphDeprecatedSinceV10`.
CALL_CATEGORY_RE = re.compile(r"deprecat|\bFutureWarning\b|\bRemovedIn\w*Warning\b", re.I)
UNRECOVERABLE = "deprecated (warning emitted; message not statically recoverable)"
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")  # an f-string's `{expression}`
# `warn(KEYWORD_MSG_ARG.format(...))`: a warning kept in a module constant (pytest's `_pytest/deprecated.py`).
WARN_CONSTANT_RE = re.compile(r"\bwarn\(\s*[A-Z][A-Z0-9_]+\b")
# How many calls down an argument's deprecation is followed (DT-DATA-39): httpx's `Client.__init__` passes `cert` to
# `_init_transport`, which passes it to `HTTPTransport`, which passes it to `create_ssl_context`, which warns.
PASS_DEPTH = 3
# Builds run on the pinned interpreter (.python-version), so this list doesn't vary between index builds.
# distutils left the stdlib in 3.12; typing_extensions is a backport of it.
STDLIB_MODULES = frozenset(sys.stdlib_module_names) | {"distutils", "typing_extensions"}


@dataclass
class SymbolRecord:
    path: str
    kind: str  # module | class | function | attribute | type alias | alias (unresolved/external) | getattr (module __getattr__)
    canonical: str | None = None
    params: list[dict[str, Any]] | None = None
    deprecated: str | None = None  # the whole symbol's deprecation
    # Arguments a warning deprecates, with its text (DT-DATA-42): named parameters, or names taken out of **kwargs.
    deprecated_params: dict[str, str] | None = None


# The kind of a class's own member that hides the same name in its bases but isn't recorded: an alias griffe can't
# resolve, or an object that is neither function, attribute nor class. Looking the name up stops there, as griffe's
# `all_members` does.
HIDDEN = "hidden"


@dataclass
class ClassRecord:
    """One class, for a package stored with its classes' own members (DT-DATA-16)."""

    path: str  # canonical
    # The classes it inherits from, canonical, in method resolution order. None for a class recorded only as
    # another class's base.
    mro: list[str] | None = None
    # Its own members by name, each with `path` = the class's canonical path + "." + name. The same names as a
    # public class's members: public names, `__init__` and `__call__`.
    members: dict[str, SymbolRecord] = field(default_factory=dict)
    # Its own members that aren't public API (DT-DATA-46), kind and canonical path only: its private names, and for
    # a class that is neither public nor a public class's base, every member. Dunders other than `__init__` and
    # `__call__` aren't recorded.
    internal: dict[str, SymbolRecord] = field(default_factory=dict)
    # Its direct bases from outside the package, canonical, in the order the class lists them (DT-CORE-32):
    # `fastapi.FastAPI` inherits `mount` from `starlette.applications.Starlette`, which `mro` can't list.
    external: list[str] = field(default_factory=list)


def resolve_member(classes: dict[str, ClassRecord], cls: str, name: str) -> SymbolRecord | None:
    """`name` on the class `cls` as Python finds it: the class's own member, else the first class in its MRO that
    has one. None when no class has it, or when the one that does is `HIDDEN`."""
    rec = classes.get(cls)
    if rec is None:
        return None
    for owner in [cls, *(rec.mro or ())]:
        found = classes.get(owner)
        member = found.members.get(name) if found is not None else None
        if member is not None:
            return None if member.kind == HIDDEN else member
    return None


def member_names(classes: dict[str, ClassRecord], cls: str) -> list[str]:
    """The names `cls` has members under, own or inherited, `HIDDEN` ones included, in griffe's order."""
    rec = classes.get(cls)
    if rec is None:
        return []
    names: dict[str, None] = {}
    for owner in [*reversed(rec.mro or ()), cls]:
        found = classes.get(owner)
        names.update(dict.fromkeys(found.members if found is not None else ()))
    return list(names)


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
    # Modules below which folders without `__init__.py` weren't loaded (coverage.toml, DT-DATA-23).
    namespace_skip: list[str] = field(default_factory=list)
    # The analysed artifact's `Requires-Dist`, normalized (DT-DATA-14); None when unknown, and in older snapshots.
    requires: list[str] | None = None
    # Its `Project-URL` entries, label to URL (DT-CORE-29); None when unknown, and in older snapshots.
    project_urls: dict[str, str] | None = None
    # Names modules bind that aren't public API (DT-DATA-22), by path: private names, a module's imports from
    # outside the standard library, and everything in private modules. Kind and canonical path only, plus the
    # deprecation a module `__getattr__` serves a private name with. Never also in `symbols`.
    internal: dict[str, SymbolRecord] = field(default_factory=dict)
    # Stored with each class's own members (coverage.toml `own_members`, DT-DATA-16): `symbols` then has no class
    # members, and `classes` has, by canonical path, every class a public class is, with its MRO, and every class
    # it inherits from. `expanded` gives the members under each public path.
    own_members: bool = False
    classes: dict[str, ClassRecord] = field(default_factory=dict)
    # Its namespace packages, folders without `__init__.py` an import root included (DT-CORE-32): other
    # distributions can install modules into them, as langgraph-checkpoint does into `langgraph`.
    namespaces: list[str] = field(default_factory=list)
    # Paths importable as a module though the index records another object there, path -> module: those made by
    # `sys.modules[...] = ...` (DT-DATA-52: fastmcp 3.2-3.4 kept `fastmcp.tools.tool` importable as
    # `fastmcp.tools.base`), and a submodule whose name its package rebinds (DT-DATA-54: `instructor.patch`).
    module_aliases: dict[str, str] = field(default_factory=dict)
    # Other distributions' artifacts analysed with the release because it star-imports them (DT-DATA-56: mcp 2.x's
    # `mcp-types`, pinned at the same version). Empty in older snapshots.
    sibling_files: list[str] = field(default_factory=list)
    # Public modules' star imports of code the analysis couldn't read, another distribution's, module -> the
    # modules, in order (DT-DATA-56): the names they bind aren't recorded, so a name missing under such a module
    # may still be there (google-generativeai's `protos`, from google-ai-generativelanguage).
    star_sources: dict[str, list[str]] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload["symbols"] = [asdict(s) for s in self.symbols.values()]
        payload["internal"] = [asdict(s) for s in self.internal.values()]
        payload["classes"] = [
            {
                "path": c.path,
                "mro": c.mro,
                "members": [asdict(m) for m in c.members.values()],
                "internal": [asdict(m) for m in c.internal.values()],
                "external": c.external,
            }
            for c in self.classes.values()
        ]
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)

    @classmethod
    def load(cls, path: Path) -> ApiSnapshot:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        symbols = {s["path"]: SymbolRecord(**s) for s in payload.pop("symbols")}
        internal = {s["path"]: SymbolRecord(**s) for s in payload.pop("internal", [])}
        classes = {
            c["path"]: ClassRecord(
                c["path"],
                c["mro"],
                {m["path"].rpartition(".")[2]: SymbolRecord(**m) for m in c["members"]},
                {m["path"].rpartition(".")[2]: SymbolRecord(**m) for m in c.get("internal", [])},
                list(c.get("external") or []),
            )
            for c in payload.pop("classes", [])
        }
        return cls(symbols=symbols, internal=internal, classes=classes, **payload)

    def with_own_members(self) -> ApiSnapshot:
        """This snapshot stored with own members (DT-DATA-49). One that isn't, such as a release rebuilt from an
        index's rows, has each public class's members, own and inherited, as its own, and no MRO: they resolve the
        same, so its diff against a release built with own members shows only what changed."""
        if self.own_members:
            return self
        symbols: dict[str, SymbolRecord] = {}
        classes: dict[str, ClassRecord] = {}
        homes = {p: r.canonical for p, r in self.symbols.items() if r.kind == "class" and r.canonical}
        for path, rec in self.symbols.items():
            parent, _, name = path.rpartition(".")
            cls = homes.get(parent)
            if cls is None:
                symbols[path] = rec
                continue
            members = classes.setdefault(cls, ClassRecord(cls, [])).members
            members.setdefault(name, replace(rec, path=f"{cls}.{name}"))
        for cls in homes.values():
            classes.setdefault(cls, ClassRecord(cls, []))
        return replace(self, symbols=symbols, own_members=True, classes=classes)

    def without_own_members(self) -> ApiSnapshot:
        """This snapshot with every class member under each public path of its class, as one not stored with own
        members has them."""
        return (
            replace(self, symbols=self.expanded(), own_members=False, classes={})
            if self.own_members
            else self
        )

    def expanded(self) -> dict[str, SymbolRecord]:
        """`symbols` with every public class's members, own and inherited, under each of its public paths: what a
        snapshot not stored with own members records."""
        if not self.own_members:
            return self.symbols
        out = dict(self.symbols)
        for path, rec in self.symbols.items():
            if rec.kind != "class" or rec.canonical not in self.classes:
                continue
            for name in member_names(self.classes, rec.canonical):
                member = resolve_member(self.classes, rec.canonical, name)
                if member is not None:
                    out.setdefault(f"{path}.{name}", replace(member, path=f"{path}.{name}"))
        return out


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


_BOUND_KINDS = (griffe.ParameterKind.positional_only, griffe.ParameterKind.positional_or_keyword)


def _params(func: griffe.Function) -> list[dict[str, Any]]:
    """The parameters a caller passes. A function defined in a class gets the instance or class as its first
    parameter, whatever its name (DT-CORE-39: pydantic's `__init__(__pydantic_self__, **data)`, click's `__self`),
    unless it's a `staticmethod`; a first `*args` takes it with the rest."""
    params = list(func.parameters)
    if (
        params
        and func.parent is not None
        and func.parent.is_class
        and "staticmethod" not in func.labels
        and params[0].kind in _BOUND_KINDS
    ):
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
    """The whole object's deprecation: see `deprecations`."""
    return deprecations(obj)[0]


# A build's readings, by object (kept alive with it), depth and the literals a caller passes (DT-DATA-39): a function
# that many others pass their arguments to is read once. None outside `build_snapshot`.
_READINGS: dict[tuple[int, int, tuple[tuple[str, str], ...]], tuple[griffe.Object, Any]] | None = None


def deprecations(
    obj: griffe.Object, depth: int = PASS_DEPTH, values: dict[str, str] | None = None
) -> tuple[str | None, dict[str, str] | None]:
    """Best-effort deprecation detection: griffe extensions and `@deprecated` decorators deprecate the object (a
    decorator's arguments read by `_decorator_reading`; a pending one doesn't), and deprecation helpers recognized
    by name deprecate it or some of its arguments (`_HELPERS`): as decorators, or for an attribute, as the call
    that binds it. For a function, every warning in its body is read
    (`_BodyWarnings`), and else its dispatcher's (`_dispatcher_deprecation`), unless warning is its job
    (`_MACHINERY_RE`); an argument it passes on to a function that deprecates it is deprecated, up to `depth` calls
    down (`_passed_on`). A class's or function's docstring notes and `'deprecated'` defaults fill in what the code
    doesn't say (`_documented_deprecations`). `values`: string literals a caller passes for some parameters, which
    fill in the messages that use them."""
    reading = _reading(obj, depth, values)
    return reading.whole, reading.params


@dataclass(frozen=True)
class _Reading:
    whole: str | None
    params: dict[str, str] | None
    # For an argument a warning in the body deprecates: the other arguments its condition tests, which a caller
    # must pass on too for the warning to be its (DT-DATA-39). pandas warns about `include` only `if obj.ndim == 1`.
    conditions: dict[str, frozenset[str]] = field(default_factory=dict)


def _reading(obj: griffe.Object, depth: int, values: dict[str, str] | None) -> _Reading:
    if _READINGS is None:
        return _deprecations(obj, depth, values)
    key = (id(obj), depth, tuple(sorted(values.items())) if values else ())
    hit = _READINGS.get(key)
    if hit is None:
        hit = _READINGS[key] = (obj, _deprecations(obj, depth, values))
    return hit[1]


def _deprecations(obj: griffe.Object, depth: int, values: dict[str, str] | None) -> _Reading:
    flagged = getattr(obj, "deprecated", None)
    if flagged:
        return _Reading((flagged if isinstance(flagged, str) else "deprecated"), None)
    whole: str | None = None
    params: dict[str, str] = {}
    # A constructor's deprecation is its class's: "InferenceApi is deprecated", not "__init__ is deprecated".
    parent = obj.parent
    name = parent.name if obj.name == "__init__" and parent is not None and parent.is_class else obj.name
    for dec in getattr(obj, "decorators", []) or []:
        text = str(dec.value)
        decorator = text if DEPRECATED_DECORATOR_RE.match(text) else _renamed_deprecated(dec, text)
        if DEPRECATED_DECORATOR_RE.match(decorator):
            reading = _decorator_reading(_parsed_call(decorator) if "(" in decorator else None, obj, name)
            if reading is None:  # pending: not deprecated yet
                continue
            return _Reading(reading, None)
        call = _helper_call(text)
        found = _helper_reading(call, obj, name) if call is not None else None
        if found is not None:
            whole = whole or found[0]
            for param, message in found[1].items():
                params.setdefault(param, message)
    if isinstance(obj, griffe.Attribute):
        return _Reading(_wrapper_deprecation(obj), None)
    own_params: dict[str, str] = {}
    conditions: dict[str, frozenset[str]] = {}
    if isinstance(obj, griffe.Function) and not _MACHINERY_RE.match(obj.name):
        own, own_params, conditions = _function_warnings(obj, obj, values)
        whole = whole or own or _dispatcher_deprecation(obj)
        for name, message in own_params.items():
            params.setdefault(name, message)
    # What the code says wins over what the docstring says, unless the snapshot can't read it (DT-DATA-38).
    doc_whole, doc_params = _documented_deprecations(obj)
    if doc_whole and whole in (None, UNRECOVERABLE):
        whole = doc_whole
    for name, message in doc_params.items():
        if params.get(name, UNRECOVERABLE) == UNRECOVERABLE:
            params[name] = message
    # A warning one call away fills in what the function's own code and docstring don't say (DT-DATA-39).
    if isinstance(obj, griffe.Function) and depth > 0 and not _MACHINERY_RE.match(obj.name):
        for name, message in _passed_on(obj, depth).items():
            if params.get(name, UNRECOVERABLE) == UNRECOVERABLE and message != UNRECOVERABLE:
                params[name] = message
            params.setdefault(name, message)
    kept = {n: c for n, c in conditions.items() if n in own_params and params.get(n) == own_params[n]}
    return _Reading(whole, params or None, kept)


# Deprecation helpers recognized by name (DT-DATA-37), from a survey of the decorators and wrapper calls in the
# covered packages: what each deprecates, `whole` (the function, class or attribute it marks) or `params`
# (arguments of the function it decorates), and the role of each positional argument. Keywords are read by name
# (`_KEYWORD_ROLES`). Not here, because they deprecate a way of calling rather than a name or an argument:
# `deprecate_nonkeyword_arguments`, `_deprecate_positional_args`, `deprecate_parameter_as_positional`,
# `deprecate_posargs`, `make_keyword_only`, `_deprecate_list_output`.
@dataclass(frozen=True)
class _Helper:
    marks: str  # "whole" | "params"
    positional: tuple[str, ...] = ()  # roles: message, new, old, name, func, version
    top_level: bool = False  # the new name is the package's own (streamlit's `st.<name>`)


_WRAPS_RENAMED = _Helper("whole", ("func", "name", "version", "message"), top_level=True)
_WRAPS_OBJECT = _Helper("whole", ("func", "name", "new", "version"), top_level=True)
_HELPERS: dict[str, _Helper] = {
    # polars
    "deprecate_function": _Helper("whole", ("message",)),
    "deprecate_renamed_function": _Helper("whole", ("new",)),
    "deprecate_renamed_parameter": _Helper("params", ("old", "new")),
    "deprecated_alias": _Helper("params"),  # old="new" keywords
    # pandas (3.x passes the warning class first: a positional warning class takes no role)
    "deprecate_kwarg": _Helper("params", ("old", "new")),
    "deprecate": _Helper("whole", ("name", "func", "version", "new")),
    # redis
    "deprecated_function": _Helper("whole"),
    "deprecated_args": _Helper("params"),
    # sqlalchemy
    "deprecated_20": _Helper("whole", ("name", "new")),
    "became_legacy_20": _Helper("whole", ("name", "new")),
    "moved_20": _Helper("whole", ("message",)),
    "deprecated_cls": _Helper("whole", ("version", "message")),
    "deprecated_20_cls": _Helper("whole", ("name", "new")),
    "deprecated_params": _Helper("params"),  # name=(version, text) keywords
    # huggingface-hub
    "_deprecate_method": _Helper("whole"),
    "_deprecate_arguments": _Helper("params"),
    # matplotlib
    "rename_parameter": _Helper("params", ("version", "old", "new")),
    "delete_parameter": _Helper("params", ("version", "old")),
    "deprecate_privatize_attribute": _Helper("whole", ("version",)),
    # langgraph
    "deprecated_parameter": _Helper("params", ("old", "version", "new")),
    # streamlit; `make_deprecated_name_warning` builds a message for `show_deprecation_warning`
    "deprecate_func_name": _WRAPS_RENAMED,
    "_deprecate_func_name": _WRAPS_RENAMED,
    "deprecate_obj_name": _WRAPS_OBJECT,
    "_deprecate_obj_name": _WRAPS_OBJECT,
    "make_deprecated_name_warning": _Helper("params", ("old", "new", "version", "message"), top_level=True),
    # weaviate
    "docstring_deprecated": _Helper("whole", ("message", "version")),
}
_KEYWORD_ROLES = {
    **dict.fromkeys(
        ("message", "msg", "reason", "details", "custom_message", "extra_message", "addendum", "description"),
        "message",
    ),
    **dict.fromkeys(
        ("alternative", "alternative_import", "new_name", "new_arg_name", "name_override", "alt_name"), "new"
    ),
    **dict.fromkeys(("old_name", "old_arg_name"), "old"),
    **dict.fromkeys(("deprecated_args", "args_to_warn"), "olds"),
    **dict.fromkeys(("name", "api_name", "clsname"), "name"),
}
# Keywords of a `params` helper that aren't arguments it renames (`deprecated_alias(old="new")`).
_OPTION_KEYWORDS = frozenset(
    {
        "version",
        "since",
        "removal",
        "removal_date",
        "removal_in",
        "deprecated_in",
        "dep_version",
        "pending",
        "moved",
        "obj_type",
        "package",
        "stacklevel",
        "allowed_args",
        "mapping",
        "klass",
        "warning",
        "category",
        "code",
    }
)
# Warning helpers called in a function body, read like `warn(...)` (`_BodyWarnings`): matplotlib's and
# langchain's `warn_deprecated(since, message=…, name=…, alternative=…)`, sqlalchemy's
# `warn_deprecated(msg, version)`, polars' `issue_deprecation_warning`, streamlit's `show_deprecation_warning`.
WARNING_HELPER_RE = re.compile(r"^_*(?:warn|issue|show|emit)_?deprecat\w*$")
# Helpers that build a deprecation message (streamlit): the message is a deprecation however it is then emitted.
MESSAGE_BUILDERS = frozenset({"make_deprecated_name_warning"})
# Functions whose job is to warn: the deprecation machinery (redis' `warn_deprecated`, polars'
# `issue_deprecation_warning`) and checks that warn about something else (pandas' `maybe_warn_args_and_kwargs`).
# A warning in their body isn't their own deprecation.
_MACHINERY_RE = re.compile(
    r"^_*(?:warn_\w+|maybe_warn\w*|(?:issue|show|emit|raise|make)_\w*(?:warning|deprecation)\w*|deprecate_\w+)$"
)
_NAME_RE = re.compile(r"[A-Za-z_][\w.]*(?:\(\))?")
_VERSION_RE = re.compile(r"^v?\d[\w.\-]*$")  # "3.6", "0.19.0", a removal date "2024-04-01"
# sqlalchemy's and matplotlib's reST roles, read as sqlalchemy's warnings print them: ":meth:`_orm.Session.get`"
# is "Session.get()".
_RST_ROLE_RE = re.compile(r":(?:\w+:)?(\w+):`~?(?:_\w+)?\.?([^`]+?)`")  # also `:py:func:`
_HELPER_HEAD_RE = re.compile(r"(?:[\w.]*\.)?(\w+)\(")
_WARNING_CLASS_RE = re.compile(r"^[\w.]*Warning$")  # `Pandas4Warning`, `exc.SADeprecationWarning`


def _callee(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None


_PENDING_CATEGORY_RE = re.compile(r"PendingDeprecation")  # also celery's CPendingDeprecationWarning


def _names_pending(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, (ast.Name, ast.Attribute)) and bool(
        _PENDING_CATEGORY_RE.search(ast.unparse(target))
    )


def _pending(call: ast.Call) -> bool:
    """A deprecation call that says it is pending (DT-DATA-31): `pending=True` (langchain's and matplotlib's
    decorators and helpers), or PendingDeprecationWarning as its category. A pending deprecation isn't one yet: it
    says the name will be deprecated, and Python doesn't show the warning by default."""
    return any(_names_pending(a) for a in call.args) or any(
        (kw.arg == "pending" and isinstance(kw.value, ast.Constant) and kw.value.value is True)
        or (kw.arg == "category" and _names_pending(kw.value))
        for kw in call.keywords
    )


def _parsed_call(text: str) -> ast.Call | None:
    try:
        node = _quiet_parse(text, "eval").body  # type: ignore[attr-defined]
    except (SyntaxError, ValueError):
        return None
    return node if isinstance(node, ast.Call) else None


def _helper_call(text: str) -> ast.Call | None:
    """`text` parsed, when it is a call of a helper in `_HELPERS`."""
    head = _HELPER_HEAD_RE.match(text)
    if head is None or head.group(1) not in _HELPERS:
        return None
    return _parsed_call(text)


def _renamed_deprecated(dec: griffe.Decorator, text: str) -> str:
    """A `deprecated` decorator imported under another name, as if written under its own: weaviate's
    `@typing_deprecated(…)` (`from typing_extensions import deprecated as typing_deprecated`)."""
    head = re.match(r"[\w.]+", text)
    if head is None or head.group(0).rsplit(".", 1)[-1] == "deprecated":
        return ""
    try:
        path = dec.callable_path
    except Exception:  # griffe raises several resolution errors
        return ""
    return "deprecated" + text[head.end() :] if path.rsplit(".", 1)[-1] == "deprecated" else ""


def _module_constant(module: griffe.Module | None, name: str) -> str | None:
    """A module-level string constant: pydantic's `warn(_ALLOW_REUSE_WARNING_MESSAGE, DeprecationWarning)`. A
    warning kept in a constant is its text (DT-DATA-39): pytest's `YIELD_FIXTURE = PytestDeprecationWarning("…")`
    and `KEYWORD_MSG_ARG = UnformattedWarning(PytestRemovedIn8Warning, "…")`, imported from `_pytest.deprecated`."""
    try:
        member = module.members.get(name) if module is not None else None
        target = _resolve(member) if member is not None else None
        value = getattr(target, "value", None) if target is not None and target.is_attribute else None
        node = _quiet_parse(str(value), "eval").body if value is not None else None  # type: ignore[attr-defined]
        rendered = _render(node)
        if (
            rendered is None
            and isinstance(node, ast.Call)
            and _WARNING_CLASS_RE.match(ast.unparse(node.func))
        ):
            rendered = next((text for arg in node.args if (text := _render(arg)) is not None), None)
    except Exception:  # griffe raises several resolution errors; a value may not parse
        return None
    return " ".join(rendered.split())[:400] or None if rendered else None


class _HelperArgs:
    """A helper call's arguments by role. `positional` gives each positional argument's role; None guesses from the
    value, for a warning helper called in a body: a version is skipped, a bare name is what is deprecated, and
    other text is the message. A `params` helper's other keywords are the arguments it renames:
    `deprecated_alias(stop_after_n_rows="n_rows")`, `deprecated_params(bind=("2.0", "… is deprecated"))`."""

    def __init__(
        self,
        call: ast.Call,
        positional: tuple[str, ...] | None,
        renames: bool,
        text: Callable[[ast.expr], str | None],
    ):
        self.text = text
        self.messages: list[str] = []
        self.new: str | None = None
        self.name: str | None = None
        self.func: ast.expr | None = None
        self.olds: list[str] = []
        self.renamed: dict[str, tuple[str | None, str | None]] = {}  # old -> (new, message)
        self.keywords = {k.arg: v for k in call.keywords if k.arg for v in [_str(k.value)] if v is not None}
        args = [a for a in call.args if not _WARNING_CLASS_RE.search(ast.unparse(a))]
        for i, arg in enumerate(args):
            if positional is None:
                self._take("guess", arg)
            elif i < len(positional):
                self._take(positional[i], arg)
        for kw in call.keywords:
            if kw.arg is None:
                continue
            role = _KEYWORD_ROLES.get(kw.arg)
            if role is not None:
                self._take(role, kw.value)
            elif renames and kw.arg not in _OPTION_KEYWORDS:
                self._rename(kw.arg, kw.value)

    @property
    def message(self) -> str | None:
        return " ".join(self.messages) or None

    def _take(self, role: str, node: ast.expr) -> None:
        if role == "func":
            self.func = node
            return
        if role == "olds":
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                self.olds += [t for e in node.elts if (t := _str(e)) and t.isidentifier()]
            return
        if role == "version":
            return
        text = self.text(node)
        if not text:
            return
        if role == "guess":
            if _VERSION_RE.match(text):
                return
            role = (
                "name" if _NAME_RE.fullmatch(text) and self.name is None and not self.messages else "message"
            )
        if role == "message":
            self.messages.append(text)
        elif (
            role == "new"
        ):  # a name, else prose: sqlalchemy's `alternative="The method is now available as …"`
            if _NAME_RE.fullmatch(text):
                self.new = self.new or text.removesuffix("()")
            else:
                self.messages.append(text)
        elif role == "old" and text.isidentifier():
            self.olds.append(text)
        elif role == "name":
            self.name = self.name or text

    def _rename(self, old: str, node: ast.expr) -> None:
        if (text := _str(node)) is not None:
            self.renamed[old] = (text if text.isidentifier() else None, None)
        elif isinstance(node, ast.Tuple):
            texts = [t for e in node.elts if (t := self.text(e)) and not _VERSION_RE.match(t)]
            self.renamed[old] = (None, texts[0] if texts else None)


def _plain_rst(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        return m.group(2) + ("()" if m.group(1) in ("func", "meth") else "")

    return _RST_ROLE_RE.sub(repl, text)


def _compose(subject: str | None, name: str | None, message: str | None, new: str | None) -> str | None:
    """A helper's deprecation as text: "<subject> is deprecated.", unless the message already says `name` is; then
    "Use `<new>` instead."; then the message."""
    message = _plain_rst(message) if message else None
    parts = []
    if subject and not (message and name and names_argument(message, name)):
        parts.append(f"{subject} is deprecated.")
    if new:
        parts.append(f"Use `{new}` instead.")
    if message:
        parts.append(message)
    return " ".join(" ".join(parts).split())[:400] or None


def _arg_text(node: ast.expr, module: griffe.Module | None) -> str | None:
    """An argument of a helper or decorator call as text: what the code builds (`_render`), or a module-level
    string constant it names."""
    rendered = _render(node)
    if rendered is None and isinstance(node, ast.Name):
        return _module_constant(module, node.id)
    return " ".join(rendered.split())[:400] or None if rendered else None


# How a `deprecated(...)` decorator's keywords are read (DT-DATA-31): PEP 702's `deprecated(message)`, langchain's and
# matplotlib's `deprecated(since, *, message, name, alternative, alternative_import, pending, addendum, removal, …)`,
# sqlalchemy's `deprecated(version, message)`, langgraph's `deprecated(since, alternative, *, removal)`, the Deprecated
# library's `deprecated(reason, version=)`, the deprecation library's `deprecated(deprecated_in=, removed_in=,
# details=)` and scikit-learn's `deprecated(extra)`. Others (`obj_type`, `category`, `stacklevel`, `example`) aren't
# part of the text.
_DECORATOR_ROLES = {
    **dict.fromkeys(("message", "reason", "details", "extra"), "message"),
    "addendum": "addendum",
    **dict.fromkeys(("alternative", "alternative_import"), "alternative"),
    **dict.fromkeys(("since", "version", "deprecated_in"), "since"),
    **dict.fromkeys(("removal", "removed_in"), "removal"),
    "name": "name",
}
# An alternative that is a sentence already: matplotlib's "No replacement. Please open an issue if you use this.",
# langchain's "Use string instead.".
_SENTENCE_ALTERNATIVE_RE = re.compile(r"^(?:use|call|no|none|see)\b|\binstead\b", re.I)
_RST_TARGET_RE = re.compile(
    r"`([^`<]+?)\s*<[^`>]*>`"
)  # `QuadMesh(coordinates).get_paths()<.QuadMesh.get_paths>`


def _decorator_reading(call: ast.Call | None, obj: griffe.Object, name: str) -> str | None:
    """A `deprecated` decorator's deprecation of `obj`, known as `name` (DT-DATA-31), or None when it's pending.

    Keywords are read by name (`_DECORATOR_ROLES`), positional arguments by value: a version says when, a bare name
    after it is the alternative (langgraph's `deprecated("0.0.44", "create_react_agent")`), and anything else is the
    message. The text is put together the way langchain and matplotlib word their warnings: the message, if it says
    what is deprecated, else "<name> was deprecated in <since> and will be removed in <removal>." before it; then
    the alternative as "Use … instead." (`_instead`); then the addendum. A bare `@deprecated`, or one that says
    nothing, is "deprecated". A pending one (`_pending`) isn't a deprecation yet."""
    if call is None:
        return "deprecated"
    if _pending(call):
        return None
    try:
        module = obj.module
    except ValueError:  # an overload griffe keeps aside may have no parent
        module = None
    since = removal = alternative = None
    subject = name
    messages: list[str] = []
    addenda: list[str] = []
    for arg in call.args:
        value = _arg_text(arg, module)
        if not value:
            continue
        if since is None and not messages and _VERSION_RE.match(value):
            since = value
        elif since is not None and alternative is None and not messages and _NAME_RE.fullmatch(value):
            alternative = value
        else:
            messages.append(value)
    for kw in call.keywords:
        role = _DECORATOR_ROLES.get(kw.arg or "")
        value = _arg_text(kw.value, module) if role else None
        if not value:
            continue
        if role == "message":
            messages.append(value)
        elif role == "addendum":
            addenda.append(value)
        elif role == "alternative":
            alternative = alternative or value
        elif role == "since":
            since = since or value
        elif role == "removal":
            removal = removal or value
        elif role == "name":
            subject = value
    if not (messages or addenda or since or removal or alternative):
        return "deprecated"
    message = _plain_rst(" ".join(messages)) if messages else None
    parts = []
    if not (message and DEPRECATION_WORDS_RE.search(message)):
        said = f"{subject} was deprecated in {since}" if since else f"{subject} is deprecated"
        parts.append(f"{said} and will be removed in {removal}." if removal else f"{said}.")
    parts += [message, _instead(alternative) if alternative else None, _plain_rst(" ".join(addenda))]
    return " ".join(" ".join(p for p in parts if p).split())[:400]


def _instead(alternative: str) -> str | None:
    """A decorator's alternative as the sentence langchain and matplotlib print, "Use … instead.": a name or an
    expression in backticks, each side of an `or` or `and` on its own ("Use `MouseEvent` or `KeyEvent` instead."),
    reST's ``literals`` as backticks, and prose as written. One that is a sentence already stays as it is."""
    text = _RST_TARGET_RE.sub(r"`\1`", " ".join(_plain_rst(alternative).replace("``", "`").split()))
    if not text:
        return None
    if _SENTENCE_ALTERNATIVE_RE.search(text):
        return text if text[-1] in ".!?" else f"{text}."
    text = text.rstrip(". ")
    return f"Use {text if '`' in text else _as_code(text)} instead."


def _as_code(text: str) -> str:
    """`text` in backticks if it's Python (an `or`/`and` of expressions: each side), else as written."""
    try:
        node = _quiet_parse(text, "eval").body  # type: ignore[attr-defined]
    except (SyntaxError, ValueError):
        try:  # langchain's `alternative_import="from langchain_huggingface import HuggingFaceEndpoint"`
            body = _quiet_parse(text).body  # type: ignore[attr-defined]
        except (SyntaxError, ValueError):
            return text
        return f"`{text}`" if len(body) == 1 and isinstance(body[0], (ast.Import, ast.ImportFrom)) else text
    if isinstance(node, ast.BoolOp):
        joiner = " or " if isinstance(node.op, ast.Or) else " and "
        return joiner.join(f"`{ast.get_source_segment(text, v)}`" for v in node.values)
    return f"`{text}`"


def _known_as(node: ast.expr | None, scope: griffe.Object | None) -> str | None:
    """The public name a function passed to a wrapper is known by (`__name__`): streamlit's `_rerun`, imported as
    `from streamlit.commands.execution_control import rerun as _rerun`, is `rerun`."""
    if isinstance(node, ast.Attribute):
        leaf = node.attr
    elif isinstance(node, ast.Name):
        leaf = node.id
        member = scope.members.get(node.id) if scope is not None else None
        if member is not None and member.is_alias:
            target = _resolve(member)
            leaf = target.name if target is not None else member.target_path.rsplit(".", 1)[-1]  # type: ignore[union-attr]
    else:
        return None
    return None if leaf.startswith("_") else leaf


def _helper_reading(
    call: ast.Call, obj: griffe.Object, name: str
) -> tuple[str | None, dict[str, str]] | None:
    """What a helper call in `_HELPERS` deprecates about `obj`, known as `name`: (whole, {argument: text})."""
    helper = _HELPERS.get(_callee(call) or "")
    if helper is None or _pending(call):
        return None
    module = obj.module if isinstance(obj, griffe.Object) else None
    args = _HelperArgs(
        call, helper.positional, helper.marks == "params", lambda node: _arg_text(node, module)
    )
    if helper.marks == "whole":
        new = args.new or _known_as(args.func, obj.parent)
        if new and helper.top_level:
            new = f"{obj.package.path}.{new}"
        return _compose(name, name, args.message, new), {}
    # `*args` and `**kwargs` themselves aren't arguments anyone passes by name (matplotlib's
    # `@delete_parameter("3.6", "args")`).
    variadic = {
        p.name
        for p in (obj.parameters if isinstance(obj, griffe.Function) else ())
        if p.kind in (griffe.ParameterKind.var_positional, griffe.ParameterKind.var_keyword)
    }
    params: dict[str, str] = {}
    for old in (o for o in args.olds if o not in variadic):
        new = args.new if len(args.olds) == 1 else None
        params.setdefault(old, _compose(f"`{old}`", old, args.message, new) or UNRECOVERABLE)
    for old, (new, message) in args.renamed.items():
        if old not in variadic:
            params.setdefault(old, _compose(f"`{old}`", old, message, new) or UNRECOVERABLE)
    return None, params


def _wrapper_deprecation(attr: griffe.Attribute) -> str | None:
    """A name bound to what a wrapping helper returns: streamlit's `experimental_rerun =
    _deprecate_func_name(_rerun, "experimental_rerun", "2024-04-01")`, matplotlib's `patch =
    _api.deprecate_privatize_attribute("3.5", alternative="ax")` in a class. A name bound to a function of its
    own scope is that function, and warns as it does: streamlit 1.27-1.36's `experimental_rerun =
    _experimental_rerun`."""
    value = attr.value
    if isinstance(value, griffe.ExprName) and attr.parent is not None:
        member = attr.parent.members.get(value.name)
        target = _resolve(member) if member is not None else None
        return deprecations(target)[0] if isinstance(target, griffe.Function) else None
    if not isinstance(value, griffe.ExprCall):
        return None
    # matplotlib 3.7's `get_cmap = _api.deprecated("3.7", name="get_cmap", alternative=…)(_get_cmap)` (DT-DATA-31).
    wrapped = _parsed_call(str(value))
    if (
        wrapped is not None
        and isinstance(wrapped.func, ast.Call)
        and DEPRECATED_DECORATOR_RE.match(ast.unparse(wrapped.func.func))
    ):
        return _decorator_reading(wrapped.func, attr, attr.name)
    call = _helper_call(str(value))
    found = _helper_reading(call, attr, attr.name) if call is not None else None
    return found[0] if found is not None else None


def _function_node(
    func: griffe.Function, source: str
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, str] | None:
    """`func`'s definition, parsed from its `source`, and the text it was parsed from; None if it doesn't parse, and
    no node when griffe made the function up (a dataclass's `__init__`: its source is someone else's)."""
    parsed = _parse_function(source)
    if parsed is None:
        return None
    tree, text = parsed
    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func.name
        ),
        None,
    )
    return node, text


def _function_warnings(
    func: griffe.Function, reader: griffe.Function, values: dict[str, str] | None = None
) -> tuple[str | None, dict[str, str], dict[str, frozenset[str]]]:
    """The warnings in `func`'s body, read as `reader`'s: its own name and class decide what they deprecate. Also,
    for each argument they deprecate, the other arguments its warning's condition tests (`_Reading.conditions`)."""
    try:
        source = func.source
    except (OSError, ValueError, AttributeError):
        return None, {}, {}
    if "warn" not in source or not (
        CALL_CATEGORY_RE.search(source) or "deprecat" in source.lower() or WARN_CONSTANT_RE.search(source)
    ):
        return None, {}, {}
    found = _function_node(func, source)
    if found is not None:
        node, text = found
        if node is None:
            return None, {}, {}
        body = _BodyWarnings(node, text, reader, values)
        try:
            whole, params = body.read()
            return whole, params or {}, body.conditions
        except RecursionError:  # a pathologically nested expression
            pass
    return _first_notice(source), {}, {}


def _member(scope: griffe.Object, name: str, inherited: bool = False) -> griffe.Object | None:
    member = (scope.all_members if inherited and scope.is_class else scope.members).get(name)  # type: ignore[attr-defined]
    return _resolve(member) if member is not None else None


def _called(call: ast.Call, func: griffe.Function) -> tuple[griffe.Function, int] | None:
    """The function of the loaded packages that `call`, in `func`'s body, runs, and how many of its leading
    parameters the call doesn't pass (an instance's `self`): a name of `func`'s module (a function, or a class for
    its `__init__`), an attribute of a module it imports, a method through `self` or `cls`, or the next class's in
    the MRO through `super()`."""
    f = call.func
    owner = func.parent if func.parent is not None and func.parent.is_class else None
    method = False
    try:
        target: griffe.Object | None = None
        if isinstance(f, ast.Name):
            target = _member(func.module, f.id)
        elif isinstance(f, ast.Attribute):
            base = f.value
            if isinstance(base, ast.Name) and base.id in ("self", "cls") and owner is not None:
                target, method = _member(owner, f.attr, inherited=True), True
            elif isinstance(base, ast.Call) and _callee(base) == "super" and owner is not None:
                bases = owner.mro()  # type: ignore[attr-defined]
                target = next((_member(c, f.attr) for c in bases if f.attr in c.members), None)
                method = True
            elif isinstance(base, ast.Name):
                module = _member(func.module, base.id)
                target = _member(module, f.attr) if module is not None and module.is_module else None
        if target is not None and target.is_class:
            target, method = _member(target, "__init__", inherited=True), True
    except Exception:  # griffe raises several resolution errors (external bases, cyclic aliases)
        return None
    if not isinstance(target, griffe.Function):
        return None
    return target, 1 if method and "staticmethod" not in target.labels else 0


_POSITIONAL = (griffe.ParameterKind.positional_only, griffe.ParameterKind.positional_or_keyword)


def _passed_on(func: griffe.Function, depth: int) -> dict[str, str]:
    """Arguments `func` passes on, under their own names, to a function that deprecates them (DT-DATA-39):
    anthropic's `Anthropic.__init__` passes `proxies=proxies` to its base class's `__init__`, which warns about it,
    selenium's chrome `Service` passes `log_path` to `ChromiumService`, and pytest's `skip` passes `msg` to
    `_resolve_msg_to_reason("skip", reason, msg)`. The function called is read `depth - 1` calls further down, with
    the string literals the call passes (`"skip"`) filling in its messages. Passed as another name, an argument
    isn't followed: the message would name the other function's parameter. A warning whose condition also tests an
    argument the call doesn't pass on as the caller's own isn't the caller's: pandas' `describe_ndframe(obj=self,
    include=include, …)` warns about `include` only for a Series.

    A function that passes its **kwargs on whole (`super().__init__(**kwargs)`: marshmallow's fields, aiohttp's
    `ClientSession.get`) takes the deprecations of the arguments it doesn't name, pass explicitly or look up in
    them itself; not if it rebinds them first."""
    names = [p.name for p in func.parameters if p.kind not in _VARIADIC and p.name not in ("self", "cls")]
    kwargs = next((p.name for p in func.parameters if p.kind is griffe.ParameterKind.var_keyword), None)
    if not names and kwargs is None:
        return {}
    try:
        source = func.source
    except (OSError, ValueError, AttributeError):
        return {}
    patterns = [rf"\*\*\s*{re.escape(kwargs)}\b"] if kwargs else []
    if names:
        alternatives = "|".join(map(re.escape, names))
        patterns.append(rf"\b(?P<k>{alternatives})\s*=\s*(?P=k)\b|[(,]\s*(?:{alternatives})\s*[,)]")
    if not re.search("|".join(patterns), source):
        return {}
    found = _function_node(func, source)
    if found is None or found[0] is None:
        return {}
    node = found[0]
    own = set(names)
    looked_up, rebound = _kwargs_use(node, kwargs) if kwargs else (frozenset(), True)
    params: dict[str, str] = {}
    for call in (n for n in _own_nodes(node) if isinstance(n, ast.Call)):
        args = [*call.args, *(k.value for k in call.keywords)]
        forwards = not rebound and any(
            k.arg is None and isinstance(k.value, ast.Name) and k.value.id == kwargs for k in call.keywords
        )
        if not forwards and not any(isinstance(a, ast.Name) and a.id in own for a in args):
            continue
        reached = _called(call, func)
        if reached is None:
            continue
        callee, skip = reached
        bound: list[tuple[str, ast.expr]] = []  # the callee's parameter -> what the call passes
        for param, arg in zip(list(callee.parameters)[skip:], call.args, strict=False):
            if isinstance(arg, ast.Starred) or param.kind not in _POSITIONAL:
                break
            bound.append((param.name, arg))
        bound += [(kw.arg, kw.value) for kw in call.keywords if kw.arg]
        passed = [name for name, arg in bound if isinstance(arg, ast.Name) and arg.id == name and name in own]
        values = {name: text for name, arg in bound if (text := _str(arg)) is not None}
        if not passed and not forwards:
            continue
        theirs = _reading(callee, depth - 1, values)
        explicit = {name for name, _ in bound}
        through = sorted(
            set(theirs.params or {}) - own - explicit - looked_up if forwards else set()
        )  # the arguments that reach the function called through **kwargs
        for name in [*passed, *through]:
            message = (theirs.params or {}).get(name)
            if (
                message is not None
                and _about_argument(message, name)
                and theirs.conditions.get(name, frozenset()) <= {*passed, *through}
            ):
                params.setdefault(name, message)
    return params


def _kwargs_use(node: ast.AST, kwargs: str) -> tuple[frozenset[str], bool]:
    """The names a function looks up in its **kwargs mapping (`kwargs.pop("x")`, `kwargs["x"]`, `"x" in kwargs`),
    and whether it binds the name again (`kwargs = {…}`), in its own body."""
    names: set[str] = set()
    rebound = False
    for n in _own_nodes(node):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            rebound = rebound or any(kwargs in _bound_names(t) for t in targets)
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == kwargs
            and n.args
        ):
            names.add(_str(n.args[0]) or "")
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == kwargs:
            names.add(_str(n.slice) or "")
        elif (
            isinstance(n, ast.Compare)
            and len(n.ops) == 1
            and isinstance(n.ops[0], (ast.In, ast.NotIn))
            and isinstance(n.comparators[0], ast.Name)
            and n.comparators[0].id == kwargs
        ):
            names.add(_str(n.left) or "")
    return frozenset(names - {""}), rebound


_DOC_NOTE_TEXT_RE = re.compile(r"^Deprecated(?: since version \S+?)?[:.]")


def _about_argument(message: str, name: str) -> bool:
    """Whether the deprecation the called function gives an argument is the argument's own, and so its callers'
    when they pass it on: a docstring note on that argument, a decorator's or a default's marking, or wording
    that deprecates the argument itself (`deprecates_argument`). Its reading of the body takes a warning that a
    condition on the argument guards and that names it, and some of those are about a value, which a caller
    passing the argument along doesn't make ("Using dict as a `filter` is deprecated")."""
    return (
        message in (UNRECOVERABLE, DEPRECATED_DEFAULT)
        or bool(_DOC_NOTE_TEXT_RE.match(message))
        or deprecates_argument(message, name)
    )


def _dispatcher_deprecation(func: griffe.Function) -> str | None:
    """numpy 1.25-1.26 warn about `product`, `alltrue`, `round_`... in the dispatcher their decorator is given,
    `@array_function_dispatch(_product_dispatcher)`, and the function itself only forwards to `prod`
    (DT-DATA-40). A function of the same scope passed to a decorator call deprecates the decorated function when
    its warnings, read as the decorated function's, deprecate it as a whole and name it other than as what to use
    instead, and don't name the helper itself: pandas' `@doc(lookup)` passes a function for its docstring, and
    "lookup() is deprecated, use select() instead" is about `lookup`."""
    scope = func.parent
    if scope is None:
        return None
    for dec in func.decorators or []:
        try:
            call = ast.parse(str(dec.value), mode="eval").body
        except (SyntaxError, ValueError):
            continue
        if not isinstance(call, ast.Call):
            continue
        for arg in [*call.args, *(k.value for k in call.keywords)]:
            if not isinstance(arg, ast.Name) or arg.id == func.name:
                continue
            helper = scope.members.get(arg.id)
            if not isinstance(helper, griffe.Function):
                continue
            whole, _, _ = _function_warnings(helper, func)
            if (
                whole
                and whole != UNRECOVERABLE
                and names_argument(whole, func.name)
                and not re.search(rf"\b{re.escape(helper.name)}\b", whole)
            ):
                return whole
    return None


# Sphinx's `.. deprecated:: <version> [text]` directive in a docstring (DT-DATA-38); scikit-learn once writes
# `..deprecated::`.
_DOC_NOTE_RE = re.compile(r"^(?P<indent>\s*)\.\.\s*deprecated::(?P<rest>.*)$")
_DOC_UNDERLINE_RE = re.compile(r"^-{3,}\s*$")  # numpy style: `Parameters` over `----------`
_DOC_GOOGLE_SECTION_RE = re.compile(
    r"^(Args|Arguments|Parameters|Params|Keyword Args|Keyword Arguments|Other Parameters|Returns?|Yields?|Raises"
    r"|Attributes|Examples?|Notes?|Warnings?|Warns|See Also|References|Todo|Methods|Deprecated):\s*$"
)
# A docstring that opens by saying the object is deprecated: ollama's "Deprecated in favor of `embed`.",
# "[DEPRECATED] …", "**DEPRECATED** - …", weaviate's "@deprecated in 1.27.0, …".
_DOC_OPENING_RE = re.compile(r"^\W*deprecated\b", re.I)
_DOC_PARAM_SECTIONS = frozenset(
    {"parameters", "params", "other parameters", "args", "arguments", "keyword args", "keyword arguments"}
)
# A parameter entry: numpy's `sparse : bool, default=True` or `x1, x2 : array_like`, google's `timeout (float): …`.
_DOC_ENTRY_RE = re.compile(
    r"^(?P<names>\*{0,2}[A-Za-z_]\w*(?:\s*,\s*\*{0,2}[A-Za-z_]\w*)*)\s*(?:\([^)]*\))?\s*(?::|$)"
)
# A Sphinx field (sqlalchemy): `:param convert_unicode=False:`, `:param \*expr:`, `:return:`.
_DOC_FIELD_RE = re.compile(r"^:(?P<field>\w+)(?:\s+(?P<arg>[^:]+?))?:(?:\s|$)")
_DOC_PARAM_FIELDS = frozenset({"param", "parameter", "arg", "argument", "key", "keyword", "kwarg", "kwparam"})
_PENDING_RE = re.compile(r"\bpending[- ]?deprecat|\b(?:will|to)\s+be\s+deprecated\b", re.I)
# A note about a value, a default or a way of calling, not about the parameter or the object itself: "The `"auto"`
# option was deprecated", "The default will change from None to 'infer'", "Passing booleans as index is
# deprecated", "The current behavior of dt.to_pydatetime is deprecated", "`sample_weight` needs to be passed as a
# keyword argument and not as a positional argument", "If str, the name of the column …". Read with literals taken
# out.
_VALUE_NOTE_RE = re.compile(
    r"\b(?:options?|values?|defaults?|behaviou?r|passing|must|positional(?:ly)?)\b|^\s*(?:If|When)\b", re.I
)
_LITERAL_SPAN_RE = re.compile(r"`[^`]*`")
# A note at the top about arguments or a usage isn't about the whole object: sqlalchemy's "creating the and_()
# construct with no arguments is deprecated", "This usage is deprecated", "The following features are deprecated".
_PARTIAL_NOTE_RE = re.compile(r"\b(?:arguments?|parameters?|keywords?|usages?|features?|patterns?)\b", re.I)
# "Passing `x` is deprecated" is about `x` itself: the words just before a name don't make it a value.
_CALLING_BEFORE_RE = re.compile(r"\b(?:passing|using|setting|specifying)\s+(?:the\s+)?[`'\"*]*$", re.I)
DEPRECATED_DEFAULT = "deprecated (marked by its default value)"
_VARIADIC = (griffe.ParameterKind.var_positional, griffe.ParameterKind.var_keyword)


@dataclass(frozen=True)
class _DocNote:
    text: str  # the note as written, reST read as plain text
    message: str  # "Deprecated since version <v>: <text>", or the opening paragraph as written
    context: str = ""  # a parameter entry's own text before the note
    about_object: bool = False  # the docstring opens with "Deprecated" or has a `Deprecated:` section


def _plain_doc(text: str) -> str:
    return _plain_rst(" ".join(text.split())).replace("``", "`")


def _doc_note(lines: list[str], context: list[str]) -> _DocNote:
    """A `.. deprecated::` note from the rest of its first line and the lines of its body."""
    first, _, inline = lines[0].strip().partition(" ")
    version = first.rstrip(".,") if _VERSION_RE.match(first) else None
    if version is None:
        inline = lines[0]
    text = _plain_doc(" ".join([inline, *lines[1:]]))
    head = f"Deprecated since version {version}" if version else "Deprecated"
    return _DocNote(text, (f"{head}: {text}" if text else f"{head}.")[:400], _plain_doc(" ".join(context)))


def _field_entry(line: str) -> tuple[str, ...] | None:
    """The parameter a Sphinx field documents (`:param convert_unicode=False:` -> convert_unicode), () for another
    field, None if the line isn't one."""
    m = _DOC_FIELD_RE.match(line)
    if m is None:
        return None
    if m.group("field") not in _DOC_PARAM_FIELDS or not m.group("arg"):
        return ()
    name = m.group("arg").split()[-1].split("=")[0].lstrip("\\*")
    return (name,) if name.isidentifier() else ()


def _doc_notes(doc: str) -> tuple[_DocNote | None, list[tuple[tuple[str, ...], _DocNote]]]:
    """The `.. deprecated::` notes of a numpy-, google- or Sphinx-style docstring: the first before any section or
    field, unindented (not inside another directive such as `.. note::`), and each under a parameter entry, with
    the names the entry documents. Notes anywhere else (Attributes, Returns, Notes, a parameter section outside an
    entry) are left out. A first paragraph that opens with "Deprecated", or a google-style `Deprecated:` section,
    is the top note, about the object itself."""
    lines = doc.splitlines()
    top: _DocNote | None = None
    opening = doc.strip().split("\n\n", 1)[0]
    if _DOC_OPENING_RE.match(opening) and not _DOC_NOTE_RE.match(opening.splitlines()[0]):
        text = _plain_doc(opening)
        top = _DocNote(text, text[:400], about_object=True)
    under: list[tuple[tuple[str, ...], _DocNote]] = []
    section: str | None = None
    entry: tuple[str, ...] = ()
    entry_indent: int | None = None
    context: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if indent == 0 and stripped and i + 1 < len(lines) and _DOC_UNDERLINE_RE.match(lines[i + 1]):
            section, entry, entry_indent = stripped.lower(), (), None
            i += 2
            continue
        if indent == 0 and _DOC_GOOGLE_SECTION_RE.match(stripped):
            section, entry, entry_indent = stripped[:-1].lower(), (), None
            i += 1
            if section == "deprecated":  # google style: the section's text is the object's deprecation
                body = []
                while i < len(lines) and (not lines[i].strip() or lines[i][0].isspace()):
                    body.append(lines[i])
                    i += 1
                if text := _plain_doc(" ".join(body)):
                    top = top or _DocNote(text, f"Deprecated: {text}"[:400], about_object=True)
            continue
        if indent == 0 and (field := _field_entry(stripped)) is not None:
            section, entry, entry_indent, context = "fields", field, 0, [_DOC_FIELD_RE.sub("", stripped)]
            i += 1
            continue
        if m := _DOC_NOTE_RE.match(line):
            own = len(m.group("indent"))
            body = [m.group("rest")]
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > own):
                body.append(lines[i])
                i += 1
            note = _doc_note(body, context)
            if section is None and own == 0:
                top = top or note
            elif section is not None and entry and entry_indent is not None and own > entry_indent:
                under.append((entry, note))
            continue
        if section in _DOC_PARAM_SECTIONS and stripped:
            entry_indent = indent if entry_indent is None else entry_indent
            if indent == entry_indent:
                e = _DOC_ENTRY_RE.match(stripped)
                entry = tuple(n.strip().lstrip("*") for n in e.group("names").split(",")) if e else ()
                context = []
        if entry:
            context.append(stripped)
        i += 1
    return top, under


def _about_value(text: str) -> bool:
    return bool(_VALUE_NOTE_RE.search(_LITERAL_SPAN_RE.sub("", text)))


def _named(text: str, names: list[str]) -> list[str]:
    """The names the wording deprecates, unless what comes first in the sentence is a value or a way of calling:
    "Negative values for `power_t` are deprecated", "Passing integer or float values together with `format` is
    deprecated"."""

    def plain_prefix(prefix: str) -> bool:
        return not _about_value(_CALLING_BEFORE_RE.sub("", prefix))

    return [n for n in names if deprecates_name(text, n, before=plain_prefix)]


def _valued(text: str, entry: list[str]) -> bool:
    """A literal named with the parameter is one of its values: "the `xlwt` engine", "The loss 'deviance'", "The
    loss arguments 'auto', …"."""
    return any(
        re.search(rf"[`'\"][^`'\"\s]+[`'\"]\s+{re.escape(n)}\b|\b{re.escape(n)}\s+(?:\w+\s+)?['\"]", text)
        for n in entry
    )


def _noted_params(note: _DocNote, entry: list[str], names: list[str]) -> list[str]:
    """The parameters a note under `entry` (the entry's names that are parameters) deprecates: those its wording
    deprecates, else the entry's, unless it deprecates something else (a value: "The loss 'deviance' was
    deprecated") or reads as being about a value, a default or a way of calling. A version alone is the entry's,
    unless the entry's own text says what is deprecated about it: numpy's `binary_repr(width)`, "If the width value
    is insufficient, it will be ignored … This behavior is deprecated"."""
    text = note.text
    if _PENDING_RE.search(text):
        return []
    if not text:
        stated = [s for s in re.split(r"(?<=[.!?])\s+", note.context) if "deprecat" in s.lower()]
        return [] if any(_about_value(s) for s in stated) else entry
    named = _named(text, names)
    if named:
        return named
    return [] if worded_deprecated(text) or _valued(text, entry) or _about_value(text) else entry


def _documented_deprecations(obj: griffe.Object) -> tuple[str | None, dict[str, str]]:
    """What a class's or function's docstring says is deprecated, and what a `'deprecated'` default marks
    (DT-DATA-38). A docstring that opens with "Deprecated" (ollama's "Deprecated in favor of `embed`.") deprecates
    the object; so does a note before any section, unless its wording names parameters (then those) or it reads as
    being about arguments, a value or a usage. A note under a parameter entry deprecates that parameter
    (`_noted_params`). scikit-learn documents a constructor's
    parameters in the class docstring, so `__init__` reads those too, and it marks a parameter it deprecates with
    the default `'deprecated'`, warning only later, in `fit`: `OneHotEncoder(sparse='deprecated')`. An entry names
    a parameter of the signature, or any name when the function takes `**kwargs` (sqlalchemy's
    `create_engine(convert_unicode)`); `*args` and `**kwargs` themselves are never deprecated parameters."""
    if not isinstance(obj, (griffe.Function, griffe.Class)):
        return None, {}
    func = obj if isinstance(obj, griffe.Function) else None
    signature = list(func.parameters) if func is not None else []
    names = [p.name for p in signature if p.kind not in _VARIADIC]
    variadic = {p.name for p in signature if p.kind in _VARIADIC}
    open_kwargs = any(p.kind is griffe.ParameterKind.var_keyword for p in signature)
    docs = [obj.docstring]
    parent = obj.parent
    if func is not None and func.name == "__init__" and isinstance(parent, griffe.Class):
        docs.append(parent.docstring)
    whole: str | None = None
    params: dict[str, str] = {}
    noted: dict[str, str] = {}  # the first note under each parameter, whatever it says
    for k, docstring in enumerate(docs):
        doc = docstring.value if docstring is not None else ""
        if "deprecat" not in doc.lower():
            continue
        top, under = _doc_notes(doc)
        if top is not None and k == 0 and top.about_object:
            whole = top.message
        elif top is not None and k == 0 and not _PENDING_RE.search(top.text):
            named = _named(top.text, names)
            for n in named:
                params.setdefault(n, top.message)
            partial = _PARTIAL_NOTE_RE.search(_LITERAL_SPAN_RE.sub("", top.text))
            if not named and not partial and not _about_value(top.text):
                whole = top.message
        for entry, note in under:
            documented = [n for n in entry if n in names or (open_kwargs and n not in variadic)]
            for n in documented:
                noted.setdefault(n, note.message)
            for n in _noted_params(note, documented, list(dict.fromkeys([*names, *documented]))):
                params.setdefault(n, note.message)
    for p in signature:
        if p.kind not in _VARIADIC and str(p.default) in ("'deprecated'", '"deprecated"'):
            params.setdefault(p.name, noted.get(p.name, DEPRECATED_DEFAULT))
    return whole, params


def _first_notice(source: str) -> str | None:
    """The reading before DT-DATA-42, for a function whose source doesn't parse on its own: the first string that
    reads like a deprecation, for the whole function."""
    if not DEPRECATION_CATEGORY_RE.search(source):
        return None
    return _notice(source) or UNRECOVERABLE


def _notice(text: str) -> str | None:
    """Joined implicitly concatenated string literals in `text`: the first that reads like a deprecation."""
    for m in ADJACENT_LITERALS_RE.finditer(text):
        literal = _literal_text(m.group(0))
        if DEPRECATION_WORDS_RE.search(literal):
            return literal.strip()[:400]
    return None


def _literal_message(text: str) -> str | None:
    """A warning's text from the code that builds it: the literal that reads like a deprecation, else all of them."""
    joined = " ".join(_literal_text(m.group(0)) for m in ADJACENT_LITERALS_RE.finditer(text))
    return _notice(text) or joined.strip()[:400] or None


def _parse_function(source: str) -> tuple[ast.AST, str] | None:
    """A function's own source, parsed, and the text it was parsed from. A method whose docstring has lines
    indented less than the method doesn't dedent, so it is parsed as the body of an `if`."""
    for text in (textwrap.dedent(source), "if True:\n" + source):
        try:
            return _quiet_parse(text), text
        except (SyntaxError, ValueError):
            continue
    return None


def _quiet_parse(text: str, mode: str = "exec") -> ast.AST:
    """`ast.parse` without the SyntaxWarnings old code raises (invalid escape sequences) on stderr."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(text, mode=mode)


_KWARGS = "**"  # the condition reads the **kwargs mapping itself
_VARARGS = "*"
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_KWARGS_LOOKUPS = {"get", "pop", "setdefault"}


def _own_nodes(node: ast.AST):
    """`node` and what it contains, without entering nested functions, classes or lambdas."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, _SCOPES):
            yield from _own_nodes(child)


def _warn_calls(st: ast.stmt):
    """`warn(...)` calls, calls of warning helpers (`WARNING_HELPER_RE`), and of helpers that only build a
    deprecation message (`MESSAGE_BUILDERS`): streamlit 1.36's `experimental_rerun` logs one."""
    for node in _own_nodes(st):
        if isinstance(node, ast.Call) and (
            (name := _callee(node)) == "warn"
            or (name and (WARNING_HELPER_RE.match(name) or name in MESSAGE_BUILDERS))
        ):
            yield node


def _bound_names(target: ast.expr) -> list[str]:
    """The local names an assignment target binds: `x`, `a, *b`; not `self.x` or `d[k]`."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in _bound_names(e)]
    return []


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _render(node: ast.expr | None, values: dict[str, str] | None = None) -> str | None:
    """A message as the code builds it: a literal, an f-string, `+`, the template of `%`, or `.format()` with the
    arguments that are literals filled in; the parts that aren't literal as `{expression}`. `values`: names whose
    string value is known, the literals a caller passes for some parameters (DT-DATA-39)."""
    if (text := _str(node)) is not None:
        return text
    if isinstance(node, ast.Name) and values and node.id in values:
        return values[node.id]
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value
            if isinstance(v, ast.Constant)
            else _render(v.value, values)  # type: ignore[attr-defined]
            if isinstance(v.value, ast.Name) and values and v.value.id in values  # type: ignore[attr-defined]
            else "{" + ast.unparse(v.value) + "}"  # type: ignore[attr-defined]
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left, values), _render(node.right, values)
        if left is None and right is None:
            return None
        return (left if left is not None else "{" + ast.unparse(node.left) + "}") + (
            right if right is not None else "{" + ast.unparse(node.right) + "}"
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _render(node.left, values)
    if _is_format(node):
        template = _render(node.func.value, values)  # type: ignore[union-attr]
        return _filled(template, node, values) if template is not None else None  # type: ignore[arg-type]
    return None


def _is_format(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"


def _filled(template: str, call: ast.Call, values: dict[str, str] | None) -> str:
    """`template.format(...)` with the arguments that are string literals (or known `values`) filled in: pytest's
    `KEYWORD_MSG_ARG.format(func="exit")`. The others stay `{name}`."""

    def known(node: ast.expr) -> str | None:
        text = _str(node)
        if text is None and isinstance(node, ast.Name) and values:
            text = values.get(node.id)
        return text

    for kw in call.keywords:
        if kw.arg and (text := known(kw.value)) is not None:
            template = template.replace("{" + kw.arg + "}", text)
    positional = [known(a) for a in call.args]
    for i, text in enumerate(positional):
        if text is not None:
            template = template.replace("{" + str(i) + "}", text)
    parts = template.split("{}")
    if len(parts) == len(positional) + 1:
        template = parts[0] + "".join(
            (text if text is not None else "{}") + part
            for text, part in zip(positional, parts[1:], strict=True)
        )
    return template


class _BodyWarnings:
    """Every warning a function's body emits (DT-DATA-42), each read for what it deprecates.

    A warning counts when its category reads like a deprecation (`CALL_CATEGORY_RE`), or, with any other category
    or none (gradio), when its text says "deprecat", unless it is pending (`_pending`). Its text is what the code
    builds (`_render`), from the call itself, the nearest earlier string the function binds to the name it passes,
    or a module-level constant.

    - A warning guarded by a condition on an argument, whose text names that argument, deprecates the argument:
      `if proxies: warn("The 'proxies' argument is deprecated…")`. A name the condition looks up in **kwargs
      counts as an argument (`if "result_type" in kwargs`, `x = kwargs.pop("x")`, and `for key in kwargs: if key
      in (…)`, where the text may name it as `{key}`); `self`, `*args` and the **kwargs mapping itself don't. A
      condition that tests the argument's absence (`if x is None`) doesn't deprecate it.
    - A warning whose wording deprecates a named parameter ("'x' is deprecated") deprecates it, guarded or not.
    - Any other warning deprecates the function when nothing about an argument guards it, unless its wording
      deprecates a quoted name that is neither an argument nor the function's or its class's own name (another
      function's argument, an attribute); when something guards it, it says nothing that can be placed, and is
      left out.

    Warnings in nested functions are theirs (a decorator factory's `wrapper`), not the function's.
    """

    def __init__(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        text: str,
        func: griffe.Function,
        values: dict[str, str] | None = None,
    ):
        self.node, self.text, self.func = node, text, func
        self.values = values or {}  # literals a caller passes for some parameters (DT-DATA-39)
        args = node.args
        own = list(_own_nodes(node))[1:]
        loops = [n for n in own if isinstance(n, (ast.For, ast.AsyncFor))]
        loop_vars = {n for loop in loops for n in _bound_names(loop.target)}
        # A loop variable that reuses a parameter's name (`for key, val in kwargs.items()`) isn't the parameter.
        named = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs} - {"self", "cls"}
        self.named = named - loop_vars
        self.kwargs = args.kwarg.arg if args.kwarg else None
        self.varargs = args.vararg.arg if args.vararg else None
        parent = func.parent
        # `_experimental_rerun`, which streamlit serves as `experimental_rerun`, warns about `experimental_rerun`.
        self.own = {func.name, func.name.lstrip("_")} | (
            {parent.name} if parent is not None and parent.is_class else set()
        )
        # Loop variables that run over the names passed in **kwargs.
        self.kwarg_keys = {
            name
            for loop in loops
            if self._iterates_kwargs(loop.iter)
            for name in [
                loop.target.id
                if isinstance(loop.target, ast.Name)
                else getattr(loop.target.elts[0], "id", None)
                if isinstance(loop.target, ast.Tuple) and loop.target.elts
                else None
            ]
            if name
        }
        self.strings: dict[
            str, list[tuple[tuple[int, int], ast.expr]]
        ] = {}  # name -> where a string is bound
        self.derived: dict[str, frozenset[str]] = {}  # local name -> the arguments its value comes from
        # (text, guard, whether it can deprecate the function as a whole)
        self.found: list[tuple[str | None, frozenset[str], bool]] = []
        self.conditions: dict[str, frozenset[str]] = {}  # see `_Reading.conditions`
        for sub in own:
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and any(_str(c) is not None for c in _own_nodes(sub.value))
            ):
                self.strings.setdefault(sub.targets[0].id, []).append(
                    ((sub.lineno, sub.col_offset), sub.value)
                )
        for _ in range(2):  # a second pass picks up chains (`a = kwargs.pop("a")`, `b = a or …`)
            for sub in own:
                self._bind(sub)

    def _iterates_kwargs(self, it: ast.expr) -> bool:
        """`kwargs`, `kwargs.keys()`, `kwargs.items()`, `list(kwargs)`, `sorted(kwargs)`."""
        if isinstance(it, ast.Call) and not it.keywords and len(it.args) <= 1:
            if isinstance(it.func, ast.Attribute) and it.func.attr in ("keys", "items") and not it.args:
                it = it.func.value
            elif isinstance(it.func, ast.Name) and it.func.id in ("list", "sorted", "set") and it.args:
                it = it.args[0]
        return isinstance(it, ast.Name) and it.id == self.kwargs

    def _bind(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets, value = [node.target], node.iter
        else:
            return
        keys = self._keys(value)
        if not keys:
            return
        for name in (n for t in targets for n in _bound_names(t)):
            self.derived[name] = self.derived.get(name, frozenset()) | keys

    def _kwargs_names(self, expr: ast.AST) -> set[str]:
        """Names `expr` looks up in **kwargs: `"x" in kwargs`, `kwargs.get("x")`, `kwargs["x"]`, and, for a loop
        variable over its names, `key == "x"` or `key in ("x", "y")`."""
        found: set[str] = set()
        for n in _own_nodes(expr):
            if isinstance(n, ast.Compare) and len(n.ops) == 1:
                left, op, right = n.left, n.ops[0], n.comparators[0]
                if (
                    isinstance(op, (ast.In, ast.NotIn))
                    and isinstance(right, ast.Name)
                    and right.id == self.kwargs
                ):
                    found.add(_str(left) or "")
                elif isinstance(left, ast.Name) and left.id in self.kwarg_keys:
                    if isinstance(op, ast.Eq):
                        found.add(_str(right) or "")
                    elif isinstance(op, ast.In) and isinstance(right, (ast.List, ast.Tuple, ast.Set)):
                        found |= {_str(e) or "" for e in right.elts}
            elif (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in _KWARGS_LOOKUPS
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == self.kwargs
                and n.args
            ):
                found.add(_str(n.args[0]) or "")
            elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == self.kwargs:
                found.add(_str(n.slice) or "")
        return {f for f in found if f.isidentifier() and not keyword.iskeyword(f) and f not in self.named}

    def _keys(self, expr: ast.AST) -> frozenset[str]:
        """The arguments `expr` reads: named parameters, names it looks up in **kwargs, and `_KWARGS` / `_VARARGS`
        for the mappings themselves."""
        keys: set[str] = set()
        for n in _own_nodes(expr):
            if not isinstance(n, ast.Name):
                continue
            if n.id in self.named:
                keys.add(n.id)
            elif n.id == self.kwargs or n.id in self.kwarg_keys:
                keys.add(_KWARGS)
            elif n.id == self.varargs:
                keys.add(_VARARGS)
            elif n.id in self.derived:
                keys |= self.derived[n.id]
        return frozenset(keys | self._kwargs_names(expr)) if keys else frozenset()

    def _absent(self, test: ast.expr) -> bool:
        """`x is None`, `x is NOT_PASSED` (a sentinel default), `not x`, `"x" not in kwargs`: the branch runs when an
        argument is left out."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return isinstance(test.operand, ast.Name)
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
            return False
        op, right = test.ops[0], test.comparators[0]
        if isinstance(op, (ast.Is, ast.Eq)) and isinstance(test.left, ast.Name):
            return (isinstance(right, ast.Constant) and right.value is None) or (
                isinstance(op, ast.Is) and isinstance(right, (ast.Name, ast.Attribute))
            )
        return isinstance(op, ast.NotIn) and isinstance(right, ast.Name) and right.id == self.kwargs

    def _valued(self, test: ast.expr) -> bool:
        """`x == "auto"`, `x is True`, `x in ("a", "b")`, `x > 1`, `isinstance(x, bool)`: the branch runs for some
        values of an argument, so what is deprecated is passing those values, not the argument. A comparison with
        None or a sentinel (`x is not NOT_PASSED`, `x != "deprecated"`), `if x` and lookups in **kwargs test that
        it was passed."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):  # `not isinstance(line, str)`
            return self._valued(test.operand)
        if isinstance(test, ast.Call):
            return isinstance(test.func, ast.Name) and test.func.id in ("isinstance", "callable")
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.left, ast.Name)):
            return False
        if test.left.id in self.kwarg_keys:  # `key in ("x", "y")`: names passed in **kwargs
            return False
        op, right = test.ops[0], test.comparators[0]
        if isinstance(op, (ast.IsNot, ast.NotEq, ast.NotIn)):
            return False
        literal = isinstance(right, ast.Constant) and right.value is not None
        return literal or isinstance(right, (ast.List, ast.Tuple, ast.Set))

    def _condition(self, test: ast.expr, holds: bool) -> frozenset[str]:
        if holds and isinstance(test, ast.BoolOp):
            return frozenset().union(*(self._condition(v, True) for v in test.values))
        keys = self._keys(test)
        # Guarded by an argument, but not by its being passed: its absence, or (either way) some of its values.
        if keys and ((holds and self._absent(test)) or self._valued(test)):
            return frozenset({_KWARGS})
        return keys

    def _visit(self, stmts: list[ast.stmt], guard: frozenset[str], branched: bool = False) -> None:
        """`branched`: under an `if`, a loop, an `except` or a `case`, whatever the condition."""
        for st in stmts:
            if isinstance(st, _SCOPES):
                continue
            if isinstance(st, ast.If):
                self._visit(st.body, guard | self._condition(st.test, True), True)
                self._visit(st.orelse, guard | self._condition(st.test, False), True)
            elif isinstance(st, ast.While):
                self._visit(st.body, guard | self._condition(st.test, True), True)
                self._visit(st.orelse, guard, True)
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                self._visit(st.body, guard | self._keys(st.iter), True)
                self._visit(st.orelse, guard, True)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                self._visit(st.body, guard, branched)
            elif isinstance(st, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                self._visit(st.body, guard, branched)
                for handler in st.handlers:
                    self._visit(handler.body, guard, True)
                for block in (st.orelse, st.finalbody):
                    self._visit(block, guard, branched)
            elif isinstance(st, ast.Match):
                for case in st.cases:
                    self._visit(case.body, guard | self._keys(st.subject), True)
            else:
                for call in _warn_calls(st):
                    self._warning(call, guard, branched)

    def _warning(self, call: ast.Call, guard: frozenset[str], branched: bool) -> None:
        if _pending(call):  # numpy's `matrix`, matplotlib's `warn_deprecated(…, pending=True)` (DT-DATA-31)
            return
        if _callee(call) in MESSAGE_BUILDERS:
            built = self._built(call, call)
            if built is not None:
                self.found.append((built, guard, not branched))
            return
        if _callee(call) != "warn":  # a warning helper: a deprecation by its name
            text, subject = self._helper_text(call)
            # Under a condition that isn't about an argument, matplotlib's and sqlalchemy's helpers deprecate a
            # behaviour ("Applying clabel() to filled contours is deprecated"), and a subject that isn't a name is
            # something else (an rcParam, "encode_kwargs['show_progress_bar']"): neither is the whole function.
            if text is not None:
                self.found.append((text, guard, not branched and subject))
            return
        keywords = {k.arg: k.value for k in call.keywords if k.arg}
        message = call.args[0] if call.args else keywords.get("message")
        category = call.args[1] if len(call.args) > 1 else keywords.get("category")
        if (
            category is None
            and isinstance(message, ast.Call)
            and re.search(r"Warning$|Deprecat", ast.unparse(message.func))
        ):  # warn(DeprecationWarning("…"))
            category, message = message.func, (message.args[0] if message.args else None)
        text = self._text(message, call)
        if CALL_CATEGORY_RE.search(ast.unparse(category) if category is not None else "") or (
            text and re.search("deprecat", text, re.I)
        ):
            self.found.append((text, guard, True))

    def _text(self, node: ast.expr | None, at: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            where = (at.lineno, at.col_offset)  # type: ignore[attr-defined]
            bound = [value for pos, value in self.strings.get(node.id, []) if pos < where]
            if not bound:
                return self._constant(node.id)
            node = bound[-1]
        if node is None:
            return None
        if _is_format(node) and isinstance(node.func.value, ast.Name):  # type: ignore[union-attr]
            # `KEYWORD_MSG_ARG.format(func=func_name)`: a template bound earlier or kept in a constant.
            template = self._text(node.func.value, at)  # type: ignore[union-attr]
            return _filled(template, node, self.values) if template is not None else None  # type: ignore[arg-type]
        rendered = _render(node, self.values)
        if rendered is None:  # `_("…")`, a conditional expression: the literal that reads like a deprecation
            segment = ast.get_source_segment(self.text, node)
            return _notice(segment) if segment else None
        return " ".join(rendered.split())[:400] or None

    def _constant(self, name: str) -> str | None:
        return _module_constant(self.func.module, name)

    def _helper_text(self, call: ast.Call) -> tuple[str | None, bool]:
        """A warning helper's text, from its arguments by role (`_HelperArgs`): matplotlib's
        `_api.warn_deprecated("3.5", name="beginarrow", alternative="arrow")` is "`beginarrow` is deprecated. Use
        `arrow` instead.", and its `%(name)s` placeholders are filled in. A message streamlit builds with
        `make_deprecated_name_warning(old, new, …)` reads the same way. None when nothing is readable: such a
        helper often checks the arguments itself (`_warn_deprecated_field_args(**kwargs)`)."""

        def text(node: ast.expr) -> str | None:
            if isinstance(node, ast.Name):  # `msg = make_deprecated_name_warning(…)`, then `show_…(msg)`
                where = (call.lineno, call.col_offset)
                bound = [value for pos, value in self.strings.get(node.id, []) if pos < where]
                built = bound[-1] if bound and isinstance(bound[-1], ast.Call) else None
            else:
                built = node if isinstance(node, ast.Call) else None
            if built is not None and _callee(built) in MESSAGE_BUILDERS:
                return self._built(built, call)
            return self._text(node, call)

        args = _HelperArgs(call, None, False, text)
        message = args.message
        for key in ("name", "alternative", "obj_type"):
            if message and key in args.keywords:
                message = message.replace(f"%({key})s", args.keywords[key])
        if message is None and args.name is None:
            return None, False
        subject = args.name is None or args.name.isidentifier()
        return _compose(f"`{args.name}`" if args.name else None, args.name, message, args.new), subject

    def _built(self, built: ast.Call, at: ast.Call) -> str | None:
        """streamlit's `make_deprecated_name_warning(old, new, …)` as text: "`old` is deprecated. Use `st.new`
        instead.", with the package's name for `st` unless `include_st_prefix=False` (an argument's new name)."""
        helper = _HELPERS[_callee(built) or ""]
        parts = _HelperArgs(built, helper.positional, False, lambda n: self._text(n, at))
        old = parts.olds[0] if parts.olds else None
        new = parts.new
        prefixed = not any(
            k.arg == "include_st_prefix" and isinstance(k.value, ast.Constant) and k.value.value is False
            for k in built.keywords
        )
        if new and helper.top_level and prefixed:
            new = f"{self.func.package.path}.{new}"
        return _compose(f"`{old}`" if old else None, old, parts.message, new)

    def read(self) -> tuple[str | None, dict[str, str] | None]:
        self._visit(self.node.body, frozenset())
        whole: str | None = None
        params: dict[str, str] = {}
        for text, guard, entire in self.found:
            if text is None:
                if not guard and whole is None:
                    whole = UNRECOVERABLE
                continue
            candidates = guard - {_KWARGS, _VARARGS}
            # Wording that deprecates a name ("`y_pred` was renamed to `y_proba`") decides; else a guarded argument
            # the text mentions, outside `{…}` placeholders and other than as what to use instead.
            marked = {n: text for n in worded_deprecated(text) if n in self.named or n in candidates}
            plain = PLACEHOLDER_RE.sub(" ", text)
            for name in sorted(candidates) if not marked else ():
                if names_argument(plain, name):
                    marked[name] = text
                elif (
                    name not in self.named
                ):  # `for key in kwargs: if key in (…): warn(f"'{key}' is deprecated")`
                    placeholder = next((k for k in self.kwarg_keys if "{" + k + "}" in text), None)
                    if placeholder:
                        marked[name] = text.replace("{" + placeholder + "}", name)
            for name, message in marked.items():
                if name not in params:
                    params[name] = message
                    # The other arguments the condition tests, unless the text names them too: "The on_startup and
                    # on_shutdown parameters are deprecated" under `if on_startup or on_shutdown` (DT-DATA-39).
                    self.conditions[name] = frozenset(
                        n for n in candidates - {name} if not re.search(rf"\b{re.escape(n)}\b", plain)
                    )
            # A warning that deprecates some other quoted name (scikit-learn's `fit` on its constructor's
            # "`base_estimator` was renamed to `estimator`") isn't this function's deprecation.
            elsewhere = (
                not marked and bool(worded_deprecated(text)) and not set(worded_deprecated(text)) & self.own
            )
            if entire and not marked and not guard and not elsewhere and whole in (None, UNRECOVERABLE):
                whole = text
        return whole, params or None


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
    """A literal collection of strings, or a module-level table's keys: `list(_module_lookup.keys())`. A starred
    element adds the keys of a table this module fills, and nothing otherwise (DT-DATA-28: instructor 1.17's
    `[..., *PUBLIC_FACTORY_ATTRS.keys()]`, a dict another module computes), so the names it lists literally stay."""
    if (literal := _string_keys(node)) is not None:
        return frozenset(literal)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and any(
        isinstance(e, ast.Starred) for e in node.elts
    ):
        names: set[str] = set()
        for e in node.elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                names.add(e.value)
            elif isinstance(e, ast.Starred):
                names |= _table_keys(e.value, tables) or set()
            else:
                return None
        return frozenset(names)
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


@dataclass(frozen=True)
class _Const:
    """A table value that is a constant other than text: starlette's `"HTTP_413_REQUEST_ENTITY_TOO_LARGE": 413`,
    or `None` for "removed, no replacement"."""

    value: Any


@dataclass(frozen=True)
class _Ref:
    """A table value that is a bare name: pandas 3.1's `"RollingGroupby": RollingGroupBy`."""

    name: str


# What a table maps a key to, as far as the source says.
_TableValue = str | tuple[str | None, ...] | _Const | _Ref | None
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

    def cell(self, node: ast.expr, local: dict[str, Any] | None = None) -> _TableValue:
        """What a table maps a key to: text or a row of it as `value` reads it, or a constant or a bare name."""
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            return _Const(node.value)
        found = _row(self.value(node, local))
        if found is None and isinstance(node, ast.Name) and (local is None or node.id not in local):
            return _Ref(node.id)
        return found

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
            return {k.value: self.cell(v) for k, v in zip(node.keys, node.values, strict=True)}  # type: ignore[union-attr]
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
            out[key] = self.cell(node.value, local) if isinstance(node, ast.DictComp) else None
        return out


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_none_cell(value: _TableValue) -> bool:
    return isinstance(value, _Const) and value.value is None


def _truthy(value: _TableValue) -> bool:
    """Whether a table value is true, counting what the source doesn't say (a name, a call) as true."""
    if isinstance(value, _Const):
        return bool(value.value)
    return value is None or isinstance(value, _Ref) or bool(value)


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
            tables[name].update({k.arg: literals.cell(k.value) for k in call.keywords if k.arg})
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
        tables[name][sub.slice.value] = literals.cell(st.value)
    else:
        tables[name].pop(sub.slice.value, None)
    return True


@dataclass
class _Shim:
    module: str
    param: str
    tables: dict[str, dict[str, _TableValue]]  # literal collections in scope, by name
    exports: frozenset[str] = frozenset()  # what `__all__` lists, when the source says so statically
    mod: griffe.Module | None = None
    bound: dict[str, str] = field(default_factory=dict)  # module-level imports: local name -> dotted path
    helpers: dict[str, ast.FunctionDef | None] = field(default_factory=dict)
    literals: _Literals = field(default_factory=_Literals)
    # Names bound to `TABLE.get(name)`, with that table: starlette's `deprecated = __deprecated__.get(name)`.
    lookups: dict[str, dict[str, _TableValue]] = field(default_factory=dict)

    def scoped(self, st: ast.stmt) -> _Shim:
        """The shim after a statement of the function's body (DT-DATA-43). A literal table bound to a local name
        is read like a module-level one (starlette 0.48's `status`, pandas 3.1's `api.typing`), a name bound to
        `TABLE.get(name)` tests the table's keys (starlette's `deprecated = __deprecated__.get(name)`), and
        binding the name to anything else ends either."""
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            target, value = st.targets[0], st.value
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            target, value = st.target, st.value
        else:
            return self
        if not isinstance(target, ast.Name):
            return self
        got = self.got(value)
        keys = self.literals.table(value) if got is None else None
        if got is None and keys is None and target.id not in self.tables and target.id not in self.lookups:
            return self
        tables = {k: v for k, v in self.tables.items() if k != target.id}
        lookups = {k: v for k, v in self.lookups.items() if k != target.id}
        if got is not None:
            lookups[target.id] = got
        elif keys is not None:
            tables[target.id] = keys
        return replace(self, tables=tables, lookups=lookups)

    def got(self, node: ast.expr) -> dict[str, _TableValue] | None:
        """The table `node` looks the attribute name up in, when it is `TABLE.get(name)` or `TABLE.get(name,
        None)`, possibly bound to a name (`x`, `x := ...`): the table's keys give a value, every other name None."""
        if isinstance(node, ast.Name):
            return self.lookups.get(node.id)
        if isinstance(node, ast.NamedExpr):
            return self.got(node.value)
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.tables
            and not node.keywords
            and 1 <= len(node.args) <= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == self.param
            and (len(node.args) == 1 or _is_none(node.args[1]))
        ):
            return None
        return self.tables[node.func.value.id]

    def called(self, func: ast.expr, bound: dict[str, str]) -> tuple[ast.FunctionDef, int] | None:
        """The helper a call runs, and how many of its parameters the call doesn't pass (a classmethod's `cls`): a
        function, or a static or class method of a class (weaviate's `_Warnings.root_module_import(name, ...)`,
        DT-DATA-53)."""
        if isinstance(func, ast.Name):
            found = self.helper(func.id, bound)
            return (found, 0) if found is not None else None
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            return None
        path = bound.get(func.value.id, f"{self.module}.{func.value.id}")
        module_path, _, cls_name = path.rpartition(".")
        module = _module_at(self.mod, module_path) if self.mod is not None and module_path else None
        tree = _source_tree(module) if module is not None else None
        body = tree.body if tree is not None else []
        cls = next((st for st in body if isinstance(st, ast.ClassDef) and st.name == cls_name), None)
        members = cls.body if cls is not None else []  # `_util.warn_deprecated`: a module, no class
        method = next(
            (st for st in members if isinstance(st, ast.FunctionDef) and st.name == func.attr), None
        )
        kinds = {d.id for d in method.decorator_list if isinstance(d, ast.Name)} if method else set()
        if method is None or not kinds & {"staticmethod", "classmethod"}:
            return None
        return method, 1 if "classmethod" in kinds else 0

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
    returns: dict[str, ast.expr | None] = field(default_factory=dict)  # each name's `return` value


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
    inner = test.operand if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) else test
    if (rows := shim.got(inner)) is not None:
        # `if x:` after `x = TABLE.get(name)`, or `if x := TABLE.get(name):` (huggingface-hub 2.4's `utils`)
        return frozenset(k for k, v in rows.items() if _truthy(v)), inner is test
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    left, op, right = test.left, test.ops[0], test.comparators[0]
    if isinstance(op, (ast.Is, ast.IsNot)) and _is_none(right) and (rows := shim.got(left)) is not None:
        return frozenset(k for k, v in rows.items() if not _is_none_cell(v)), isinstance(op, ast.IsNot)
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


def _walrus_bindings(test: ast.expr) -> list[ast.Assign]:
    """The `x := value` bindings a condition makes, as the assignments they are, so the branch's warning can use
    `x`. Only at the top of the test, of `not`, of a comparison's left side, or of `and`/`or`."""
    if isinstance(test, ast.BoolOp):
        return [b for v in test.values for b in _walrus_bindings(v)]
    if isinstance(test, ast.UnaryOp):
        test = test.operand
    elif isinstance(test, ast.Compare):
        test = test.left
    if isinstance(test, ast.NamedExpr):
        return [ast.Assign(targets=[ast.Name(test.target.id, ast.Store())], value=test.value, lineno=0)]
    return []


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
                served.returns.setdefault(name, st.value)
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
                for bound in _walrus_bindings(st.test):  # `if (new := TABLE.get(name)) is not None:`
                    trail, shim = [*trail, bound], shim.scoped(bound)
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
            # The lookup ran for the keys that get past: a warning after the `try` can use what it bound.
            trail, shim = [*trail, *st.body], shim.scoped(st.body[0])
            continue
        # Other `try`, `with` and `match` bodies aren't followed: their returns don't count.
        trail = [*trail, st]
        shim = shim.scoped(st)
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


def _warning_text(
    node: ast.expr, env: dict[str, str], lookup: Callable[[ast.expr], str | None] | None = None
) -> str | None:
    """A warning's text; a placeholder `lookup` doesn't fill (`{TABLE[name]}`, `{new.__name__}`) reads "…"."""
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
        elif isinstance(v, ast.FormattedValue) and lookup is not None and (text := lookup(v.value)):
            parts.append(text)
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


def _deprecation_text(
    call: ast.Call, env: dict[str, str], lookup: Callable[[ast.expr], str | None] | None = None
) -> str | None:
    """A `warn(...)` call's message, when the text or the category says it is a deprecation."""
    text = _warning_text(call.args[0], env, lookup)
    category = (
        call.args[1]
        if len(call.args) > 1
        else next((k.value for k in call.keywords if k.arg == "category"), None)
    )
    category_name = ast.unparse(category) if category is not None else ""
    deprecation = DEPRECATION_WORDS_RE.search(text or "") or DEPRECATION_CATEGORY_RE.search(category_name)
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
    text = _warning_text(node, _known(env)) if node is not None else None
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
    package whose body warns counts as that warning.

    Tables the function defines itself fill it too (DT-DATA-43: starlette 0.48's `{deprecation_changes[name]}`),
    and so does the `__name__` of a table's bare-name value (pandas 3.1's `new = deprecated[name]` ...
    `{new.__name__}`)."""
    env = {shim.param: name, "__name__": shim.module}
    refs: dict[str, str] = {}  # local name -> the bare name a table value bound it to

    def lookup(node: ast.expr) -> str | None:  # reads `shim` as the loop below has scoped it
        if isinstance(node, ast.Attribute) and node.attr == "__name__":
            ref = refs.get(node.value.id) if isinstance(node.value, ast.Name) else None
            mapped = _mapped(node.value, name, shim)
            ref = ref or (mapped.name if isinstance(mapped, _Ref) else None)
            return _object_name(ref, _bindings(trail, shim), shim) if ref else None
        mapped = _mapped(node, name, shim)
        return mapped if isinstance(mapped, str) else None

    for st in trail:
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            target, mapped = st.targets[0], _mapped(st.value, name, shim)
            if isinstance(target, ast.Name) and isinstance(mapped, str):
                env[target.id] = mapped
            elif isinstance(target, ast.Name) and isinstance(mapped, _Ref):
                refs[target.id] = mapped.name
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
            return _deprecation_text(call, env, lookup)
        elif (
            isinstance(st, ast.Expr)
            and isinstance(st.value, ast.Call)
            and (found := shim.called(st.value.func, _bindings(trail, shim)))
            and (text := _helper_warning(_looked_up(st.value, lookup, found[1]), found[0], dict(env)))
            is not None
        ):
            return text
        shim = shim.scoped(st)
    return None


def _looked_up(call: ast.Call, lookup: Callable[[ast.expr], str | None], skip: int) -> ast.Call:
    """`call` with each argument a table lookup settles as that text (weaviate's `map_[name]` is "auth"), and
    `skip` leading placeholders for the parameters it doesn't pass (a classmethod's `cls`)."""
    args = [
        a if isinstance(a, ast.Constant) or (v := lookup(a)) is None else ast.Constant(v) for a in call.args
    ]
    return ast.Call(func=call.func, args=[ast.Constant(None)] * skip + args, keywords=call.keywords)


def _object_name(local: str, bound: dict[str, str], shim: _Shim) -> str | None:
    """`x.__name__` for a name the module binds or imports, when griffe finds what it is: a class's or a
    function's own name (what `from pandas.core.window import RollingGroupBy` imports keeps it), or a module's
    dotted path. Names from other distributions stay unknown."""
    found = _object_at(shim.mod, bound.get(local, f"{shim.module}.{local}")) if shim.mod is not None else None
    if found is None:
        return None
    if found.is_module:
        return found.path
    return found.name if found.is_class or found.is_function else None


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
    still lists (numpy 1.25's `import math` ... `del math`). `private`: served `_names`, as `names` (DT-DATA-22:
    flask 2.3's `_app_ctx_stack`)."""

    names: dict[str, str | None] = field(default_factory=dict)
    forwards: list[_Forward] = field(default_factory=list)
    bound: frozenset[str] | None = None
    copied: frozenset[str] = frozenset()
    deleted: frozenset[str] = frozenset()
    private: dict[str, str | None] = field(default_factory=dict)
    # Served names whose branch returns `getattr(<module>, name)`: the module, whose object it is (DT-DATA-32).
    sources: dict[str, str] = field(default_factory=dict)


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
        return _class_getattr(tree.body, mod)
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
    shim = _Shim(mod.path, args[0].arg, tables, exports, mod, bound, literals=literals)
    served = _Served()
    _reach(func.body, _Names(frozenset(), exclude=True), [], shim, served)
    names: dict[str, str | None] = {}
    private: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    for name, trail in sorted(served.names.items()):
        if name.isidentifier():
            into = private if _is_private(name) else names
            into[name] = _shim_message(trail, name, shim)
            value = served.returns.get(name)
            if value is not None and (source := _forwarded(value, trail, shim)):
                sources[name] = source
    if not served.forwards:
        return _GetattrShim(names, deleted=frozenset(deleted), private=private, sources=sources)
    own, copied = _module_names(tree.body)
    forwards = [_Forward(target, excluded, trail, shim) for target, excluded, trail in served.forwards]
    return _GetattrShim(names, forwards, own, copied, frozenset(deleted), private, sources)


def _class_getattr(body: list[ast.stmt], mod: griffe.Module) -> _GetattrShim:
    """A module-level `class __getattr__`, which matplotlib turns into the module's `__getattr__` with
    `@_api.caching_module_getattr`: each public property it defines is a name the module serves, with the
    deprecation its `deprecated(...)` decorator states (DT-DATA-53: `matplotlib.rcsetup.interactive_bk` from 3.9)."""
    cls = next((st for st in body if isinstance(st, ast.ClassDef) and st.name == "__getattr__"), None)
    names: dict[str, str | None] = {}
    for item in cls.body if cls is not None else []:
        if not isinstance(item, ast.FunctionDef) or _is_private(item.name):
            continue
        decorators = item.decorator_list
        if not any(isinstance(d, ast.Name) and d.id == "property" for d in decorators):
            continue
        marks = [d for d in decorators if DEPRECATED_DECORATOR_RE.match(ast.unparse(d))]
        call = next((d for d in marks if isinstance(d, ast.Call)), None)
        names[item.name] = _decorator_reading(call, mod, item.name) if marks else None
    return _GetattrShim(names)


@dataclass(frozen=True)
class _Imports:
    """How a module's top-level `from x import y` statements bind names. An import under `if TYPE_CHECKING:` or
    `try:` may not exist at runtime, so only top-level statements count."""

    redundant: frozenset[str]  # `from x import y as y`, the explicit re-export convention
    plain: frozenset[str]  # `from x import y`, with x outside the standard library
    imports_only: bool  # nothing but imports and a docstring, like sqlalchemy 1.4's `schema.py`
    stars: tuple[str, ...] = ()  # the modules `from x import *` statements name, absolute, in order
    raises: bool = False  # a top-level `raise`: importing the module always fails (mcp 2.x's `fastmcp.py`)
    deleted: frozenset[str] = frozenset()  # names the module `del`s and doesn't bind again (pandas' `del v`)


def _is_docstring(st: ast.stmt) -> bool:
    return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


def _deleted_names(body: list[ast.stmt]) -> frozenset[str]:
    """Names a module's top level, or a block under it (`if`, `try`, `with`), deletes and doesn't bind again."""
    deleted: set[str] = set()
    for st in body:
        if isinstance(st, ast.Delete):
            deleted.update(t.id for t in st.targets if isinstance(t, ast.Name))
            continue
        if isinstance(st, (ast.If, ast.Try, ast.With)):
            nested = [*st.body, *getattr(st, "orelse", []), *getattr(st, "finalbody", [])]
            nested += [s for h in getattr(st, "handlers", []) for s in h.body]
            deleted |= _deleted_names(nested)
            continue
        bound, _ = _module_names([st])
        deleted -= bound or set()
    return frozenset(deleted)


def module_imports(mod: griffe.Module) -> _Imports:
    tree = _source_tree(mod)
    body = tree.body if tree is not None else []
    redundant: set[str] = set()
    plain: set[str] = set()
    stars: list[str] = []
    for st in body:
        if isinstance(st, ast.ImportFrom):
            stdlib = st.level == 0 and (st.module or "").split(".")[0] in STDLIB_MODULES
            redundant.update(a.name for a in st.names if a.asname == a.name)
            plain.update(a.name for a in st.names if a.asname is None and a.name != "*" and not stdlib)
            if any(a.name == "*" for a in st.names):
                stars.append(_absolute(mod.path, mod.is_init_module, st.level, st.module))
    imports_only = bool(body) and all(
        isinstance(st, (ast.Import, ast.ImportFrom)) or _is_docstring(st) for st in body
    )
    raises = any(isinstance(st, ast.Raise) for st in body)
    return _Imports(
        frozenset(redundant), frozenset(plain), imports_only, tuple(stars), raises, _deleted_names(body)
    )


# `__all__` built by code rather than written out: a method call, `+=`, or a value that isn't a literal.
_ALL_IN_CODE_RE = re.compile(r"__all__\s*(?:\.\s*\w+\s*\(|\+=|=\s*(?![\s\[(=]))")
# A literal `__all__` with a starred element: instructor 1.17's `[..., *PUBLIC_FACTORY_ATTRS.keys()]`. griffe can't
# evaluate it and reads no `__all__` at all (DT-DATA-28).
_ALL_STARRED_RE = re.compile(r"__all__\s*(?::[^=\n]*)?=\s*[\[(][^\])]*\*")


@dataclass(frozen=True)
class _Exports:
    names: frozenset[str] | None  # None: the source never binds `__all__`
    stars: tuple[str, ...]  # modules the source star-imports, in order


def _object_at(mod: griffe.Module, path: str) -> griffe.Object | None:
    try:
        return _resolve(mod.modules_collection.get_member(path))
    except (KeyError, ValueError, griffe.AliasResolutionError, griffe.CyclicAliasError):
        return None


def _module_at(mod: griffe.Module, path: str) -> griffe.Module | None:
    found = _object_at(mod, path)
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
    def starred(mod: griffe.Module) -> bool:
        """Whether `mod`'s `.py` assigns `__all__` a literal with a starred element."""
        path = mod.filepath
        if not isinstance(path, Path) or path.suffix != ".py":
            return False
        try:
            return bool(_ALL_STARRED_RE.search(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            return False

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
        return "__all__" in text and (
            path.with_suffix(".pyi").exists()
            or bool(_ALL_IN_CODE_RE.search(text))
            or bool(_ALL_STARRED_RE.search(text))
        )

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


def _is_private(name: str) -> bool:
    """`_name` and `__name`, not a dunder."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _is_test_name(part: str) -> bool:
    return part in {"tests", "conftest"} or part.startswith("test_")


def _is_test_module(path: str) -> bool:
    """Bundled test suites (`numpy.lib.tests.test_shape_base`) aren't API; `numpy.testing` and `django.test` are."""
    return any(_is_test_name(part) for part in path.split("."))


def _resolve(member: griffe.Object | griffe.Alias) -> griffe.Object | None:
    if not member.is_alias:
        return member  # type: ignore[return-value]
    try:
        return member.final_target  # type: ignore[union-attr]
    except griffe.CyclicAliasError:
        return _through_cycle(member)  # type: ignore[arg-type]
    except griffe.AliasResolutionError:
        return None


def _through_cycle(alias: griffe.Alias) -> griffe.Object | None:
    """What an import griffe reports as cyclic names, found by following each import's path by hand (DT-DATA-32).

    From numpy 2.2, `numpy._core.multiarray.ndarray` imports `numpy.ndarray`, a class the stub defines, yet griffe
    reports the cycle `numpy._core.ndarray` -> `numpy._core.numeric.ndarray` -> `numpy._core.multiarray.ndarray` ->
    `numpy.ndarray` -> `numpy._core.ndarray`. The `numpy.core` forwarders then served `ndarray`, `dtype`, `nditer`
    and `broadcast` as unresolved imports in 2.2-2.4 and as classes before and after. None for a real cycle."""
    seen: set[str] = set()
    obj: griffe.Object | griffe.Alias = alias
    while obj.is_alias:
        path = obj.target_path  # type: ignore[union-attr]
        if path in seen:
            return None
        seen.add(path)
        try:
            obj = alias.modules_collection.get_member(path)
        except (KeyError, ValueError, AttributeError, griffe.AliasResolutionError, griffe.CyclicAliasError):
            return None  # a path through another import griffe can't follow (chromadb 0.5.7)
    return obj  # type: ignore[return-value]


class _Walker:
    def __init__(self, snapshot: ApiSnapshot, root_prefixes: tuple[str, ...]):
        self.snap = snapshot
        self.roots = root_prefixes
        self.visiting: set[tuple[str, str]] = set()
        self.shims: dict[str, dict[str, str | None]] = {}
        self.imports: dict[str, _Imports] = {}
        self.runtime_all = _RuntimeAll()
        self.exported: dict[str, frozenset[str]] = {}
        self.finished: set[str] = set()
        self.unlisting: set[tuple[str, str]] = set()
        self.private_classes: dict[str, griffe.Class] = {}  # classes `internal` finds, by canonical path
        # Classes a public class's member is (a nested class, or `Generative = _Generative`), recorded with their
        # own members after the public walk (DT-CORE-32).
        self.member_classes: dict[str, griffe.Class] = {}

    def _shapes(self, mod: griffe.Module) -> _Imports:
        if mod.path not in self.imports:
            self.imports[mod.path] = module_imports(mod)
        return self.imports[mod.path]

    def _finish_stars(self, mod: griffe.Module) -> None:
        """Add the names `mod`'s star imports bind that griffe left out (DT-DATA-33), by griffe's own rule.

        griffe expands a package's star imports, but merges what they bring only after it has recursed into the
        package's submodules. A submodule that star-imports the package itself then gets only the names the
        package imports by name: pydantic 1.10.17's `pydantic/v1/__init__.py` is `from pydantic import *`, and
        `pydantic.v1.BaseModel` read removed in 1.10.17 and added again in 2.0. Names griffe already bound stay.
        """
        if mod.path in self.finished:
            return
        self.finished.add(mod.path)
        for source in reversed(self._shapes(mod).stars):  # the last star import wins, as at runtime
            target = _module_at(mod, source)
            if target is None or target.path == mod.path:
                continue  # compiled, another distribution (griffe leaves a marker), or itself
            self._finish_stars(target)
            for name, member in list(target.members.items()):
                if name in mod.members:
                    continue
                try:
                    exposed = member.is_wildcard_exposed
                    loops = member.is_alias and member.target_path == f"{mod.path}.{name}"  # type: ignore[union-attr]
                except (griffe.AliasResolutionError, griffe.CyclicAliasError):
                    continue
                if exposed and not loops:
                    # Point past an alias, so one griffe can't resolve keeps the path it names (numpy 2.2's
                    # `numpy.matlib.array2string` -> `numpy._core.array2string`, like `numpy.array2string`).
                    to = member.target_path if member.is_alias else member  # type: ignore[union-attr]
                    mod.set_member(name, griffe.Alias(name, to, parent=mod, wildcard_imported=True))

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

    def _rebound(
        self, mod: griffe.Module, name: str, member: griffe.Object | griffe.Alias
    ) -> griffe.Object | griffe.Alias | None:
        """What a package binds a submodule's name to when it imports a same-named object from it (DT-DATA-54):
        after `from .patch import patch`, `instructor.patch` is the function, though griffe lists the submodule.
        `import instructor.patch` still finds the module, so its names stay under the path too. A star import
        whose module exports the name rebinds it the same way. None for anything else."""
        if member.is_alias or not member.is_module:
            return None
        target = mod.imports.get(name)
        found: griffe.Object | griffe.Alias | str | None = None
        if target is not None and target != member.path:
            try:
                found = mod.modules_collection[target]
            except Exception:  # outside what griffe loaded
                return None
        elif target is None and self.runtime_all.of(mod).stars:
            found = self._from_stars(mod, name)
        if found is None or isinstance(found, str):
            return None
        resolved = _resolve(found)
        return found if resolved is not None and not resolved.is_module else None

    def module(self, mod: griffe.Module, public_path: str, depth: int = 0, forwarded: bool = False) -> None:
        key = (mod.path, public_path)
        if key in self.visiting or depth > MAX_DEPTH or self._shapes(mod).raises:
            return
        self.visiting.add(key)
        self._finish_stars(mod)
        self.record(SymbolRecord(public_path, "module", mod.path))
        for source in self.runtime_all.of(mod).stars:  # another distribution's, which griffe didn't load
            outside = not self._internal(source) and source.split(".")[0] not in STDLIB_MODULES
            if outside and _module_at(mod, source) is None:
                unread = self.snap.star_sources.setdefault(public_path, [])
                if source not in unread:
                    unread.append(source)
        # A module another one forwards to is public as at its own path, but its submodules aren't walked again.
        reexporter = mod.path if (public_path == mod.path or forwarded) and mod.is_init_module else None
        walk_from = None if forwarded else reexporter
        exported = self._exported(mod)
        if exported and not mod.exports and _RuntimeAll.starred(mod):
            # griffe read no `__all__` from one it can't evaluate (DT-DATA-28): instructor 1.17's
            # `instructor.v2` lists its lazy names next to `*PUBLIC_FACTORY_ATTRS.keys()`, and its imports and
            # `__all__` itself read as public.
            mod.exports = sorted(exported)
        for name, member in list(mod.members.items()):
            if member.is_alias and member.wildcard:  # type: ignore[union-attr]  # a star import griffe couldn't expand
                continue
            if name in exported or self._public(mod, name, member, reexporter):
                if (rebound := self._rebound(mod, name, member)) is not None:
                    self.member(
                        rebound, f"{public_path}.{name}", depth, walk_from
                    )  # recorded first, so it wins
                    self.snap.module_aliases.setdefault(f"{public_path}.{name}", member.path)
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
                # A real attribute never reaches __getattr__; one the module deletes again does, and so does one
                # it binds only for type checkers (pandas 3.1's `RollingGroupby`, under `if TYPE_CHECKING:`).
                member = mod.members.get(name)
                if member is None or name in shim.deleted or not member.runtime:
                    path = f"{public_path}.{name}"
                    if not self._served_object(mod, path, name, shim, message, depth):
                        self.record(SymbolRecord(path, "getattr", canonical, None, message))
                        # Typed only in a stub (matplotlib's `rcsetup.pyi`), it is already recorded: the warning
                        # the module serves it with is still its deprecation (DT-DATA-53).
                        rec = self.snap.symbols[path]
                        if message and rec.deprecated is None:
                            self.snap.symbols[path] = replace(rec, deprecated=message)

    def _served_object(
        self, mod: griffe.Module, path: str, name: str, shim: _GetattrShim, message: str | None, depth: int
    ) -> bool:
        """Record a name `__getattr__` serves as `getattr(<module>, name)` as that module's object, with the
        branch's warning (DT-DATA-32): django 6.0's `core.mail.SafeMIMEText` is `core.mail.message.SafeMIMEText`,
        and its members read removed in 6.0. False when the module or the name in it can't be found."""
        source = shim.sources.get(name)
        target = _module_at(mod, source) if source else None
        found = target.members.get(name) if target is not None and target.path != mod.path else None
        resolved = _resolve(found) if found is not None else None
        if resolved is None or resolved.is_module:
            return False
        self.member(found, path, depth)  # type: ignore[arg-type]
        rec = self.snap.symbols.get(path)
        if rec is not None and message and rec.deprecated is None:
            self.snap.symbols[path] = replace(rec, deprecated=message)
        return rec is not None

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
        if mod.exports and not name.startswith("_") and member.runtime:
            return self._unlisted(mod, name, member)
        source = member.target_path if member.is_alias else mod.imports.get(name)  # type: ignore[union-attr]
        if source is None or mod.exports or name.startswith("_"):
            return False
        shapes = self._shapes(mod)
        if name in shapes.redundant or (shapes.imports_only and name in shapes.plain):
            return True
        if reexporter is None:
            return False
        return self._internal(source) or (mod.path in self.roots and name in shapes.plain)

    def _unlisted(self, mod: griffe.Module, name: str, member: griffe.Object | griffe.Alias) -> bool:
        """Whether a name `mod`'s `__all__` leaves out is public anyway (DT-DATA-44).

        `__all__` says what `from mod import *` binds, and libraries document names it leaves out: django's
        `django.contrib.postgres.indexes.OpClass`, `django.db.close_old_connections`, openai 1.x's `openai.api_key`
        (which read removed in 1.0.0). So a module's own definitions count, as they do without `__all__`, unless the
        module deletes them or only annotates them. So do the public names a star import brings from inside the
        package: pandas' `pandas/api/types/__init__.py` star-imports its 39 `is_*` functions and lists six other
        names. Imports by name stay implementation, as `__all__` says.
        """
        if not member.is_alias:
            if member.is_attribute and member.value is None:  # type: ignore[union-attr]  # `timeout: float`
                return False
            return name not in self._shapes(mod).deleted
        if not member.wildcard_imported:  # type: ignore[union-attr]
            return False
        key = (mod.path, name)
        if key in self.unlisting:  # star imports that import each other
            return False
        self.unlisting.add(key)
        try:
            for source in reversed(self.runtime_all.of(mod).stars):  # the last star import that binds it wins
                target = _module_at(mod, source)
                if target is None or target.path == mod.path or not self._star_exports(target, name):
                    continue
                found = target.members.get(name)
                if found is None or not self._internal(target.path):
                    return False
                reexporter = target.path if target.is_init_module else None
                return name in self._exported(target) or self._public(target, name, found, reexporter)
            return False
        finally:
            self.unlisting.discard(key)

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
            # A module that always raises on import isn't API, and neither is anything below it (DT-DATA-34): mcp
            # 2.x keeps `mcp/server/fastmcp.py` only to raise "FastMCP was renamed to MCPServer", dspy 3.4 keeps
            # `dspy/core/types.py` to say where its 3.3 types went. A raise under `if` or `try` doesn't count.
            if _is_test_module(target.path) or self._shapes(target).raises:  # type: ignore[arg-type]
                return
            if self._internal(target.path) and self._walks(target.path, public_path, reexporter):
                self.module(target, public_path, depth + 1)  # type: ignore[arg-type]
            else:
                self.record(SymbolRecord(public_path, "module", target.path))
        elif target.is_class:
            self.cls(target, public_path)  # type: ignore[arg-type]
        elif target.is_function:
            self.record(
                SymbolRecord(public_path, "function", target.path, _params(target), *deprecations(target))
            )  # type: ignore[arg-type]
        elif (bound := _bound_class(target)) is not None:
            self.cls(bound, public_path)  # `masked_array = MaskedArray`: the class itself (DT-CORE-32)
        else:
            self.record(
                SymbolRecord(public_path, target.kind.value, target.path, None, deprecation_message(target))
            )

    def internal(self, root: griffe.Module) -> None:
        """Record what the modules under `root` bind that isn't public API (DT-DATA-22), after the public walk.

        Integration libraries import these, and their users get the error when they go: motor 3.5 imports
        pymongo 4.8's `pymongo.cursor._QUERY_OPTIONS`, openinference imports `handle_response_model` from
        instructor's `core.patch`, which only imported it, and fastapi-pagination imports sqlalchemy's
        `AsyncConnection` from sqlmodel's `ext.asyncio.session`. Recorded by the path they're bound at: a module's
        private names, its imports from outside the standard library, the private names its `__getattr__`
        serves, and every name of a module that isn't public (`pymongo._csot`), with the module itself. Not
        recorded: dunders, names only type checkers see, annotations without a value, names the module deletes,
        class members, test modules and modules that always raise. Kind and canonical path only: no signatures.
        """
        stack, seen = [root], set()
        while stack:
            mod = stack.pop()
            if mod.path in seen or _is_test_module(mod.path) or self._shapes(mod).raises:
                continue
            seen.add(mod.path)
            if mod.path not in self.snap.symbols and mod.path not in self.roots:
                self.snap.internal[mod.path] = SymbolRecord(mod.path, "module", mod.path)
            deleted = self._shapes(mod).deleted
            for name, member in list(mod.members.items()):
                if not member.is_alias and member.is_module:
                    stack.append(member)  # type: ignore[arg-type]
                    continue
                path = f"{mod.path}.{name}"
                if path in self.snap.symbols or (name.startswith("__") and name.endswith("__")):
                    continue
                if name in deleted or not member.runtime:
                    continue
                rec = self._internal_record(path, member)
                if rec is not None:
                    self.snap.internal[path] = rec
                    if self.snap.own_members and rec.kind == "class" and self._internal(rec.canonical or ""):
                        target = _resolve(member)
                        if isinstance(target, griffe.Class):
                            self.private_classes.setdefault(target.path, target)
            hook = mod.members.get("__getattr__")
            if hook is not None and not hook.is_alias:
                if mod.path not in self.shims:
                    self.shims[mod.path] = getattr_served(mod)
                shim = self.shims[mod.path]
                for name, message in shim.private.items():
                    member = mod.members.get(name)
                    if member is None or name in shim.deleted or not member.runtime:
                        path = f"{mod.path}.{name}"
                        self.snap.internal[path] = SymbolRecord(
                            path, "getattr", f"{mod.path}.__getattr__", None, message
                        )

    def _internal_record(self, path: str, member: griffe.Object | griffe.Alias) -> SymbolRecord | None:
        if not member.is_alias:
            if member.is_attribute and member.value is None:  # type: ignore[union-attr]  # `timeout: float`
                return None
            return SymbolRecord(path, member.kind.value, member.path)
        if member.wildcard:  # type: ignore[union-attr]  # a star import griffe couldn't expand
            return None
        source = member.target_path  # type: ignore[union-attr]
        if source.split(".")[0] in STDLIB_MODULES or source.startswith("__future__."):
            return None
        target = _resolve(member)
        if target is None:
            return SymbolRecord(path, "alias", source)
        return SymbolRecord(path, target.kind.value, target.path)

    def cls(self, cls: griffe.Class, public_path: str) -> None:
        self.record(SymbolRecord(public_path, "class", cls.path, None, deprecation_message(cls)))
        if self.snap.own_members:
            self._class_record(cls)
            return
        try:
            members = cls.all_members  # includes inherited members when bases resolve
        except Exception as exc:  # griffe raises several resolution errors for external bases
            self.snap.errors.append(f"{cls.path}: inherited members unavailable ({type(exc).__name__})")
            members = cls.members
        for name, member in members.items():
            if _is_member_name(name) and (rec := _member_record(f"{public_path}.{name}", member)):
                self.record(rec)

    def _class_record(self, cls: griffe.Class, public: bool = True) -> None:
        """Record `cls` with its MRO and own members, and the own members of each class in its MRO (DT-DATA-16).
        Inherited members are looked up as griffe's `all_members` finds them: no MRO when griffe can't compute
        one (an inheritance cycle), and an error recorded when resolving the bases fails otherwise. `public`: a
        class users reach by a public path; else one that isn't public API, whose members are all internal and
        whose failures aren't the release's (DT-DATA-46)."""
        rec = self._own_members(cls, public)
        if rec.mro is not None:
            return
        try:
            mro = cls.mro()
        except ValueError:  # an inheritance cycle: all_members has no inherited members then
            mro = []
        except Exception as exc:  # griffe raises several resolution errors for external bases
            if public:
                self.snap.errors.append(f"{cls.path}: inherited members unavailable ({type(exc).__name__})")
            mro = []
        rec.mro = [base.path for base in mro]
        for base in mro:
            self._own_members(base, public)

    def _own_members(self, cls: griffe.Class, public: bool = True) -> ClassRecord:
        rec = self.snap.classes.get(cls.path)
        if rec is None:
            rec = self.snap.classes[cls.path] = ClassRecord(cls.path, external=_external_bases(cls))
            for name, member in cls.members.items():
                path = f"{cls.path}.{name}"
                if public and _is_member_name(name):
                    rec.members[name] = _member_record(path, member) or SymbolRecord(path, HIDDEN)
                    if rec.members[name].kind == "class" and (inner := _class_of(member)) is not None:
                        self.member_classes.setdefault(inner.path, inner)
                elif _is_member_name(name) or _is_private(name):
                    rec.internal[name] = _internal_member(path, member)
        return rec

    def record_member_classes(self) -> None:
        """Record each class a public class's member is, with its own members, as public classes are (DT-CORE-32):
        `weaviate.classes.config.Configure.Generative.cohere` is `_Generative.cohere`."""
        done: set[str] = set()
        while pending := [c for path, c in self.member_classes.items() if path not in done]:
            for cls in pending:
                done.add(cls.path)
                try:
                    self._class_record(cls)
                except Exception:  # the public API is recorded; one class's bases mustn't sink it
                    continue

    def record_private_classes(self) -> None:
        """Record the classes `internal` found that no public path reaches (DT-DATA-46), after every public walk:
        haystack called weaviate's `client.collections._get_all()`, a method the private `_Collections` inherited
        from `_CollectionsBase` until 4.7.0. Classes public paths reach keep the members they have."""
        for cls in self.private_classes.values():
            try:
                self._class_record(cls, public=False)
            except Exception:  # the public API is recorded; one class's bases mustn't sink it
                continue


def _is_member_name(name: str) -> bool:
    """A class member the index records: a public name, `__init__` or `__call__`."""
    return not name.startswith("_") or name in {"__init__", "__call__"}


def _internal_member(path: str, member: griffe.Object | griffe.Alias) -> SymbolRecord:
    """A member that isn't public API (DT-DATA-46): kind and canonical path only, as other internal names; `HIDDEN`
    when griffe can't resolve it or it is neither function, attribute nor class."""
    target = _resolve(member)
    if target is None or not (target.is_function or target.is_attribute or target.is_class):
        return SymbolRecord(path, HIDDEN)
    return SymbolRecord(path, target.kind.value, target.path)


def _member_record(path: str, member: griffe.Object | griffe.Alias) -> SymbolRecord | None:
    """A class member's record at `path`; None for an alias griffe can't resolve and for other kinds of object."""
    target = _resolve(member)
    if target is None:
        return None
    if target.is_function:
        return SymbolRecord(path, "function", target.path, _params(target), *deprecations(target))  # type: ignore[arg-type]
    if target.is_attribute and (bound := _bound_class(target)) is not None:
        return SymbolRecord(path, "class", bound.path, None, deprecation_message(target))
    if target.is_attribute:
        return SymbolRecord(path, "attribute", target.path, None, deprecation_message(target))
    if target.is_class:
        return SymbolRecord(path, "class", target.path, None, deprecation_message(target))
    return None


def _bound_class(attr: griffe.Object) -> griffe.Class | None:
    """The package's class an attribute is bound to by name (`Generative = _Generative`, `masked_array =
    MaskedArray`), which Python then finds under the attribute's name too (DT-CORE-32); None for any other value,
    a call (`x = Cls()` is an instance) or a subscript among them."""
    value = getattr(attr, "value", None)
    if not isinstance(value, (griffe.ExprName, griffe.ExprAttribute)):
        return None
    try:
        found = attr.modules_collection[value.canonical_path]
    except Exception:  # outside the package, or not loaded
        return None
    found = _resolve(found) if isinstance(found, griffe.Alias) else found
    return found if found is not None and found.is_class else None  # type: ignore[return-value]


def _class_of(member: griffe.Object | griffe.Alias) -> griffe.Class | None:
    """The class a class member is: a nested class, or a class it's bound to."""
    target = _resolve(member)
    if target is None:
        return None
    if target.is_class:
        return target  # type: ignore[return-value]
    return _bound_class(target) if target.is_attribute else None


def _external_bases(cls: griffe.Class) -> list[str]:
    """`cls`'s direct bases that this analysis didn't load, canonical (DT-CORE-32): another distribution's
    (`starlette.applications.Starlette`), the standard library's (`enum.Enum`, `Exception`). `object` is left out."""
    out: list[str] = []
    collection = cls.modules_collection
    for base in cls.bases:
        path = base if isinstance(base, str) else getattr(base, "canonical_path", None)
        # A base imported through the package's own re-export of another distribution's class is an alias in the
        # package (DT-DATA-55): langchain-community's `BasePDFLoader(BaseLoader)` takes `BaseLoader` from its own
        # `document_loaders.base`, which imports it from langchain-core. Follow it to the path it names.
        followed = False
        for _ in range(MAX_DEPTH):
            if not path or path == "object":
                path = None
                break
            try:
                obj = collection[path]
            except Exception:  # not loaded: from outside the package
                break
            if not isinstance(obj, griffe.Alias):
                path = None
                break
            path, followed = obj.target_path, True
        else:
            path = None
        # Followed to a name a loaded package doesn't have (a lazy `__getattr__` name): not another distribution's.
        if path and not (followed and path.split(".", 1)[0] in collection.members) and path not in out:
            out.append(path)
    return out


def _is_sys_modules(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _module_key(node: ast.expr, module: str) -> str | None:
    """The module name a `sys.modules[...]` key spells: a literal, `__name__`, an f-string of those, or a sum."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id == "__name__":
        return module
    if isinstance(node, ast.JoinedStr):
        parts = [
            _module_key(v.value if isinstance(v, ast.FormattedValue) else v, module)
            if not isinstance(v, ast.FormattedValue) or (v.conversion == -1 and v.format_spec is None)
            else None
            for v in node.values
        ]
        return None if None in parts else "".join(parts)  # type: ignore[arg-type]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _module_key(node.left, module), _module_key(node.right, module)
        return None if left is None or right is None else left + right
    return None


def module_aliases(mod: griffe.Module) -> dict[str, str]:
    """`sys.modules[alias] = <module>` statements at a module's top level (in `if` and `try` blocks too), alias ->
    module (DT-DATA-52): fastmcp 3.4's `sys.modules[f"{__name__}.tool"] = sys.modules[f"{__name__}.base"]`, or a
    module the file imported (`sys.modules[__name__ + ".old"] = base`)."""
    filepath = mod.filepath
    try:  # read, not parsed, first: only a few files of a package mention sys.modules (transformers has 2,500)
        if not isinstance(filepath, Path) or "sys.modules" not in filepath.read_text(
            encoding="utf-8", errors="replace"
        ):
            return {}
    except OSError:
        return {}
    tree = _source_tree(mod)
    if tree is None:
        return {}
    out: dict[str, str] = {}

    def target(node: ast.expr) -> str | None:
        if isinstance(node, ast.Subscript) and _is_sys_modules(node.value):
            return _module_key(node.slice, mod.path)
        if isinstance(node, ast.Name) and (member := mod.members.get(node.id)) is not None:
            found = _resolve(member)
            return found.path if found is not None and found.is_module else None
        return None

    def visit(stmts: list[ast.stmt]) -> None:
        for st in stmts:
            if isinstance(st, ast.If):
                visit(st.body)
                visit(st.orelse)
            elif isinstance(st, ast.Try):
                for block in (st.body, *(h.body for h in st.handlers), st.orelse, st.finalbody):
                    visit(block)
            elif (
                isinstance(st, ast.Assign)
                and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Subscript)
                and _is_sys_modules(st.targets[0].value)
                and (alias := _module_key(st.targets[0].slice, mod.path))
                and (module := target(st.value))
                and alias != module
            ):
                out[alias] = module

    visit(tree.body)
    return out


def _modules_of(mod: griffe.Module):
    yield mod
    for sub in mod.modules.values():
        if not sub.is_alias:
            yield from _modules_of(sub)  # type: ignore[arg-type]


def _sorted_py_modules(self: griffe_finder.ModuleFinder, path: Path):
    """griffe's `ModuleFinder._filter_py_modules`, walking folders and files in sorted order (DT-DATA-30): unsorted,
    modules loaded in the file system's order, APFS's on macOS and ext4's on CI, and a few numpy re-exports resolved
    to different definitions."""
    for root, dirs, files in os.walk(path, topdown=True, followlinks=True):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for relfile in sorted(files):
            if Path(relfile).suffix in self.extensions_set:
                yield Path(root, relfile)


griffe_finder.ModuleFinder._filter_py_modules = _sorted_py_modules  # type: ignore[method-assign]


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


class _Rebound(griffe.Extension):
    """A class or function its scope rebinds to a call on itself is still that class or function (DT-DATA-50). griffe
    records `AutoModel = auto_class_update(AutoModel)` as an attribute, so transformers' 41 Auto classes had no
    members, and `AutoModelForCausalLM.from_pretrained` read `never_existed`; django's `args_check =
    staticmethod(args_check)` and `as_manager = classmethod(as_manager)` had no signature. Not when the call is a
    deprecation wrapper: the attribute is where its message is read (`_wrapper_deprecation`). `property(f)` stays an
    attribute, as griffe has `@property`."""

    def __init__(self) -> None:
        super().__init__()
        self.defined: dict[str, griffe.Class | griffe.Function] = {}

    def on_class_instance(self, *, cls: griffe.Class, **kwargs: Any) -> None:
        self.defined[cls.path] = cls

    def on_function_instance(self, *, func: griffe.Function, **kwargs: Any) -> None:
        self.defined[func.path] = func

    def on_attribute_instance(
        self, *, node: ast.AST | griffe.ObjectNode, attr: griffe.Attribute, **kwargs: Any
    ) -> None:
        obj = self.defined.get(attr.path)
        value = getattr(node, "value", None)
        if (
            obj is None
            or attr.parent is None
            or not isinstance(value, ast.Call)
            or not value.args
            or not isinstance(value.args[0], ast.Name)
            or value.args[0].id != attr.name
            or _wrapper_deprecation(attr)
        ):
            return
        if isinstance(obj, griffe.Function):
            wrapper = ast.unparse(value.func)
            if wrapper.rpartition(".")[2] in {"property", "cached_property"}:
                return
            if wrapper in {"classmethod", "staticmethod"}:
                obj.labels.add(wrapper)
        attr.parent.set_member(attr.name, obj)


def _file_expr(node: ast.expr, files: set[str]) -> bool:
    """`__file__`, `globals()["__file__"]`, or a name the module bound to one of them."""
    if isinstance(node, ast.Name):
        return node.id == "__file__" or node.id in files
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "globals"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "__file__"
    )


def _structure_folder(node: ast.expr, files: set[str], here: Path) -> Path | None:
    """The folder a `define_import_structure(<path>)` argument names: the module's own file (its folder), or
    `Path(__file__).parent / "models"`, as transformers 5's top-level `__init__` passes."""
    if _file_expr(node, files):
        return here
    parts: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return None
        parts.insert(0, node.right.value)
        node = node.left
    if not (
        parts
        and isinstance(node, ast.Attribute)
        and node.attr == "parent"
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) in {"Path", "pathlib.Path"}
        and len(node.value.args) == 1
        and _file_expr(node.value.args[0], files)
    ):
        return None
    folder = here.joinpath(*parts)
    return folder if all(p.isidentifier() for p in parts) else None


def _requires_decorated(st: ast.stmt) -> bool:
    if not isinstance(st, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in st.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(func, (ast.Name, ast.Attribute)) and ast.unparse(func).rpartition(".")[2] == "requires":
            return True
    return False


class _ImportStructure(griffe.Extension):
    """transformers' lazy packages export every name their folder's modules list (DT-DATA-05).

    A model package's `__init__` hands `_LazyModule` `define_import_structure(_file)`, which reads, in every module
    file below the package's folder, its `__all__` and the classes and functions decorated `@requires(...)`; the
    `if TYPE_CHECKING:` imports are only for type checkers, generated, and sometimes stale. transformers 4.57's
    `models/t5gemma/__init__.py` star-imports `configuration_encdecgemma2`, a module that doesn't exist, and
    `models/__init__.py` leaves out SmolLM3, Apertus and 13 more models, so `transformers.SmolLM3ForCausalLM`
    read absent in every 4.x release though it imports. Each such name becomes an import of it from the module
    that lists it, unless the package already binds it. Read from the files with `ast`; nothing is imported."""

    def __init__(self) -> None:
        super().__init__()
        self.listed: dict[Path, list[str]] = {}

    def on_module_members(
        self, *, node: ast.AST | griffe.ObjectNode, mod: griffe.Module, **kwargs: Any
    ) -> None:
        if not (isinstance(node, ast.Module) and mod.is_init_module and isinstance(mod.filepath, Path)):
            return
        source = mod.imports.get("define_import_structure", "")
        if not source.endswith(".define_import_structure") or not _within(source, mod.package.path):
            return
        files = {
            t.id
            for st in ast.walk(node)
            if isinstance(st, ast.Assign) and _file_expr(st.value, set())
            for t in st.targets
            if isinstance(t, ast.Name)
        }
        here = mod.filepath.parent
        found = False
        for call in ast.walk(node):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "define_import_structure"
                and call.args
            ):
                continue
            folder = _structure_folder(call.args[0], files, here)
            if folder is None or not folder.is_dir():
                continue
            found = True
            prefix = ".".join([mod.path, *folder.relative_to(here).parts])
            for name, module in self._exports(folder, prefix).items():
                if name not in mod.members:
                    mod.set_member(
                        name, griffe.Alias(name, f"{module}.{name}", runtime=True, analysis="static")
                    )
        # The `_LazyModule` replaces the module, so the helper it imported isn't there at runtime.
        helper = mod.members.get("define_import_structure")
        if found and helper is not None and helper.is_alias:
            mod.del_member("define_import_structure")

    def _exports(self, folder: Path, prefix: str) -> dict[str, str]:
        """Each name `define_import_structure(folder)` exports, and the module it's listed in: the module files
        beside, then the folders below (their `__init__` excluded), skipping `modular_*` files beside a
        `modeling_*` file (transformers' sources for generated code)."""
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return {}
        files = [p for p in entries if p.is_file()]
        modular = any("modular_" in p.name for p in files) and any("modeling_" in p.name for p in files)
        found: dict[str, str] = {}
        for path in files:
            if path.suffix != ".py" or path.name == "__init__.py" or not path.stem.isidentifier():
                continue
            if modular and "modular_" in path.name:
                continue
            for name in self._listed(path):
                found.setdefault(name, f"{prefix}.{path.stem}")
        for sub in entries:
            if sub.is_dir() and sub.name.isidentifier():
                for name, module in self._exports(sub, f"{prefix}.{sub.name}").items():
                    found.setdefault(name, module)
        return found

    def _listed(self, path: Path) -> list[str]:
        if path not in self.listed:
            decorated: list[str] = []
            listed: list[str] = []  # the last `__all__ = [...]`, as transformers' `fetch__all__` reads it
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                tree = _quiet_parse(text) if "__all__" in text or "@requires" in text else None
            except (OSError, SyntaxError, ValueError):
                tree = None
            for st in tree.body if isinstance(tree, ast.Module) else ():
                if _requires_decorated(st):
                    decorated.append(st.name)  # type: ignore[union-attr]
                elif (
                    isinstance(st, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "__all__" for t in st.targets)
                    and isinstance(st.value, (ast.List, ast.Tuple))
                ):
                    listed = [
                        e.value
                        for e in st.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
            self.listed[path] = [n for n in dict.fromkeys([*decorated, *listed]) if n.isidentifier()]
        return self.listed[path]


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
            # What binds the first parameter (DT-CORE-39): a stub's `@staticmethod` overloads make a staticmethod.
            func.labels |= {"staticmethod", "classmethod"}.intersection(*(o.labels for o in overloads))
            func.overloads = overloads
            obj.set_member(name, func)
            del obj.overloads[name]


_STAR_IMPORT_RE = re.compile(rb"^[ \t]*from[ \t]+([A-Za-z_][\w.]*)[ \t]+import[ \t]*\*", re.M)


def external_star_imports(search_path: Path, import_names: list[str]) -> set[str]:
    """The top-level modules that the code under `search_path`'s import roots star-imports from outside itself and
    the standard library (DT-DATA-56: `mcp_types`, for mcp 2.x's `mcp/types/__init__.py`). A cheap textual scan:
    it only decides which pinned distributions are worth fetching, and griffe reads the code itself."""
    tops = {n.split(".")[0] for n in import_names}
    found: set[str] = set()
    for name in import_names:
        base = search_path.joinpath(*name.split("."))
        files = sorted(base.rglob("*.py")) if base.is_dir() else [base.with_suffix(".py")]
        for file in files:
            try:
                data = file.read_bytes()
            except OSError:
                continue
            if b"import" not in data or b"*" not in data:
                continue
            for m in _STAR_IMPORT_RE.finditer(data):
                top = m.group(1).decode("ascii", "replace").split(".")[0]
                if top not in tops and top not in STDLIB_MODULES:
                    found.add(top)
    return found


def _clear_griffe_caches() -> None:
    """Empty griffe's module-level memo caches. They hold loaded objects, and through their parents the whole
    package, for the life of the process: the dataclasses extension's `_dataclass_parameters` kept one package
    tree per release built (DT-DATA-18)."""
    for name, module in list(sys.modules.items()):
        if name == "griffe" or name.startswith("griffe."):
            for value in list(vars(module).values()):
                if not isinstance(value, type) and callable(getattr(value, "cache_clear", None)):
                    value.cache_clear()


def add_namespace_inits(search_path: Path, import_names: list[str], skip: list[str] = ()) -> list[str]:
    """Write an empty `__init__.py` into each folder below an import root that Python imports as a namespace
    package, so griffe loads it (DT-DATA-23). `search_path` must be a private copy: it is changed in place.

    A folder qualifies when its name is an identifier, it isn't a test suite, no same-name module next to it
    takes precedence, it holds Python source somewhere below, and it isn't at or below a module in `skip`.
    Returns the module paths of the folders it made into packages."""
    made: list[str] = []
    skipped = tuple(skip)

    def holds_python(folder: Path) -> bool:
        return any(p.suffix in {".py", ".pyi"} for p in folder.rglob("*") if p.is_file())

    def shadowed(folder: Path, name: str) -> bool:  # a module of the same name wins over a namespace package
        return any(next(folder.glob(f"{name}{ext}"), None) for ext in (".py", ".pyi", ".*.so", ".*.pyd"))

    def visit(folder: Path, module: str) -> None:
        for child in sorted(folder.iterdir()):
            name = child.name
            sub = f"{module}.{name}"
            if (
                not child.is_dir()
                or not name.isidentifier()
                or keyword.iskeyword(name)
                or _is_test_name(name)
            ):
                continue
            if sub in skipped or sub.startswith(tuple(s + "." for s in skipped)):
                continue
            if not ((child / "__init__.py").exists() or (child / "__init__.pyi").exists()):
                if shadowed(folder, name) or not holds_python(child):
                    continue
                (child / "__init__.py").write_text("")
                made.append(sub)
            visit(child, sub)

    for import_name in import_names:
        root = search_path.joinpath(*import_name.split("."))
        if root.is_dir() and import_name not in skipped:
            visit(root, import_name)
    return made


def build_snapshot(
    package: str,
    version: str,
    import_names: list[str],
    search_path: Path,
    namespace_skip: list[str] = (),
    own_members: bool = False,
    siblings: Sequence[tuple[Path, list[str]]] = (),
) -> ApiSnapshot:
    """Snapshot the public API under `search_path`, a private extraction of one release: folders without
    `__init__.py` get an empty one first (see `add_namespace_inits`). `own_members` records each class's own
    members once, with its MRO, rather than every member under every public path (DT-DATA-16).

    `siblings`: other distributions' private extractions, with the import names to load from each, analysed with
    the release because it star-imports them (DT-DATA-56: mcp 2.x's `mcp.types` is `from mcp_types import *`,
    and each release pins `mcp-types` at its own version). Their names are recorded only where the release's own
    modules bind them."""
    snap = ApiSnapshot(
        package=package,
        version=version,
        import_names=list(import_names),
        namespace_skip=list(namespace_skip),
        own_members=own_members,
    )
    roots = [search_path.joinpath(*n.split(".")) for n in import_names]
    bare = [
        n
        for n, d in zip(import_names, roots, strict=True)
        if d.is_dir() and not (d / "__init__.py").exists() and not (d / "__init__.pyi").exists()
    ]
    made = add_namespace_inits(search_path, list(import_names), list(namespace_skip))
    snap.namespaces = sorted({*bare, *made})
    walker = _Walker(snap, tuple(import_names))
    global _READINGS
    _READINGS = {}
    # With siblings, one collection for the release and them, loaded first, so griffe expands star imports of
    # them. Without, a fresh one per import name, as always.
    shared = (griffe.ModulesCollection(), griffe.LinesCollection()) if siblings else (None, None)
    paths = [str(search_path), *(str(root) for root, _ in siblings)]

    def load(name: str, where: list[str]) -> griffe.Object | griffe.Alias:
        return griffe.load(
            name,
            search_paths=where,
            modules_collection=shared[0],
            lines_collection=shared[1],
            allow_inspection=False,  # never import untrusted code
            try_relative_path=False,
            resolve_aliases=True,
            # None loads only a private sibling (`_pytest` for `pytest`), found in the same wheel because
            # search_paths replaces sys.path; other distributions' names stay aliases.
            resolve_external=None,
            extensions=griffe.load_extensions(
                _RuntimeElse(), _OverloadsOnly(), _Rebound(), _ImportStructure()
            ),
        )

    try:
        for root, names in siblings:
            for name in names:
                try:
                    load(name, [str(root)])
                except Exception as exc:  # the release's own names still load; star imports of it stay unread
                    snap.errors.append(f"load sibling {name}: {type(exc).__name__}: {exc}")
        for import_name in import_names:
            try:
                loaded = load(import_name, paths)
            except Exception as exc:  # report and continue: one bad import name must not sink the release
                snap.errors.append(f"load {import_name}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(loaded, griffe.Module):
                walker.module(loaded, import_name)
                for module in _modules_of(loaded):
                    for alias, target in module_aliases(module).items():
                        if any(_within(alias, r) for r in import_names):
                            snap.module_aliases.setdefault(alias, target)
            else:
                walker.member(loaded, import_name, 0)
                continue
            try:
                walker.internal(loaded)
            except Exception as exc:  # the public API is already recorded; don't lose it over these
                snap.errors.append(f"internal names of {import_name}: {type(exc).__name__}: {exc}")
        walker.record_member_classes()
        walker.record_private_classes()
    finally:
        _READINGS = None
        _clear_griffe_caches()
    return snap
