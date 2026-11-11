"""Score a holdout set (DT-QA-07) against the index in DEPTIME_HOME, by the rules fixed in bench/holdout/README.md.

    uv run python bench/holdout/score.py <set dir> [--json <out.json>]
    uv run python bench/holdout/score.py <set dir> --baseline <set dir>/baseline.json         regression check (CI)
    uv run python bench/holdout/score.py <set dir> --write-baseline <set dir>/baseline.json   record the current run

Uses: each labelled use (works / breaks) is asked through `check_symbols` at `to` and through `diff_versions` between
`from` and `to`, and counted as flagged when the answer says it breaks. Tracebacks (<set dir>/tracebacks.jsonl, the
bench/cases format) go through `explain_error`, scored as bench/README.md defines.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from packaging.version import InvalidVersion, Version

from deptime.bench import load_cases, metrics, run
from deptime.config import Settings, load_coverage
from deptime.index.location import open_index
from deptime.models import DiffResult, SymbolQuery
from deptime.query import MAX_DIFF_EVENTS, DepTime

Z = 1.96
MAX_RECALL_DROP = 2.0  # points, per tool, on the uses the baseline shares with this run
MAX_FPR_RISE = 1.0
MAX_TRACEBACK_DROP = 2.0
TOOLS = ("check_symbols", "diff_versions")
CHANGE_TYPES = (
    "removed",
    "param_removed",
    "param_now_required",
    "positional",
    "symbol_not_yet_added",
    "param_not_yet_added",
    "other",
)


def wilson(x: int, n: int) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = x / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def rate(x: int, n: int) -> str:
    if n == 0:
        return "-"
    lo, hi = wilson(x, n)  # type: ignore[misc]
    return f"{100 * x / n:.1f}% ({x}/{n}; {100 * lo:.1f}-{100 * hi:.1f})"


def _v(s: str) -> Version:
    try:
        return Version(s)
    except InvalidVersion:
        return Version("0")


# ---------------------------------------------------------------------------------------------- asking DepTime
def ask_check_symbols(dt: DepTime, row: dict, items: list[dict]) -> list[dict]:
    queries = [
        SymbolQuery(path=i["path"], kwargs=i.get("kwargs", []), positional=i.get("npos"))
        if i["kind"] == "call"
        else SymbolQuery(path=i["path"])
        for i in items
    ]
    res = dt.check_symbols(row["package"], row["to"], symbols=queries)
    out = []
    for i, r in zip(items, res.results, strict=True):
        if r.status in ("not_indexed", "unknown"):  # `unknown`: the index can't tell (DT-CORE-32)
            out.append({"answered": False, "why": r.hint or res.note or r.status})
            continue
        rejected = [k for k in i.get("kwargs", []) if k in r.rejected_kwargs] if i["kind"] == "call" else []
        # A call to a callable whose body only raises fails (DT-DATA-67).
        raises = getattr(r, "raises", None) if i["kind"] == "call" else None
        # More arguments by position than the signature at `to` takes, with no `*args` (DT-CORE-50).
        most = getattr(r, "max_positional", None) if i["kind"] == "call" else None
        out.append(
            {
                "answered": True,
                "flagged": r.status == "missing"
                or bool(rejected)
                or bool(r.missing_args)
                or bool(raises)
                or most is not None,
                "deprecated": bool(r.deprecated),
                "said": f"{r.status}"
                + (f" rejected={rejected}" if rejected else "")
                + (f" missing_args={r.missing_args}" if r.missing_args else "")
                + (f" max_positional={most}" if most is not None else "")
                + (f" raises={raises}" if raises else "")
                + (f" ({r.hint})" if r.hint else ""),
            }
        )
    return out


def _state_at_to(
    events: list,
    path: str,
    up: bool,
    present: str,
    absent: str,
    param: str | None = None,
    kwargs_at_to: bool = False,
):
    """Whether `to` lacks the path (or the parameter), from the membership events on it: None when no event says.
    A parent's event that lists the path in `affects` (DT-CORE-33) counts as one on the path. Upgrading, a parameter
    removed into `**kwargs` (`absorbed_by_var_keyword`) still takes the keyword when the signature DepTime has at `to`
    still has `**kwargs` (`kwargs_at_to`; DT-QA-12: litellm 1.103's `completion(*args, **kwargs)`, but not
    pydantic-ai's `Agent(result_type=)`, whose `**_deprecated_kwargs` went later); sets scored before it, to 2026-11e,
    counted it as lacking."""
    evs = [
        e
        for e in events
        if e.kind in (present, absent)
        and (param is None or (e.detail or {}).get("param") == param)
        and (e.path == path or (param is None and path in (getattr(e, "affects", None) or ())))
    ]
    if not evs:
        return None
    evs.sort(key=lambda e: _v(e.version))
    if up:  # the last change before `to`
        last = evs[-1]
        return last.kind == absent and not (
            param is not None and kwargs_at_to and (last.detail or {}).get("absorbed_by_var_keyword")
        )
    return evs[0].kind == present  # going back to `to`: the first change after it says what `to` had


REQUIREDNESS = ("param_added", "param_removed", "param_now_required", "param_now_optional")


def _required_at_to(events: list, path: str, param: str, up: bool) -> bool:
    """Whether the events say `param` of `path` is required at `to` and wasn't on the other side: upgrading, the last
    change to it before `to` is `param_now_required` or a required `param_added`; going down, the first change after
    `to` is `param_now_optional`."""
    evs = sorted(
        (
            e
            for e in events
            if e.path == path and e.kind in REQUIREDNESS and (e.detail or {}).get("param") == param
        ),
        key=lambda e: _v(e.version),
    )
    if not evs:
        return False
    if up:
        last = evs[-1]
        return last.kind == "param_now_required" or (
            last.kind == "param_added" and bool((last.detail or {}).get("required"))
        )
    return evs[0].kind == "param_now_optional"


def _by_position(params: list | None, npos: int | None) -> set[str]:
    """The parameters a call with `npos` positional arguments fills, from a signature (a method's `self` or `cls`
    comes bound); none when either is unknown."""
    if not params or not npos:
        return set()
    if params[0].name in ("self", "cls") and params[0].kind != "keyword-only":
        params = params[1:]
    filled: set[str] = set()
    for p in params:
        if len(filled) >= npos or p.kind not in ("positional-only", "positional or keyword"):
            break
        filled.add(p.name)
    return filled


SLOT_EVENTS = ("param_removed", "param_added", "param_kind_changed")


def _positional_lost(
    at_from: list | None, at_to: list | None, npos: int, spare: int, events: list, targets: tuple[str, ...]
) -> str | None:
    """The parameter whose slot a call's positional arguments lose, or None (DT-CORE-50): DepTime's signature at `to`
    has room for fewer than `npos - spare` of them, and no `*args`, and an event changes a parameter the call fills by
    position in its signature at `from` (removed, even into `**kwargs`, which takes it only by keyword; made
    keyword-only; or, going down, added after `to`). pydantic 2.0's `FieldInfo(default)`. Slots are counted, not
    names: a renamed parameter still takes its positional argument."""
    if at_to is None or any(p.kind == "variadic positional" for p in at_to):
        return None
    if len(_by_position(at_to, npos)) >= npos - spare:
        return None
    lost = sorted(
        p
        for p in _by_position(at_from, npos)
        if any(
            e.path in targets and e.kind in SLOT_EVENTS and (e.detail or {}).get("param") == p for e in events
        )
    )
    return lost[0] if lost else None


def _never_seen_at_to(res: DiffResult, path: str, up: bool) -> bool:
    """Whether diff_versions lists the path in `never_seen` as missing at `to` (DT-CORE-54): no indexed release has it,
    so no event can say so (langgraph 0.3.31 -> 0.0.37's `checkpoint.memory.InMemorySaver`). Going down, `to` is the
    range's `from_version`."""
    end = res.to_version if up else res.from_version
    return any(u.path == path and end in u.missing_in for u in res.never_seen)


def ask_diff_versions(dt: DepTime, row: dict, item: dict) -> dict:
    up = _v(row["to"]) >= _v(row["from"] or "0")
    lo, hi = (row["from"], row["to"]) if up else (row["to"], row["from"])
    if not lo:
        return {"answered": False, "why": "no from version"}
    res = dt.diff_versions(
        row["package"], lo, hi, used_symbols=[item["path"]], include_added=True, limit=MAX_DIFF_EVENTS
    )
    if res.note and "Could not resolve" in res.note:
        return {"answered": False, "why": res.note}
    if res.from_version is None or res.to_version is None:
        return {"answered": False, "why": res.note or "unresolved"}
    events = res.events
    path = item["path"]
    reasons = []
    if _state_at_to(events, path, up, "added", "removed"):
        reasons.append("removed" if up else "added after to")
    if _never_seen_at_to(res, path, up):
        reasons.append("in no indexed release, missing at to")
    if item["kind"] == "call":
        targets = (path, path + ".__init__")
        # Upgrading across a release from which the callable only raises (DT-DATA-67).
        if up and any(e.kind == "now_raises" and e.path in targets for e in events):
            reasons.append("now raises")
        sig_to = dt._signature(row["package"], path, row["to"])
        kwargs_at_to = any(p.kind == "variadic keyword" for p in sig_to or ())
        for k in item.get("kwargs", []):
            for t in targets:
                if _state_at_to(
                    events, t, up, "param_added", "param_removed", param=k, kwargs_at_to=kwargs_at_to
                ):
                    reasons.append(f"param {k} {'removed' if up else 'added after to'}")
        # A parameter required at `to` that the call passes neither by keyword nor by position, where the signature
        # DepTime has at `to` places the call's positional arguments (DT-QA-10: `StateGraph(State)` passes
        # `state_schema`). Upgrading, the last change to it before `to` made it required (a default removed, or added
        # without one); going down, the first change after `to` made it optional (polars 0.19.6's `int_range(n)`).
        passed = set(item.get("kwargs", [])) | _by_position(sig_to, item.get("npos"))
        for t in targets:
            for p in sorted({(e.detail or {}).get("param") for e in events if e.path == t} - {None} - passed):
                if _required_at_to(events, t, p, up):
                    reasons.append(f"param {p} {'now required' if up else 'required at to'}")
        # A positional argument whose slot an event in the range takes away (DT-CORE-50). A method called through
        # its class (not `via: instance`) may be passed its instance, which the probe counts in `npos`, as
        # check_symbols allows.
        if npos := item.get("npos"):
            rel = dt._release(row["package"], row["to"])
            spare = int(
                rel is not None
                and item.get("via") != "instance"
                and dt._through_class(row["package"], path, rel["ordinal"])
            )
            at_from, at_to = (dt._signature(row["package"], path, row[v]) for v in ("from", "to"))
            if lost := _positional_lost(at_from, at_to, npos, spare, events, targets):
                reasons.append(f"positional {lost} {'taken away' if up else 'not yet taken at to'}")
    dep = any(
        (e.path == path and e.kind == "deprecated")
        or (
            e.kind == "param_deprecated"
            and e.path in (path, path + ".__init__")
            and (e.detail or {}).get("param") in item.get("kwargs", [])
        )
        for e in events
    )
    return {
        "answered": True,
        "flagged": bool(reasons),
        "deprecated": dep if up else False,
        "said": "; ".join(sorted(set(reasons))) or f"{len(events)} events, none breaking this use",
        "truncated": res.truncated,
    }


# ---------------------------------------------------------------------------------------------- scoring
def confusion(rows: list[dict], tool: str) -> dict[str, int]:
    c = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "abstain_breaks": 0, "abstain_works": 0}
    for r in rows:
        a = r[tool]
        if not a["answered"]:
            c["abstain_breaks" if r["label"] == "breaks" else "abstain_works"] += 1
            continue
        if r["label"] == "breaks":
            c["tp" if a["flagged"] else "fn"] += 1
        else:
            c["fp" if a["flagged"] else "tn"] += 1
    return c


def prf(c: dict[str, int]) -> dict[str, str]:
    return {
        "precision": rate(c["tp"], c["tp"] + c["fp"]),
        "recall": rate(c["tp"], c["tp"] + c["fn"]),
        "false_positive_rate": rate(c["fp"], c["fp"] + c["tn"]),
        "abstained": f"{c['abstain_breaks']} breaks, {c['abstain_works']} works",
    }


def table(groups: dict[str, list[dict]], tool: str) -> list[str]:
    lines = [
        "| group | uses (breaks/works) | precision | recall | false-positive rate |",
        "|---|---|---|---|---|",
    ]
    for name, rows in groups.items():
        c = confusion(rows, tool)
        p = prf(c)
        nb = sum(r["label"] == "breaks" for r in rows)
        lines.append(
            f"| {name} | {len(rows)} ({nb}/{len(rows) - nb}) | {p['precision']} | {p['recall']} | {p['false_positive_rate']} |"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("set", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--baseline", type=Path, help="fail on a regression against this")
    ap.add_argument("--write-baseline", type=Path, help="write this run as the new baseline")
    a = ap.parse_args(argv)
    settings = Settings.from_env()
    store = open_index(settings.index_path)
    if store is None:
        print(f"no index at {settings.index_path}; run `deptime index pull` first", file=sys.stderr)
        return 2
    dt = DepTime(store, load_coverage(settings.coverage_file), settings.index_path)
    root = a.set
    upgrades = {
        r["id"]: r
        for r in (
            json.loads(x)
            for x in (root / "upgrades.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()
        )
    }
    labels = [
        json.loads(x) for x in (root / "labels.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()
    ]

    scored: list[dict] = []
    excluded = defaultdict(int)
    for lab in labels:
        row = upgrades[lab["id"]]
        items = [i for i in lab["items"] if i["label"] in ("works", "breaks")]
        for i in lab["items"]:
            if i["label"] == "excluded":
                excluded[i["why"].split(":")[0][:60]] += 1
        if not items:
            continue
        checks = ask_check_symbols(dt, row, items)
        direction = "tmb" if row["source"] == "tmb" else ("down" if _v(row["to"]) < _v(row["from"]) else "up")
        for i, cs in zip(items, checks, strict=True):
            scored.append(
                {
                    **i,
                    "upgrade": row["id"],
                    "package": row["package"],
                    "tier": row["tier"],
                    "source": row["source"],
                    "direction": direction,
                    "stratum": row.get("stratum") or "main",
                    "from": row["from"],
                    "to": row["to"],
                    "check_symbols": cs,
                    "diff_versions": ask_diff_versions(dt, row, i),
                }
            )

    out: list[str] = [f"index built {store.built_at()}", ""]
    nb = sum(r["label"] == "breaks" for r in scored)
    out.append(
        f"**Uses:** {len(scored)} scored ({nb} break, {len(scored) - nb} work) in {len({r['upgrade'] for r in scored})}"
        f" upgrades; {sum(excluded.values())} excluded."
    )
    out.append("")
    out.append("| tool | precision | recall | false-positive rate | abstained |")
    out.append("|---|---|---|---|---|")
    summary: dict = {"uses": len(scored), "breaks": nb, "tools": {}}
    for tool in TOOLS:
        c = confusion(scored, tool)
        p = prf(c)
        summary["tools"][tool] = {**c}
        out.append(
            f"| `{tool}` | {p['precision']} | {p['recall']} | {p['false_positive_rate']} | {p['abstained']} |"
        )
    out.append("")
    out.append("Rates with 95% Wilson intervals. A use counts once per upgrade it is asked in.")

    def grouped(key) -> dict[str, list[dict]]:
        g: dict[str, list[dict]] = defaultdict(list)
        for r in scored:
            g[key(r)].append(r)
        return dict(sorted(g.items()))

    for tool in TOOLS:
        out += ["", f"### `{tool}`", "", "**By change type (recall):**", ""]
        out += ["| change type | breaks | recall |", "|---|---|---|"]
        for ct in CHANGE_TYPES:
            rows = [r for r in scored if r["label"] == "breaks" and r.get("change") == ct]
            if not rows:
                continue
            c = confusion(rows, tool)
            out.append(f"| {ct} | {len(rows)} | {rate(c['tp'], c['tp'] + c['fn'])} |")
        out += ["", "**By tier:**", "", *table(grouped(lambda r: r["tier"]), tool)]
        out += ["", "**By source:**", "", *table(grouped(lambda r: r["direction"]), tool)]
        if any(r["stratum"] != "main" for r in scored):  # DT-QA-10's exercise stratum
            out += ["", "**By stratum:**", "", *table(grouped(lambda r: r["stratum"]), tool)]
        out += [
            "",
            "**By kind of use:**",
            "",
            *table(
                grouped(lambda r: r["kind"] + (" (instance)" if r.get("via") == "instance" else "")), tool
            ),
        ]
        out += ["", "**By package:**", "", *table(grouped(lambda r: f"{r['package']} ({r['tier']})"), tool)]
        # Upgrades where nothing breaks: how often does the tool raise any false alarm?
        clean = defaultdict(list)
        for r in scored:
            clean[r["upgrade"]].append(r)
        ok = {u: rs for u, rs in clean.items() if all(x["label"] == "works" for x in rs)}
        alarms = sum(any(x[tool]["answered"] and x[tool]["flagged"] for x in rs) for rs in ok.values())
        out += [
            "",
            f"**Upgrades where every use works:** at least one false alarm in {rate(alarms, len(ok))}.",
        ]
        dep_true = [r for r in scored if r["label"] == "works" and r.get("deprecated")]
        dep_flag = [
            r for r in scored if r["label"] == "works" and r[tool]["answered"] and r[tool]["deprecated"]
        ]
        hit = sum(1 for r in dep_true if r[tool]["answered"] and r[tool]["deprecated"])
        out += [
            "",
            f"**Deprecations** (uses that work; truth: the probe's evidence that the name is deprecated, as"
            f" `../README.md` defines it for this set): flagged {len(dep_flag)}, of which {hit} have that evidence;"
            f" recall {rate(hit, len(dep_true))}.",
        ]
        fps = [r for r in scored if r["label"] == "works" and r[tool]["answered"] and r[tool]["flagged"]]
        fns = [r for r in scored if r["label"] == "breaks" and r[tool]["answered"] and not r[tool]["flagged"]]
        out += ["", f"<details><summary>False positives ({len(fps)})</summary>", ""]
        out += [f"- `{r['package']}` {r['from']} → {r['to']}: `{_use(r)}`: {r[tool]['said']}" for r in fps]
        out += ["", "</details>", "", f"<details><summary>Missed breaks ({len(fns)})</summary>", ""]
        out += [
            f"- `{r['package']}` {r['from']} → {r['to']}: `{_use(r)}` ({r['change']}: {r.get('detail', '')[:120]}): {r[tool]['said']}"
            for r in fns
        ]
        out += ["", "</details>"]

    out += ["", "**Excluded uses:**", ""] + [
        f"- {n}: {why}" for why, n in sorted(excluded.items(), key=lambda x: -x[1])
    ]

    tb = root / "tracebacks.jsonl"
    results = []
    if tb.exists():
        cases = load_cases([tb])
        results = run(dt, cases)
        m = metrics(results)
        n = len(results)
        rc = sum(r.root_cause for r in results)
        rcb = sum(r.passed for r in results)
        answered = [r for r in results if not r.got.startswith("no finding")]
        wb = [r for r in results if r.replacement is not None]
        out += ["", "### `explain_error` on tracebacks", ""]
        out += [
            f"- root cause {rate(rc, n)}",
            f"- root cause + boundary {rate(rcb, n)}",
            f"- replacement {rate(sum(bool(r.replacement) for r in wb), len(wb))}",
            f"- precision (a finding, and the right one) {rate(sum(r.root_cause for r in answered), len(answered))}",
            "",
        ]
        by_tier: dict[str, list] = defaultdict(list)
        for r in results:
            by_tier[r.tier].append(r)
        for t, rs in sorted(by_tier.items()):
            out.append(f"- tier {t}: root cause + boundary {rate(sum(r.passed for r in rs), len(rs))}")
        by_type: dict[str, list] = defaultdict(list)
        known = [c["expected"] for c in load_cases([Path(__file__).parent.parent / "cases"])]
        split: dict[str, list] = defaultdict(list)
        for c, r in zip(cases, results, strict=True):
            by_type[change_type(c["expected"])].append(r)
            seen = any(_same_break(c["expected"], k) for k in known)
            split["break already in bench/cases" if seen else "new break"].append(r)
        out.append("")
        for t, rs in sorted(by_type.items()):
            out.append(f"- {t}: root cause + boundary {rate(sum(r.passed for r in rs), len(rs))}")
        out.append("")
        for t, rs in sorted(split.items()):
            out.append(f"- {t}: root cause + boundary {rate(sum(r.passed for r in rs), len(rs))}")
        out += ["", "<details><summary>Cases</summary>", ""]
        out += [f"- {'PASS' if r.passed else 'FAIL'} `{r.id}`: {r.got}" for r in results]
        out += ["", "</details>"]
        summary["tracebacks"] = {"cases": n, **m}
    print("\n".join(out))
    if a.json:
        a.json.write_text(json.dumps({"summary": summary, "uses": scored}, indent=1) + "\n", encoding="utf-8")
    doc = baseline_doc(scored, results, store.built_at())
    if a.write_baseline:
        a.write_baseline.write_text(json.dumps(doc, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    if a.baseline and (failed := regressions(doc, json.loads(a.baseline.read_text(encoding="utf-8")))):
        print("\nregression:\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1
    return 0


def baseline_doc(scored: list[dict], results: list, built_at: str | None) -> dict:
    """Each use's label and what each tool said (flagged, or None when it abstained), and each traceback's pass."""
    return {
        "index_built_at": built_at,
        "uses": {
            f"{r['upgrade']}|{r['key']}": {
                "label": r["label"],
                **{t: (r[t]["flagged"] if r[t]["answered"] else None) for t in TOOLS},
            }
            for r in scored
        },
        "tracebacks": {r.id: r.passed for r in results},
    }


def regressions(now: dict, base: dict) -> list[str]:
    """Recall that fell by more than MAX_RECALL_DROP points, or a false-positive rate that rose by more than MAX_FPR_RISE,
    for either tool on the uses both runs share; and root cause + boundary on the shared tracebacks."""
    failed = []
    shared = [k for k in base["uses"] if k in now["uses"]]
    for tool in TOOLS:
        for label, name, worse in (
            ("breaks", "recall", lambda b, n: n < b - MAX_RECALL_DROP),
            ("works", "false-positive rate", lambda b, n: n > b + MAX_FPR_RISE),
        ):
            keys = [k for k in shared if base["uses"][k]["label"] == label]
            if not keys:
                continue
            before = 100 * sum(bool(base["uses"][k][tool]) for k in keys) / len(keys)
            after = 100 * sum(bool(now["uses"][k][tool]) for k in keys) / len(keys)
            if worse(before, after):
                failed.append(f"{tool} {name}: {before:.1f} -> {after:.1f} on {len(keys)} shared uses")
    cases = [c for c in base["tracebacks"] if c in now["tracebacks"]]
    if cases:
        before = 100 * sum(base["tracebacks"][c] for c in cases) / len(cases)
        after = 100 * sum(now["tracebacks"][c] for c in cases) / len(cases)
        if after < before - MAX_TRACEBACK_DROP:
            failed.append(
                f"explain_error root cause + boundary: {before:.1f} -> {after:.1f} on {len(cases)} tracebacks"
            )
    return failed


def change_type(exp: dict) -> str:
    """A traceback's change type, as DT-QA-07 reports them: a removal with a stated replacement is a move (or, for a
    parameter, a rename)."""
    issue, moved = exp["issue"], "replacement_contains" in exp
    if issue in ("symbol_removed", "module_removed"):
        return "moved" if moved else "removed"
    if issue == "param_removed":
        return "parameter renamed" if moved else "parameter removed"
    if issue == "param_now_required":
        return "now required"
    return "not yet added"


def _same_break(a: dict, b: dict) -> bool:
    x, y = a["symbol_suffix"], b["symbol_suffix"]
    return a["package"] == b["package"] and a["issue"] == b["issue"] and (x.endswith(y) or y.endswith(x))


def _use(r: dict) -> str:
    if r["kind"] == "call":
        args = ["_"] * (r.get("npos") or 0) + [f"{k}=" for k in r.get("kwargs", [])]
        return f"{r['path']}({', '.join(args)})"
    return r["path"]


if __name__ == "__main__":
    sys.exit(main())
