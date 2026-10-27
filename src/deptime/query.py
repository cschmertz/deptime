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

from deptime.config import Coverage, PackageSpec
from deptime.index.store import IndexStore
from deptime.ingest.diff import is_required
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

MAX_DIFF_EVENTS = 500
MAX_FINDINGS_PER_PACKAGE = 5
# Replacement hints below this stay in the structured `replacements` list (with their confidence) but are never
# stated as advice in prose: agents read `explanation` as fact. Matches spike S2's precision threshold.
EXPLAIN_MIN_CONFIDENCE = 0.75
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


def _params(raw: str | None) -> list[ParamInfo] | None:
    return None if raw is None else [ParamInfo(**p) for p in json.loads(raw)]


def _param_names(raw: str | None) -> set[str]:
    return {p["name"] for p in json.loads(raw)} if raw else set()


def _requires(raw: str | None, param: str) -> bool:
    return any(p["name"] == param and is_required(p) for p in json.loads(raw)) if raw else False


def _accepts_kwarg(raw: str | None, kwarg: str) -> bool:
    for p in json.loads(raw) if raw else []:
        if p["kind"] == "variadic keyword":
            return True
        if p["name"] == kwarg and p["kind"] not in {"positional-only", "variadic positional"}:
            return True
    return False


class DepTime:
    def __init__(self, store: IndexStore | None, coverage: Coverage, index_path: Path | None = None):
        self._store = store
        self.coverage = coverage
        # Opened lazily if it appears after start-up, e.g. another process finished the first download.
        self.index_path = index_path
        self._open_lock = threading.Lock()

    @property
    def store(self) -> IndexStore | None:
        if self._store is None and self.index_path is not None and self.index_path.exists():
            with self._open_lock:
                if self._store is None:
                    store = None
                    try:
                        store = IndexStore.open(self.index_path)
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

    @staticmethod
    def _replacement(row: sqlite3.Row) -> Replacement:
        return Replacement(
            target=row["target"], confidence=row["confidence"], source=row["source"], evidence=row["evidence"]
        )

    def _not_indexed_note(self, name: str) -> str:
        if self.store is None:
            return NO_INDEX_NOTE
        configured = self.coverage.get(name) is not None
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
        replacements = [self._replacement(r) for r in self.store.replacements(name, full)]
        replacements += [
            self._replacement(r).model_copy(update={"target": f"{r['path']} -> {r['target']}"})
            for r in self.store.param_replacements(name, full)
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
            if span is None:
                spans = self.store.spans(name, full)
                if spans:
                    seen = f"seen in {self._version(name, spans[0]['first_ordinal'])}..{self._version(name, max(s['last_ordinal'] for s in spans))}"
                else:
                    seen = "never seen in any indexed release"
                repl = [
                    r
                    for r in self.store.replacements(name, full)
                    if r["confidence"] >= EXPLAIN_MIN_CONFIDENCE
                ]
                hint = seen + (f"; try {repl[0]['target']}" if repl else "")
                results.append(SymbolCheck(path=full, status="missing", hint=hint))
                continue
            params_raw = span["params"]
            if params_raw is None and span["kind"] == "class":
                init = self.store.span_at(name, full + ".__init__", row["ordinal"])
                params_raw = init["params"] if init else None
            rejected = [k for k in q.kwargs if params_raw is not None and not _accepts_kwarg(params_raw, k)]
            results.append(
                SymbolCheck(
                    path=full, status="present", rejected_kwargs=rejected, deprecated=span["deprecated"]
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
            else f"{version} is not indexed; answered for the nearest earlier indexed release {row['version']}.",
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
            return ExplainResult(signal=signal.to_model(), findings=[], note=NO_INDEX_NOTE)
        installed = parse_installed(installed_versions, pip_freeze)
        candidates = self._candidates(signal, installed)
        findings: list[Finding] = []
        not_covered: list[str] = []
        for spec in candidates:
            if not self._indexed(spec.name):
                not_covered.append(spec.name)
                continue
            findings.extend(
                self._findings_for(spec, signal, installed.get(spec.name))[:MAX_FINDINGS_PER_PACKAGE]
            )
        if signal.kind in MODULE_SIGNALS and not candidates and signal.module:
            not_covered.append(signal.module.split(".")[0])
        callee: list[Finding] = []
        if signal.kind in OBJECT_SIGNALS and all(f.issue in INCONCLUSIVE_ISSUES for f in findings):
            callee = self._callee_findings(signal, installed, {s.name for s in candidates})
            if callee:
                # A concrete change in the called package explains the error; "never existed" in the caller is noise.
                findings = [f for f in findings if f.issue != "never_existed"] + callee
        rank = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda f: (rank[f.confidence], f.issue == "present_in_installed_version"))
        notes = []
        if not findings:
            notes.append(
                "No version-history explanation found."
                + (
                    ""
                    if installed
                    else " Pass installed_versions or pip_freeze so DepTime can check the versions you actually run."
                )
            )
        elif not installed:
            notes.append(
                "installed_versions/pip_freeze not provided; status is inferred from the latest indexed release."
            )
        if callee and signal.library_modules:
            caller = signal.library_modules[-1].split(".")[0]
            changed = ", ".join(sorted({f.package for f in callee}))
            notes.append(
                f"The failing call is made inside {caller}'s code, and the change is in {changed}, which it calls. "
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

    def _callee_findings(
        self, signal: ErrorSignal, installed: dict[str, str], tried: set[str]
    ) -> list[Finding]:
        """Concrete changes in packages the traceback's code may be *calling*.

        When library A calls library B with an argument B no longer accepts, Python raises at A's call site
        before any of B's code runs, so B never appears among the frames (openai -> httpx.Client(proxies=...)).
        Search other indexed packages that have the symbol, installed ones first. Only concrete version changes
        count here: a name that is simply absent from an unrelated package is not evidence.
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
        for group in ([p for p in packages if p in installed], [p for p in packages if p not in installed]):
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
                    spec, f"{signal.module}.{signal.name}", resolved, exact, installed_version
                )
            ]
        if signal.kind == "module_missing":
            return [
                self._symbol_finding(
                    spec, signal.module or "", resolved, exact, installed_version, module=True
                )
            ]
        if signal.kind in {"object_attribute", "class_attribute"} and signal.owner and signal.name:
            owner_leaf = signal.owner.rsplit(".", 1)[-1]
            paths = self._rank(self.store.find_by_suffix([name], f"{owner_leaf}.{signal.name}"), signal)
            if paths:
                return [self._symbol_finding(spec, p, resolved, exact, installed_version) for p in paths]
            owners = self._rank(self.store.find_by_suffix([name], owner_leaf), signal)
            return (
                [self._symbol_finding(spec, f"{owners[0]}.{signal.name}", resolved, exact, installed_version)]
                if owners
                else []
            )
        if signal.kind in {"unexpected_kwarg", "missing_argument"} and signal.owner:
            paths = self._rank(self.store.find_by_suffix([name], signal.owner), signal)
            param = signal.kwarg if signal.kind == "unexpected_kwarg" else (signal.missing_args or [None])[0]
            out = [
                self._param_finding(spec, p, param, signal.kind, resolved, exact, installed_version)
                for p in paths
                if param
            ]
            return [f for f in out if f is not None]
        return []

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
        """When `installed` is not indexed, it sits between two indexed releases; report a change inside that window."""
        if resolved is None or exact:
            return None
        nxt = resolved["ordinal"] + 1
        upper = self._version(name, nxt)
        if upper is None or not changed_at(nxt):
            return None
        lo = resolved["version"]
        return (
            lo,
            upper,
            f"between {name} {lo} and {upper} (your {installed} is in that window and not indexed)",
        )

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
                return (f"<={version}" if row["prev_ordinal"] is None else version), detail.get("message")
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
    ) -> Finding:
        assert self.store is not None
        name = spec.name
        rels = self.store.releases(name)
        spans = self.store.spans(name, path)
        base = {
            "package": name,
            "symbol": path,
            "installed_version": installed,
            "resolved_version": resolved["version"] if resolved else None,
        }
        if not spans:
            return Finding(
                **base,
                issue="never_existed",
                confidence="medium" if resolved else "low",
                explanation=f"{path} does not exist in any indexed release of {name} ({rels[0]['version']}..{rels[-1]['version']}). "
                "It may be misremembered or belong to another package.",
            )
        ordinal = resolved["ordinal"] if resolved else rels[-1]["ordinal"]
        dep_version, dep_message = self._deprecation(name, path)
        replacements = [self._replacement(r) for r in self.store.replacements(name, path)]
        if self.store.span_at(name, path, ordinal) is not None:
            window = self._window(
                name, resolved, exact, installed, lambda o: self.store.span_at(name, path, o) is None
            )
            if window:
                lo, hi, text = window
                return Finding(
                    **base,
                    issue="module_removed" if module else "symbol_removed",
                    last_version_with=lo,
                    first_version_without=hi,
                    deprecated_in=dep_version,
                    deprecation_message=dep_message,
                    replacements=replacements,
                    pin_to_restore=f"{name}<={lo}",
                    confidence="medium",
                    explanation=f"{path} disappears {text}.",
                )
            return Finding(
                **base,
                issue="present_in_installed_version",
                confidence="low",
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                explanation=f"{path} exists in {name} {self._version(name, ordinal)} per the index; the error likely has another cause "
                "(different interpreter/venv, a shadowing local module, or a lazily-imported optional dependency).",
            )
        before = [s for s in spans if s["last_ordinal"] < ordinal]
        if before:
            last_ord = max(s["last_ordinal"] for s in before)
            last_with, first_without = self._version(name, last_ord), self._version(name, last_ord + 1)
            strong = [r for r in replacements if r.confidence >= EXPLAIN_MIN_CONFIDENCE]
            hint = f" Use {strong[0].target} instead." if strong else ""
            return Finding(
                **base,
                issue="module_removed" if module else "symbol_removed",
                last_version_with=last_with,
                first_version_without=first_without,
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                replacements=replacements,
                pin_to_restore=f"{name}<={last_with}",
                confidence=self._confidence(resolved, exact),
                explanation=f"{path} exists up to {name} {last_with} and is absent from {first_without} onward."
                + hint,
            )
        first_with = self._version(name, min(s["first_ordinal"] for s in spans))
        return Finding(
            **base,
            issue="symbol_not_yet_added",
            pin_to_restore=f"{name}>={first_with}",
            confidence=self._confidence(resolved, exact),
            explanation=f"{path} first appears in {name} {first_with}; your version predates it. Upgrade, or use the older API.",
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
        replacements = [self._replacement(r) for r in self.store.replacements(name, symbol)]
        current = self.store.span_at(name, path, ordinal)
        if current is not None and _accepts_kwarg(current["params"], param):

            def rejects(o: int) -> bool:
                span = self.store.span_at(name, path, o)
                return span is not None and not _accepts_kwarg(span["params"], param)

            window = self._window(name, resolved, exact, installed, rejects)
            if window is None:
                return None  # kwarg is valid at this version; not a version-change explanation
            lo, hi, text = window
            return Finding(
                **base,
                issue="param_removed",
                last_version_with=lo,
                first_version_without=hi,
                deprecated_in=dep_version,
                deprecation_message=dep_message,
                replacements=replacements,
                pin_to_restore=f"{name}<={lo}",
                confidence="medium",
                explanation=f"{path} stops accepting `{param}` {text}.",
            )
        had = [s for s in spans if param in _param_names(s["params"])]
        if not had:
            return Finding(
                **base,
                issue="never_existed",
                replacements=replacements,
                confidence="medium" if resolved else "low",
                explanation=f"{path} never accepted `{param}` in any indexed release ({rels[0]['version']}..{rels[-1]['version']}).",
            )
        earlier = [s for s in had if s["last_ordinal"] < ordinal]
        if earlier:
            last_ord = max(s["last_ordinal"] for s in earlier)
            last_with, first_without = self._version(name, last_ord), self._version(name, last_ord + 1)
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
        first_with = self._version(name, min(s["first_ordinal"] for s in had))
        return Finding(
            **base,
            issue="param_not_yet_added",
            pin_to_restore=f"{name}>={first_with}",
            confidence=confidence,
            explanation=f"`{param}` was added to {path} in {name} {first_with}; your version predates it.",
        )
