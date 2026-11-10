"""Label each use of a holdout set from the probe's results (DT-QA-07), by the rules in bench/holdout/README.md.
Nothing here asks DepTime anything.

    uv run python bench/holdout/labels.py <set dir> <probe artifacts dir>

Reads <set dir>/upgrades.jsonl and envs.json and every <shard>.jsonl under the artifacts dir; writes
<set dir>/labels.jsonl, one line per upgrade with each use's label:

- `works`: the use resolves (and its call shape binds) in `to`.
- `breaks`: it doesn't, with the change type: `removed` (the name doesn't resolve), `param_removed` (a keyword the call
  passes isn't accepted), `param_now_required` (a required argument the call doesn't pass), `positional` (too many
  positional arguments, or a keyword that became positional-only), `symbol_not_yet_added` and `param_not_yet_added`
  (the same, where `to` is older than `from`).
- `excluded`, with the reason: the probe can't tell (an import failing for another reason, no signature, a keyword
  only `**kwargs` takes), the use doesn't work in `from` either, or (TimeMachine-bench) its break can't be confirmed.

Uses listed in <set dir>/label-overrides.json (checked by hand against the library's source) are excluded.

A name that resolves to an object built to fail when called (probe.py's `built_to_fail`: a function, or an instance's
`__call__`, whose body is one `raise`) doesn't work for a call, nor for a name the project also calls (DT-QA-10).

`deprecated` is true when the probe found evidence in `to` that the name itself is deprecated (probe.py's
`deprecation_evidence`): reading it raises a deprecation warning that names it, the object carries its own
`__deprecated__`, a deprecation wrapper sits on it (the package's own `@deprecated`), or its docstring has a
deprecation note about it rather than one of its parameters. Sets probed before DT-QA-08 (2026-11) took any
deprecation warning, `__deprecated__` and any docstring note.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from envs import TMB_DATE, exclude_newer, set_cutoff, use_key


def env_id(package: str, version: str, tmb: bool) -> str:
    return f"{package}=={version}@{TMB_DATE if tmb else exclude_newer(package, version)}"


def works(r: dict, use: dict, called: bool = False) -> tuple[bool | None, str]:
    """(works?, why) for one probe result; None when the probe can't tell. `called`: the project also calls the name,
    so a name that resolves to an object built to fail when called (openai 1.x's `ChatCompletion` proxy) doesn't
    work for it (DT-QA-10)."""
    if r is None:
        return None, "no probe result"
    if r.get("resolved") is None:
        return None, f"probe can't tell: {r.get('error', '')[:120]}"
    if use.get("via") == "instance" and r.get("resolved") and not r.get("builder_is_class"):
        return None, "built by a call that isn't a class"
    if r["resolved"] is False:
        return False, "removed"
    if r.get("raises_when_called") and (use["kind"] == "call" or called):
        return False, "removed"
    if use["kind"] != "call":
        return True, ""
    if r.get("binds") is None:
        return None, f"no signature: {r.get('bind_error', '')[:120]}"
    if r["binds"]:
        return True, ""
    err = r.get("bind_error", "")
    if "unexpected keyword argument" in err:
        return False, "param_removed"
    if "missing a required" in err:
        return False, "param_now_required"
    if "too many positional" in err or "positional only" in err or "positional-only" in err:
        return False, "positional"
    return False, "other"


def var_keyword_only(r: dict | None) -> set[str]:
    return {k for k, v in ((r or {}).get("kwargs") or {}).items() if v == "var_keyword"}


def deprecated(r: dict | None) -> bool:
    return bool(r and r.get("resolved") and r.get("deprecation"))


def label(row: dict, use: dict, res: dict[str, dict[str, dict]]) -> dict:
    k = use_key(use)
    tmb = row["source"] == "tmb"
    called = use["kind"] == "name" and any(
        u["kind"] == "call" and u["path"] == use["path"] for u in row["uses"]
    )
    to = res.get(env_id(row["package"], row["to"], tmb), {}).get(k)
    out = {"key": k, "kind": use["kind"], "path": use["path"]}
    for f in ("npos", "kwargs", "via"):
        if f in use:
            out[f] = use[f]
    if tmb:
        leaf = use["path"].rsplit(".", 1)[-1]
        if any(re.search(rf"\b{re.escape(leaf)}\b", line) for line in row.get("gold_patch_removed", [])):
            return {**out, "label": "excluded", "why": "named in the gold patch"}
        ok, why = works(to, use, called)
        if ok is None:
            return {**out, "label": "excluded", "why": why}
        if not ok:
            return {
                **out,
                "label": "excluded",
                "why": f"fails in to ({why}) but the tests passed: unexercised or not a real path",
            }
        if use["kind"] == "call" and var_keyword_only(to):
            return {**out, "label": "excluded", "why": "a keyword only **kwargs takes"}
        return {**out, "label": "works", "deprecated": deprecated(to)}
    fr = res.get(env_id(row["package"], row["from"], False), {}).get(k)
    ok, why = works(fr, use, called)
    if ok is None:
        return {**out, "label": "excluded", "why": f"from: {why}"}
    if not ok:
        return {**out, "label": "excluded", "why": f"doesn't work in from ({why})"}
    if use["kind"] == "call" and var_keyword_only(fr):
        return {**out, "label": "excluded", "why": "from: a keyword only **kwargs takes"}
    ok, why = works(to, use, called)
    if ok is None:
        return {**out, "label": "excluded", "why": f"to: {why}"}
    if ok:
        if use["kind"] == "call" and var_keyword_only(to):
            return {**out, "label": "excluded", "why": "to: a keyword only **kwargs takes"}
        return {**out, "label": "works", "deprecated": deprecated(to)}
    if use.get("via") == "instance" and why == "removed" and (to or {}).get("set_on_instances"):
        # gradio 3 sets `click` on each instance in an event mixin's __init__ (2026-11b's label errors, DT-QA-09).
        return {
            **out,
            "label": "excluded",
            "why": "to: an attribute each instance sets (self.<name> = in the MRO)",
        }
    if (
        use.get("via") == "instance"
        and why == "removed"
        and (fr or {}).get("objkind") not in ("function", "class")
    ):
        # A property or attribute on the class in `from` can be an attribute each instance sets in `to`'s __init__,
        # which reading it off the class can't see (openai's `OpenAI.chat` before 1.x's cached properties).
        return {**out, "label": "excluded", "why": "to: an attribute that may be set on instances"}
    down = _older(row)
    change = why
    if down and why == "removed":
        change = "symbol_not_yet_added"
    elif down and why == "param_removed":
        change = "param_not_yet_added"
    detail = (to or {}).get("bind_error") or (to or {}).get("error") or ""
    return {**out, "label": "breaks", "change": change, "detail": detail[:200]}


def _older(row: dict) -> bool:
    from packaging.version import Version

    return Version(row["to"]) < Version(row["from"])


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    root, arts = Path(sys.argv[1]), Path(sys.argv[2])
    set_cutoff(root)
    res: dict[str, dict[str, dict]] = {}
    envs_meta: dict[str, dict] = {}
    for f in sorted(arts.rglob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            envs_meta[rec["id"]] = {
                k: rec.get(k)
                for k in ("result", "python", "requires", "source_builds_allowed")
                if rec.get(k) is not None
            }
            if rec.get("result") == "ok":
                res.setdefault(rec["id"], {}).update({r["key"]: r for r in rec["results"]})
    rows = [
        json.loads(x) for x in (root / "upgrades.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    overrides_file = root / "label-overrides.json"
    overrides = (
        {
            (o["upgrade"], o["key"]): o
            for o in json.loads(overrides_file.read_text(encoding="utf-8"))["overrides"]
        }
        if overrides_file.exists()
        else {}
    )
    with open(root / "labels.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            items = [label(row, u, res) for u in row["uses"]]
            for it in items:
                if (o := overrides.get((row["id"], it["key"]))) is not None:
                    it.update(label="excluded", why=f"checked by hand: {o['why']}")
                    it.pop("change", None)
                    it.pop("detail", None)
            f.write(json.dumps({"id": row["id"], "items": items}) + "\n")
    (root / "probe-envs.json").write_text(
        json.dumps(envs_meta, indent=0, sort_keys=True) + "\n", encoding="utf-8"
    )
    counts: dict[str, int] = {}
    for line in (root / "labels.jsonl").read_text(encoding="utf-8").splitlines():
        for it in json.loads(line)["items"]:
            counts[it["label"]] = counts.get(it["label"], 0) + 1
    failed = sorted(e for e, m in envs_meta.items() if m.get("result") != "ok")
    print(f"labels: {counts}; {len(failed)} environments failed to install: {failed}", file=sys.stderr)


if __name__ == "__main__":
    main()
