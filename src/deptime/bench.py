"""Traceback benchmark runner (DT-QA-02): replay `bench/cases/*.jsonl` through `explain_error` and score it.

  python -m deptime.bench bench/cases                                       report
  python -m deptime.bench bench/cases --baseline bench/baseline.json        also fail on a regression
  python -m deptime.bench bench/cases --write-baseline bench/baseline.json  record the current run

Scoring is defined in bench/README.md. It runs against the index in DEPTIME_HOME (`deptime index pull`).
A regression is measured on the cases both runs share, so adding new (harder) cases never fails the check.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from deptime.config import Coverage, Settings, load_coverage
from deptime.index.store import IndexStore
from deptime.models import ExplainResult
from deptime.query import DepTime

METRICS = ("root_cause", "root_cause_and_boundary", "boundary", "replacement")
MAX_DROP_POINTS = 2.0


@dataclass(frozen=True)
class CaseResult:
    id: str
    tier: str
    status: str
    origin: str  # "reported" (a real traceback from an issue or post) or "constructed"
    root_cause: bool
    boundary: bool | None  # None when the case has no expected boundary
    replacement: bool | None  # None when the case has no expected replacement
    got: str

    @property
    def passed(self) -> bool:
        """Root cause and, where one is expected, boundary: the docs/06 accuracy criterion."""
        return self.root_cause and self.boundary is not False


def load_cases(paths: list[Path]) -> list[dict]:
    files = sorted(f for p in paths for f in (p.glob("*.jsonl") if p.is_dir() else [p]))
    cases, seen = [], set()
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            case = json.loads(line)
            if case["id"] in seen:
                raise ValueError(f"{f}:{n}: duplicate case id {case['id']!r}")
            seen.add(case["id"])
            cases.append(case)
    return cases


def score_case(case: dict, result: ExplainResult, coverage: Coverage) -> CaseResult:
    exp = case["expected"]
    top = result.findings[0] if result.findings else None
    root_cause = (
        top is not None
        and canonicalize_name(top.package) == canonicalize_name(exp["package"])
        and top.symbol.endswith(exp["symbol_suffix"])
        and top.issue == exp["issue"]
    )
    # The boundary and replacement of a wrong finding mean nothing, so both require the root cause.
    boundary = replacement = None
    if "last_version_with" in exp:
        boundary = root_cause and (top.last_version_with or "").startswith(exp["last_version_with"])
    elif "first_version_with" in exp:
        # *_not_yet_added: the first release that has it is the one DepTime advises upgrading to (`pkg>=X`).
        first_with = (top.pin_to_restore or "").partition(">=")[2] if top else ""
        boundary = root_cause and first_with.startswith(exp["first_version_with"])
    if "replacement_contains" in exp:
        replacement = root_cause and any(
            exp["replacement_contains"] in r.target for r in top.replacements[:3]
        )
    spec = coverage.get(exp["package"])
    if top is None:
        got = f"no finding ({result.note or 'no note'})"
    else:
        targets = ", ".join(r.target for r in top.replacements[:3]) or "-"
        got = (
            f"{top.package} {top.symbol} {top.issue} last_with={top.last_version_with} "
            f"pin={top.pin_to_restore} confidence={top.confidence} replacements=[{targets}]"
        )
    return CaseResult(
        id=case["id"],
        tier=spec.tier if spec else "-",
        status=case.get("status", "expected_pass"),
        origin="constructed" if case.get("source") == "constructed" else "reported",
        root_cause=root_cause,
        boundary=boundary,
        replacement=replacement,
        got=got,
    )


def run(dt: DepTime, cases: list[dict]) -> list[CaseResult]:
    return [
        score_case(c, dt.explain_error(c["traceback"], c.get("installed") or None), dt.coverage)
        for c in cases
    ]


def _pct(hits: int, total: int) -> float | None:
    return round(100 * hits / total, 1) if total else None


def metrics(results: list[CaseResult]) -> dict[str, float | None]:
    with_boundary = [r for r in results if r.boundary is not None]
    with_replacement = [r for r in results if r.replacement is not None]
    return {
        "root_cause": _pct(sum(r.root_cause for r in results), len(results)),
        "root_cause_and_boundary": _pct(sum(r.passed for r in results), len(results)),
        "boundary": _pct(sum(bool(r.boundary) for r in with_boundary), len(with_boundary)),
        "replacement": _pct(sum(bool(r.replacement) for r in with_replacement), len(with_replacement)),
    }


def _grouped(results: list[CaseResult], field: str) -> dict:
    groups = {}
    for value in sorted({getattr(r, field) for r in results}):
        rs = [r for r in results if getattr(r, field) == value]
        groups[value] = {"cases": len(rs), **metrics(rs)}
    return groups


def summarize(results: list[CaseResult]) -> dict:
    return {
        "cases": len(results),
        "known_gap_share": _pct(sum(r.status == "known_gap" for r in results), len(results)),
        "overall": metrics(results),
        "by_tier": _grouped(results, "tier"),
        "by_origin": _grouped(results, "origin"),
    }


def regressions(results: list[CaseResult], baseline: dict, max_drop: float = MAX_DROP_POINTS) -> list[str]:
    """Metrics that fell by more than `max_drop` points on the cases shared with the baseline."""
    base = {
        cid: CaseResult(id=cid, tier="", status="", origin="", got="", **v)
        for cid, v in baseline["cases"].items()
    }
    now = [r for r in results if r.id in base]
    before, after = metrics([base[r.id] for r in now]), metrics(now)
    return [
        f"{m}: {before[m]} -> {after[m]} on {len(now)} shared cases"
        for m in METRICS
        if before[m] is not None and after[m] is not None and after[m] < before[m] - max_drop
    ]


def baseline_doc(results: list[CaseResult], index_built_at: str | None) -> dict:
    return {
        "index_built_at": index_built_at,
        "summary": summarize(results),
        "cases": {
            r.id: {k: v for k, v in asdict(r).items() if k in ("root_cause", "boundary", "replacement")}
            for r in results
        },
    }


def _mark(value: bool | None) -> str:
    return "-" if value is None else ("y" if value else "n")


def report(results: list[CaseResult], index_built_at: str | None) -> str:
    lines = [f"index built {index_built_at}", "", "result  rc bd rp  tier  case"]
    for r in results:
        surprise = ""
        if r.status == "known_gap" and r.passed:
            surprise = "  << known_gap now passes: update its status"
        elif r.status != "known_gap" and not r.passed:
            surprise = "  << expected_pass fails"
        lines.append(
            f"{'PASS' if r.passed else 'FAIL':6}  {_mark(r.root_cause)}  {_mark(r.boundary)}  {_mark(r.replacement)}"
            f"   {r.tier:4}  {r.id}{surprise}"
        )
        lines.append(f"{'':20}got: {r.got}")
    s = summarize(results)
    lines += ["", f"{s['cases']} cases, known_gap share {s['known_gap_share']}%", ""]
    lines.append(f"{'':12}{'cases':>6}  " + "  ".join(f"{m:>23}" for m in METRICS))
    rows = [
        ("overall", {"cases": s["cases"], **s["overall"]}),
        *s["by_tier"].items(),
        *s["by_origin"].items(),
    ]
    for name, row in rows:
        cells = "  ".join(f"{'-' if row[m] is None else f'{row[m]}%':>23}" for m in METRICS)
        lines.append(f"{name:12}{row['cases']:>6}  {cells}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deptime.bench", description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="+", type=Path, help="case files or directories of *.jsonl")
    parser.add_argument(
        "--baseline", type=Path, help="fail if a metric drops by more than 2 points against this"
    )
    parser.add_argument("--write-baseline", type=Path, help="write this run as the new baseline")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON instead of the report")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not settings.index_path.exists():
        print(f"no index at {settings.index_path}; run `deptime index pull` first", file=sys.stderr)
        return 2
    store = IndexStore.open(settings.index_path)
    dt = DepTime(store, load_coverage(settings.coverage_file), settings.index_path)
    results = run(dt, load_cases(args.cases))
    built_at = store.built_at()

    print(json.dumps(summarize(results), indent=2) if args.json else report(results, built_at))
    if args.write_baseline:
        doc = baseline_doc(results, built_at)
        args.write_baseline.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    if baseline and (failed := regressions(results, baseline)):
        print("\nregression of more than 2 points:\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
