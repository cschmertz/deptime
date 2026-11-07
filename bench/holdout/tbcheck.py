"""Check each holdout traceback's boundary with the probe (DT-QA-07): the failing use must work in every `works_in`
release and fail in every `fails_in` release. Nothing here asks DepTime anything.

    uv run python bench/holdout/tbcheck.py <set dir> <probe artifacts dir>

Writes <set dir>/traceback-checks.json and exits 1 when a check disagrees with the case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from envs import exclude_newer, use_key
from labels import works


def main() -> int:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    root, arts = Path(sys.argv[1]), Path(sys.argv[2])
    res: dict[str, dict[str, dict]] = {}
    for f in sorted(arts.rglob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip() and (rec := json.loads(line)).get("result") == "ok":
                res.setdefault(rec["id"], {}).update({r["key"]: r for r in rec["results"]})
    report, bad = {}, 0
    for line in (root / "tracebacks.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        c = case.get("check")
        if not c:
            continue
        k = use_key(c["use"])
        rows = {}
        for want, versions in ((True, c["works_in"]), (False, c["fails_in"])):
            for v in versions:
                r = res.get(f"{c['package']}=={v}@{exclude_newer(c['package'], v)}", {}).get(k)
                got, why = works(r, c["use"]) if r else (None, "environment didn't install")
                rows[v] = {"expected": "works" if want else "fails", "got": got, "why": why}
        ok = all(row["got"] is (row["expected"] == "works") for row in rows.values())
        bad += not ok
        report[case["id"]] = {"ok": ok, "versions": rows}
        print(
            f"{'ok ' if ok else 'BAD'} {case['id']}: " + ", ".join(f"{v}={r['got']}" for v, r in rows.items())
        )
    (root / "traceback-checks.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"{len(report) - bad}/{len(report)} boundary checks agree", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
