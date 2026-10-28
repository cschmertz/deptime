"""Static API snapshot of one release, extracted with griffe (no code is imported or executed).

A snapshot maps every *public import path* (what users type: `httpx.Client`, not `httpx._client.Client`)
to its kind, canonical definition path, parameters, and deprecation message, if any.
"""

from __future__ import annotations

import gzip
import json
import re
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


@dataclass
class SymbolRecord:
    path: str
    kind: str  # module | class | function | attribute | type alias | alias (unresolved/external)
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

    def _public(
        self, mod: griffe.Module, name: str, member: griffe.Object | griffe.Alias, reexporter: str | None
    ) -> bool:
        """griffe's rule, plus what a package's `__init__` imports from the package itself when `__all__` is
        missing (or computed at runtime, as in numpy).

        griffe counts imported names as private, but `from .app import Flask as Flask` is how flask, fastapi,
        click and sqlalchemy expose their API. When an `__init__.pyi` redefines an imported name (numpy 2.0's
        `float64`, `add`), griffe merges the stub and the name is no longer an alias, so the `.py` import counts.
        """
        try:
            if member.is_public:
                return True
        except (griffe.AliasResolutionError, griffe.CyclicAliasError):
            return False
        source = member.target_path if member.is_alias else mod.imports.get(name)  # type: ignore[union-attr]
        return (
            reexporter is not None
            and source is not None
            and not mod.exports
            and not name.startswith("_")
            and self._internal(source)
        )

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
