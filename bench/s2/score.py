"""S2 (DT-SPIKE-02): how often DepTime's replacement hints are right, on 50 documented migrations.

`migrations.jsonl` was collected from changelogs, migration guides and deprecation messages without looking at
DepTime's answers, and committed with this script before either was run against an index.

    uv run python bench/s2/score.py bench/s2/migrations.jsonl [--index PATH] [--json OUT]

For each migration, the hints DepTime offers for the old path (`symbol_history`; for a parameter, the hints
recorded for `func(param)`) are graded against the documented replacement:

- a parameter hint is right when it names the new parameter;
- a symbol hint is right when its dotted path and the documented one agree on every component of the shorter
  one, counted from the end: `DataFrame.map` and `pandas.DataFrame.map`, or a bare `force_str`.

Anything else is graded wrong here and listed for a reader to check: a hint can name the same object by
another public path. Pass (docs/07-risks-and-spikes.md): precision >= 80% at confidence >= 0.75.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deptime.config import Settings, load_coverage
from deptime.index.store import IndexStore
from deptime.query import DepTime

BAR = 0.75


def _parts(path: str) -> list[str]:
    return path.strip().removesuffix("()").split(".")


def right(kind: str, target: str, new: str) -> bool:
    if kind == "param":
        return target.strip() == new.strip()
    a, b = _parts(target), _parts(new)
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return long[-len(short) :] == short


def hints(dt: DepTime, package: str, old: str, kind: str) -> list[tuple[str, float, str]]:
    """(target, confidence, source) DepTime offers for `old`, best first."""
    if kind == "param":
        func, _, param = old.partition("(")
        history = dt.symbol_history(package, func)
        prefix = f"{history.path}({param.rstrip(')')}) -> "
        found = [
            (r.target.removeprefix(prefix), r.confidence, r.source)
            for r in history.replacements
            if r.target.startswith(prefix)
        ]
    else:
        history = dt.symbol_history(package, old)
        found = [(r.target, r.confidence, r.source) for r in history.replacements if " -> " not in r.target]
    return sorted(found, key=lambda h: -h[1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("migrations")
    ap.add_argument("--index", type=Path, help="index to score (default: the configured one)")
    ap.add_argument("--json", type=Path, help="also write per-migration results here")
    args = ap.parse_args()

    settings = Settings.from_env()
    index = args.index or settings.index_path
    dt = DepTime(IndexStore.open(index), load_coverage(settings.coverage_file), index)
    rows = [json.loads(line) for line in Path(args.migrations).read_text().splitlines() if line.strip()]

    results = []
    for m in rows:
        history = dt.symbol_history(m["package"], m["old"].partition("(")[0])
        offered = hints(dt, m["package"], m["old"], m["kind"])
        graded = [(t, c, s, right(m["kind"], t, m["new"])) for t, c, s in offered]
        results.append({**m, "indexed": history.ever_seen, "hints": graded})

    shown = [(r, h) for r in results for h in r["hints"] if h[1] >= BAR]
    correct = sum(h[3] for _, h in shown)
    indexed = [r for r in results if r["indexed"]]
    found = [r for r in results if any(h[3] and h[1] >= BAR for h in r["hints"])]
    found_any = [r for r in results if any(h[3] for h in r["hints"])]
    top_right = [r for r in results if r["hints"] and r["hints"][0][3]]
    with_hints = [r for r in results if r["hints"]]

    built = dt.store.built_at() if dt.store else "?"
    precision = f"{correct}/{len(shown)} = {100 * correct / len(shown):.1f}%" if shown else "no hints"
    verdict = "PASS" if shown and correct / len(shown) >= 0.8 else "FAIL"
    out = [
        f"# S2 replacement precision (DT-SPIKE-02), index built {built}",
        "",
        f"**{verdict}**: precision at confidence >= {BAR}: {precision} (pass at >= 80%).",
        "",
        f"- Migrations: {len(results)}; the old path is in the index for {len(indexed)}.",
        f"- With a right hint at >= {BAR}: {len(found)}/{len(results)}; at any confidence: {len(found_any)}.",
        f"- With any hint: {len(with_hints)}; the best one is right for {len(top_right)}.",
        "",
        "| migration | indexed | hints (target, confidence, source; ✓ right) |",
        "|---|---|---|",
    ]
    for r in results:
        cell = "<br>".join(f"`{t}` {c:.2f} {s}{' ✓' if ok else ''}" for t, c, s, ok in r["hints"]) or "none"
        out.append(f"| {r['id']} (→ `{r['new']}`) | {'yes' if r['indexed'] else 'no'} | {cell} |")
    wrong = [(r["id"], h) for r, h in shown if not h[3]]
    if wrong:
        out += [
            "",
            f"Hints at >= {BAR} graded wrong (check by hand: another public path can be the same object):",
        ]
        out += [f"- {i}: `{t}` {c:.2f} {s}" for i, (t, c, s, _ok) in wrong]
    print("\n".join(out))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
