"""S3 (DT-SPIKE-03): does `explain_error` map a traceback to the right kind of error and the right package?

`cases.jsonl` holds real output from known breaks run on Python 3.10-3.14 and printed the ways users see them
(bench/s3/run.py, in GitHub Actions). It was committed with this script before either was run against an index.

    uv run python bench/s3/score.py bench/s3/cases.jsonl [--index PATH] [--json OUT]

A case passes when `signal.kind` is the expected kind and the first finding's package is the expected package.
Every case is scored twice: with the venv's distributions as `installed_versions` (the headline), and without
them. Pass (docs/07-risks-and-spikes.md): >= 90% of all cases.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from deptime.config import Settings, load_coverage
from deptime.index.location import active_index
from deptime.index.store import IndexStore
from deptime.query import DepTime

BAR = 90.0


def grade(dt: DepTime, case: dict, installed: bool) -> dict:
    result = dt.explain_error(case["traceback"], installed_versions=case["installed"] if installed else None)
    kind = result.signal.kind if result.signal else None
    package = result.findings[0].package if result.findings else None
    want = case["expected"]
    return {
        "kind": kind,
        "package": package,
        "kind_ok": kind == want["kind"],
        "package_ok": package == want["package"],
        "passed": kind == want["kind"] and package == want["package"],
    }


def pct(hits: int, total: int) -> str:
    return f"{100 * hits / total:.1f}%" if total else "-"


def table(rows: list[dict], field: str, mode: str) -> list[str]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r[field]].append(r[mode])
    out = [f"| {field} | cases | kind | package | kind + package |", "|---|---:|---:|---:|---:|"]
    for name in sorted(groups):
        g = groups[name]
        n = len(g)
        out.append(
            f"| {name} | {n} | {pct(sum(x['kind_ok'] for x in g), n)} | {pct(sum(x['package_ok'] for x in g), n)} "
            f"| {pct(sum(x['passed'] for x in g), n)} |"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", type=Path)
    ap.add_argument("--index", type=Path, help="index to score (default: the configured one)")
    ap.add_argument("--json", type=Path, help="also write per-case results here")
    args = ap.parse_args()
    settings = Settings.from_env()
    index = args.index or active_index(settings.index_path)
    store = IndexStore.open(index)
    dt = DepTime(store, load_coverage(settings.coverage_file), index)
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for case in cases:
        rows.append(
            {
                "id": case["id"],
                "scenario": case["scenario"],
                "python": case["python"],
                "presentation": case["presentation"],
                "kind_expected": case["expected"]["kind"],
                "package_expected": case["expected"]["package"],
                "with_installed": grade(dt, case, installed=True),
                "without_installed": grade(dt, case, installed=False),
            }
        )
    n = len(rows)
    head = sum(r["with_installed"]["passed"] for r in rows)
    bare = sum(r["without_installed"]["passed"] for r in rows)
    lines = [
        f"# S3 on {n} cases (index built {store.built_at()})",
        "",
        f"- **Kind + package, with installed versions: {head}/{n} = {pct(head, n)}** (bar: {BAR:.0f}%)",
        f"- Kind + package, without installed versions: {bare}/{n} = {pct(bare, n)}",
        "",
    ]
    for field in ("presentation", "python", "kind_expected", "scenario"):
        lines += [f"## By {field} (with installed versions)", "", *table(rows, field, "with_installed"), ""]
    lines += ["## By presentation (without installed versions)", ""]
    lines += [*table(rows, "presentation", "without_installed"), ""]
    lines += ["## Failures (with installed versions)", ""]
    for r in rows:
        g = r["with_installed"]
        if not g["passed"]:
            lines.append(
                f"- `{r['id']}`: kind {g['kind']} (want {r['kind_expected']}), "
                f"package {g['package']} (want {r['package_expected']})"
            )
    print("\n".join(lines))
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
