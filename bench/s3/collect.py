"""Collect the S3 runs (DT-SPIKE-03) into cases. Nothing here runs DepTime.

    uv run python bench/s3/collect.py <artifacts dir> bench/s3/scenarios.json bench/s3/cases.jsonl

<artifacts dir> is what `gh run download` writes for an s3 workflow run: one <python>.jsonl per Python version.
A run becomes a case when its output contains the scenario's `expect` text, which shows the intended break
happened; the others are listed and left out. The expected kind and package come from scenarios.json.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    runs_dir, scenarios_file, out = (Path(a) for a in sys.argv[1:])
    scenarios = {s["id"]: s for s in json.loads(scenarios_file.read_text(encoding="utf-8"))["scenarios"]}
    runs = [
        json.loads(line)
        for path in sorted(runs_dir.rglob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases, dropped = [], Counter()
    for r in runs:
        scenario = scenarios[r["scenario"]]
        if r["result"] != "ran":
            dropped[f"{r['scenario']} py{r['python']} {r.get('presentation')}: {r['result']}"] += 1
            continue
        if scenario["expect"] not in r["output"]:
            dropped[f"{r['scenario']} py{r['python']} {r['presentation']}: expected text missing"] += 1
            continue
        cases.append(
            {
                "id": f"{r['scenario']}-py{r['python']}-{r['presentation']}",
                "scenario": r["scenario"],
                "python": r["python"],
                "python_full": r["python_full"],
                "presentation": r["presentation"],
                "traceback": r["output"],
                "installed": r["installed"],
                "expected": {"kind": scenario["kind"], "package": scenario["package"]},
            }
        )
    cases.sort(key=lambda c: c["id"])
    with open(out, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    print(f"{len(cases)} cases from {len(runs)} runs; {sum(dropped.values())} left out")
    for reason in sorted(dropped):
        print(f"  left out: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
