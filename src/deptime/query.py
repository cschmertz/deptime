"""Query layer shared by the MCP tools, the HTTP API, and the CLI.

Deterministic and LLM-free (ADR 0006): every answer is derived from the index and carries its evidence.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from packaging.utils import canonicalize_name

from deptime import stdlib
from deptime.config import Coverage, PackageSpec
from deptime.index.location import active_index
from deptime.index.store import IndexStore
from deptime.ingest.diff import is_required
from deptime.ingest.replacements import clean_evidence, expand_alias, mine, moved_to_package, same_class
from deptime.models import (
    CheckSymbolsResult,
    Confidence,
    CoverageEntry,
    CoverageResult,
    DiffResult,
    ExplainResult,
    Finding,
    HistoryEvent,
    ParamInfo,
    Replacement,
    SymbolCheck,
    SymbolHistory,
    SymbolQuery,
)
from deptime.tracebacks import ErrorSignal, parse_traceback
from deptime.untrusted import instruction_like, untrusted_text

MAX_DIFF_EVENTS = 500
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
}
NO_INDEX_NOTE = (
    "DepTime has no index yet. The first run downloads it in the background; retry in a minute, "
    "or run `deptime index pull`."
)
# After a change that only the error shows, past the newest indexed release (DT-DATA-04).
OTHER_CAUSE = (
    " If no newer release changed it, the error has another cause (a different interpreter or venv, a shadowing"
    " local module, or a lazily-imported optional dependency)."
)
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


def _and(items: list[str], word: str = "and") -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} {word} {items[-1]}"


def _params(raw: str | None) -> list[ParamInfo] | None:
    return None if raw is None else [ParamInfo(**p) for p in json.loads(raw)]


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


class DepTime:
    def __init__(self, store: IndexStore | None, coverage: Coverage, index_path: Path | None = None):
        self._store = store
        self.coverage = coverage
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

    def _event(self, name: str, row: sqlite3.Row) -> HistoryEvent:
        return HistoryEvent(
            path=row["path"],
            version=self._version(name, row["ordinal"]) or "?",
            prev_version=self._version(name, row["prev_ordinal"]),
            kind=row["kind"],
            detail=json.loads(row["detail"]),
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

    @staticmethod
    def _replacement(row: sqlite3.Row) -> Replacement:
        return Replacement(
            target=row["target"], confidence=row["confidence"], source=row["source"], evidence=row["evidence"]
        )

    def _no_index_note(self) -> str:
        return f"DepTime has no index yet: {self.index_problem}" if self.index_problem else NO_INDEX_NOTE

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
            else f"{name} is not in DepTime's coverage; call the `coverage` tool for the list."
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
        replacements = [self._replacement(r) for r in self._hints(name, full)]
        replacements += [
            self._replacement(r).model_copy(update={"target": f"{r['path']} -> {r['target']}"})
            for r in self._param_hints(name, full)
        ]
        return SymbolHistory(
            package=name,
            path=full,
            indexed=True,
            ever_seen=True,
            first_seen=self._version(name, spans[0]["first_ordinal"]),
            last_seen=self._version(name, last_ordinal),
            latest_indexed_version=latest["version"],
            present_in_latest=last_ordinal == latest["ordinal"],
            signature_at_last_seen=_params(spans[-1]["params"]),
            events=[self._event(name, r) for r in self.store.events(name, full)],
            replacements=replacements,
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

    def check_symbols(self, package: str, version: str, symbols: list[SymbolQuery]) -> CheckSymbolsResult:
        name = canonicalize_name(package)
        spec = self._spec(name)
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
            if span is None:
                spans = sorted(
                    [*self.store.spans(name, full), *self.store.internal_spans(name, full)],
                    key=lambda s: s["first_ordinal"],
                )
                if spans:
                    seen = f"seen in {self._version(name, spans[0]['first_ordinal'])}..{self._version(name, max(s['last_ordinal'] for s in spans))}"
                else:
                    seen = "never seen in any indexed release"
                repl = [r for r in self._hints(name, full) if r["confidence"] >= EXPLAIN_MIN_CONFIDENCE]
                hint = seen + (f"; try {repl[0]['target']}" if repl else "")
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
                SymbolCheck(path=full, status="present", rejected_kwargs=rejected, deprecated=deprecated)
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
        rows, total = self.store.events_between(
            name,
            lo["ordinal"],
            hi["ordinal"],
            limit,
            severity=SEVERITY,
            include_added=include_added,
            paths=[self._qualify(spec, u) for u in used_symbols or []],
        )
        return DiffResult(
            package=name,
            from_version=lo["version"],
            to_version=hi["version"],
            events=[self._event(name, r) for r in rows],
            truncated=total > len(rows),
            note=None
            if lo["version"] == from_version or DATE_RE.match(from_version)
            else f"from_version resolved to nearest indexed release {lo['version']}.",
        )

    # ------------------------------------------------------------------ explain_error
    def explain_error(
        self, traceback: str, installed_versions: dict[str, str] | None = None, pip_freeze: str | None = None
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
            if entry is not None and signal.kind == "module_missing":
                found, stdlib_notes = self._stdlib_findings(signal, entry, installed.get("python"), raiser)
                findings += found
            else:
                not_covered.append(signal.module.split(".")[0])
                if entry is not None:
                    stdlib_notes.append(
                        f"`{entry.module}` is in the standard library: DepTime knows which standard modules each "
                        "Python version has, not the names inside them."
                    )
        callee: list[Finding] = []
        declared: set[str] = set()
        if signal.kind in OBJECT_SIGNALS and all(f.issue in INCONCLUSIVE_ISSUES for f in findings):
            declared = self._declared_by(raiser, installed, dropped)
            callee = self._callee_findings(signal, installed, {s.name for s in named}, declared)
            if callee:
                # A concrete change in the called package explains the error; "never existed" in the caller is noise.
                findings = [f for f in findings if f.issue != "never_existed"] + callee
        rank = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda f: (rank[f.confidence], f.issue == "present_in_installed_version"))
        notes = list(owner_notes)
        notes += [alias_notes[p] for p in sorted({f.package for f in findings}) if p in alias_notes]
        if signal.wrapped_in:
            notes.append(
                f"The last exception, {signal.wrapped_in}, was raised from or while handling this "
                f"{signal.exception_type}, which is the one explained here."
            )
        notes += stdlib_notes
        if not findings and not stdlib_notes:
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
        note = " ".join(notes) or None
        return ExplainResult(
            signal=signal.to_model(), findings=findings, not_covered=sorted(set(not_covered)), note=note
        )

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
                    pin_to_restore=f"{PYTHON}<={last}",
                    confidence=confidence,
                    explanation=f"`{mod}` was removed from the standard library in Python {gone}; {last} is the last "
                    f"version with it.{part}{you}{unsure}{theirs}",
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
        return []

    def _findings_for(
        self, spec: PackageSpec, signal: ErrorSignal, installed_version: str | None
    ) -> list[Finding]:
        assert self.store is not None
        name = spec.name
        resolved, exact = (
            self.store.resolve_version(name, installed_version) if installed_version else (None, False)
        )
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
                else []
            )
        if signal.kind in {"unexpected_kwarg", "missing_argument"} and signal.owner:
            param = signal.kwarg if signal.kind == "unexpected_kwarg" else (signal.missing_args or [None])[0]
            out = []
            # The owner the message names, then a class whose __init__ passed **kwargs on to it (DT-CORE-27).
            for owner in [signal.owner, *signal.forwarded_owners]:
                paths = self._rank(self.store.find_by_suffix([name], owner), signal)
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
            # A concrete change first, so the per-package limit never drops it behind inconclusive ones.
            return sorted((f for f in out if f is not None), key=lambda f: f.issue in INCONCLUSIVE_ISSUES)
        return []

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
    def _rank(pairs: list[tuple[str, str]], signal: ErrorSignal) -> list[str]:
        refs = signal.referenced_names

        def score(path: str) -> int:
            return 0 if any(path == r or path.startswith(r + ".") for r in refs) else 1

        return sorted((p for _, p in pairs), key=lambda p: (score(p), len(p)))[:MAX_FINDINGS_PER_PACKAGE]

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
        if (now := has(ordinal)) is not None:
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
            replacements = _merge(self._error_hints(spec, path, [ordinal, last_ord + 1], message), indexed)
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
            confidence="medium" if resolved and not dynamic and not unindexed_member and not ahead else "low",
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
