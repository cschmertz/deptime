"""Choose the S1 sample (DT-SPIKE-01): 5 indexed releases per package, and the Python to probe each one on.

  uv run python bench/s1/plan.py --index ~/.cache/deptime/index.sqlite > bench/s1/plan.json

Reads the index only. Releases are spread evenly in time across each package's indexed range, first and latest
included (by ordinal, langchain's 349 0.0.x releases would take three of the five). Each one is probed on the
newest Python that had been out for six months when it was released, falling back to the one before, and each
slot lists the neighbouring releases to try if a release won't install from wheels or import offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from deptime.config import load_coverage
from deptime.index.store import IndexStore

PACKAGES = (
    "langchain",
    "langchain-community",
    "langchain-core",
    "openai",
    "pydantic",
    "huggingface-hub",
    "litellm",
    "pydantic-ai-slim",
    "instructor",
    "numpy",
)
VERSIONS_PER_PACKAGE = 5
PYTHONS = {  # x.y.0 release dates
    "3.9": date(2020, 10, 5),
    "3.10": date(2021, 10, 4),
    "3.11": date(2022, 10, 24),
    "3.12": date(2023, 10, 2),
    "3.13": date(2024, 10, 7),
    "3.14": date(2025, 10, 7),
}
PYTHON_SETTLE = timedelta(days=183)


def pythons_for(released: datetime) -> list[str]:
    usable = [v for v, d in PYTHONS.items() if d + PYTHON_SETTLE <= released.date()] or ["3.9"]
    return usable[::-1][:2]


def _when(row) -> datetime:
    return datetime.fromisoformat(row["released_at"]).astimezone(UTC)


def pick(rows: list, n: int) -> list[int]:
    """Indexes into `rows` (version order) of the releases closest to n evenly spaced instants."""
    first, last = _when(rows[0]), _when(rows[-1])
    chosen: list[int] = []
    for i in range(n):
        target = first + (last - first) * i / (n - 1)
        order = sorted(range(len(rows)), key=lambda j: (abs(_when(rows[j]) - target), j))
        chosen.append(next(j for j in order if j not in chosen))
    return sorted(chosen)


def entry(row) -> dict:
    return {"version": row["version"], "released_at": row["released_at"], "python": pythons_for(_when(row))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=Path, required=True)
    args = ap.parse_args()
    store = IndexStore.open(args.index)
    coverage = load_coverage()
    packages = []
    for name in PACKAGES:
        spec = coverage.get(name)
        # Only releases the index analysed from a wheel: the sandbox installs wheels only (openai 0.26 is sdist-only).
        rows = [
            r for r in store.releases(name) if r["released_at"] and (r["source_file"] or "").endswith(".whl")
        ]
        if spec is None or len(rows) < VERSIONS_PER_PACKAGE:
            print(f"{name}: not covered or too few indexed releases", file=sys.stderr)
            return 1
        chosen = pick(rows, VERSIONS_PER_PACKAGE)
        slots = []
        for j in chosen:
            near = (j + 1, j - 1, j + 2, j - 2, j + 3, j - 3)
            alternates = [k for k in near if 0 <= k < len(rows) and k not in chosen]
            slots.append([entry(rows[k]) for k in [j, *alternates]])
        packages.append({"name": name, "import_names": list(spec.import_names), "slots": slots})
    plan = {
        "index_built_at": store.built_at(),
        "per_stratum": 25,
        "seed": "DT-SPIKE-01",
        "packages": packages,
    }
    json.dump(plan, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
