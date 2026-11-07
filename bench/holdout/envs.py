"""Turn a holdout set's upgrades (and traceback checks) into the environments the probe installs (DT-QA-07).

    uv run python bench/holdout/envs.py <set dir>

Reads <set dir>/upgrades.jsonl and writes <set dir>/envs.json: one environment per package version, with every use any
upgrade asks about there; and from the `check` of each case in <set dir>/tracebacks.jsonl, envs-tracebacks.json.

- TimeMachine-bench upgrades: `to` only, on Python 3.12 with PyPI as of 2025-08-01 (the dataset's migration date).
- Pinned upgrades: both `from` and `to`. A version installs with PyPI as of 30 days after its release, so its
  dependencies are ones it was used with; the newest releases install with PyPI as of the set's cutoff.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collect import pypi_releases

CUTOFF = "2026-11-07"
TMB_DATE = "2025-08-01"
OLD_PYTHONS = ["3.11", "3.10", "3.12", "3.9"]
NEW_PYTHONS = ["3.12", "3.11", "3.13"]


def use_key(u: dict) -> str:
    raw = json.dumps([u["kind"], u["path"], u.get("npos"), u.get("kwargs", []), u.get("via", "import")])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def exclude_newer(package: str, version: str) -> str:
    released = pypi_releases(package)[version]
    end = min(released + timedelta(days=30), datetime.fromisoformat(CUTOFF).replace(tzinfo=UTC))
    return end.strftime("%Y-%m-%d")


def newest(package: str, version: str) -> bool:
    return exclude_newer(package, version) == CUTOFF


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = Path(sys.argv[1])
    envs: dict[str, dict] = {}
    checks: dict[str, dict] = {}

    def add(
        package: str, version: str, pythons: list[str], date: str, uses: list[dict], into: dict | None = None
    ) -> None:
        envs_ = envs if into is None else into
        eid = f"{package}=={version}@{date}"
        env = envs_.setdefault(
            eid,
            {
                "id": eid,
                "python": pythons,
                "exclude_newer": date,
                "requires": [f"{package}=={version}"],
                "uses": {},
            },
        )
        for u in uses:
            k = use_key(u)
            env["uses"].setdefault(
                k, {"key": k, **{f: u[f] for f in ("kind", "path", "npos", "kwargs", "via") if f in u}}
            )

    rows = [
        json.loads(x) for x in (root / "upgrades.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    for r in rows:
        p = r["package"]
        if r["source"] == "tmb":
            add(p, r["to"], ["3.12"], TMB_DATE, r["uses"])
            continue
        for v in (r["from"], r["to"]):
            date = exclude_newer(p, v)
            add(p, v, NEW_PYTHONS if date == CUTOFF else OLD_PYTHONS, date, r["uses"])
    tracebacks = root / "tracebacks.jsonl"
    if tracebacks.exists():
        for line in tracebacks.read_text(encoding="utf-8").splitlines():
            if line.strip() and (c := json.loads(line).get("check")):
                package = c["package"]
                for v in [*c["works_in"], *c["fails_in"]]:
                    date = exclude_newer(package, v)
                    add(package, v, NEW_PYTHONS if date == CUTOFF else OLD_PYTHONS, date, [c["use"]], checks)
    for name, found in (("envs.json", envs), ("envs-tracebacks.json", checks)):
        out = [{**e, "uses": sorted(e["uses"].values(), key=lambda u: u["key"])} for e in found.values()]
        out.sort(key=lambda e: e["id"])
        (root / name).write_text(json.dumps({"envs": out}, indent=0) + "\n", encoding="utf-8")
        print(f"{name}: {len(out)} environments, {sum(len(e['uses']) for e in out)} uses", file=sys.stderr)


if __name__ == "__main__":
    main()
