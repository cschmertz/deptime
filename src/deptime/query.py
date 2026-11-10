"""Query layer shared by the MCP tools, the HTTP API, and the CLI.

Deterministic and LLM-free (ADR 0006): every answer is derived from the index and carries its evidence.
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
import sys
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from deptime import snippet, stdlib
from deptime import suppressions as project
from deptime.config import Coverage, PackageSpec
from deptime.index.location import active_index
from deptime.index.store import IndexStore
from deptime.ingest.curated import CURATED_CONFIDENCE, hints_for
from deptime.ingest.diff import is_required
from deptime.ingest.replacements import clean_evidence, expand_alias, mine, moved_to_package, same_class
from deptime.models import (
    CheckSymbolsResult,
    Confidence,
    ConfidenceFactor,
    CoverageEntry,
    CoverageResult,
    DiffResult,
    ExplainResult,
    Finding,
    Fix,
    HistoryEvent,
    Link,
    ParamInfo,
    Replacement,
    SymbolCheck,
    SymbolHistory,
    SymbolQuery,
)
from deptime.tracebacks import ErrorSignal, parse_traceback
from deptime.untrusted import instruction_like, untrusted_text

MAX_DIFF_EVENTS = 500
MAX_CODE_SYMBOLS = 300  # names read from a snippet that check_symbols checks (DT-CORE-07)
# The `package` of a finding about the standard library (DT-DATA-06).
PYTHON = "python"
MAX_FINDINGS_PER_PACKAGE = 5
# Replacement hints below this stay in the structured `replacements` list (with their confidence) but are never
# stated as advice in prose: agents read `explanation` as fact. Matches spike S2's precision threshold.
EXPLAIN_MIN_CONFIDENCE = 0.75
PACKAGE_MOVE_CONFIDENCE = 0.85  # "moved to the `X` package", once X is found to have the name
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FREEZE_RE = re.compile(r"^\s*([A-Za-z0-9][\w.-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")
PIP_LIST_RE = re.compile(r"^\s*([A-Za-z0-9][\w.-]*)\s+(\d[\w.+!-]*)\s*$")
SEVERITY = {
    "removed": 0,
    "param_removed": 1,
    "param_now_required": 2,
    "kind_changed": 3,
    "param_kind_changed": 4,
    "param_added": 5,
    "param_deprecated": 6,
    "deprecated": 7,
    "moved": 8,
    "param_now_optional": 9,
    "added": 10,
    "now_raises": 1,  # calling it fails from then on, as with a removed parameter (DT-DATA-67)
}
# What check_symbols calls a prefix it can't look inside (DT-CORE-32).
OBJECT_KINDS = {
    "attribute": "an attribute",
    "function": "a function",
    "alias": "an alias the index can't follow",
    "getattr": "served by a module `__getattr__`",
    "type alias": "a type alias",
}
# Attributes every object, module and class has, which the index doesn't record (DT-CORE-38). Fixed lists, not
# `dir()`, so answers don't depend on the Python running DepTime.
_OBJECT_DUNDERS = frozenset(
    {
        "__class__",
        "__delattr__",
        "__dir__",
        "__doc__",
        "__eq__",
        "__format__",
        "__ge__",
        "__getattribute__",
        "__gt__",
        "__hash__",
        "__init__",
        "__init_subclass__",
        "__le__",
        "__lt__",
        "__ne__",
        "__new__",
        "__reduce__",
        "__reduce_ex__",
        "__repr__",
        "__setattr__",
        "__sizeof__",
        "__str__",
        "__subclasshook__",
    }
)
# `__path__` a package's, `__file__` a module loaded from a file.
MODULE_DUNDERS = _OBJECT_DUNDERS | {
    "__name__",
    "__spec__",
    "__loader__",
    "__package__",
    "__dict__",
    "__file__",
    "__path__",
    "__builtins__",
    "__cached__",
}
CLASS_DUNDERS = _OBJECT_DUNDERS | {
    "__name__",
    "__qualname__",
    "__module__",
    "__dict__",
    "__mro__",
    "__bases__",
    "__base__",
    "__subclasses__",
}
# A parent's changes that take its members with it, for diff_versions' used paths (DT-CORE-33).
PARENT_KINDS = ("removed", "moved", "kind_changed", "added")
NO_INDEX_NOTE = (
    "DepTime has no index yet. The first run downloads it in the background; retry in a minute, "
    "or run `deptime index pull`."
)
# After a change that only the error shows, past the newest indexed release (DT-DATA-04).
OTHER_CAUSE = (
    " If no newer release changed it, the error has another cause (a different interpreter or venv, a shadowing"
    " local module, or a lazily-imported optional dependency)."
)
# "f() takes 2 positional arguments but 3 were given" (DT-CORE-09): the counts include a method's instance.
POSITIONAL_COUNT_RE = re.compile(
    r"takes (?:from \d+ to )?(?P<takes>\d+) positional arguments? but (?P<given>\d+) (?:were|was) given"
)
POSITIONAL_KINDS = {"positional-only", "positional or keyword"}
# Where a user asks for a package DepTime doesn't index (DT-OSS-11).
PACKAGE_REQUEST_URL = "https://github.com/cschmertz/deptime/issues/new?template=package-request.yml"
# DT-CORE-29: a finding's fix comes from a replacement at least this sure, or from the same object at another path.
FIX_MIN_CONFIDENCE = 0.75
FIXABLE_ISSUES = {"symbol_removed", "param_removed", "module_removed", "never_existed"}
PARAM_SYMBOL_RE = re.compile(r"(?P<path>[^()]+)\((?P<param>[^()]+)\)")
DOTTED_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
# Project-URL labels, normalized as PEP 753 does, that name a changelog or release notes, best first.
CHANGELOG_LABELS = ("changelog", "releasenotes", "changes", "whatsnew", "history", "releases")
# Findings that don't identify a version change (the name is unknown, or present at the installed version).
INCONCLUSIVE_ISSUES = {"never_existed", "present_in_installed_version"}
MODULE_SIGNALS = {"import_name", "module_missing", "module_attribute"}
OBJECT_SIGNALS = {
    "object_attribute",
    "class_attribute",
    "unexpected_kwarg",
    "missing_argument",
    "too_many_positional",
}


def parse_installed(installed_versions: dict[str, str] | None, pip_freeze: str | None) -> dict[str, str]:
    """Accept {"httpx": "0.28.1"}, `pip freeze` output, or `pip list` output; keys are canonicalized."""
    out: dict[str, str] = {}
    for key, value in (installed_versions or {}).items():
        out[canonicalize_name(key)] = str(value).strip().lstrip("=v ")
    for line in (pip_freeze or "").splitlines()[:5000]:
        m = FREEZE_RE.match(line) or PIP_LIST_RE.match(line)
        if m:
            out.setdefault(canonicalize_name(m.group(1)), m.group(2))
    return out


# Standard modules check_symbols may import to ask a base class for a name (DT-CORE-32): what libraries subclass.
SAFE_STDLIB = frozenset(
    {
        "abc",
        "argparse",
        "asyncio",
        "builtins",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "decimal",
        "email",
        "enum",
        "fractions",
        "functools",
        "http",
        "io",
        "json",
        "logging",
        "numbers",
        "os",
        "pathlib",
        "queue",
        "re",
        "socketserver",
        "string",
        "threading",
        "types",
        "typing",
        "unittest",
        "urllib",
        "uuid",
        "weakref",
    }
)


# What a base from `typing` or `typing_extensions` is at runtime when it isn't a class: a TypedDict is a dict.
_RUNTIME_BASES = {"TypedDict": ("builtins", "dict")}


def _stdlib_member(base: str, name: str) -> bool | None:
    """Whether a standard-library (or builtin) class has `name`; None when `base` isn't one DepTime reads.
    `typing_extensions` re-exports `typing`'s classes (7,373 classes of the format-20 index have one as a base,
    most openai's and mistralai's TypedDicts), so its names are read as `typing`'s."""
    module, _, attr = base.rpartition(".")
    module = module or "builtins"
    if module in ("typing", "typing_extensions"):
        module, attr = _RUNTIME_BASES.get(attr, ("typing", attr))
    if module.split(".")[0] not in SAFE_STDLIB or module.split(".")[0] not in sys.stdlib_module_names | {
        "builtins"
    }:
        return None
    try:
        obj = getattr(sys.modules.get(module) or importlib.import_module(module), attr)
    except Exception:  # not there in this Python: say nothing
        return None
    if not isinstance(obj, type):
        return None
    # The class dictionaries, not `hasattr`: `Enum.value` is a descriptor that raises on the class itself.
    return any(name in vars(k) for k in obj.__mro__) or hasattr(obj, name)


def _event_key(row: Mapping) -> tuple:
    return (row["path"], row["ordinal"], row["kind"], row["detail"])


def _and(items: list[str], word: str = "and") -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} {word} {items[-1]}"


def _stand_in(package: str, release: Mapping) -> tuple[str, str] | None:
    """The distribution and version a release's code was read from, when it has no code of its own and pins another
    distribution at another version (DT-DATA-63: dspy 0.1.5 ships only `dspy_dummy` and requires `dspy-ai==2.4.5`).
    None for a release read from its own artifact, or from one it pins at its own version (DT-DATA-17)."""
    file = release["source_file"]
    if not file:
        return None
    try:
        dist, version = (
            parse_wheel_filename(file)[:2] if file.endswith(".whl") else parse_sdist_filename(file)
        )
        if dist == package or version == Version(release["version"]):
            return None
    except (InvalidSdistFilename, InvalidWheelFilename, InvalidVersion):
        return None
    return str(dist), str(version)


def _params(raw: str | None) -> list[ParamInfo] | None:
    return None if raw is None else list(_parsed_params(raw))


@lru_cache(maxsize=2048)
def _parsed_params(raw: str) -> tuple[ParamInfo, ...]:
    """A signature's parameters, validated once: transformers' configs take a hundred."""
    return tuple(ParamInfo(**p) for p in json.loads(raw))


def _raises(span: Mapping | None) -> str | None:
    """What a callable whose body only raises raises at a release (DT-DATA-67); None in an index from before it."""
    return span["raises"] if span is not None and "raises" in span.keys() else None  # noqa: SIM118


def _deprecated_params(span: sqlite3.Row | None) -> dict[str, str]:
    """{argument: text} a span's warnings deprecate; an index built before DT-DATA-42 has no such column."""
    # `in` on a sqlite3.Row tests its values, not its column names.
    if span is None or "deprecated_params" not in span.keys() or not span["deprecated_params"]:  # noqa: SIM118
        return {}
    return json.loads(span["deprecated_params"])


def _param_names(raw: str | None) -> set[str]:
    return {p["name"] for p in json.loads(raw)} if raw else set()


def _requires(raw: str | None, param: str) -> bool:
    return any(p["name"] == param and is_required(p) for p in json.loads(raw)) if raw else False


def _is_internal(span: sqlite3.Row) -> bool:
    """Whether a span row is an internal name's (DT-DATA-22), from `IndexStore.internal_spans`."""
    return "internal" in span.keys()  # noqa: SIM118


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _internal_reason(path: str, span: sqlite3.Row) -> str:
    """Why `path` isn't public API, for an explanation."""
    module, _, leaf = path.rpartition(".")
    if span["kind"] == "getattr":
        return f"{module}'s `__getattr__` serves the private name `{leaf}`"
    if _is_private(leaf):
        return f"`{leaf}` is a private name"
    if "member_of" in span.keys():  # noqa: SIM118  # a member of a class that isn't public API (DT-DATA-46)
        return f"{span['member_of']} is a class that isn't public API"
    private = next((i for i, part in enumerate(path.split(".")[:-1]) if _is_private(part)), None)
    if private is not None:
        return f"it is in the private module {'.'.join(path.split('.')[: private + 1])}"
    canonical = span["canonical"]
    if canonical and canonical != path:
        return f"{module} imports it from {canonical.rpartition('.')[0] or canonical} for its own use"
    return "the package doesn't publish it"


def _before_index(rels: list[sqlite3.Row]) -> str:
    """The index starts at a release, so a name removed before it looks the same as one that never existed."""
    return f" If it was removed before {rels[0]['version']}, the earliest release DepTime indexes, it won't show here."


def _error_hint_paths(spec: PackageSpec, path: str, target: str) -> list[str]:
    """Where a target named in error text may live, most literal first: `.items` -> a member of the missing name's
    class; `np.nan` -> `numpy.nan`; as written; next to the missing name; at the package's top level."""
    candidates = [member] if (member := same_class(target, path)) else []
    target = target.lstrip(".")
    for named in dict.fromkeys([expand_alias(target), target]):
        if any(named == n or named.startswith(n + ".") for n in spec.import_names):
            candidates.append(named)
    if owner := path.rpartition(".")[0]:
        candidates.append(f"{owner}.{target}")
    candidates += [f"{n}.{target}" for n in spec.import_names]
    return [c for c in dict.fromkeys(candidates) if c != path]


def _merge(live: list[Replacement], indexed: list[Replacement]) -> list[Replacement]:
    """Best first; a target the error text names (and the index confirmed) wins a tie and replaces its duplicate."""
    named = {r.target for r in live}
    return sorted([*live, *(r for r in indexed if r.target not in named)], key=lambda r: -r.confidence)


def _missing_args(raw: str | None, q: SymbolQuery) -> list[str]:
    """Required parameters a call with `q.positional` arguments by position and `q.kwargs` by name doesn't pass
    (DT-CORE-34): streamlit 1.67's `pyplot()` without `fig`. A method's `self` or `cls` comes bound; `*args` takes
    every extra positional argument. Empty without a positional count or a signature."""
    if q.positional is None or raw is None:
        return []
    params = json.loads(raw)
    if params and params[0]["name"] in ("self", "cls") and params[0]["kind"] != "keyword-only":
        params = params[1:]
    left, missing = q.positional, []
    for p in params:
        by_position = p["kind"] in ("positional-only", "positional or keyword") and left > 0
        if by_position:
            left -= 1
            continue
        if is_required(p) and (p["kind"] == "positional-only" or p["name"] not in q.kwargs):
            missing.append(p["name"])
    return missing


def _accepts_kwarg(raw: str | None, kwarg: str) -> bool:
    for p in json.loads(raw) if raw else []:
        if p["kind"] == "variadic keyword":
            return True
        if p["name"] == kwarg and p["kind"] not in {"positional-only", "variadic positional"}:
            return True
    return False


def _accepts(span: sqlite3.Row, kwarg: str) -> bool:
    """Whether a release accepts `kwarg`: its signature does, or a decorator that renamed it still takes the old name
    and warns (DT-DATA-37: polars' `@deprecate_renamed_parameter("periods", "n")`)."""
    return _accepts_kwarg(span["params"], kwarg) or kwarg in _deprecated_params(span)


def _takes(span: sqlite3.Row, param: str) -> bool:
    """Whether a release names `param`: in its signature, or as an argument it deprecates."""
    return param in _param_names(span["params"]) or param in _deprecated_params(span)


def _stdlib_curated(path: str) -> list[Replacement]:
    """What's New's replacements for a removed standard module or name (DT-DATA-48, curated.toml)."""
    return [
        Replacement(target=h.target, confidence=CURATED_CONFIDENCE, source="curated", evidence=h.evidence)
        for h in hints_for(PYTHON)
        if h.path == path
    ]


def _stdlib_module(path: str) -> bool:
    entry = stdlib.lookup(path)
    return entry is not None and entry.module == path


def _stdlib_has(path: str, minor: int) -> bool | None:
    """Whether Python 3.<minor> has the standard module `path`, or `module.name`; None when the tables can't tell:
    a deeper path such as a class's member, or a path outside the standard library."""
    if _stdlib_module(path):
        return stdlib.lookup(path).has(minor)  # type: ignore[union-attr]
    module, _, name = path.rpartition(".")
    if not _stdlib_module(module):
        return None
    named = stdlib.lookup_name(module, name)
    return stdlib.lookup(module).has(minor) and (named is None or named.has(minor))  # type: ignore[union-attr]


def _stdlib_fix(old: str, is_module: bool, replacements: list[Replacement], minor: int) -> Fix | None:
    """DT-CORE-29's fix for a standard-library finding: the same object at another path, else the one replacement
    the tables show at Python 3.<minor>, a module for a module and a name for a name."""
    found = []
    for r in replacements:
        if r.confidence < FIX_MIN_CONFIDENCE and r.source != "moved_heuristic":
            continue
        if _stdlib_module(r.target) != is_module or not _stdlib_has(r.target, minor):
            continue
        fix = Fix(
            kind="path",
            old=old,
            new=r.target,
            verified_at=f"3.{minor}",
            same_object=r.source == "moved_heuristic",
            confidence=r.confidence,
            source=r.source,
            evidence=r.evidence,
        )
        if fix.same_object:
            return fix
        found.append(fix)
    return found[0] if len({f.new for f in found}) == 1 else None


class DepTime:
    def __init__(
        self,
        store: IndexStore | None,
        coverage: Coverage,
        index_path: Path | None = None,
        suppressions: tuple[project.Suppression, ...] = (),
    ):
        self._store = store
        self.coverage = coverage
        # The project's own suppressions (DT-CORE-30): local mode only, and every answer counts what they leave out.
        self.suppressions = suppressions
        # Opened lazily if it appears after start-up, e.g. another process finished the first download.
        self.index_path = index_path
        self._open_lock = threading.Lock()
        # Why there is no index, once the background download has tried (DT-OSS-10).
        self.index_problem: str | None = None

    @property
    def store(self) -> IndexStore | None:
        if self._store is None and self.index_path is not None and active_index(self.index_path).exists():
            with self._open_lock:
                if self._store is None:
                    store = None
                    try:
                        store = IndexStore.open(active_index(self.index_path))
                        store.built_at()  # fails fast on a file that is not a usable index
                        self._store = store
                    except sqlite3.Error:  # not usable (yet); keep answering with the no-index note
                        if store is not None:
                            store.close()
        return self._store

    def set_store(self, store: IndexStore | None) -> None:
        """Swap in a newly downloaded index. Reference assignment is atomic; the old connection is left to GC
        so requests already holding it finish normally."""
        self._store = store

    # ------------------------------------------------------------------ helpers
    def _indexed(self, name: str) -> bool:
        return self.store is not None and self.store.has_package(name)

    def _spec(self, name: str) -> PackageSpec:
        return self.coverage.get(name) or PackageSpec(
            name=name, import_names=(name.replace("-", "_"),), tier="?"
        )

    def _qualify(self, spec: PackageSpec, path: str) -> str:
        path = path.strip().removesuffix("()")
        if any(path == n or path.startswith(n + ".") for n in spec.import_names):
            return path
        return f"{spec.import_names[0]}.{path}"

    def _version(self, name: str, ordinal: int | None) -> str | None:
        return None if ordinal is None or self.store is None else self.store.version_at(name, ordinal)

    def _event(self, name: str, row: sqlite3.Row, affects: list[str] | None = None) -> HistoryEvent:
        return HistoryEvent(
            path=row["path"],
            version=self._version(name, row["ordinal"]) or "?",
            prev_version=self._version(name, row["prev_ordinal"]),
            kind=row["kind"],
            detail=json.loads(row["detail"]),
            affects=affects or [],
        )

    # Hints mined from text that reads like instructions are dropped with the text (DT-CORE-08).
    def _hints(self, name: str, path: str) -> list[sqlite3.Row]:
        assert self.store is not None
        return [r for r in self.store.replacements(name, path) if not instruction_like(r["evidence"])]

    def _param_hints(self, name: str, func_path: str) -> list[sqlite3.Row]:
        assert self.store is not None
        return [
            r for r in self.store.param_replacements(name, func_path) if not instruction_like(r["evidence"])
        ]

    def _followed(
        self, name: str, replacements: list[Replacement], ordinal: int, depth: int = 2
    ) -> list[Replacement]:
        """`replacements` with a target of the package that release `ordinal` no longer has followed to that
        target's own hint, when the index has one the release has (DT-CORE-40): pydantic-ai's `MCPServerHTTP` says to
        use `MCPServerSSE`, which 1.97 deprecates for `MCPToolset` and 2.0 removes, so on 2.x the advice is
        `MCPToolset`. The followed target comes first, then the one it stands for."""
        assert self.store is not None
        out: list[Replacement] = []
        for r in replacements:
            gone = (
                depth > 0
                and self.store.span_at(name, r.target, ordinal) is None
                and bool(self.store.spans(name, r.target))
            )
            if gone:
                later = [
                    Replacement(
                        target=h["target"],
                        confidence=min(r.confidence, h["confidence"]),
                        source=h["source"],
                        evidence=f"Followed from {r.target}, which this release doesn't have: {h['evidence']}",
                    )
                    for h in sorted(self._hints(name, r.target), key=lambda h: -h["confidence"])
                ]
                out.extend(
                    f
                    for f in self._followed(name, later, ordinal, depth - 1)
                    if self.store.span_at(name, f.target, ordinal) is not None
                )
            out.append(r)
        first: dict[str, Replacement] = {}
        for r in out:
            first.setdefault(r.target, r)
        return list(first.values())

    @staticmethod
    def _replacement(row: sqlite3.Row) -> Replacement:
        return Replacement(
            target=row["target"], confidence=row["confidence"], source=row["source"], evidence=row["evidence"]
        )

    def _no_index_note(self) -> str:
        return f"DepTime has no index yet: {self.index_problem}" if self.index_problem else NO_INDEX_NOTE

    def _stand_in_note(self, name: str, versions: Iterable[str | None]) -> str | None:
        """Says which of `versions` were read from another distribution's code (DT-DATA-63), else None."""
        if self.store is None:
            return None
        said = []
        for version in dict.fromkeys(v for v in versions if v):
            release = self.store.release(name, version)
            if release is not None and (source := _stand_in(name, release)) is not None:
                said.append(
                    f"{name} {version} has no code of its own: it requires {source[0]}=={source[1]}, whose code is "
                    f"what installing it imports, and what is answered for {name} {version}."
                )
        return " ".join(said) or None

    def _not_indexed_note(self, name: str) -> str:
        if self.store is None:
            return self._no_index_note()
        configured = self.coverage.get(name) is not None
        under = None if configured else self.coverage.installed_under(name)
        if under is not None:
            ranges = [str(r.specifier) for r in under.installed_as if canonicalize_name(r.name) == name]
            within = f" ({name}{ranges[0]})" if ranges and ranges[0] else ""
            return (
                f"{name}{within} installs the code DepTime indexes as {under.name}, with the same version numbers; "
                f'call again with package="{under.name}".'
            )
        return (
            f"{name} is configured but not indexed yet."
            if configured
            else f"{name} is not in DepTime's coverage; call the `coverage` tool for the list, or ask for it at "
            f"{PACKAGE_REQUEST_URL}."
        )

    # ------------------------------------------------------------------ tools
    def coverage_report(self) -> CoverageResult:
        entries: list[CoverageEntry] = []
        indexed: set[str] = set()
        if self.store is not None:
            for row in self.store.package_rows():
                rels = self.store.releases(row["name"])
                indexed.add(row["name"])
                entries.append(
                    CoverageEntry(
                        package=row["name"],
                        import_names=json.loads(row["import_names"]),
                        tier=row["tier"],
                        versions_indexed=len(rels),
                        first_version=rels[0]["version"] if rels else None,
                        latest_version=rels[-1]["version"] if rels else None,
                        indexed_at=row["indexed_at"],
                        latest_released_at=rels[-1]["released_at"] if rels else None,
                    )
                )
        return CoverageResult(
            index_built_at=self.store.built_at() if self.store else None,
            policy=self.coverage.policy.describe(),
            packages=entries,
            configured_but_not_indexed=[p.name for p in self.coverage.packages if p.name not in indexed],
        )

    def symbol_history(self, package: str, path: str) -> SymbolHistory:
        name = canonicalize_name(package)
        spec = self._spec(name)
        full = self._qualify(spec, path)
        if not self._indexed(name):
            return SymbolHistory(package=name, path=full, indexed=False, note=self._not_indexed_note(name))
        assert self.store is not None
        rels = self.store.releases(name)
        latest = rels[-1]
        spans = self.store.spans(name, full)
        internal = self.store.internal_spans(name, full)
        if internal:
            return self._internal_history(name, full, spans, internal, latest)
        if not spans:
            return SymbolHistory(
                package=name,
                path=full,
                indexed=True,
                latest_indexed_version=latest["version"],
                note=(
                    f"No indexed release of {name} ({rels[0]['version']}..{latest['version']}) exposes {full}. "
                    "If the name came from memory it may not exist; check spelling or try the parent path."
                ),
            )
        last_ordinal = max(s["last_ordinal"] for s in spans)
        events, hidden = self._unsuppressed(
            name, [self._event(name, r) for r in self.store.events(name, full)]
        )
        replacements = [self._replacement(r) for r in self._hints(name, full)]
        replacements += [
            self._replacement(r).model_copy(update={"target": f"{r['path']} -> {r['target']}"})
            for r in self._param_hints(name, full)
        ]
        first_seen, last_seen = (
            self._version(name, spans[0]["first_ordinal"]),
            self._version(name, last_ordinal),
        )
        stand_in = self._stand_in_note(
            name, [first_seen, last_seen, *(v for e in events for v in (e.version, e.prev_version))]
        )
        return SymbolHistory(
            package=name,
            path=full,
            indexed=True,
            ever_seen=True,
            first_seen=first_seen,
            last_seen=last_seen,
            latest_indexed_version=latest["version"],
            present_in_latest=last_ordinal == latest["ordinal"],
            signature_at_last_seen=_params(spans[-1]["params"]),
            events=events,
            replacements=replacements,
            suppressed=hidden,
            note=" ".join(n for n in (project.note(hidden, "event"), stand_in) if n) or None,
        )

    def _internal_history(
        self, name: str, path: str, spans: list[sqlite3.Row], internal: list[sqlite3.Row], latest: sqlite3.Row
    ) -> SymbolHistory:
        """History of a path that was, at least for a while, a name that isn't public API (DT-DATA-22)."""
        both = sorted([*spans, *internal], key=lambda s: s["first_ordinal"])
        last = max(both, key=lambda s: s["last_ordinal"])
        note = f"{path} is not public API: {_internal_reason(path, internal[-1])}."
        if spans:
            public_last = self._version(name, max(s["last_ordinal"] for s in spans))
            note += f" It was public API up to {name} {public_last}."
        return SymbolHistory(
            package=name,
            path=path,
            indexed=True,
            ever_seen=True,
            first_seen=self._version(name, both[0]["first_ordinal"]),
            last_seen=self._version(name, last["last_ordinal"]),
            latest_indexed_version=latest["version"],
            present_in_latest=last["last_ordinal"] == latest["ordinal"],
            signature_at_last_seen=_params(spans[-1]["params"]) if spans else None,
            events=[self._event(name, r) for r in self.store.events(name, path)],
            replacements=[self._replacement(r) for r in self._hints(name, path)],
            note=note,
            internal=_is_internal(last),
        )

    def check_symbols(
        self, package: str, version: str, symbols: list[SymbolQuery] | None = None, code: str | None = None
    ) -> CheckSymbolsResult:
        """Each symbol, and with `code`, what the snippet uses from the package (DT-CORE-07), at `version`."""
        name = canonicalize_name(package)
        spec = self._spec(name)
        queries, read = list(symbols or []), None
        if code is not None:
            uses = snippet.uses_in(code, tuple(spec.import_names))
            if uses is None:
                read = "The code doesn't parse as Python, so nothing was read from it."
            else:
                queries, count = self._code_queries(name, version, queries, uses)
                read = f"{count} name{'' if count == 1 else 's'} of {name} read from the code."
                if len(queries) > MAX_CODE_SYMBOLS:
                    read += f" Only the first {MAX_CODE_SYMBOLS} names are checked."
                    queries = queries[:MAX_CODE_SYMBOLS]
        result = self._check_symbols(name, spec, version, queries)
        stand_in = self._stand_in_note(name, [result.resolved_version])
        if read or stand_in:
            result.note = " ".join(n for n in (read, result.note, stand_in) if n)
        return result

    def _unseen(
        self, name: str, spec: PackageSpec, q: SymbolQuery, full: str, row: sqlite3.Row, depth: int
    ) -> SymbolCheck | None:
        """A path with no span at the release, read from its nearest indexed prefix (DT-CORE-32). Something other than
        a module or class: what it holds is made at runtime (`pytest.mark` is an instance whose attributes are any
        mark's name), so `unknown`, unless it is an alias into another covered distribution (`fastapi.status` is
        `starlette.status`), checked there. A class: its bases from other distributions (`_inherited`). A module
        imported under another path: its own names. A namespace package: `unknown`, since another distribution may
        install the module. None (so `missing`) when the prefix's own names, which the index lists, settle it."""
        assert self.store is not None
        parts = full.split(".")
        for k in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:k])
            span = self.store.span_at(name, prefix, row["ordinal"])
            if span is None:
                continue
            rest = ".".join(parts[k:])
            if span["kind"] in ("module", "class") and (
                dunder := self._dunder(name, full, prefix, span["kind"], rest)
            ):
                return dunder
            if span["kind"] == "module" and span["canonical"] != prefix and depth < 2:
                # A module imported into another (`matplotlib.pyplot.cm` is `matplotlib.cm`): its names are the
                # module's own, which the index lists at the module's path.
                got = self._check_symbols(
                    name,
                    spec,
                    row["version"],
                    [q.model_copy(update={"path": f"{span['canonical']}.{rest}"})],
                    depth + 1,
                ).results[0]
                return (
                    got.model_copy(
                        update={"path": full, "hint": got.hint or f"as {span['canonical']}.{rest}"}
                    )
                    if got.status != "missing"
                    else None
                )
            if span["kind"] == "class" and depth < 2:
                return self._inherited(name, row, q, full, prefix, span["canonical"], rest, depth)
            if (
                span["kind"] == "module"
                and depth < 2
                and (sources := self.store.star_sources(name, row["ordinal"]).get(prefix))
                and (starred := self._starred(name, row, q, full, prefix, sources, depth)) is not None
            ):
                return starred
            if (
                span["kind"] == "module"
                and prefix in self.store.namespaces(name, row["ordinal"])
                and (shared := self._namespace(name, row, full, prefix)) is not None
            ):
                return shared
            if span["kind"] in ("module", "class"):
                return None
            target = self._outside(name, spec, span, row["ordinal"])
            if target is not None and depth < 2:
                return self._through(name, row, q, full, prefix, f"{target}.{rest}", depth)
            what = OBJECT_KINDS.get(span["kind"], f"a {span['kind']}")
            obj = f" (`{target or span['canonical']}`)" if (target or span["canonical"]) != prefix else ""
            return SymbolCheck(
                path=full,
                status="unknown",
                hint=f"{prefix} is {what}{obj}, not a module or class, so the index doesn't list its attributes"
                " (often made at runtime)",
            )
        return None

    def _dunder(self, name: str, full: str, prefix: str, kind: str, rest: str) -> SymbolCheck | None:
        """A dunder name under a module or class that the index doesn't record (DT-CORE-38): one every module or class
        has (`mcp.__name__`) is present; another (`polars.__version__` where it isn't bound by name, pydantic's
        generated `__pydantic_serializer__`), or a path through one, is `unknown`. None for any other name, and for a
        dunder the index has at that path in another release, which really went (werkzeug 3.1's `__version__`)."""
        assert self.store is not None
        first, _, deeper = rest.partition(".")
        if not _is_dunder(first):
            return None
        standard = first in (MODULE_DUNDERS if kind == "module" else CLASS_DUNDERS)
        if not standard and self.store.spans(name, f"{prefix}.{first}"):
            return None
        if standard and not deeper:
            return SymbolCheck(path=full, status="present", hint=f"every {kind} has `{first}`")
        if standard:
            why = f"`{prefix}.{first}` is an attribute every {kind} has, and the index doesn't list its attributes"
        elif kind == "module":
            why = f"no indexed release of {prefix} binds `{first}` by name; it may be set at import time"
        else:
            why = (
                f"the index doesn't record `{first}` on {prefix}: a dunder name is often set when the class is made "
                "(by a metaclass or `__init_subclass__`)"
            )
        return SymbolCheck(path=full, status="unknown", hint=why)

    def _inherited(
        self,
        name: str,
        row: sqlite3.Row,
        q: SymbolQuery,
        full: str,
        prefix: str,
        cls: str,
        rest: str,
        depth: int,
    ) -> SymbolCheck | None:
        """A name a class lacks, looked up in its bases from outside the package (DT-CORE-32): `fastapi.FastAPI.mount`
        is starlette's `Starlette.mount`, checked in starlette at the newest release fastapi's requirement allows; a
        standard-library base (`enum.Enum`, `Exception`) is asked directly. None (so `missing`) when the class has no
        such base, or every base that DepTime can read lacks the name; `unknown` when a base it can't read might
        have it."""
        assert self.store is not None
        bases = self.store.external_bases(name, cls, row["ordinal"])
        unread: list[str] = []
        for base in bases:
            first, _, deeper = rest.partition(".")
            stdlib_has = _stdlib_member(base, first)
            if stdlib_has is not None:
                if stdlib_has:
                    where = (
                        "as `typing` has it"
                        if base.startswith("typing_extensions.")
                        else "in the standard library"
                    )
                    hint = f"inherited from `{base}`, {where}"
                    if deeper:
                        return SymbolCheck(
                            path=full,
                            status="unknown",
                            hint=f"{prefix}.{first} is {hint}; the index doesn't list what's under it",
                        )
                    return SymbolCheck(path=full, status="present", hint=hint)
                continue
            got = self._through(
                name, row, q, full, prefix, f"{base}.{rest}", depth, lead=f"inherited from `{base}`"
            )
            if got.status == "present":
                return got
            if got.status == "unknown":
                unread.append(base)
        if unread:
            return SymbolCheck(
                path=full,
                status="unknown",
                hint=f"{prefix} inherits from {_and([f'`{b}`' for b in unread])}, from another distribution DepTime"
                " can't check here, which may have it",
            )
        return None

    def _starred(
        self,
        name: str,
        row: sqlite3.Row,
        q: SymbolQuery,
        full: str,
        prefix: str,
        sources: list[str],
        depth: int,
    ) -> SymbolCheck | None:
        """A path missing under a module that star-imports code DepTime didn't read (DT-DATA-56): each such module
        is asked for the rest of the path, in the covered package that has it at the newest release `name`'s
        release allows (matplotlib's `pylab` is `from numpy import *`), and one DepTime can't ask makes it
        `unknown` (google-generativeai's `protos`, from google-ai-generativelanguage). None (so `missing`) when
        every one was asked and lacks it."""
        rest = full[len(prefix) + 1 :]
        unknown: SymbolCheck | None = None
        for source in reversed(sources):  # the last star import wins at runtime
            got = self._through(
                name,
                row,
                q,
                full,
                prefix,
                f"{source}.{rest}",
                depth,
                lead=f"{prefix} star-imports `{source}`",
            )
            if got.status == "present":
                return got
            if got.status == "unknown" and unknown is None:
                unknown = got
        return unknown

    def _namespace(self, name: str, row: sqlite3.Row, full: str, prefix: str) -> SymbolCheck | None:
        """A path missing directly under a namespace package (DT-CORE-32), when the release requires a distribution
        named after the same root, which may install it: langgraph requires langgraph-checkpoint, which installs
        `langgraph.checkpoint` and `langgraph.store`. None otherwise: mistralai 2.0's root is a namespace package
        too, but nothing it requires installs into it, and `mistralai.Mistral` really went."""
        assert self.store is not None
        root = prefix.split(".")[0].replace("_", "-")
        siblings = sorted(
            d for d in self.store.declared_dependencies(name, row["ordinal"]) if d.startswith(root + "-")
        )
        if not siblings:
            return None
        return SymbolCheck(
            path=full,
            status="unknown",
            hint=f"{prefix} is a namespace package, and {name} {row['version']} requires {_and(siblings)}, which can"
            f" install modules into it; {name} itself doesn't have this one",
        )

    def _outside(self, name: str, spec: PackageSpec, span: sqlite3.Row, ordinal: int) -> str | None:
        """Where an alias leads outside the package, following its aliases inside it: `fastapi.status` ->
        `starlette.status`. None for anything else."""
        assert self.store is not None
        seen: set[str] = set()
        while span is not None and span["kind"] == "alias" and span["canonical"] not in seen:
            target = span["canonical"]
            if not any(target == r or target.startswith(r + ".") for r in spec.import_names):
                return target
            seen.add(target)
            span = self.store.span_at(name, target, ordinal)
        return None

    def _through(
        self,
        name: str,
        row: sqlite3.Row,
        q: SymbolQuery,
        full: str,
        via: str,
        target: str,
        depth: int,
        lead: str | None = None,
    ) -> SymbolCheck:
        """`full` checked as `target` in the covered package that has it, at the newest indexed release that `name`'s
        release allows (its Requires-Dist, DT-DATA-14); `unknown` when no covered package has it or the release
        doesn't require that one."""
        assert self.store is not None
        other = next(
            (
                s
                for s in self.coverage.packages_for_module(target)
                if s.name != name and self._indexed(s.name)
            ),
            None,
        )
        req = self.store.requirement(name, row["ordinal"], other.name) if other else None
        allowed = (
            [
                r
                for r in self.store.releases(other.name)
                if req.specifier.contains(r["version"], prereleases=True)
            ]
            if other and req
            else []
        )
        if other is None or not allowed:
            why = (
                f"which DepTime doesn't index ({target.split('.')[0]})"
                if other is None
                else f"{other.name}, which {name} {row['version']} doesn't require"
                if req is None
                else f"{other.name}, with no indexed release that {name} {row['version']} allows ({req})"
            )
            return SymbolCheck(
                path=full,
                status="unknown",
                hint=f"{lead or f'{via} comes from another distribution'}: `{target}`, {why}",
            )
        pick = allowed[-1]["version"]
        got = self._check_symbols(other.name, other, pick, [q.model_copy(update={"path": target})], depth + 1)
        result = got.results[0]
        hint = (
            f"{lead or f'{via} comes from {other.name}'}: checked as `{target}` in {other.name} {pick}, the newest"
            f" release {name} {row['version']} allows ({req})"
        )
        return result.model_copy(
            update={"path": full, "hint": hint + (f"; {result.hint}" if result.hint else "")}
        )

    def _code_queries(
        self, name: str, version: str, given: list[SymbolQuery], uses: snippet.Uses
    ) -> tuple[list[SymbolQuery], int]:
        """`given` plus the snippet's paths, with the keyword arguments passed to each; an attribute read off an object
        a call built counts only where the index shows that call builds a class at `version`."""
        merged: dict[str, list[str]] = {}
        positional: dict[str, int | None] = {q.path: q.positional for q in given if q.positional is not None}
        for q in given:
            merged.setdefault(q.path, []).extend(q.kwargs)
        found = dict(uses.paths)
        for key, count in uses.positional.items():
            if isinstance(key, str) and key not in positional:
                positional[key] = count
        row = self.store.resolve_version(name, version)[0] if self.store and self._indexed(name) else None
        for (built_by, attr), kwargs in uses.members.items():
            span = (
                self.store.span_at(name, built_by, row["ordinal"]) if row is not None and self.store else None
            )
            if span is not None and span["kind"] == "class":
                found.setdefault(f"{built_by}.{attr}", []).extend(kwargs)
                if (built_by, attr) in uses.positional:
                    positional.setdefault(f"{built_by}.{attr}", uses.positional[(built_by, attr)])
        for path, kwargs in found.items():
            known = merged.setdefault(path, [])
            known.extend(k for k in kwargs if k not in known)
        return [SymbolQuery(path=p, kwargs=k, positional=positional.get(p)) for p, k in merged.items()], len(
            found
        )

    def _check_symbols(
        self, name: str, spec: PackageSpec, version: str, symbols: list[SymbolQuery], depth: int = 0
    ) -> CheckSymbolsResult:
        if not self._indexed(name):
            return CheckSymbolsResult(
                package=name,
                requested_version=version,
                resolved_version=None,
                exact=False,
                results=[
                    SymbolCheck(path=self._qualify(spec, q.path), status="not_indexed") for q in symbols
                ],
                note=self._not_indexed_note(name),
            )
        assert self.store is not None
        row, exact = self.store.resolve_version(name, version)
        if row is None:
            return CheckSymbolsResult(
                package=name,
                requested_version=version,
                resolved_version=None,
                exact=False,
                results=[
                    SymbolCheck(path=self._qualify(spec, q.path), status="not_indexed") for q in symbols
                ],
                note=f"{version} is older than the earliest indexed release of {name}, or not a valid version.",
            )
        results = []
        for q in symbols:
            full = self._qualify(spec, q.path)
            span = self.store.span_at(name, full, row["ordinal"])
            if (
                span is None
                and (inner := self.store.internal_span_at(name, full, row["ordinal"])) is not None
            ):
                hint = f"present, but not public API: {_internal_reason(full, inner)}"
                results.append(
                    SymbolCheck(
                        path=full, status="present", deprecated=inner["deprecated"], hint=hint, internal=True
                    )
                )
                continue
            if span is None and (unseen := self._unseen(name, spec, q, full, row, depth)) is not None:
                results.append(unseen)
                continue
            if span is None:
                spans = sorted(
                    [*self.store.spans(name, full), *self.store.internal_spans(name, full)],
                    key=lambda s: s["first_ordinal"],
                )
                if spans:
                    seen = f"seen in {self._version(name, spans[0]['first_ordinal'])}..{self._version(name, max(s['last_ordinal'] for s in spans))}"
                else:
                    seen = "never seen in any indexed release"
                repl = [
                    r
                    for r in self._followed(
                        name, [self._replacement(r) for r in self._hints(name, full)], row["ordinal"]
                    )
                    if r.confidence >= EXPLAIN_MIN_CONFIDENCE
                ]
                hint = seen + (f"; try {repl[0].target}" if repl else "")
                internal = bool(spans) and _is_internal(max(spans, key=lambda s: s["last_ordinal"]))
                results.append(SymbolCheck(path=full, status="missing", hint=hint, internal=internal))
                continue
            params_raw, callable_span = span["params"], span
            if params_raw is None and span["kind"] == "class":
                init = self.store.span_at(name, full + ".__init__", row["ordinal"])
                params_raw, callable_span = (init["params"], init) if init else (None, None)
            # The whole symbol's deprecation, else that of an argument the caller passes (DT-DATA-42).
            by_param = _deprecated_params(callable_span)
            rejected = [
                k
                for k in q.kwargs
                if params_raw is not None and not _accepts_kwarg(params_raw, k) and k not in by_param
            ]
            deprecated = span["deprecated"] or next((by_param[k] for k in q.kwargs if k in by_param), None)
            results.append(
                SymbolCheck(
                    path=full,
                    status="present",
                    rejected_kwargs=rejected,
                    missing_args=_missing_args(params_raw, q),
                    deprecated=deprecated,
                    raises=_raises(callable_span),
                )
            )
        return CheckSymbolsResult(
            package=name,
            requested_version=version,
            resolved_version=row["version"],
            exact=exact,
            results=results,
            note=None
            if exact
            else f"{version} is not indexed; answered for the nearest earlier indexed release {row['version']}."
            + (
                f" That is {self._newest_note(name, version)}, so names added after {row['version']} read missing."
                if self._ahead(name, row, exact)
                else ""
            ),
        )

    def diff_versions(
        self,
        package: str,
        from_version: str,
        to_version: str | None = None,
        used_symbols: list[str] | None = None,
        include_added: bool = False,
        limit: int = 200,
    ) -> DiffResult:
        name = canonicalize_name(package)
        spec = self._spec(name)
        if not self._indexed(name):
            return DiffResult(
                package=name,
                from_version=from_version,
                to_version=to_version,
                events=[],
                note=self._not_indexed_note(name),
            )
        assert self.store is not None
        rels = self.store.releases(name)
        lo = (
            self.store.release_on_or_before(name, from_version)
            if DATE_RE.match(from_version)
            else self.store.resolve_version(name, from_version)[0]
        )
        hi = self.store.resolve_version(name, to_version)[0] if to_version else rels[-1]
        if lo is None or hi is None:
            return DiffResult(
                package=name,
                from_version=from_version,
                to_version=to_version,
                events=[],
                note="Could not resolve the version range against the index; see `coverage`.",
            )
        limit = max(1, min(limit, MAX_DIFF_EVENTS))
        filtering = any(x.package == name for x in self.suppressions)
        used = list(dict.fromkeys(self._qualify(spec, u) for u in used_symbols or []))
        rows, total = self.store.events_between(
            name,
            lo["ordinal"],
            hi["ordinal"],
            MAX_DIFF_EVENTS if filtering else limit,  # room for what the suppressions leave out
            severity=SEVERITY,
            include_added=include_added,
            paths=used,
        )
        affects: dict[tuple, list[str]] = {}
        if used:
            extra, affects = self._parent_events(
                name, used, lo["ordinal"], hi["ordinal"], include_added, rows
            )
            extra += self._internal_events(name, used, lo["ordinal"], hi["ordinal"], include_added, rows)
            if extra:
                rows = sorted(
                    [*rows, *extra],
                    key=lambda r: (SEVERITY.get(r["kind"], 99), r["path"], r["ordinal"], r["kind"]),
                )
                total += len(extra)
            kept = [r for r in rows if not self._still_inherited(name, r, set(used))]
            total -= len(rows) - len(kept)
            rows = kept
        filled: dict[str, str] = {}
        kept = [r for r in rows if not self._namespace_filled(name, r, hi, filled)]
        total -= len(rows) - len(kept)
        rows = kept
        events, hidden = self._unsuppressed(
            name, [self._event(name, r, affects.get(_event_key(r))) for r in rows]
        )
        notes = [
            None
            if lo["version"] == from_version or DATE_RE.match(from_version)
            else f"from_version resolved to nearest indexed release {lo['version']}.",
            self._stand_in_note(name, [lo["version"], hi["version"]]),
            project.note(hidden, "event"),
            *(
                f"Removals under {module} aren't listed: {why}, so the import may still work; check_symbols"
                " answers `unknown` there."
                for module, why in sorted(filled.items())
            ),
        ]
        return DiffResult(
            package=name,
            from_version=lo["version"],
            to_version=hi["version"],
            events=events[:limit],
            truncated=total > len(rows) or len(events) > limit,
            note=" ".join(n for n in notes if n) or None,
            suppressed=hidden,
        )

    def _namespace_filled(self, name: str, row: Mapping, hi: sqlite3.Row, filled: dict[str, str]) -> bool:
        """A `removed` event under a module directly in a namespace package that the `to` release no longer has,
        when that release requires a distribution that can install modules into the namespace (DT-CORE-41), as
        check_symbols reads it: langgraph 0.3.1 moved `langgraph.prebuilt` to langgraph-prebuilt, which langgraph
        requires and which installs it into `langgraph`. Records the module and the reason in `filled`."""
        assert self.store is not None
        if row["kind"] != "removed":
            return False
        namespaces = self.store.namespaces(name, hi["ordinal"])
        parts = row["path"].split(".")
        module = next(
            (".".join(parts[: k + 1]) for k in range(1, len(parts)) if ".".join(parts[:k]) in namespaces),
            None,
        )
        if module is None:
            return False
        if module not in filled:
            if self.store.span_at(name, module, hi["ordinal"]) is not None:
                return False
            shared = self._namespace(name, hi, module, module.rpartition(".")[0])
            if shared is None:
                return False
            filled[module] = shared.hint or ""
        return True

    def _still_inherited(self, name: str, row: Mapping, used: set[str]) -> bool:
        """A used class member's `removed` event where the class still has the name through a base from another
        distribution (DT-CORE-32): sqlmodel 0.0.12 dropped its own `Session.get`, and sqlalchemy's stays."""
        assert self.store is not None
        if row["kind"] != "removed" or row["path"] not in used:
            return False
        parent, _, leaf = row["path"].rpartition(".")
        cls = self.store.span_at(name, parent, row["ordinal"])
        if cls is None or cls["kind"] != "class":
            return False
        release = self.store.releases(name)[row["ordinal"]]
        got = self._inherited(
            name, release, SymbolQuery(path=row["path"]), row["path"], parent, cls["canonical"], leaf, 0
        )
        return got is not None and got.status == "present"

    def _parent_events(
        self, name: str, used: list[str], after: int, upto: int, include_added: bool, rows: list
    ) -> tuple[list[Mapping], dict[tuple, list[str]]]:
        """A used path's parent module or class that was removed, moved or retyped in (after, upto], or added there,
        which takes the path with it (DT-CORE-33): `used_symbols` keeps events on a path and below, so mcp 2.0
        removing `mcp.server.fastmcp` said nothing about `mcp.server.fastmcp.FastMCP.tool`.

        Returns the parents' events not already in `rows`, and for each event (new or in `rows`) the used paths it
        takes. Per used path and change, only the outermost parent (the module, not also each class in it), and
        none where the index still has the path itself across the change (a name re-exported elsewhere)."""
        assert self.store is not None
        have = {_event_key(r): r for r in rows}
        extra: dict[tuple, Mapping] = {}
        affects: dict[tuple, list[str]] = defaultdict(list)
        cache: dict[str, list[Mapping]] = {}
        for path in used:
            taken: set[tuple[int, str]] = set()
            parts = path.split(".")
            for k in range(1, len(parts)):
                parent = ".".join(parts[:k])
                if parent not in cache:
                    cache[parent] = [
                        r
                        for r in self.store.events(name, parent)
                        if after < r["ordinal"] <= upto
                        and r["kind"] in PARENT_KINDS
                        and (include_added or r["kind"] != "added")
                    ]
                for r in cache[parent]:
                    if (r["ordinal"], r["kind"]) in taken:
                        continue
                    at = r["prev_ordinal"] if r["kind"] == "added" else r["ordinal"]
                    if at is not None and self.store.span_at(name, path, at) is not None:
                        continue
                    taken.add((r["ordinal"], r["kind"]))
                    key = _event_key(r)
                    if key not in have:
                        extra.setdefault(key, r)
                    affects[key].append(path)
        return list(extra.values()), dict(affects)

    def _internal_events(
        self, name: str, used: list[str], after: int, upto: int, include_added: bool, rows: list
    ) -> list[Mapping]:
        """`added` and `removed` events in (after, upto] for used paths that aren't public API in some release
        (DT-CORE-46), which the index's events don't record: langchain 0.0.134 added the private
        `SQLDatabase._get_sample_rows`, and flask 3.0 removed `_app_ctx_stack`, which its `__getattr__` served until
        2.3. Worked out from the path's public and internal spans, with `detail.internal` true; a change the events
        already have isn't repeated, and neither is one where the path's parent is then neither a module nor a class
        of the package (langchain 0.0.349's `sql_database.SQLDatabase` is an alias into langchain-community, whose
        members it doesn't record)."""
        assert self.store is not None
        have = {(r["path"], r["ordinal"], r["kind"]) for r in rows}
        out: list[Mapping] = []
        for path in used:
            internal = self.store.internal_spans(name, path)
            if not internal:
                continue
            at: dict[int, str] = {}  # ordinal -> the path's kind there, between `after` and `upto`
            for span in [*self.store.spans(name, path), *internal]:
                for o in range(max(span["first_ordinal"], after), min(span["last_ordinal"], upto) + 1):
                    at.setdefault(o, span["kind"])
            for o in range(after + 1, upto + 1):
                was, now = at.get(o - 1), at.get(o)
                if (was is None) == (now is None):
                    continue
                kind = "added" if now is not None else "removed"
                if (kind == "added" and not include_added) or (path, o, kind) in have:
                    continue
                parent = path.rpartition(".")[0]
                held = self.store.span_at(name, parent, o if kind == "removed" else o - 1)
                if held is not None and held["kind"] not in ("module", "class"):
                    continue
                detail = {"kind": now or was, "internal": True}
                out.append(
                    {
                        "path": path,
                        "ordinal": o,
                        "prev_ordinal": o - 1,
                        "kind": kind,
                        "detail": json.dumps(detail, sort_keys=True),
                    }
                )
        return out

    def _unsuppressed(self, name: str, events: list[HistoryEvent]) -> tuple[list[HistoryEvent], int]:
        kept = [e for e in events if not any(x.matches(name, e.path, e.kind) for x in self.suppressions)]
        return kept, len(events) - len(kept)

    # ------------------------------------------------------------------ explain_error
    def explain_error(
        self,
        traceback: str,
        installed_versions: dict[str, str] | None = None,
        pip_freeze: str | None = None,
        min_confidence: Confidence | None = None,
    ) -> ExplainResult:
        signal = parse_traceback(traceback)
        if signal is None:
            return ExplainResult(
                signal=None,
                findings=[],
                note="No Python exception line found. Pass the traceback text including the final 'SomeError: message' line.",
            )
        if signal.kind == "circular_import":
            return ExplainResult(
                signal=signal.to_model(),
                findings=[],
                note="Circular import inside your own code, not a library version change.",
            )
        if signal.kind == "unknown":
            return ExplainResult(
                signal=signal.to_model(),
                findings=[],
                note="This error shape is not a version-change symptom DepTime analyses (ImportError, ModuleNotFoundError, AttributeError, TypeError on call signatures).",
            )
        if self.store is None:
            # Without an index every package would look "not covered", which teaches the agent the opposite of
            # the truth on its very first call.
            return ExplainResult(signal=signal.to_model(), findings=[], note=self._no_index_note())
        installed, alias_notes = self._read_aliases(parse_installed(installed_versions, pip_freeze))
        named = self._candidates(signal, installed)
        candidates, rivals, owner_notes = self._import_owners(
            named, installed, complete=bool(pip_freeze and pip_freeze.strip())
        )
        # Covered packages whose import name another distribution owns here: never searched as the callee either.
        dropped = {s.name for s in named} - {s.name for s in candidates}
        findings: list[Finding] = []
        not_covered: list[str] = list(rivals)
        for spec in candidates:
            if not self._indexed(spec.name):
                not_covered.append(spec.name)
                continue
            findings.extend(
                self._findings_for(spec, signal, installed.get(spec.name))[:MAX_FINDINGS_PER_PACKAGE]
            )
        # The module whose code raised: a library's (in site-packages) or None for the user's own code.
        raiser = signal.frames[-1].module if signal.frames else None
        stdlib_notes: list[str] = []
        if signal.kind in MODULE_SIGNALS and not named and signal.module:
            entry = stdlib.lookup(signal.module)
            changed = stdlib.lookup_name(signal.module, signal.name) if entry and signal.name else None
            if entry is not None and signal.kind == "module_missing":
                found, stdlib_notes = self._stdlib_findings(signal, entry, installed.get("python"), raiser)
                findings += found
            elif changed is not None:
                found, stdlib_notes = self._stdlib_name_findings(
                    signal, changed, installed.get("python"), raiser
                )
                findings += found
            else:
                not_covered.append(signal.module.split(".")[0])
                if entry is not None:
                    stdlib_notes.append(
                        f"`{entry.module}` is in the standard library. DepTime knows which standard modules each "
                        "Python version has, and the names inside them that typeshed's stubs show changing between "
                        "Python 3.9 and 3.15"
                        + (f"; `{signal.name}` isn't one of them." if signal.name else ".")
                    )
        callee: list[Finding] = []
        declared: set[str] = set()
        if signal.kind in OBJECT_SIGNALS and all(f.issue in INCONCLUSIVE_ISSUES for f in findings):
            declared = self._declared_by(raiser, installed, dropped)
            callee = self._callee_findings(signal, installed, {s.name for s in named}, declared)
            callee = self._through_reexports(callee, installed, signal)
            if callee:
                # A concrete change in the called package explains the error; "never existed" in the caller is noise.
                findings = [f for f in findings if f.issue != "never_existed"] + callee
        elif signal.kind in OBJECT_SIGNALS:
            findings = self._through_reexports(findings, installed, signal)
        rank = {"high": 0, "medium": 1, "low": 2}
        misnamed = self._misnamed(findings, signal)
        findings.sort(
            key=lambda f: (id(f) in misnamed, rank[f.confidence], f.issue == "present_in_installed_version")
        )
        findings = [self._act_on(f, installed) for f in findings]
        findings = [
            f.model_copy(update={"confidence_factors": self._factors(f, installed)}) for f in findings
        ]
        # The project's suppressions, then the confidence asked for (DT-CORE-30): counted, never silent.
        kept = [
            f for f in findings if not any(x.matches(f.package, f.symbol, f.issue) for x in self.suppressions)
        ]
        suppressed, findings = len(findings) - len(kept), kept
        floor = rank.get(min_confidence, len(rank)) if isinstance(min_confidence, str) else len(rank)
        kept = [f for f in findings if rank[f.confidence] <= floor]
        below, findings = len(findings) - len(kept), kept
        notes = list(owner_notes)
        notes += [alias_notes[p] for p in sorted({f.package for f in findings}) if p in alias_notes]
        if signal.kind in {"unexpected_kwarg", "missing_argument", "too_many_positional"} and signal.owner:
            notes += self._owner_unknown_note(signal.owner, findings)
        if signal.kind == "import_name" and not signal.module and findings:
            notes.append(
                f"The error names `{signal.name}` without its module (compiled code, or a trimmed message), so "
                "DepTime looked it up among the modules of the installed packages."
            )
        if signal.wrapped_in:
            notes.append(
                f"The last exception, {signal.wrapped_in}, was raised from or while handling this "
                f"{signal.exception_type}, which is the one explained here."
            )
        notes += stdlib_notes
        if suppressed:
            notes.append(project.note(suppressed, "finding"))
        if below:
            notes.append(
                f"{below} finding{'' if below == 1 else 's'} below {min_confidence} confidence left out (min_confidence)."
            )
        if not findings and not stdlib_notes and not suppressed and not below:
            notes.append(
                "No version-history explanation found."
                + (
                    ""
                    if installed
                    else " Pass installed_versions or pip_freeze so DepTime can check the versions you actually run."
                )
            )
        elif not installed and any(f.package != PYTHON for f in findings):
            notes.append(
                "installed_versions/pip_freeze not provided; status is inferred from the latest indexed release."
            )
        if callee and raiser:
            caller = raiser.split(".")[0]
            changed = ", ".join(sorted({f.package for f in callee}))
            calls = (
                "which it lists in its requirements"
                if all(f.package in declared for f in callee)
                else "which it calls"
            )
            notes.append(
                f"The failing call is made inside {caller}'s code, and the change is in {changed}, {calls}. "
                f"Either pin {changed} (see pin_to_restore) or use a {caller} release that supports your {changed} "
                f"version; DepTime does not check {caller}'s side."
            )
        by_package: dict[str, list[str | None]] = defaultdict(list)
        for f in findings:
            if f.package != PYTHON:
                by_package[f.package] += [
                    f.resolved_version,
                    f.last_version_with,
                    f.first_version_without,
                    f.last_version_without,
                    f.first_version_with,
                ]
        notes += [
            n for package, versions in by_package.items() if (n := self._stand_in_note(package, versions))
        ]
        # A distribution DepTime doesn't index (not a standard module): where to look, and where to ask (DT-OSS-11).
        unindexed = sorted({n for n in not_covered if stdlib.lookup(n) is None})
        if unindexed and not findings:
            notes.append(
                f"DepTime doesn't index {_and([f'`{n}`' for n in unindexed])}: the `coverage` tool lists the packages "
                f"it does, and {PACKAGE_REQUEST_URL} asks for one."
            )
        note = " ".join(notes) or None
        return ExplainResult(
            signal=signal.to_model(),
            findings=findings,
            not_covered=sorted(set(not_covered)),
            note=note,
            suppressed=suppressed,
            below_min_confidence=below,
        )

    def _factors(self, f: Finding, installed: dict[str, str]) -> list[ConfidenceFactor]:
        """Why `f` has its confidence (DT-CORE-30), from the same facts that set it."""
        out: list[ConfidenceFactor] = []
        top = {
            "deprecation_message": "replacement_from_library",
            "moved_heuristic": "replacement_inferred",
            "curated": "replacement_curated",
            "error_message": "replacement_from_error",
        }
        if f.package == PYTHON:
            out.append(
                "python_version_given"
                if installed.get(PYTHON)
                else "python_version_from_traceback"
                if f.installed_version
                else "python_version_unknown"
            )
            return out + ([top[f.replacements[0].source]] if f.replacements else [])  # type: ignore[list-item]
        name = f.package
        if not f.installed_version:
            out.append("installed_version_unknown")
        elif f.resolved_version is None:
            out.append("installed_version_before_first")
        else:
            try:
                exact = Version(f.resolved_version) == Version(f.installed_version)
            except InvalidVersion:
                exact = False
            ahead = self._ahead(name, self._release(name, f.resolved_version), exact)
            out.append(
                "installed_version_indexed"
                if exact
                else "installed_version_after_newest"
                if ahead
                else "installed_version_between_releases"
            )
        anchor = self._release(name, f.resolved_version or f.last_version_with or f.first_version_with or "")
        anchor = anchor or (self.store.newest_release(name) if self.store else None)
        path = f.symbol.partition("(")[0]
        if anchor is not None and self.store is not None:
            o = anchor["ordinal"]
            own = self.store.span_at(name, path, o)
            parts = path.split(".")
            module = next(
                (
                    m
                    for i in range(len(parts) - 1, 0, -1)
                    if (s := self.store.span_at(name, (m := ".".join(parts[:i])), o)) is not None
                    and s["kind"] == "module"
                ),
                None,
            )
            if own is not None and own["kind"] == "getattr":
                out.append("served_by_getattr")
            elif module is not None and self.store.span_at(name, f"{module}.__getattr__", o) is not None:
                out.append("module_has_getattr")
            else:
                out.append("module_static")
        out.append("internal_name" if f.internal else "public_name")
        if f.replacements:
            out.append(top[f.replacements[0].source])  # type: ignore[arg-type]
        return out

    def _act_on(self, f: Finding, installed: dict[str, str]) -> Finding:
        """What an agent needs to act on a finding without looking further (DT-CORE-29): the callable's signature on
        each side of the change, the two releases' PyPI pages and the project's changelog, and a fix where the index
        proves one. Everything comes from the index; nothing is written."""
        if self.store is None or f.package == PYTHON or not self._indexed(f.package):
            return f
        name = f.package
        m = PARAM_SYMBOL_RE.fullmatch(f.symbol)
        path, param = (m["path"], m["param"]) if m else (f.symbol, None)
        not_yet = f.issue in {"symbol_not_yet_added", "param_not_yet_added"}
        before = f.last_version_without if not_yet else f.last_version_with
        after = (f.first_version_with if not_yet else f.first_version_without) or f.resolved_version
        update: dict = {}
        if f.issue not in {"never_existed", "module_removed"}:
            update["signature_before"] = self._signature(name, path, before)
            update["signature_after"] = self._signature(name, path, after)
        links = [
            Link(title=f"{name} {v} on PyPI", url=f"https://pypi.org/project/{name}/{v}/")
            for v in dict.fromkeys(v for v in (before, after) if v)
            if self._release(name, v) is not None
        ]
        anchor = self._release(name, after or before or "") or self.store.newest_release(name)
        if anchor is not None and (changelog := self._changelog(name, anchor["ordinal"])) is not None:
            links.append(changelog)
        update["links"] = links
        if f.issue in FIXABLE_ISSUES:
            update["fix"] = self._fix(f, path, param, installed)
        return f.model_copy(update=update)

    def _release(self, name: str, version: str) -> sqlite3.Row | None:
        """The indexed release a finding names: its versions are written as the index has them."""
        assert self.store is not None
        return self.store.release(name, version) if version else None

    def _callable_params(self, name: str, path: str, ordinal: int) -> str | None:
        """The JSON parameters of `path` at a release: a function's, or a class's `__init__`'s."""
        assert self.store is not None
        span = self.store.span_at(name, path, ordinal)
        if span is None:
            return None
        if span["params"] is None and span["kind"] == "class":
            init = self.store.span_at(name, f"{path}.__init__", ordinal)
            return init["params"] if init else None
        return span["params"]

    def _signature(self, name: str, path: str, version: str | None) -> list[ParamInfo] | None:
        rel = self._release(name, version) if version else None
        return None if rel is None else _params(self._callable_params(name, path, rel["ordinal"]))

    def _changelog(self, name: str, ordinal: int) -> Link | None:
        """The changelog or release notes a release's `Project-URL` entries name (or the nearest release's)."""
        assert self.store is not None
        urls = self.store.project_urls(name, ordinal)
        labels = {label: "".join(c for c in label.lower() if c.isalnum()) for label in urls}
        for wanted in CHANGELOG_LABELS:
            for label, norm in labels.items():
                url = urls[label]
                if not (norm == wanted or norm.endswith(wanted)):
                    continue
                if (
                    not url.lower().startswith(("https://", "http://"))
                    or len(url) > 500
                    or any(c.isspace() or ord(c) < 32 for c in url)
                    or instruction_like(label)
                    or instruction_like(url)
                ):
                    continue
                return Link(title=f"{name} {untrusted_text(label)}", url=url)
        return None

    def _verify_release(self, name: str, installed: dict[str, str]) -> sqlite3.Row | None:
        """The release a fix is checked at: the installed version's indexed release (the nearest earlier one),
        else the newest."""
        assert self.store is not None
        version = installed.get(name)
        if version:
            row, _ = self.store.resolve_version(name, version)
            if row is not None:
                return row
        return self.store.newest_release(name)

    def _fix(self, f: Finding, path: str, param: str | None, installed: dict[str, str]) -> Fix | None:
        """The one change the index proves. A replacement qualifies at confidence >= 0.75, or when its target is the
        very object the old path was, and only if the index shows the target at the installed release (else the
        newest): a path there, or an argument the callable accepts there. The same object wins; otherwise exactly one
        replacement must qualify, since the library naming two ("Use 'proxy' or 'mounts'") is a choice to make."""
        assert self.store is not None
        name = f.package
        found: list[tuple[Fix, str]] = []  # each with the object it names, so two paths to one object agree
        old = self._release(name, f.last_version_with or "")
        was = self.store.span_at(name, path, old["ordinal"]) if old is not None else None
        was_module = f.issue == "module_removed" or (was is not None and was["kind"] == "module")
        checked: dict[str, sqlite3.Row | None] = {}

        def verify(owner: str) -> sqlite3.Row | None:
            if owner not in checked:
                checked[owner] = self._verify_release(owner, installed)
            return checked[owner]

        for r in f.replacements:
            if r.target == (param or path):
                continue
            if param is not None:
                rel = verify(name)
                if (
                    r.confidence >= FIX_MIN_CONFIDENCE
                    and r.target.isidentifier()
                    and rel is not None
                    and _accepts_kwarg(self._callable_params(name, path, rel["ordinal"]), r.target)
                ):
                    # A rename the index pairs in one release differs only in case or underscores (DT-CORE-24).
                    renamed = r.source == "moved_heuristic"
                    fix = Fix(
                        kind="argument",
                        old=param,
                        new=r.target,
                        verified_at=rel["version"],
                        same_object=renamed,
                        confidence=r.confidence,
                        source=r.source,
                        evidence=r.evidence,
                    )
                    found.append((fix, r.target))
                continue
            if not DOTTED_RE.fullmatch(r.target):
                continue
            # The package whose path it is: this one, or another indexed one (a curated cross-package hint).
            owner = next(
                (s.name for s in self.coverage.packages_for_module(r.target) if self._indexed(s.name)), None
            )
            rel = verify(owner) if owner else None
            target = self.store.span_at(owner, r.target, rel["ordinal"]) if owner and rel else None
            # A module for a module, and a name for a name: a package to install isn't a path to write.
            if target is None or (target["kind"] == "module") != was_module:
                continue
            same = bool(was is not None and was["canonical"] and was["canonical"] == target["canonical"])
            if r.confidence >= FIX_MIN_CONFIDENCE or same:
                fix = Fix(
                    kind="path",
                    old=path,
                    new=r.target,
                    verified_at=rel["version"],
                    same_object=same,
                    confidence=r.confidence,
                    source=r.source,
                    evidence=r.evidence,
                    signature=_params(self._callable_params(owner, r.target, rel["ordinal"])),
                )
                found.append((fix, target["canonical"] or r.target))
        same = [fix for fix, _ in found if fix.same_object]
        if same:
            return same[0]
        return found[0][0] if len({obj for _, obj in found}) == 1 else None

    def _candidates(self, signal: ErrorSignal, installed: dict[str, str]) -> list[PackageSpec]:
        found: dict[str, PackageSpec] = {}
        if signal.kind in MODULE_SIGNALS and signal.module:
            specs = self.coverage.packages_for_module(signal.module)
            if len(specs) > 1 and installed:
                specs = [s for s in specs if s.name in installed] or specs
            for s in specs:
                found.setdefault(s.name, s)
        else:
            for module in [*reversed(signal.library_modules), *signal.referenced_names]:
                for s in self.coverage.packages_for_module(module):
                    found.setdefault(s.name, s)
            if not found:
                for pkg in installed:
                    s = self.coverage.get(pkg)
                    if s is not None:
                        found.setdefault(s.name, s)
        return list(found.values())

    @staticmethod
    def _stdlib_findings(
        signal: ErrorSignal, entry: stdlib.StdlibModule, given: str | None, raiser: str | None
    ) -> tuple[list[Finding], list[str]]:
        """A missing standard-library module, against the Python version installed_versions gives as "python" or
        the frames' paths name (DT-DATA-06). Without a version, the change the module had is reported at medium."""
        version = given or signal.python_version
        minor = stdlib.minor_of(version)
        mod = entry.module
        part = "" if mod == signal.module else f" `{signal.module}` is part of it."
        you = (
            f" You run Python {version}{'' if given else ' (from the traceback paths)'}."
            if minor is not None
            else ""
        )
        # Without a version, a module still there and added by the oldest Python typeshed supports is taken as there.
        present = (
            entry.has(minor)
            if minor is not None
            else entry.last is None and entry.first <= stdlib.OLDEST_MINOR
        )
        if present:
            where = (
                f"Python {version}"
                if minor is not None
                else "every Python 3 version"
                if entry.first == 0
                else f"Python 3.{entry.first} and later"
            )
            if mod != signal.module:
                return [], [
                    f"`{mod}` is in the standard library of {where}, and DepTime only knows the submodules typeshed's "
                    f"stdlib/VERSIONS lists, so it can't tell whether `{signal.module}` ever existed."
                ]
            return [], [
                f"`{mod}` is in the standard library of {where} (typeshed's stdlib/VERSIONS), so this isn't a version "
                "change: some standard modules exist only on some platforms, or only when Python was built with the "
                "system library they need."
            ]
        confidence: Confidence = "high" if minor is not None else "medium"
        if entry.last is not None and (minor is None or minor > entry.last):
            gone, last = f"3.{entry.last + 1}", f"3.{entry.last}"
            unsure = (
                ""
                if minor is not None
                else f' If you run Python {gone} or later, that is the cause; pass installed_versions={{"python": '
                '"3.X"} to confirm.'
            )
            caller = raiser.split(".")[0] if raiser else None
            replacements = _stdlib_curated(mod)
            targets = _and([f"`{r.target}`" for r in replacements], "or") if replacements else ""
            use = f" What's New in Python {gone} says to use {targets}." if targets else ""
            theirs = (
                f" The import is in {caller}'s code: a {caller} release made for Python {gone} may not need it; "
                f"DepTime does not check {caller}'s side."
                if caller
                else ""
            )
            return [
                Finding(
                    package=PYTHON,
                    symbol=mod,
                    issue="module_removed",
                    installed_version=version,
                    resolved_version=f"3.{minor}" if minor is not None else None,
                    last_version_with=last,
                    first_version_without=gone,
                    replacements=replacements,
                    fix=_stdlib_fix(mod, True, replacements, minor if minor is not None else entry.last + 1),
                    pin_to_restore=f"{PYTHON}<={last}",
                    confidence=confidence,
                    explanation=f"`{mod}` was removed from the standard library in Python {gone}; {last} is the last "
                    f"version with it.{part}{use}{you}{unsure}{theirs}",
                )
            ], []
        first = f"3.{entry.first}"
        unsure = (
            ""
            if minor is not None
            else ' If you run an earlier Python, that is the cause; pass installed_versions={"python": "3.X"} to '
            "confirm."
        )
        return [
            Finding(
                package=PYTHON,
                symbol=mod,
                issue="symbol_not_yet_added",
                installed_version=version,
                resolved_version=f"3.{minor}" if minor is not None else None,
                last_version_without=f"3.{entry.first - 1}",
                first_version_with=first,
                pin_to_restore=f"{PYTHON}>={first}",
                confidence=confidence,
                explanation=f"`{mod}` was added to the standard library in Python {first}.{part}{you}{unsure}",
            )
        ], []

    @staticmethod
    def _stdlib_name_findings(
        signal: ErrorSignal, entry: stdlib.StdlibName, given: str | None, raiser: str | None
    ) -> tuple[list[Finding], list[str]]:
        """A name inside a standard module that some Python versions have and others don't (DT-DATA-47), against
        the Python version installed_versions gives as "python" or the traceback's paths name. Without one, its
        change is reported at medium."""
        version = given or signal.python_version
        minor = stdlib.minor_of(version)
        path = entry.path
        if (
            minor is not None
            and not stdlib.NAMES_FIRST <= minor <= stdlib.NAMES_LAST
            and not entry.has(minor)
        ):
            return [], [
                f"`{path}` changes between Python 3.{stdlib.NAMES_FIRST} and 3.{stdlib.NAMES_LAST}, the versions "
                f"DepTime knows the standard library's names for, and you run Python {version}."
            ]
        if minor is not None and entry.has(minor):
            return [], [
                f"`{path}` is in Python {version}'s standard library (typeshed's stubs), so this isn't a version "
                "change: a local file named like the module can shadow it, and some names exist only on some platforms."
            ]
        you = (
            f" You run Python {version}{'' if given else ' (from the traceback paths)'}."
            if minor is not None
            else ""
        )
        caller = (
            raiser.split(".")[0] if raiser and raiser.split(".")[0] != signal.module.split(".")[0] else None
        )
        resolved = f"3.{minor}" if minor is not None else None
        confidence: Confidence = "high" if minor is not None else "medium"
        last = entry.last_before(minor) if minor is not None else None
        if minor is None and entry.ranges[-1][1] is not None:
            last = entry.ranges[-1][1]
        if last is not None:
            gone = f"3.{last + 1}"
            unsure = (
                ""
                if minor is not None
                else f' If you run Python {gone} or later, that is the cause; pass installed_versions={{"python": '
                '"3.X"} to confirm.'
            )
            theirs = (
                f" The name is used in {caller}'s code: a {caller} release made for Python {gone} may not need it; "
                f"DepTime does not check {caller}'s side."
                if caller
                else ""
            )
            replacements, moved = _stdlib_curated(path), ""
            if entry.moved:
                evidence = (
                    f"typeshed's stubs give `{entry.moved}` the same object in Python {gone}, the first version "
                    f"without `{path}`."
                )
                replacements.insert(
                    0,
                    Replacement(
                        target=entry.moved, confidence=0.75, source="moved_heuristic", evidence=evidence
                    ),
                )
                moved = f" The same object is `{entry.moved}`."
            elif replacements:
                use = _and([f"`{r.target}`" for r in replacements], "or")
                moved = f" What's New in Python {gone} says to use {use}."
            fix = _stdlib_fix(path, False, replacements, minor if minor is not None else last + 1)
            return [
                Finding(
                    package=PYTHON,
                    symbol=path,
                    issue="symbol_removed",
                    installed_version=version,
                    resolved_version=resolved,
                    last_version_with=f"3.{last}",
                    first_version_without=gone,
                    replacements=replacements,
                    fix=fix,
                    pin_to_restore=f"{PYTHON}<=3.{last}",
                    confidence=confidence,
                    explanation=f"`{path}` was removed from the standard library in Python {gone}; 3.{last} is the "
                    f"last version with it (typeshed's stubs).{moved}{you}{unsure}{theirs}",
                )
            ], []
        first = entry.first_after(minor) if minor is not None else entry.ranges[0][0]
        assert first is not None
        unsure = (
            ""
            if minor is not None
            else ' If you run an earlier Python, that is the cause; pass installed_versions={"python": "3.X"} to '
            "confirm."
        )
        return [
            Finding(
                package=PYTHON,
                symbol=path,
                issue="symbol_not_yet_added",
                installed_version=version,
                resolved_version=resolved,
                last_version_without=f"3.{first - 1}",
                first_version_with=f"3.{first}",
                pin_to_restore=f"{PYTHON}>=3.{first}",
                confidence=confidence,
                explanation=f"`{path}` was added to the standard library in Python 3.{first} (typeshed's stubs).{you}"
                f"{unsure}",
            )
        ], []

    def _read_aliases(self, installed: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """Read a covered package's version off a distribution that installs the same code under another name
        (`installed_as`: pinecone-client 3.2.2 is pinecone 3.2.2) when the covered one isn't listed (DT-CORE-03).
        Returns the versions and, per package read this way, the note that says so."""
        out, notes = dict(installed), {}
        for spec in self.coverage.packages:
            alias = None if spec.name in installed else spec.alias_version(installed)
            if alias:
                other, version = alias
                out[spec.name] = version
                notes[spec.name] = (
                    f"{other} {version} is read as {spec.name} {version}: {other} installs the same code."
                )
        return out, notes

    def _import_owners(
        self, specs: list[PackageSpec], installed: dict[str, str], complete: bool
    ) -> tuple[list[PackageSpec], list[str], list[str]]:
        """Settle which distribution a shared import name (`shares_import_name_with`: `agents`) belongs to here.

        A package that is installed stays. One whose rival is installed instead, or that a whole `pip freeze`
        leaves out, is dropped: its history would explain another library's error. With nothing to go on it stays,
        with a note that says what was assumed (DT-CORE-03). Returns the kept packages, what goes to
        `not_covered`, and the notes.
        """
        kept: list[PackageSpec] = []
        others: list[str] = []
        notes: list[str] = []
        for spec in specs:
            if not spec.shares_import_name_with:
                kept.append(spec)
                continue
            names = _and([f"`{n}`" for n in spec.import_names])
            rivals = [d for d in spec.shares_import_name_with if d in installed]
            alternatives = _and(list(spec.shares_import_name_with), "or")
            if spec.name in installed:
                kept.append(spec)
                if rivals:
                    notes.append(
                        f"{_and([spec.name, *rivals])} are both installed, and both install {names}: "
                        "whichever was installed last overwrote the other's files, which can cause this error "
                        "by itself. Uninstall both, then reinstall the one you use."
                    )
            elif rivals:
                others += rivals
                notes.append(
                    f"{names} here is {_and(rivals)}'s, which DepTime does not cover; {spec.name}, "
                    "which it covers, installs the same import name, so its history doesn't apply."
                )
            elif complete:
                others.append(spec.import_names[0])
                notes.append(
                    f"pip_freeze lists no {spec.name}, so {names} comes from another distribution that installs "
                    f"the same import name, such as {alternatives}; DepTime doesn't cover those."
                )
            else:
                kept.append(spec)
                notes.append(
                    f"{names} is also the import name of {_and(list(spec.shares_import_name_with))}; this answer "
                    f"assumes {spec.name}. Pass installed_versions or pip_freeze to tell them apart."
                )
        return kept, others, notes

    def _declared_by(
        self, module: str | None, installed: dict[str, str], skip: set[str] | frozenset[str] = frozenset()
    ) -> set[str]:
        """The distributions that `module`'s indexed package requires (DT-DATA-14): at the installed release (the
        nearest indexed one at or below it), or in any indexed release when it isn't installed or predates the
        index. `skip`: packages that don't own the module here (DT-CORE-03)."""
        assert self.store is not None
        out: set[str] = set()
        for spec in self.coverage.packages_for_module(module) if module else []:
            if not self._indexed(spec.name) or spec.name in skip:
                continue
            version = installed.get(spec.name)
            resolved = self.store.resolve_version(spec.name, version)[0] if version else None
            out |= self.store.declared_dependencies(spec.name, resolved["ordinal"] if resolved else None)
        return out

    def _callee_findings(
        self,
        signal: ErrorSignal,
        installed: dict[str, str],
        tried: set[str],
        declared: set[str],
    ) -> list[Finding]:
        """Concrete changes in packages the traceback's code may be *calling*.

        When library A calls library B with an argument B no longer accepts, Python raises at A's call site
        before any of B's code runs, so B never appears among the frames (openai -> httpx.Client(proxies=...)).
        Search other indexed packages that have the symbol: first those A requires (`declared`, DT-CORE-16), then
        the others, installed ones first within each, and stop at the first group with a finding. weaviate-client
        also removed `Client(proxies)`, but openai doesn't require it. A requirement only ranks: langchain-community
        0.3 calls anthropic without listing it. Only concrete version changes count here: a name that is simply
        absent from an unrelated package is not evidence.
        """
        assert self.store is not None
        if signal.kind in {"unexpected_kwarg", "missing_argument"}:
            suffix = signal.owner
        elif signal.owner and signal.name:
            suffix = f"{signal.owner.rsplit('.', 1)[-1]}.{signal.name}"
        else:
            suffix = None
        if not suffix:
            return []
        packages = [p for p in self.store.packages_with_suffix(suffix) if p not in tried]
        if installed and "." not in suffix:
            # A bare name is in most packages (`create` in 26): with the installed versions given, only those
            # installed or required can be the callee (DT-CORE-36), not litellm for openai's `create(file_ids)`.
            packages = [p for p in packages if p in installed or p in declared]
        groups = [
            [p for p in packages if (p in declared) == want_declared and (p in installed) == want_installed]
            for want_declared in (True, False)
            for want_installed in (True, False)
        ]
        for group in groups:
            found: list[Finding] = []
            for p in group:
                out = self._findings_for(self._spec(p), signal, installed.get(p))[:MAX_FINDINGS_PER_PACKAGE]
                found.extend(f for f in out if f.issue not in INCONCLUSIVE_ISSUES)
            if found:
                return found
        if not packages and signal.kind in {"object_attribute", "class_attribute"}:
            return self._private_member_findings(signal, installed, tried)
        return []

    def _private_member_findings(
        self, signal: ErrorSignal, installed: dict[str, str], tried: set[str]
    ) -> list[Finding]:
        """`_private_member_finding` in each installed covered package not searched yet (DT-CORE-42)."""
        assert self.store is not None
        out: list[Finding] = []
        for name, version in installed.items():
            if name in tried or self.coverage.get(name) is None or not self.store.has_package(name):
                continue
            resolved, exact = self.store.resolve_version(name, version)
            out += self._private_member_finding(self._spec(name), signal, resolved, exact, version)
        return out

    def _private_member_finding(
        self,
        spec: PackageSpec,
        signal: ErrorSignal,
        resolved: sqlite3.Row | None,
        exact: bool,
        installed: str | None,
    ) -> list[Finding]:
        """A private attribute missing from a class no covered package has, found among the package's members that
        aren't public API (DT-CORE-42): gplearn's `SymbolicRegressor.fit` calls `self._validate_data`, which
        scikit-learn 1.7 removed from `BaseEstimator`. Only at a known installed release, when exactly one class of
        the package had the name until before it and no class has it there; inferred, so at most medium."""
        assert self.store is not None
        member, owner = signal.name or "", (signal.owner or "").rsplit(".", 1)[-1]
        if resolved is None or not _is_private(member):
            return []
        rows = self.store.internal_member_owners(spec.name, member)
        ordinal = resolved["ordinal"]
        if not rows or any(r["first_ordinal"] <= ordinal <= r["last_ordinal"] for r in rows):
            return []  # never a member, or some class still has it: the error has another cause
        gone = {r["cls"]: r for r in rows if r["last_ordinal"] < ordinal}
        if len(gone) != 1:
            return []
        (row,) = gone.values()
        f = self._symbol_finding(
            spec, f"{row['label']}.{member}", resolved, exact, installed, message=signal.message
        )
        if f.issue != "symbol_removed":
            return []
        why = (
            f"`{owner}` isn't a class of a package DepTime covers; of {spec.name}'s classes, only "
            f"`{row['label']}` had `{member}`, which subclasses inherit."
        )
        return [self._inferred(f, why)]

    def _findings_for(
        self, spec: PackageSpec, signal: ErrorSignal, installed_version: str | None
    ) -> list[Finding]:
        assert self.store is not None
        name = spec.name
        resolved, exact = (
            self.store.resolve_version(name, installed_version) if installed_version else (None, False)
        )
        if signal.kind == "import_name" and not signal.module:
            return self._unplaced_import_findings(spec, signal, resolved, exact, installed_version)
        if signal.kind in {"import_name", "module_attribute"}:
            return [
                self._symbol_finding(
                    spec,
                    f"{signal.module}.{signal.name}",
                    resolved,
                    exact,
                    installed_version,
                    message=signal.message,
                )
            ]
        if signal.kind == "module_missing":
            return [
                self._symbol_finding(
                    spec,
                    signal.module or "",
                    resolved,
                    exact,
                    installed_version,
                    module=True,
                    message=signal.message,
                )
            ]
        if signal.kind in {"object_attribute", "class_attribute"} and signal.owner and signal.name:
            owner_leaf = signal.owner.rsplit(".", 1)[-1]
            paths = self._rank(self.store.find_by_suffix([name], f"{owner_leaf}.{signal.name}"), signal)
            if paths:
                findings = []
                for path, inherited in self._defining_paths(name, paths):
                    f = self._symbol_finding(
                        spec, path, resolved, exact, installed_version, message=signal.message
                    )
                    if inherited:
                        f = f.model_copy(update={"explanation": f"{inherited} {f.explanation}"})
                    findings.append(f)
                return findings
            owners = self._rank(self.store.find_by_suffix([name], owner_leaf), signal)
            if not owners and _is_private(owner_leaf):
                # A private class users reach only through an instance (DT-DATA-46): weaviate's `_Collections`.
                owners = self._rank(self.store.find_internal_classes(name, owner_leaf), signal)
            return (
                [
                    self._symbol_finding(
                        spec,
                        f"{owners[0]}.{signal.name}",
                        resolved,
                        exact,
                        installed_version,
                        message=signal.message,
                    )
                ]
                if owners
                else self._subclass_member_findings(spec, signal, resolved, exact, installed_version)
                or self._private_member_finding(spec, signal, resolved, exact, installed_version)
            )
        if signal.kind in {"unexpected_kwarg", "missing_argument"} and signal.owner:
            param = signal.kwarg if signal.kind == "unexpected_kwarg" else (signal.missing_args or [None])[0]
            out = []
            # The owner the message names, then a class whose __init__ passed **kwargs on to it (DT-CORE-27).
            for owner in [signal.owner, *signal.forwarded_owners]:
                pairs, takes = self._takers(name, owner, param, resolved)
                paths = self._rank(pairs, signal, takes)
                out += [
                    self._param_finding(
                        spec,
                        p,
                        param,
                        signal.kind,
                        resolved,
                        exact,
                        installed_version,
                        message=signal.message,
                    )
                    for p in paths
                    if param
                ]
            if not out and signal.kind == "unexpected_kwarg" and param:
                out = self._override_findings(spec, signal, param, resolved, exact, installed_version)
            # A concrete change first, so the per-package limit never drops it behind inconclusive ones.
            return sorted((f for f in out if f is not None), key=lambda f: f.issue in INCONCLUSIVE_ISSUES)
        if signal.kind == "too_many_positional" and signal.owner:
            paths = self._rank(self.store.find_by_suffix([name], signal.owner), signal)
            found = (
                self._positional_finding(spec, p, signal.message, resolved, exact, installed_version)
                for p in paths
            )
            return [f for f in found if f is not None]
        return []

    def _misnamed(self, findings: list[Finding], signal: ErrorSignal) -> set[int]:
        """The findings whose function Python wouldn't have named as the message does, when another one's it would:
        an argument error names the class that defines the function (its `__qualname__`), so `Client.__init__() got
        an unexpected keyword argument` comes from httpx's `Client`, not anthropic's, an alias of the class
        `Anthropic` whose `__init__` is `Anthropic.__init__`. They rank after the others, not out: a wrapper may
        rename a function. By `id`."""
        assert self.store is not None
        owner = signal.owner or ""
        if signal.kind not in {"unexpected_kwarg", "missing_argument"} or "." not in owner:
            return set()
        method = owner.rpartition(".")[2]

        def fits(f: Finding) -> bool | None:
            if f.package == PYTHON:
                return None
            path = f.symbol.partition("(")[0]
            # The release the finding is about: anthropic 0.2's `Client` was a class of its own.
            version = f.resolved_version or f.last_version_with or f.first_version_with
            row = self.store.resolve_version(f.package, version)[0] if version else None
            span = self.store.span_at(f.package, path, row["ordinal"]) if row is not None else None
            spans = [span] if span is not None else self.store.spans(f.package, path)
            names = {
                s["canonical"] if path.rpartition(".")[2] == method else f"{s['canonical']}.{method}"
                for s in spans
                if s["canonical"]
            }
            owners = [owner, *signal.forwarded_owners]  # a class whose __init__ passed it on (DT-CORE-27)
            return any(n == o or n.endswith("." + o) for n in names for o in owners) if names else None

        verdicts = {id(f): fits(f) for f in findings}
        if True not in verdicts.values():
            return set()
        return {key for key, fit in verdicts.items() if fit is False}

    @staticmethod
    def _owner_unknown_note(owner: str, findings: list[Finding]) -> list[str]:
        """An error that names a method without its class (`sort() got an unexpected keyword argument`, as older
        Pythons and some wrappers print it), matched in several classes that changed the same way (DT-CORE-31)."""
        if "." in owner:
            return []
        top = findings[0] if findings else None
        same = [
            f
            for f in findings
            if top is not None
            and (f.package, f.issue, f.last_version_with, f.first_version_with)
            == (top.package, top.issue, top.last_version_with, top.first_version_with)
            and f.symbol.partition("(")[0].rpartition(".")[2] == owner
        ]
        classes = [f.symbol.partition("(")[0].rpartition(".")[0].rpartition(".")[2] for f in same]
        if len(set(classes)) < 2:
            return []
        return [
            f"The error names `{owner}()` without its class, and {len(classes)} classes' `{owner}` changed the same way "
            f"({_and([f'`{c}`' for c in classes])}); the findings list each, with the same versions and pin."
        ]

    def _through_reexports(
        self, findings: list[Finding], installed: dict[str, str], signal: ErrorSignal
    ) -> list[Finding]:
        """A class member read removed where its class's path became a re-export of another covered distribution's
        class, as that class's member there (DT-CORE-43): langchain 0.0.341 made `langchain.schema.BaseRetriever`
        langchain-core's class, so langchain stopped recording its members, and `get_relevant_documents` read removed
        after 0.0.340. langchain-core removed it in 1.0.0, which is what `'MultiQueryRetriever' object has no
        attribute 'get_relevant_documents'` on langchain-core 1.0.0 is about. At the other distribution's installed
        release, else its newest."""
        assert self.store is not None
        out: list[Finding] = []
        for f in findings:
            moved = None
            if f.issue == "symbol_removed" and f.first_version_without and f.package != PYTHON:
                owner, _, member = f.symbol.rpartition(".")
                row = self.store.resolve_version(f.package, f.first_version_without)[0]
                span = (
                    self.store.span_at(f.package, owner, row["ordinal"])
                    if row is not None and owner
                    else None
                )
                target = span["canonical"] if span is not None and span["kind"] == "alias" else None
                others = self.coverage.packages_for_module(target.rpartition(".")[0]) if target else []
                other = next(
                    (o for o in others if o.name != f.package and self.store.has_package(o.name)), None
                )
                if other is not None:
                    version = installed.get(other.name)
                    resolved, exact = (
                        self.store.resolve_version(other.name, version) if version else (None, False)
                    )
                    g = self._symbol_finding(
                        other, f"{target}.{member}", resolved, exact, version, message=signal.message
                    )
                    if g.issue != "never_existed":  # still there: present, and the error has another cause
                        why = (
                            f"{f.package}'s `{owner}` is {other.name}'s `{target}` from {f.package} "
                            f"{f.first_version_without} on, so its `{member}` is {other.name}'s."
                        )
                        moved = g.model_copy(update={"explanation": f"{why} {g.explanation}"})
            if moved is not None and all(moved.symbol != o.symbol for o in out):
                out.append(moved)
            elif moved is None:
                out.append(f)
        return out

    @staticmethod
    def _inferred(f: Finding, why: str) -> Finding:
        """A finding about a class the user's code subclasses, as DT-CORE-02 infers it: at most medium."""
        confidence: Confidence = "medium" if f.confidence == "high" else f.confidence
        return f.model_copy(update={"explanation": f"{why} {f.explanation}", "confidence": confidence})

    def _subclass_member_findings(
        self,
        spec: PackageSpec,
        signal: ErrorSignal,
        resolved: sqlite3.Row | None,
        exact: bool,
        installed: str | None,
    ) -> list[Finding]:
        """An attribute missing from a class the package doesn't have, named like one of its classes (DT-CORE-02):
        marshmallow 4 dropped `Field.fail` and `Schema.context`, and the error names the user's subclass,
        `'MACAddressField' object has no attribute 'fail'`. Each class of the package whose name ends the error's
        class name and whose `name` the installed release no longer has, by its defining class; ambiguity shows as
        several findings at low confidence."""
        assert self.store is not None
        name, owner = spec.name, (signal.owner or "").rsplit(".", 1)[-1]
        found: dict[str, Finding] = {}
        for _, path in self.store.find_by_suffix([name], signal.name or "", limit=200):
            cls = path.rpartition(".")[0]
            leaf = cls.rpartition(".")[2]
            if not leaf or leaf == owner or not owner.endswith(leaf) or not leaf[:1].isupper():
                continue
            for defining, _ in self._defining_paths(name, [path]):
                f = self._symbol_finding(spec, defining, resolved, exact, installed, message=signal.message)
                if f.issue == "symbol_removed":
                    found.setdefault(f.symbol, f)
        if not found or len(found) > 3:
            return []
        out = []
        for f in found.values():
            base = f.symbol.rpartition(".")[0]
            why = (
                f"`{owner}` isn't a class of {name}; it looks like a subclass of `{base}`, and that class lost "
                f"`{signal.name}`."
            )
            g = self._inferred(f, why)
            out.append(g.model_copy(update={"confidence": "low"}) if len(found) > 1 else g)
        return out

    def _override_findings(
        self,
        spec: PackageSpec,
        signal: ErrorSignal,
        param: str,
        resolved: sqlite3.Row | None,
        exact: bool,
        installed: str | None,
    ) -> list[Finding]:
        """The package calling a user's override with an argument the override doesn't take (DT-CORE-02): click
        8.2 calls `ParamType.get_metavar(param, ctx)`, and a subclass written for 8.1 raises `LogParam.get_metavar()
        got an unexpected keyword argument 'ctx'`. Only when the class isn't the package's and the frame that raised
        is the package's own code: the method the package gave that required parameter, by its shortest path."""
        assert self.store is not None
        name = spec.name
        cls, _, method = (signal.owner or "").rpartition(".")
        raiser = signal.frames[-1].module if signal.frames else None
        if (
            not cls
            or not method
            or raiser is None
            or not any(raiser == n or raiser.startswith(n + ".") for n in spec.import_names)
            or self.store.find_by_suffix([name], cls.rpartition(".")[2])
        ):
            return []
        found = []
        for _, path in self.store.find_by_suffix([name], method, limit=200):
            if path.rpartition(".")[2] != method:
                continue
            f = self._param_finding(spec, path, param, "missing_argument", resolved, exact, installed)
            if f is not None and f.issue == "param_now_required":
                found.append((path, f))
        if not found:
            return []
        # The base class's method, which the others override: `ParamType.get_metavar`, not `Choice.get_metavar`.
        ordinal = resolved["ordinal"] if resolved else self.store.releases(name)[-1]["ordinal"]
        homes = {}
        for path, _ in found:
            span = self.store.span_at(name, path.rpartition(".")[0], ordinal)
            homes[path] = span["canonical"] if span is not None else None
        bases = [b for path, _ in found if homes[path] for b in self.store.mro(name, homes[path], ordinal)]
        path, f = min(found, key=lambda pf: (-bases.count(homes[pf[0]]), len(pf[0]), pf[0]))
        why = (
            f"`{cls}` isn't a class of {name}: it looks like a subclass overriding `{method}`, which {name}'s own code "
            f"now calls with `{param}`, so the override has to accept it."
        )
        return [self._inferred(f, why)]

    def _positional_finding(
        self,
        spec: PackageSpec,
        path: str,
        message: str,
        resolved: sqlite3.Row | None,
        exact: bool,
        installed: str | None,
    ) -> Finding | None:
        """Too many positional arguments (DT-CORE-09): the last release whose signature still took them all by
        position, and the first parameter that release takes by position and the installed one doesn't. One that
        became keyword-only is `param_kind_changed`, with passing it by name as the fix; one that's gone is
        `param_removed`. Only when the installed signature takes exactly the count the message gives."""
        assert self.store is not None
        m = POSITIONAL_COUNT_RE.search(message or "")
        if m is None:
            return None
        name = spec.name
        rels = self.store.releases(name)
        ordinal = resolved["ordinal"] if resolved else rels[-1]["ordinal"]

        def signature(o: int) -> list[dict] | None:
            raw = self._callable_params(name, path, o)
            return None if raw is None else json.loads(raw)

        def positional(params: list[dict]) -> list[dict]:
            return [p for p in params if p["kind"] in POSITIONAL_KINDS]

        current = signature(ordinal)
        if current is None or any(p["kind"] == "variadic positional" for p in current):
            return None
        offset = int(m["takes"]) - len(
            positional(current)
        )  # the instance Python counts and the index doesn't
        if offset not in {0, 1}:
            return None  # this signature doesn't produce the message
        need, after = int(m["given"]) - offset, ordinal
        for o in range(ordinal - 1, -1, -1):
            before = signature(o)
            if before is None:
                continue
            if len(positional(before)) < need and not any(p["kind"] == "variadic positional" for p in before):
                after = o
                continue
            later = {p["name"]: p for p in signature(after) or []}
            moved = next(
                (p for p in positional(before) if p["name"] not in {q["name"] for q in positional(current)}),
                None,
            )
            if moved is None:
                return None
            param, prev, changed_in = moved["name"], self._version(name, o), self._version(name, after)
            if param not in later:
                return self._param_finding(
                    spec, path, param, "unexpected_kwarg", resolved, exact, installed, message=message
                )
            if later[param]["kind"] != "keyword-only":
                return None
            now = self._version(name, ordinal)
            return Finding(
                package=name,
                symbol=f"{path}({param})",
                issue="param_kind_changed",
                installed_version=installed,
                resolved_version=resolved["version"] if resolved else None,
                last_version_with=prev,
                first_version_without=changed_in,
                pin_to_restore=f"{name}<={prev}",
                confidence=self._confidence(resolved, exact),
                fix=Fix(
                    kind="keyword",
                    old=param,
                    new=param,
                    verified_at=now or changed_in or "",
                    same_object=True,
                    confidence=1.0,
                    source="signature",
                    evidence=f"{path} takes `{param}` by position in {name} {prev} and only by keyword in {changed_in}.",
                ),
                explanation=f"`{param}` of {path} became keyword-only in {name} {changed_in}; {prev} still took it by "
                f"position. Pass it as `{param}=...`, or pin {name}<={prev}.",
            )
        return None

    def _defining_paths(self, name: str, paths: list[str]) -> list[tuple[str, str | None]]:
        """Each path, or for a member a class inherits, the defining class's shortest public path with the same
        object (DT-CORE-28): Django 4.0's removal of `is_ajax` reads `'WSGIRequest' object has no attribute
        'is_ajax'`, and the release notes say `HttpRequest.is_ajax()`. Only when the defining class's path is no
        deeper than the subclass's. Paired with a sentence saying so."""
        assert self.store is not None
        out: list[tuple[str, str | None]] = []
        for path in paths:
            spans = self.store.spans(name, path)
            canonical = spans[-1]["canonical"] if spans else None
            owner, _, member = path.rpartition(".")
            defining, _, defined = (canonical or "").rpartition(".")
            cls, sub = defining.rpartition(".")[2], owner.rpartition(".")[2]
            choice: tuple[str, str | None] = (path, None)
            if canonical and defined == member and cls and cls != sub:
                same = [
                    p
                    for _, p in self.store.find_by_suffix([name], f"{cls}.{member}")
                    if p.rpartition(".")[0].rpartition(".")[2] == cls
                    and any(s["canonical"] == canonical for s in self.store.spans(name, p))
                ]
                best = min(same, key=lambda p: (p.count("."), len(p), p)) if same else None
                # Not a mixin deeper in the package than the class users import (qdrant-client's
                # `QdrantClient.get_fastembed_vector_params` comes from `qdrant_fastembed.QdrantFastembedMixin`).
                if best and best.count(".") <= path.count("."):
                    choice = (best, f"{sub} inherits {member} from {cls}.")
            if choice[0] not in [p for p, _ in out]:
                out.append(choice)
        return out

    @staticmethod
    def _rank(
        pairs: list[tuple[str, str]], signal: ErrorSignal, prefer: frozenset[str] = frozenset()
    ) -> list[str]:
        """The paths most likely meant, shortest first after those the code names and then those in `prefer`."""
        refs = signal.referenced_names
        # The classes the code names, by their own name: `pl.DataFrame(data).sort(...)` names polars' `DataFrame`
        # through an alias the traceback doesn't resolve (DT-CORE-31).
        named = {r.rpartition(".")[2] for r in refs if r.rpartition(".")[2][:1].isupper()}

        def score(path: str) -> int:
            if any(path == r or path.startswith(r + ".") for r in refs):
                return 0
            return 0 if path.rpartition(".")[0].rpartition(".")[2] in named else 1

        paths = dict.fromkeys(p for _, p in pairs)
        return sorted(paths, key=lambda p: (score(p), p not in prefer, len(p)))[:MAX_FINDINGS_PER_PACKAGE]

    def _takers(
        self, name: str, owner: str, param: str | None, resolved: sqlite3.Row | None
    ) -> tuple[list[tuple[str, str]], frozenset[str]]:
        """The paths ending with `owner`, and which to rank first (DT-CORE-36): for a bare name, those whose signature
        had `param` in some release and that exist in the installed one, the only ones a call can have reached.
        `create() got an unexpected keyword argument 'file_ids'` on openai 1.28.1 means `Assistants.create`, one of
        hundreds of `create` paths, not the shortest, nor `Containers.create`, which took `file_ids` from 1.81."""
        assert self.store is not None
        takes = self.store.find_by_suffix([name], owner, param=param) if param and "." not in owner else []
        prefer = {p for _, p in takes}
        if resolved is not None:
            prefer = {p for p in prefer if self.store.span_at(name, p, resolved["ordinal"]) is not None}
        return [*takes, *self.store.find_by_suffix([name], owner)], frozenset(prefer)

    def _confidence(self, resolved: sqlite3.Row | None, exact: bool) -> Confidence:
        if resolved is not None and exact:
            return "high"
        return "medium" if resolved is not None else "low"

    def _window(
        self, name: str, resolved: sqlite3.Row | None, exact: bool, installed: str | None, changed_at
    ) -> tuple[str, str, str] | None:
        """When `installed` is not indexed, it sits between two indexed releases; report a change inside that window.

        Past the newest indexed release the window has no upper end, and the error is the only sign of a change:
        the release that made it isn't indexed yet (DT-DATA-04). The returned upper bound is then None."""
        if resolved is None or exact:
            return None
        nxt = resolved["ordinal"] + 1
        upper = self._version(name, nxt)
        lo = resolved["version"]
        if upper is None:
            return (lo, None, f"after {name} {lo}, {self._newest_note(name, installed)}")
        if not changed_at(nxt):
            return None
        return (
            lo,
            upper,
            f"between {name} {lo} and {upper} (your {installed} is in that window and not indexed)",
        )

    def _ahead(self, name: str, resolved: sqlite3.Row | None, exact: bool) -> bool:
        """Whether the installed release is newer than every indexed one (`resolved` is the newest, inexactly)."""
        assert self.store is not None
        return resolved is not None and not exact and self._version(name, resolved["ordinal"] + 1) is None

    def _newest_note(self, name: str, installed: str | None) -> str:
        """ "the newest release the index has (built …): your X is newer and not indexed yet"."""
        assert self.store is not None
        built = (self.store.built_at() or "")[:10]
        return (
            f"the newest release the index has{f' (built {built})' if built else ''}: your {installed} is newer and "
            "not indexed yet"
        )

    def _error_hints(
        self, spec: PackageSpec, path: str, ordinals: list[int], message: str | None, param: str | None = None
    ) -> list[Replacement]:
        """Replacements the error text itself names (DT-CORE-06), kept only when the index has them.

        The traceback is untrusted input, so a named target counts only as a public path of this package at one of
        `ordinals` (the release the installed version resolves to, and the first release without the old name), or
        of the covered package the message says the name moved to; a parameter only if the function takes it there.
        """
        assert self.store is not None
        if not message:
            return []
        name, evidence = spec.name, clean_evidence(message)
        found: dict[str, Replacement] = {}

        def keep(target: str, confidence: float) -> None:
            found.setdefault(
                target,
                Replacement(target=target, confidence=confidence, source="error_message", evidence=evidence),
            )

        params = (
            set().union(
                *(_param_names(s["params"]) for o in ordinals if (s := self.store.span_at(name, path, o)))
            )
            if param is not None
            else set()
        )
        for hint in mine(message, skip_expressions=True):
            t = hint.target
            if param is not None:
                t = t.lstrip(".")  # "in favor of `.condition`"
                if t != param and t.isidentifier() and t in params:
                    keep(t, hint.confidence)
                continue
            for candidate in _error_hint_paths(spec, path, t):
                if any(self.store.span_at(name, candidate, o) is not None for o in ordinals):
                    keep(candidate, hint.confidence)
                    break
        if param is None and (dist := moved_to_package(message)):
            other = self.coverage.get(dist)
            if other is not None and self._indexed(other.name):
                leaf = path.rpartition(".")[2]
                for n in other.import_names:
                    if self.store.spans(other.name, f"{n}.{leaf}"):
                        keep(f"{n}.{leaf}", PACKAGE_MOVE_CONFIDENCE)
                        break
        return list(found.values())

    def _deprecation(self, name: str, path: str, param: str | None = None) -> tuple[str | None, str | None]:
        """(version, message) of the first observed deprecation.

        A deprecation already present in the earliest indexed release has no observed start, so the version is
        reported as "<=X" rather than claiming it began in X.
        """
        assert self.store is not None
        for row in self.store.events(name, path):
            detail = json.loads(row["detail"])
            wanted = (param is None and row["kind"] == "deprecated") or (
                param is not None and row["kind"] == "param_deprecated" and detail.get("param") == param
            )
            if wanted:
                version = self._version(name, row["ordinal"])
                message = untrusted_text(detail.get("message"))  # it is quoted in explanations too
                return (f"<={version}" if row["prev_ordinal"] is None else version), message
        return None, None

    def _unplaced_import_findings(
        self,
        spec: PackageSpec,
        signal: ErrorSignal,
        resolved: sqlite3.Row | None,
        exact: bool,
        installed: str | None,
    ) -> list[Finding]:
        """`cannot import name X` with no module (DT-CORE-37): Cython's message, whose frames have no source line,
        or a trimmed paste. `from M import X` takes a name from a module, so X is looked up as a module's name in any
        indexed release, shortest path first: scikit-learn 1.3's `SCORERS` is `sklearn.metrics.SCORERS`. Medium
        confidence at most when more than one module has it."""
        assert self.store is not None
        name = spec.name
        paths = [
            p
            for _, p in self.store.find_by_suffix([name], signal.name or "", limit=200)
            if any(s["kind"] == "module" for s in self.store.spans(name, p.rpartition(".")[0]))
        ][:MAX_FINDINGS_PER_PACKAGE]
        out = [
            self._symbol_finding(spec, p, resolved, exact, installed, message=signal.message) for p in paths
        ]
        if len(paths) > 1:
            out = [
                f.model_copy(update={"confidence": "medium"}) if f.confidence == "high" else f for f in out
            ]
        return out

    def _symbol_finding(
        self,
        spec: PackageSpec,
        path: str,
        resolved: sqlite3.Row | None,
        exact: bool,
        installed: str | None,
        *,
        module: bool = False,
        message: str | None = None,
    ) -> Finding:
        assert self.store is not None
        name = spec.name
        rels = self.store.releases(name)
        # Public spans, and spans of the same path as a name that isn't public API (DT-DATA-22).
        spans = sorted(
            [*self.store.spans(name, path), *self.store.internal_spans(name, path)],
            key=lambda s: s["first_ordinal"],
        )
        base = {
            "package": name,
            "symbol": path,
            "installed_version": installed,
            "resolved_version": resolved["version"] if resolved else None,
        }
        ordinal = resolved["ordinal"] if resolved else rels[-1]["ordinal"]
        if not spans:
            return self._never_existed(name, path, rels, resolved, exact, ordinal, module, base)

        def has(o: int) -> sqlite3.Row | None:
            return self.store.span_at(name, path, o) or self.store.internal_span_at(name, path, o)

        dep_version, dep_message = self._deprecation(name, path)
        if dep_version is None:
            dep_version, dep_message = self._internal_deprecation(name, spans)
        indexed = [self._replacement(r) for r in self._hints(name, path)]
        now = has(ordinal)
        was_module = [s for s in spans if s["kind"] == "module" and s["last_ordinal"] < ordinal]
        imports = self.store.module_alias_ordinals(name, path) if module else []
        aliased = [o for o in imports if o < ordinal]
        if (
            now is not None
            and module
            and now["kind"] != "module"
            and ordinal not in imports
            and (was_module or aliased)
        ):
            # `No module named 'fastmcp.tools.tool'` in fastmcp 4: the path is still a name, the `tool` function
            # its package exports, but no longer a module, so the import fails (DT-CORE-35). A `sys.modules` alias
            # kept it importable through 3.4 (DT-DATA-52).
            last_ord = max([s["last_ordinal"] for s in was_module] + aliased)
            last_with, first_without = self._version(name, last_ord), self._version(name, last_ord + 1)
            then = self.store.span_at(name, path, last_ord + 1) or now
            what = (
                f"the {then['kind']} `{then['canonical']}`"
                if then["canonical"] != path
                else f"a {then['kind']}"
            )
            via = (
                f" (from {self._version(name, min(aliased))} the name is bound to another object, but the module"
                " stays importable)"
                if aliased and max(aliased) == last_ord
                else ""
            )
            return Finding(
                **base,
                issue="module_removed",
                last_version_with=last_with,
                first_version_without=first_without,
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                replacements=_merge(self._error_hints(spec, path, [ordinal, last_ord + 1], message), indexed),
                pin_to_restore=f"{name}<={last_with}",
                # A package can keep the old module importable in ways the index doesn't read (`importlib` tricks,
                # a `sys.modules` alias made in a function), so the boundary isn't certain.
                confidence="medium",
                explanation=f"{path} can be imported as a module up to {name} {last_with}{via}. From"
                f" {first_without} the name is {what}, which can't be imported as a module: import what you need"
                " from the package instead."
                + (
                    ""
                    if via
                    else " The package may have kept the old module importable for a few more releases in a way"
                    " the index doesn't read, so the last release that imports it can be later."
                ),
                internal=_is_internal(now),
            )
        if now is not None:
            internal = _is_internal(now)
            not_public = self._not_public(name, path, spans, now) if internal else ""
            window = self._window(name, resolved, exact, installed, lambda o: has(o) is None)
            if window:
                lo, hi, text = window
                live = self._error_hints(spec, path, [ordinal, ordinal + 1], message)
                return Finding(
                    **base,
                    issue="module_removed" if module or spans[-1]["kind"] == "module" else "symbol_removed",
                    last_version_with=lo,
                    first_version_without=hi,
                    deprecated_in=dep_version,
                    deprecation_message=dep_message,
                    replacements=_merge(live, indexed),
                    pin_to_restore=f"{name}<={lo}",
                    confidence="medium",
                    explanation=f"{not_public}{path} disappears {text}."
                    + (OTHER_CAUSE if hi is None else ""),
                    internal=internal,
                )
            return Finding(
                **base,
                issue="present_in_installed_version",
                confidence="low",
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                explanation=f"{not_public}{path} exists in {name} {self._version(name, ordinal)} per the index; the error likely has another cause "
                "(different interpreter/venv, a shadowing local module, or a lazily-imported optional dependency).",
                internal=internal,
            )
        before = [s for s in spans if s["last_ordinal"] < ordinal]
        if before:
            last = max(before, key=lambda s: s["last_ordinal"])
            last_ord = last["last_ordinal"]
            last_with, first_without = self._version(name, last_ord), self._version(name, last_ord + 1)
            replacements = self._followed(
                name,
                _merge(self._error_hints(spec, path, [ordinal, last_ord + 1], message), indexed),
                ordinal,
            )
            strong = [r for r in replacements if r.confidence >= EXPLAIN_MIN_CONFIDENCE]
            hint = f" Use {strong[0].target} instead." if strong else ""
            internal = _is_internal(last)
            explanation = f"{path} exists up to {name} {last_with} and is absent from {first_without} onward."
            if internal:
                explanation = (
                    self._not_public(name, path, spans, last)
                    + explanation
                    + f" Code that imports it, usually another library, needs a release that supports {name}"
                    f" {first_without}, or pin {name}<={last_with}."
                )
            # `from pkg import sub` and `pkg.sub` name a submodule as an attribute; the index knows it's a module.
            was_module = module or last["kind"] == "module"
            return Finding(
                **base,
                issue="module_removed" if was_module else "symbol_removed",
                last_version_with=last_with,
                first_version_without=first_without,
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                replacements=replacements,
                pin_to_restore=f"{name}<={last_with}",
                confidence=self._confidence(resolved, exact),
                explanation=explanation + hint,
                internal=internal,
            )
        # Unindexed releases may lie between the last release without it and the first with it (DT-CORE-18).
        first_ord = min(s["first_ordinal"] for s in spans)
        last_without, first_with = self._version(name, first_ord - 1), self._version(name, first_ord)
        window = self._window(name, resolved, exact, installed, lambda o: o == first_ord)
        first = spans[0]
        internal = _is_internal(first)
        return Finding(
            **base,
            issue="symbol_not_yet_added",
            last_version_without=last_without,
            first_version_with=first_with,
            pin_to_restore=f"{name}>={first_with}",
            confidence=self._confidence(resolved, exact),
            explanation=(self._not_public(name, path, spans, first) if internal else "")
            + (
                f"{path} appears {window[2]}."
                if window
                else f"{path} appears between {name} {last_without} and {first_with}: {last_without} doesn't have it,"
                f" {first_with} does. Your version predates it."
            )
            + f" Upgrade to {name}>={first_with}, or use the older API.",
            internal=internal,
        )

    def _not_public(self, name: str, path: str, spans: list[sqlite3.Row], span: sqlite3.Row) -> str:
        """The start of an explanation about a name that isn't public API (DT-DATA-22)."""
        text = f"{path} is not public API ({_internal_reason(path, span)}), so {name} can change it without a deprecation."
        public = [
            s["last_ordinal"]
            for s in spans
            if not _is_internal(s) and s["last_ordinal"] < span["first_ordinal"]
        ]
        if public:
            text += f" It was public API up to {name} {self._version(name, max(public))}."
        return text + " "

    def _internal_deprecation(self, name: str, spans: list[sqlite3.Row]) -> tuple[str | None, str | None]:
        """(version, message) of the first warning a module `__getattr__` serves a private name with (flask 2.3's
        `_app_ctx_stack`); "<=X" when it's already there in the earliest indexed release X."""
        for span in spans:
            if _is_internal(span) and span["deprecated"]:
                version = self._version(name, span["first_ordinal"])
                return (f"<={version}" if span["first_ordinal"] == 0 else version), untrusted_text(
                    span["deprecated"]
                )
        return None, None

    def _never_existed(
        self,
        name: str,
        path: str,
        rels: list[sqlite3.Row],
        resolved: sqlite3.Row | None,
        exact: bool,
        ordinal: int,
        module: bool,
        base: dict,
    ) -> Finding:
        assert self.store is not None
        explanation = f"{path} does not exist in any indexed release of {name} ({rels[0]['version']}..{rels[-1]['version']})."
        # A module or class __getattr__ can create names at runtime (numpy served np.float this way), and
        # `import a.b` never consults one.
        owner, _, leaf = path.rpartition(".")
        dynamic = (
            not module
            and bool(owner)
            and self.store.span_at(name, f"{owner}.__getattr__", ordinal) is not None
        )
        if dynamic:
            explanation += (
                f" {owner} defines `__getattr__` at {self._version(name, ordinal)}, so it can create names"
                " at runtime, which DepTime's static index can't see."
            )
        # A module that star-imports another distribution's code binds names the index doesn't list (DT-DATA-56).
        stars = self.store.star_sources(name, ordinal)
        parts = path.split(".")
        starred = next(
            (p for p in (".".join(parts[:k]) for k in range(len(parts) - 1, 0, -1)) if p in stars), None
        )
        if starred is not None:
            sources = _and([f"`{m}`" for m in stars[starred]])
            explanation += (
                f" At {self._version(name, ordinal)}, {starred} star-imports {sources}, another distribution's code,"
                " which DepTime doesn't read, so it can't tell whether that binds the name."
            )
        # A class's private members, and a private class's members, are indexed only for packages stored with own
        # members, by indexes from DT-DATA-46 on, and only for a class the index has a record of.
        owners = [*self.store.spans(name, owner), *self.store.internal_spans(name, owner)] if owner else []
        classes = {s["canonical"] for s in owners if s["kind"] == "class"}
        unindexed_member = (
            bool(classes)
            and (_is_private(leaf) or all(_is_internal(s) for s in owners))
            and not self.store.records_internal_members(name, classes)
        )
        if unindexed_member:
            explanation += (
                f" {owner} is a class, and DepTime doesn't index the private members of classes or the members of"
                f" private classes, so it can't tell whether {leaf} existed."
            )
        else:
            explanation += " It may be misremembered or belong to another package."
        ahead = self._ahead(name, resolved, exact)
        if ahead:
            explanation += (
                f" Your {base['installed_version']} is newer than {rels[-1]['version']}, the newest release the index"
                " has, so DepTime can't tell whether a later release adds it."
            )
        return Finding(
            **base,
            issue="never_existed",
            confidence="medium"
            if resolved and not dynamic and not unindexed_member and not ahead and starred is None
            else "low",
            explanation=explanation + _before_index(rels),
        )

    def _param_finding(
        self,
        spec: PackageSpec,
        path: str,
        param: str,
        kind: str,
        resolved: sqlite3.Row | None,
        exact: bool,
        installed: str | None,
        *,
        message: str | None = None,
    ) -> Finding | None:
        assert self.store is not None
        name = spec.name
        spans = self.store.spans(name, path)
        if not spans:
            return None
        rels = self.store.releases(name)
        ordinal = resolved["ordinal"] if resolved else rels[-1]["ordinal"]
        symbol = f"{path}({param})"
        base = {
            "package": name,
            "symbol": symbol,
            "installed_version": installed,
            "resolved_version": resolved["version"] if resolved else None,
        }
        confidence = self._confidence(resolved, exact)

        if kind == "missing_argument":
            current = self.store.span_at(name, path, ordinal)
            if current is None or not _requires(current["params"], param):
                return None  # this version's signature doesn't produce the error; not a version-change story
            # The latest transition into "required" at or before this version starts the current interval.
            start = None
            for row in self.store.events(name, path):
                detail = json.loads(row["detail"])
                if (
                    row["ordinal"] <= ordinal
                    and detail.get("param") == param
                    and (
                        row["kind"] == "param_now_required"
                        or (row["kind"] == "param_added" and detail.get("required"))
                    )
                ):
                    start = row
            if start is None:
                return None
            changed_in = self._version(name, start["ordinal"])
            prev = self._version(name, start["prev_ordinal"])
            change = (
                f"gained the required parameter `{param}` in {name} {changed_in} (absent in {prev})"
                if start["kind"] == "param_added"
                else f"made `{param}` required in {name} {changed_in} (it had a default in {prev})"
            )
            return Finding(
                **base,
                issue="param_now_required",
                first_version_without=changed_in,
                last_version_with=prev,
                pin_to_restore=f"{name}<={prev}" if prev else None,
                confidence=confidence,
                explanation=f"{path} {change}.",
            )

        dep_version, dep_message = self._deprecation(name, path, param)
        replacements = [self._replacement(r) for r in self._hints(name, symbol)]
        current = self.store.span_at(name, path, ordinal)
        if current is not None and current["params"] is None:
            return None  # no signature at this version (an unresolved alias, a class); no parameter verdict
        had = [s for s in spans if _takes(s, param)]
        accepts = current is not None and _accepts(current, param)
        if accepts and had and not _takes(current, param):
            # A **kwargs catch-all that replaced a declared parameter is how libraries reject it with their own
            # error (pydantic 2's Field(**extra) raises "`regex` is removed").
            accepts = False
        if accepts:

            def rejects(o: int) -> bool:
                span = self.store.span_at(name, path, o)
                return span is not None and span["params"] is not None and not _accepts(span, param)

            window = self._window(name, resolved, exact, installed, rejects)
            if window is None:
                return None  # kwarg is valid at this version; not a version-change explanation
            lo, hi, text = window
            live = self._error_hints(spec, path, [ordinal, ordinal + 1], message, param)
            return Finding(
                **base,
                issue="param_removed",
                last_version_with=lo,
                first_version_without=hi,
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                replacements=_merge(live, replacements),
                pin_to_restore=f"{name}<={lo}",
                confidence="medium",
                explanation=f"{path} stops accepting `{param}` {text}." + (OTHER_CAUSE if hi is None else ""),
            )
        if not had:
            if any(s["params"] is None for s in spans):
                return None  # some releases have no known signature, so "never accepted" can't be shown
            ahead = self._ahead(name, resolved, exact)
            return Finding(
                **base,
                issue="never_existed",
                replacements=replacements,
                confidence="medium" if resolved and not ahead else "low",
                explanation=f"{path} never accepted `{param}` in any indexed release ({rels[0]['version']}..{rels[-1]['version']})."
                + (
                    f" Your {installed} is newer than {rels[-1]['version']}, the newest release the index has, so"
                    " DepTime can't tell whether a later release adds it."
                    if ahead
                    else ""
                )
                + _before_index(rels),
            )
        earlier = [s for s in had if s["last_ordinal"] < ordinal]
        if earlier:
            last_ord = max(s["last_ordinal"] for s in earlier)
            last_with, first_without = self._version(name, last_ord), self._version(name, last_ord + 1)
            live = self._error_hints(spec, path, [ordinal, last_ord + 1], message, param)
            replacements = _merge(live, replacements)
            since = (
                f"in or before {dep_version[2:]}"
                if dep_version and dep_version.startswith("<=")
                else f"in {dep_version}"
            )
            deprecated = f' Deprecated {since} ("{(dep_message or "").strip()}").' if dep_version else ""
            alts = " or ".join(
                f"`{r.target}`" for r in replacements if r.confidence >= EXPLAIN_MIN_CONFIDENCE
            )
            return Finding(
                **base,
                issue="param_removed",
                last_version_with=last_with,
                first_version_without=first_without,
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                replacements=replacements,
                pin_to_restore=f"{name}<={last_with}",
                confidence=confidence,
                explanation=f"{path} accepted `{param}` up to {name} {last_with}; it is gone from {first_without} onward.{deprecated}"
                + (
                    f" Use {alts} instead, or pin {name}<={last_with}."
                    if alts
                    else f" Pin {name}<={last_with} or update the call."
                ),
            )
        first_ord = min(s["first_ordinal"] for s in had)
        if first_ord <= ordinal:
            return None  # the installed release names it but can't take it by keyword (positional-only)
        # The last release before it shown not to take `param`: one without the function, or whose signature lacks
        # it, not an unknown one. The installed release is one (checked above), so there always is one (DT-CORE-18).
        last_ord = next(
            o
            for o in range(first_ord - 1, ordinal - 1, -1)
            if (s := self.store.span_at(name, path, o)) is None or s["params"] is not None
        )
        last_without, first_with = self._version(name, last_ord), self._version(name, first_ord)
        window = self._window(name, resolved, exact, installed, lambda o: o == first_ord)
        return Finding(
            **base,
            issue="param_not_yet_added",
            last_version_without=last_without,
            first_version_with=first_with,
            pin_to_restore=f"{name}>={first_with}",
            confidence=confidence,
            explanation=(
                f"`{param}` was added to {path} {window[2]}."
                if window
                else f"`{param}` was added to {path} between {name} {last_without} and {first_with}: {last_without}"
                f" doesn't take it, {first_with} does. Your version predates it."
            )
            + f" Upgrade to {name}>={first_with}, or call it without `{param}`.",
        )
