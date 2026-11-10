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
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).parent))
from uses import as_dict, uses_in_tree

from deptime.config import load_coverage

MAX_USES = 40


# The names huggingface_hub binds at its top level to a method of its module-level `HfApi()` (`list_models =
# api.list_models` in `hf_api.py`), read from the wheels of 0.12.0, 0.17.0, 0.20.0, 0.26.0, 0.30.0, 0.36.0, 1.0.0 and
# 2.4.0 (DT-DATA-66).
_HF_BOUND = """
    accept_access_request add_collection_item add_space_secret add_space_variable auth_check batch_bucket_files
    bucket_info cancel_access_request cancel_job change_discussion_status comment_discussion copy_files
    create_branch create_bucket create_collection create_commit create_commits_on_pr create_discussion
    create_inference_endpoint create_inference_endpoint_from_catalog create_pull_request create_repo
    create_scheduled_job create_scheduled_uv_job create_tag create_webhook dataset_info delete_branch
    delete_bucket delete_collection delete_collection_item delete_file delete_files delete_folder
    delete_inference_endpoint delete_repo delete_scheduled_job delete_space_secret delete_space_storage
    delete_space_variable delete_space_volumes delete_tag delete_webhook disable_space_dev_mode disable_webhook
    download_bucket_files duplicate_repo duplicate_space edit_discussion_comment enable_space_dev_mode
    enable_webhook fetch_job_logs fetch_job_metrics fetch_space_logs file_exists get_bucket_file_metadata
    get_bucket_paths_info get_collection get_dataset_leaderboard get_dataset_tags get_discussion_details
    get_full_repo_name get_inference_endpoint get_model_tags get_organization_overview get_paths_info
    get_repo_discussions get_safetensors_metadata get_sharded_safetensors_metadata get_space_runtime
    get_space_secrets get_space_variables get_token_permission get_user_overview get_webhook get_zero_gpu_quota
    grant_access inspect_job inspect_scheduled_job kernel_info like list_accepted_access_requests
    list_bucket_tree list_buckets list_collections list_daily_papers list_dataset_parquet_files list_datasets
    list_files_info list_inference_catalog list_inference_endpoints list_inference_endpoints_hardware list_jobs
    list_jobs_hardware list_lfs_files list_liked_repos list_metrics list_models list_organization_followers
    list_organization_members list_papers list_pending_access_requests list_rejected_access_requests
    list_repo_commits list_repo_files list_repo_likers list_repo_refs list_repo_tree list_scheduled_jobs
    list_space_templates list_spaces list_spaces_hardware list_user_followers list_user_following
    list_user_repos list_webhooks merge_pull_request model_info move_bucket move_repo paper_info
    parse_safetensors_file_metadata pause_inference_endpoint pause_space permanently_delete_lfs_files
    preupload_lfs_files read_paper reject_access_request rename_discussion repo_exists repo_info
    request_space_hardware request_space_storage rerun_job resolve_revision restart_space
    resume_inference_endpoint resume_scheduled_job revision_exists run_as_future run_job run_uv_job
    scale_to_zero_inference_endpoint search_spaces set_access_token set_space_sleep_time set_space_volumes
    space_info super_squash_history suspend_scheduled_job sync_bucket sync_job_volume trigger_scheduled_job
    unlike unset_access_token update_bucket_settings update_collection_item update_collection_metadata
    update_collection_resource_group update_inference_endpoint update_job_expose update_job_labels
    update_repo_settings update_repo_visibility update_scheduled_job_labels update_scheduled_job_schedule
    update_webhook upload_attachment upload_file upload_folder upload_large_folder verify_repo_checksums
    wait_for_job wait_for_space whoami
"""
HF_BOUND = frozenset(_HF_BOUND.split())
POLARS_ERROR = re.compile(r"polars\.[A-Z]\w*(Error|Warning)")


# Uses of fixes the set before didn't exercise (from 2026-11d, DT-QA-10). For each package here, `find_pinned.py
# --exercise` picks up to `--per` more repositories (none of the main stratum's) whose code has such a use and whose pin
# is below the bound, and `capped` keeps those uses. Each entry: (what it exercises, pin bound or None, the use it
# needs). 2026-11d's rules were mcp.types before 2.0.0, crewai's `Agent` and `Task` with keywords, and a module dunder
# of numpy, pandas, polars or pydantic; 2026-11e's (DT-QA-11) are below.
EXERCISE = {
    "huggingface-hub": (
        "a call with keywords to a top-level function bound to an HfApi method, such as list_models (DT-DATA-66)",
        None,
        lambda u: (
            u["kind"] == "call"
            and u["path"].startswith("huggingface_hub.")
            and u["path"].removeprefix("huggingface_hub.") in HF_BOUND
            and bool(u.get("kwargs"))
        ),
    ),
    "polars": (
        "a top-level exception or warning, such as polars.ComputeError, pinned before 2.0.0 (DT-DATA-64)",
        "2.0.0",
        lambda u: POLARS_ERROR.fullmatch(u["path"]) is not None and "via" not in u,
    ),
}
COV = {canonicalize_name(p.name): p for p in load_coverage().packages}
_pypi: dict[str, dict] = {}


def fetch(url: str, timeout: float) -> bytes:
    """The body at `url`, retrying a network error (a DNS lookup that fails for a moment) three times."""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except OSError:  # URLError, socket errors, timeouts
            if attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))
    raise AssertionError("unreachable")


def pypi_releases(name: str) -> dict[str, datetime]:
    """Final releases and their first upload time, from PyPI's JSON API."""
    if name not in _pypi:
        doc = json.loads(fetch(f"https://pypi.org/pypi/{name}/json", 60))
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
