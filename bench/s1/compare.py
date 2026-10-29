"""Score S1 (DT-SPIKE-01): how often does the index call a name absent that exists at runtime?

  uv run python bench/s1/compare.py <downloaded artifacts dir> --index <index.sqlite> [--json results.json]

Reads the probe output (untrusted data: parsed as JSON, never executed) and the index; imports no package under
test. For each probed release it samples 25 runtime-public names from package `__init__` modules and 25 from the
other modules (topping up from one stratum when the other runs short), with a seed fixed per release, and checks
each against the spans at that exact indexed release. A false absent is a sampled name with no span there.
Pass: at most 10% false absent overall (docs/07 §S1). The rate over every runtime name is reported alongside.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from deptime.index.store import IndexStore

PASS_RATE = 0.10
COLUMNS = ["module", "name", "stratum", "how", "kind", "origin"]


def load_probes(root: Path) -> list[dict]:
    probes = []
    for path in sorted(root.rglob("*.json")):
        if path.name == "attempts.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not (
            isinstance(data, dict)
            and data.get("columns") == COLUMNS
            and isinstance(data.get("names"), list)
            and all(
                isinstance(r, list) and len(r) == 6 and all(isinstance(c, str) for c in r)
                for r in data["names"]
            )
        ):
            raise SystemExit(f"{path}: not a probe result")
        probes.append(data)
    return probes


def sample(rows: list[dict], per_stratum: int, seed: str) -> list[dict]:
    rng = random.Random(seed)
    init = [r for r in rows if r["stratum"] == "init"]
    other = [r for r in rows if r["stratum"] != "init"]
    k_other = min(per_stratum, len(other))
    k_init = min(len(init), 2 * per_stratum - k_other)  # top up from whichever stratum has more
    k_other = min(len(other), 2 * per_stratum - k_init)
    return rng.sample(init, k_init) + rng.sample(other, k_other)


def rate(misses: int, total: int) -> str:
    return f"{misses}/{total} = {misses / total:.1%}" if total else "n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("probes", type=Path)
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--plan", type=Path, default=Path(__file__).with_name("plan.json"))
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    store = IndexStore.open(args.index)
    probes = load_probes(args.probes)
    if not probes:
        raise SystemExit(f"no probe results under {args.probes}")

    results = []
    by_pkg: dict[str, Counter] = defaultdict(Counter)
    by_stratum: Counter = Counter()
    by_shape: Counter = Counter()
    for probe in probes:
        name, version = probe["package"], probe["version"]
        row, exact = store.resolve_version(name, version)
        if row is None or not exact:
            raise SystemExit(f"{name} {version} is not an indexed release of this index")
        ordinal = row["ordinal"]
        rows = {}
        for r in probe["names"]:
            rec = dict(zip(COLUMNS, r, strict=True))
            rows.setdefault(f"{rec['module']}.{rec['name']}", rec)
        population = [{"path": p, **rec} for p, rec in rows.items()]
        for rec in population:
            rec["present"] = store.span_at(name, rec["path"], ordinal) is not None
        picked = sample(population, plan["per_stratum"], f"{plan['seed']}:{name}:{version}")
        for rec in picked:
            if not rec["present"]:
                rec["seen_elsewhere"] = bool(store.spans(name, rec["path"]))
        misses = [r for r in picked if not r["present"]]
        c = by_pkg[name]
        c["sampled"] += len(picked)
        c["missed"] += len(misses)
        c["population"] += len(population)
        c["population_missed"] += sum(not r["present"] for r in population)
        for r in picked:
            by_stratum[r["stratum"], "sampled"] += 1
            by_stratum[r["stratum"], "missed"] += not r["present"]
        for r in population:
            if not r["present"]:
                by_shape[r["stratum"], r["how"], r["origin"]] += 1
        results.append(
            {
                "package": name,
                "version": version,
                "python": probe.get("python"),
                "modules_imported": probe.get("modules_imported"),
                "modules_failed": len(probe.get("modules_failed", {})),
                "unresolved_names": sum((probe.get("unresolved_names") or {}).values()),
                "population": len(population),
                "sampled": len(picked),
                "missed": misses,
            }
        )

    sampled = sum(c["sampled"] for c in by_pkg.values())
    missed = sum(c["missed"] for c in by_pkg.values())
    verdict = "PASS" if sampled and missed / sampled <= PASS_RATE else "FAIL"
    print(f"# S1 false-absent rate (DT-SPIKE-01), index built {store.built_at()}\n")
    print(f"**{verdict}**: sampled {rate(missed, sampled)} (pass at <= {PASS_RATE:.0%}).\n")
    for stratum in ("init", "module"):
        print(f"- {stratum}: {rate(by_stratum[stratum, 'missed'], by_stratum[stratum, 'sampled'])}")
    print("\n| package | releases | sampled | every runtime name |\n|---|---|---|---|")
    for name in sorted(by_pkg):
        c = by_pkg[name]
        n = sum(r["package"] == name for r in results)
        print(
            f"| {name} | {n} | {rate(c['missed'], c['sampled'])} | {rate(c['population_missed'], c['population'])} |"
        )
    print(
        "\n| package | version | python | modules (failed) | names | sampled misses |\n|---|---|---|---|---|---|"
    )
    for r in results:
        print(
            f"| {r['package']} | {r['version']} | {r['python']} | {r['modules_imported']} ({r['modules_failed']})"
            f" | {r['population']} | {len(r['missed'])}/{r['sampled']} |"
        )
    print("\nEvery runtime name the index lacks, by shape (stratum, how listed, where defined):\n")
    for (stratum, how, origin), n in by_shape.most_common():
        print(f"- {stratum} / {how} / {origin}: {n}")
    print("\nSampled misses:\n")
    for r in results:
        for m in r["missed"]:
            seen = "seen in other releases" if m["seen_elsewhere"] else "never indexed"
            print(
                f"- {r['package']} {r['version']}: `{m['path']}` ({m['stratum']}, {m['how']}, {m['kind']}, {m['origin']}; {seen})"
            )
    if args.json:
        args.json.write_text(
            json.dumps(
                {"verdict": verdict, "sampled": sampled, "missed": missed, "results": results}, indent=1
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
