"""Build a holdout set's upgrades (DT-QA-07) from project sources: which names and call shapes each project uses from
a covered package, and the upgrade it is asked about. Nothing here runs DepTime or any project's code.

    uv run python bench/holdout/collect.py tmb <timemachine-bench verified.jsonl> <sources dir> <out.jsonl>
    uv run python bench/holdout/collect.py pinned <pinned.json> <sources dir> <out.jsonl>

<sources dir> holds each project's source at its commit, as <owner>__<repo>/ (downloaded tarballs, read as data).

- `tmb`: each TimeMachine-bench verified repository, for each covered package its migrated environment pins. `to` is
  that pin (the 2025-07-31 environment, Python 3.12), `from` the package's newest release on PyPI on or before the
  repository's reproduction date (for the record; the probe doesn't install it).
- `pinned`: repositories that pin a covered package; pinned.json lists each with its commit, package, pinned version
  (`from`) and the version it is asked about (`to`).

The package's root module isn't a use (it is never removed). Each upgrade keeps at most MAX_USES uses, chosen by the sha256 of the use, so one large project can't dominate.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).parent))
from uses import as_dict, uses_in_tree

from deptime.config import load_coverage

MAX_USES = 40


def _module_dunder(u: dict) -> bool:
    parts = u["path"].split(".")
    return len(parts) == 2 and parts[1].startswith("__") and parts[1].endswith("__") and "via" not in u


# DT-QA-10: uses of fixes the set before didn't exercise. For each package here, `find_pinned.py --exercise` picks up
# to `--per` more repositories (none of the main stratum's) whose code has such a use and whose pin is below the bound,
# and `capped` keeps those uses. Each entry: (what it exercises, pin bound or None, the use it needs).
EXERCISE = {
    "mcp": (
        "a name under mcp.types, pinned before 2.0.0 (DT-DATA-56)",
        "2.0.0",
        lambda u: u["path"].startswith("mcp.types."),
    ),
    "crewai": (
        "a call to crewai.Agent or crewai.Task with keywords (DT-CORE-39)",
        None,
        lambda u: (
            u["kind"] == "call" and u["path"] in ("crewai.Agent", "crewai.Task") and bool(u.get("kwargs"))
        ),
    ),
    **{
        p: ("a module dunder of the package, such as __version__ (DT-CORE-38)", None, _module_dunder)
        for p in ("numpy", "pandas", "polars", "pydantic")
    },
}
COV = {canonicalize_name(p.name): p for p in load_coverage().packages}
_pypi: dict[str, dict] = {}


def pypi_releases(name: str) -> dict[str, datetime]:
    """Final releases and their first upload time, from PyPI's JSON API."""
    if name not in _pypi:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=60) as r:
            doc = json.load(r)
        out = {}
        for v, files in doc["releases"].items():
            try:
                pv = Version(v)
            except InvalidVersion:
                continue
            if pv.is_prerelease or pv.is_devrelease or not files:
                continue
            out[v] = min(
                datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00")) for f in files
            )
        _pypi[name] = out
    return _pypi[name]


def newest_on_or_before(name: str, day: str) -> str | None:
    cutoff = datetime.fromisoformat(day).replace(tzinfo=UTC).replace(hour=23, minute=59, second=59)
    rels = [(Version(v), v) for v, t in pypi_releases(name).items() if t <= cutoff]
    return max(rels)[1] if rels else None


def capped(uses: list[dict], upgrade_id: str, first=None) -> tuple[list[dict], int]:
    """At most MAX_USES uses, by hash; `first` (an exercise rule's use test) picks uses kept before the others."""

    def h(u: dict) -> str:
        key = json.dumps([upgrade_id, u["kind"], u["path"], u.get("npos"), u.get("kwargs"), u.get("via")])
        return hashlib.sha256(key.encode()).hexdigest()

    if len(uses) <= MAX_USES:
        return uses, len(uses)
    order = sorted(range(len(uses)), key=lambda i: (not (first and first(uses[i])), h(uses[i])))
    keep = set(order[:MAX_USES])
    return [u for i, u in enumerate(uses) if i in keep], len(uses)


def upgrade(src: Path, base: dict, package: str) -> dict | None:
    spec = COV[package]
    roots = tuple(spec.import_names)
    # A package's root module is never removed, so it isn't asked about (nor a parent of it, `google`).
    uses = [
        as_dict(u)
        for u in uses_in_tree(src, roots)
        if not any(r == u.path or r.startswith(u.path + ".") for r in roots)
    ]
    if not uses:
        return None
    uid = base["id"]
    first = EXERCISE[package][2] if base.get("stratum") == "exercise" else None
    kept, total = capped(uses, uid, first)
    return {**base, "package": package, "tier": spec.tier, "uses_found": total, "uses": kept}


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def tmb(data: Path, sources: Path) -> list[dict]:
    out = []
    for line in data.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        pins = {
            canonicalize_name(x.split("==")[0]): x.split("==")[1].strip()
            for x in r["dependency_versions"].splitlines()
            if "==" in x
        }
        src = sources / slug(r["repo_name"])
        for package in sorted(p for p in pins if p in COV):
            base = {
                "id": f"tmb-{slug(r['repo_name']).lower()}-{package}",
                "source": "tmb",
                "repo": r["repo_url"],
                "commit": r["commit_hash"],
                "from": newest_on_or_before(package, r["reproduction_target_date"]),
                "from_date": r["reproduction_target_date"],
                "to": pins[package],
                "python": "3.12",
                "gold_patch_removed": [
                    ln[1:].strip()
                    for ln in r["gold_patch"].splitlines()
                    if ln.startswith("-") and not ln.startswith("---") and ln[1:].strip()
                ],
            }
            if (u := upgrade(src, base, package)) is not None:
                out.append(u)
    return out


def pinned(doc: Path, sources: Path) -> list[dict]:
    out = []
    for p in json.loads(doc.read_text(encoding="utf-8"))["repos"]:
        package = canonicalize_name(p["package"])
        base = {
            "id": p["id"],
            "source": "pinned",
            "repo": p["repo"],
            "commit": p["commit"],
            "pinned_in": p["pinned_in"],
            "from": p["from"],
            "to": p["to"],
            "python": p.get("python", "3.11"),
        }
        if p.get("stratum"):
            base.update(stratum=p["stratum"], exercise=p["exercise"])
        if (
            u := upgrade(sources / slug(p["repo"].removeprefix("https://github.com/")), base, package)
        ) is not None:
            out.append(u)
    return out


def main() -> None:
    if len(sys.argv) != 5 or sys.argv[1] not in ("tmb", "pinned"):
        sys.exit(__doc__)
    mode, data, sources, dest = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
    rows = tmb(data, sources) if mode == "tmb" else pinned(data, sources)
    dest.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"{len(rows)} upgrades, {sum(len(r['uses']) for r in rows)} uses -> {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
