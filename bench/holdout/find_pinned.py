"""Pick the pinned-repository upgrades of a holdout set (DT-QA-07) by the rule in bench/holdout/README.md. Nothing here
runs DepTime or any project's code; sources are downloaded as tarballs and only parsed.

    uv run python bench/holdout/find_pinned.py <packages, comma-separated> <sources dir> <pinned.json> [--per 2]
        [--cutoff YYYY-MM-DD] [--exclude <earlier set dir>,...]

For each package, in GitHub code search's order for `<package>== filename:requirements.txt`, a repository qualifies
when it isn't a fork and isn't the package itself, its requirements file pins a final release at or after the
package's first indexed release and older than its newest release on PyPI, and its source at the default branch's head
uses the package at least MIN_USES times (bench/holdout/uses.py). The first `--per` qualifying repositories are kept,
each asked about two moves: an upgrade from the pin to the newest release (`from` = pin), and the same code on the
newest release at least a year older than the pin, when that is indexed (`from` = pin, `to` = older: the
"not yet added" direction). A repository an earlier set already asked about (`--exclude`) doesn't qualify, so each set
is new code.
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
import urllib.request
from datetime import UTC, timedelta
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).parent))
from collect import COV, pypi_releases
from uses import uses_in_tree

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


def gh(*args: str) -> str:
    for attempt in range(6):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        if "rate limit" in p.stderr.lower() or "secondary" in p.stderr.lower():
            time.sleep(30 * (attempt + 1))
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
    dest.mkdir(parents=True)
    with urllib.request.urlopen(f"https://codeload.github.com/{repo}/tar.gz/{sha}", timeout=300) as r:
        data = r.read()
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
    """Repositories an earlier set asked about, as owner/name in lower case."""
    found = set()
    for d in sets:
        for line in (d / "upgrades.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                found.add(json.loads(line)["repo"].removeprefix("https://github.com/").lower())
    return found


def candidates(package: str, sources: Path, per: int, log: list, skip: set[str] = frozenset()) -> list[dict]:
    spec = COV[package]
    roots = tuple(spec.import_names)
    hits = json.loads(
        gh(
            "search",
            "code",
            f"{package}==",
            "--filename",
            "requirements.txt",
            "--limit",
            "100",
            "--json",
            "repository,path",
        )
    )
    time.sleep(7)  # code search allows 10 requests a minute
    latest = newest(package)
    kept: list[dict] = []
    seen: set[str] = set()
    for h in hits:
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
            except InvalidVersion:
                ok = False
            if not ok:
                why = f"pin {pin!r} not in [{FIRST[package]}, {latest})"
        if why is None:
            src = sources / repo.replace("/", "__")
            fetch_source(repo, sha, src)
            own = any(
                (src / p / r.replace(".", "/") / "__init__.py").exists() for p in ("", "src") for r in roots
            )
            n = sum(
                not any(r == u.path or r.startswith(u.path + ".") for r in roots)
                for u in uses_in_tree(src, roots)
            )
            if own:
                why = "the package's own source"
            elif n < MIN_USES:
                why = f"{n} uses"
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
        kept.append({**base, "id": f"pin-{slug}-{package}-up", "from": pin, "to": latest})
        if (older := older_target(package, pin)) is not None:
            kept.append({**base, "id": f"pin-{slug}-{package}-down", "from": pin, "to": older})
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
    ap.add_argument("--exclude", default="", help="earlier set dirs, comma-separated")
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
            doc["repos"].extend(candidates(package, a.sources, a.per, log, skip))
        except RuntimeError as e:
            log.append({"package": package, "error": str(e)})
        doc["log"].extend(log)
        a.out.write_text(json.dumps(doc, indent=1) + "\n")
        print(package, sum(1 for r in doc["repos"] if r["package"] == package), file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
