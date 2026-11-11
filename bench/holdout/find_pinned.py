"""Pick the pinned-repository upgrades of a holdout set (DT-QA-07) by the rule in bench/holdout/README.md. Nothing here
runs DepTime or any project's code; sources are downloaded as tarballs and only parsed.

    uv run python bench/holdout/find_pinned.py <packages, comma-separated> <sources dir> <pinned.json> [--per 2]
        [--cutoff YYYY-MM-DD] [--exclude <earlier set dir>,...] [--exercise]

For each package, in GitHub code search's order for `<package>== filename:requirements.txt`, a repository qualifies
when it isn't a fork and isn't the package itself, its requirements file pins a final release at or after the
package's first indexed release and older than its newest release on PyPI, and its source at the default branch's head
uses the package at least MIN_USES times (bench/holdout/uses.py). The first `--per` qualifying repositories are kept,
each asked about two moves: an upgrade from the pin to the newest release (`from` = pin), and the same code on the
newest release at least a year older than the pin, when that is indexed (`from` = pin, `to` = older: the
"not yet added" direction). A repository an earlier set already asked about (`--exclude`) doesn't qualify, so each set
is new code.

`--exercise` (DT-QA-10) picks the exercise stratum, for the packages in collect.EXERCISE: by the same search and rules,
the first `--per` repositories that aren't in an earlier set or the main stratum (its pinned.json in `--exclude`),
whose pin is below the rule's bound and whose code has the use the rule needs (for 2026-11f: huggingface_hub's
`Repository` before 1.0.0, langchain-core's `RunnableWithMessageHistory`). Their ids start `ex-`.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
import tarfile
import time
from datetime import UTC, timedelta
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).parent))
from collect import COV, EXERCISE, fetch, pypi_releases
from uses import as_dict, uses_in_tree

MIN_USES = 3
FIRST = {  # first indexed release per package (`deptime coverage`, 2026-11-07)
    "aiohttp": "3.8.0", "anthropic": "0.2.2", "celery": "5.3.0", "chromadb": "0.1.0", "click": "8.1.0",
    "crewai": "0.1.0", "django": "3.2", "dspy": "0.0.1", "fastapi": "0.89.0", "fastmcp": "0.1.0", "flask": "2.2.0",
    "google-genai": "0.7.0", "google-generativeai": "0.1.0", "gradio": "3.16.0", "httpx": "0.23.0",
    "huggingface-hub": "0.12.0", "instructor": "0.2.1", "langchain": "0.0.1", "langchain-community": "0.0.1",
    "langchain-core": "0.0.1", "langchain-openai": "0.0.2", "langgraph": "0.0.9", "litellm": "0.1.0",
    "llama-index-core": "0.9.41", "marshmallow": "3.20.0", "matplotlib": "3.6.0", "mcp": "0.9.1",
    "mistralai": "0.0.7", "numpy": "1.24.0", "ollama": "0.0.0", "openai": "0.26.0", "openai-agents": "0.0.2",
    "pandas": "1.5.0", "pinecone": "2.2.2", "polars": "0.15.1", "pydantic": "1.10.0", "pydantic-ai-slim": "0.0.6",
    "pydantic-settings": "1.99", "pymongo": "4.4.0", "pytest": "7.2.0", "qdrant-client": "0.11.0", "redis": "4.3.0",
    "scikit-learn": "1.2.0", "selenium": "4.8.0", "sqlalchemy": "1.4.0", "sqlmodel": "0.0.1", "starlette": "0.24.0",
    "streamlit": "1.17.0", "transformers": "4.26.0", "typer": "0.8.0", "urllib3": "1.26.0", "weaviate-client": "3.11.0",
    "werkzeug": "2.2.0",
}  # fmt: skip
CUTOFF = "2026-11-07"  # the set's cutoff (--cutoff): newest releases as of this day


def _until_reset() -> float:
    """Seconds until the exhausted GitHub rate limits reset (`gh api rate_limit` costs no quota), at most an hour."""
    try:
        doc = json.loads(subprocess.run(["gh", "api", "rate_limit"], capture_output=True, text=True).stdout)
        resets = [r["reset"] for r in doc["resources"].values() if r.get("remaining") == 0]
    except (ValueError, KeyError, TypeError):
        resets = []
    return min(3600.0, max([r - time.time() for r in resets], default=0.0)) + 5


def gh(*args: str) -> str:
    """A gh call; a rate limit waits for its reset (the core quota is shared with whatever else uses the token), so a
    package is never logged as an error for it."""
    for attempt in range(8):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        if "rate limit" in p.stderr.lower() or "secondary" in p.stderr.lower():
            time.sleep(max(30 * (attempt + 1), _until_reset()))
            continue
        raise RuntimeError(p.stderr.strip()[:300])
    raise RuntimeError("rate limited")


def pin_in(text: str, package: str) -> str | None:
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*==\s*([^\s;#,]+)", line)
        if m and canonicalize_name(m.group(1)) == package:
            return m.group(3)
    return None


def fetch_source(repo: str, sha: str, dest: Path) -> None:
    if dest.exists():
        return
    data = fetch(f"https://codeload.github.com/{repo}/tar.gz/{sha}", 300)
    dest.mkdir(
        parents=True
    )  # after the download, so one that fails leaves nothing to read as an empty project
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith((".py", ".txt", ".toml", ".cfg")):
                continue
            rel = Path(*Path(m.name).parts[1:])
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(m)
            if f is not None:
                out.write_bytes(f.read())


def older_target(package: str, pin: str) -> str | None:
    rels = pypi_releases(package)
    cutoff = rels[pin] - timedelta(days=365)
    first = Version(FIRST[package])
    older = [(Version(v), v) for v, t in rels.items() if t <= cutoff and Version(v) >= first]
    return max(older)[1] if older else None


def newest(package: str) -> str:
    from datetime import datetime

    end = datetime.fromisoformat(CUTOFF).replace(tzinfo=UTC)
    return max((Version(v), v) for v, t in pypi_releases(package).items() if t < end)[1]


def earlier_repos(sets: list[Path]) -> set[str]:
    """Repositories an earlier set asked about (a set dir), or a pinned.json picked (this set's main stratum, for
    `--exercise`), as owner/name in lower case."""
    found = set()
    for d in sets:
        if d.suffix == ".json":
            repos = [r["repo"] for r in json.loads(d.read_text(encoding="utf-8"))["repos"]]
        else:
            lines = (d / "upgrades.jsonl").read_text(encoding="utf-8").splitlines()
            repos = [json.loads(line)["repo"] for line in lines if line.strip()]
        found.update(r.removeprefix("https://github.com/").lower() for r in repos)
    return found


# The first 100 results, then up to 500 when those didn't give `--per` repositories: two sets on, earlier sets have
# asked about many of the first ones, and pymongo and urllib3 had one qualifying repository in 100 (DT-QA-09).
SEARCH_LIMITS = (100, 500)


def _hits(package: str):
    """Code search hits for `<package>==` in requirements files, in its order, each repository once per call."""
    done = 0
    for limit in SEARCH_LIMITS:
        hits = json.loads(
            gh(
                "search",
                "code",
                f"{package}==",
                "--filename",
                "requirements.txt",
                "--limit",
                str(limit),
                "--json",
                "repository,path",
            )
        )
        time.sleep(7)  # code search allows 10 requests a minute
        yield from hits[done:]
        done = len(hits)


def candidates(
    package: str, sources: Path, per: int, log: list, skip: set[str] = frozenset(), exercise: bool = False
) -> list[dict]:
    """The first `per` qualifying repositories; with `exercise`, only those that also have the use EXERCISE names
    for the package, pinned below its bound (DT-QA-10)."""
    spec = COV[package]
    rule = EXERCISE[package] if exercise else None
    roots = tuple(spec.import_names)
    latest = newest(package)
    kept: list[dict] = []
    seen: set[str] = set()
    for h in _hits(package):
        repo = h["repository"]["nameWithOwner"]
        if repo in seen:
            continue
        seen.add(repo)
        why = None
        if h["repository"].get("isFork"):
            why = "fork"
        elif repo.lower() in skip:
            why = "in an earlier set"
        if why is None:
            info = json.loads(gh("api", f"repos/{repo}"))
            branch = info["default_branch"]
            sha = json.loads(gh("api", f"repos/{repo}/commits/{branch}"))["sha"]
            try:
                raw = json.loads(gh("api", f"repos/{repo}/contents/{h['path']}?ref={sha}"))
                text = base64.b64decode(raw["content"]).decode("utf-8", errors="replace")
            except (RuntimeError, KeyError, TypeError, ValueError):
                text = ""
            pin = pin_in(text, package)
            try:
                ok = pin is not None and Version(FIRST[package]) <= Version(pin) < Version(latest)
                ok = ok and not Version(pin).is_prerelease and pin in pypi_releases(package)
                ok = ok and (rule is None or rule[1] is None or Version(pin) < Version(rule[1]))
            except InvalidVersion:
                ok = False
            if not ok:
                upper = latest if rule is None or rule[1] is None else min(latest, rule[1], key=Version)
                why = f"pin {pin!r} not in [{FIRST[package]}, {upper})"
        if why is None:
            src = sources / repo.replace("/", "__")
            fetch_source(repo, sha, src)
            own = any(
                (src / p / r.replace(".", "/") / "__init__.py").exists() for p in ("", "src") for r in roots
            )
            found = [
                as_dict(u)
                for u in uses_in_tree(src, roots)
                if not any(r == u.path or r.startswith(u.path + ".") for r in roots)
            ]
            if own:
                why = "the package's own source"
            elif len(found) < MIN_USES:
                why = f"{len(found)} uses"
            elif rule is not None and not any(rule[2](u) for u in found):
                why = f"no use for the exercise: {rule[0]}"
        log.append({"package": package, "repo": repo, "path": h["path"], "skipped": why})
        if why is not None:
            continue
        slug = repo.replace("/", "__").lower()
        base = {
            "repo": f"https://github.com/{repo}",
            "commit": sha,
            "pinned_in": h["path"],
            "package": package,
        }
        prefix = "pin"
        if rule is not None:
            base.update(stratum="exercise", exercise=rule[0])
            prefix = "ex"
        kept.append({**base, "id": f"{prefix}-{slug}-{package}-up", "from": pin, "to": latest})
        if (older := older_target(package, pin)) is not None:
            kept.append({**base, "id": f"{prefix}-{slug}-{package}-down", "from": pin, "to": older})
        if len({k["repo"] for k in kept}) >= per:
            break
    return kept


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("packages")
    ap.add_argument("sources", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--per", type=int, default=2)
    ap.add_argument("--cutoff", default=CUTOFF)
    ap.add_argument(
        "--exclude", default="", help="earlier set dirs (or this set's pinned.json), comma-separated"
    )
    ap.add_argument(
        "--exercise", action="store_true", help="the exercise stratum: collect.EXERCISE's rule per package"
    )
    a = ap.parse_args()
    globals()["CUTOFF"] = a.cutoff
    skip = earlier_repos([Path(x) for x in a.exclude.split(",") if x])
    doc = json.loads(a.out.read_text()) if a.out.exists() else {"repos": [], "log": []}
    done = {r["package"] for r in doc["repos"]} | {x["package"] for x in doc["log"]}
    for package in [canonicalize_name(p) for p in a.packages.split(",")]:
        if package in done:
            continue
        log: list = []
        try:
            doc["repos"].extend(candidates(package, a.sources, a.per, log, skip, a.exercise))
        except RuntimeError as e:
            log.append({"package": package, "error": str(e)})
        doc["log"].extend(log)
        a.out.write_text(json.dumps(doc, indent=1) + "\n")
        print(package, sum(1 for r in doc["repos"] if r["package"] == package), file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
