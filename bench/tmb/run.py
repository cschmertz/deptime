"""Reproduce the tracebacks behind bench/tmb/cases (DT-QA-04): run each in-scope repository's tests in its
TimeMachine-bench migration environment and keep the logs.

    python3 -I bench/tmb/run.py <timemachine-bench>/benchmark/data/v1/timemachine-bench-verified.jsonl \
        bench/tmb/triage.json <runs_dir> [--jobs N] [--only owner/repo,...] [--mem 1200m]

The host only clones and patches; no repository code runs on it. Each repository runs in a throwaway
python:3.12.11 container with no host mounts: setup (the dataset's script, with its recorded dependency versions as
pip constraints) has network, the tests run with the network disconnected. Needs git, patch and Docker."""

import argparse
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IMAGE = "python:3.12.11"
BUILD_CAPS = "setuptools<=80.9.0\nwheel<=0.45.1\n"  # what the 2025-07-31 date filter would have allowed

ap = argparse.ArgumentParser()
ap.add_argument("data")
ap.add_argument("triage")
ap.add_argument("runs")
ap.add_argument("--jobs", type=int, default=3)
ap.add_argument("--only", default="")
ap.add_argument("--mem", default="1200m")
a = ap.parse_args()
with open(a.data, encoding="utf-8") as f:
    rows = {json.loads(line)["repo_name"]: json.loads(line) for line in f}
repos = [
    t["repo"]
    for t in json.loads(Path(a.triage).read_text())
    if t["category"] in ("in_scope", "in_scope_mixed")
]
if a.only:
    repos = [r for r in repos if r in a.only.split(",")]
runs = Path(a.runs)


def sh(cmd, log=None, timeout=None, **kw):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out = (
            "timeout",
            ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            + "\n[TIMEOUT]",
        )
    if log:
        Path(log).write_text(out)
    return rc, out, round(time.time() - t0)


def prep(repo, d):
    r = rows[repo]
    name = repo.replace("/", "__")
    src = d / "src"
    if src.exists():
        shutil.rmtree(src)
    rc, out, _ = sh(
        ["git", "clone", "-q", "--filter=blob:none", "--no-checkout", r["repo_url"], str(src)],
        timeout=600,
        env={
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_LFS_SKIP_SMUDGE": "1",
        },
    )
    if rc != 0:
        return f"clone failed: {out[-300:]}"
    rc, out, _ = sh(
        ["git", "-C", str(src), "-c", "core.hooksPath=/dev/null", "checkout", "-q", r["commit_hash"]],
        timeout=600,
        env={
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_LFS_SKIP_SMUDGE": "1",
        },
    )
    if rc != 0:
        return f"checkout failed: {out[-300:]}"
    shutil.rmtree(src / ".git")
    for kind, text in (("init", r["patch"]), ("test", r["test_patch"])):
        if not text:
            continue
        pf = d / f"{kind}.patch"
        pf.write_text(text)
        rc, out, _ = sh(
            ["patch", "-p0", "-t", "-E", "-d", str(src), "-i", str(pf)],
            log=d / f"patch-{kind}.log",
            timeout=60,
        )
        if rc != 0:
            return f"{kind} patch failed: {out[-300:]}"
    pins = "\n".join(x for x in r["dependency_versions"].splitlines() if "==" in x) + "\n"
    names = {x.split("==")[0].lower() for x in pins.splitlines() if x}
    caps = "".join(c + "\n" for c in BUILD_CAPS.split() if c.split("<")[0] not in names)
    (d / "constraints.txt").write_text(pins + caps)
    for s in (f"setup_{name}.sh", f"test_{name}.sh"):
        if not (src / s).exists():
            return f"missing {s}"
    return None


def run(repo):
    name = repo.replace("/", "__")
    d = runs / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"repo": repo, "commit": rows[repo]["commit_hash"], "image": IMAGE}
    err = prep(repo, d)
    if err:
        meta["error"] = err
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        return repo, err
    cname = f"tmb-{name.lower()}"[:60].replace(".", "-")
    sh(["docker", "rm", "-f", cname])
    rc, out, _ = sh(
        [
            "docker",
            "run",
            "-d",
            "--name",
            cname,
            "--label",
            "deptime-tmb=1",
            "--memory",
            a.mem,
            "--cpus",
            "2",
            "--pids-limit",
            "1024",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "NET_RAW",
            "--cap-drop",
            "MKNOD",
            "-w",
            "/work",
            IMAGE,
            "sleep",
            "infinity",
        ],
        timeout=120,
    )
    if rc != 0:
        meta["error"] = f"docker run: {out[-300:]}"
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        return repo, meta["error"]
    try:
        sh(["docker", "cp", f"{d / 'src'}/.", f"{cname}:/work"], timeout=300)
        sh(["docker", "exec", cname, "mkdir", "-p", "/tmb"])
        sh(["docker", "cp", str(d / "constraints.txt"), f"{cname}:/tmb/constraints.txt"])
        rc, _, secs = sh(
            [
                "docker",
                "exec",
                "-e",
                "PIP_CONSTRAINT=/tmb/constraints.txt",
                "-e",
                "PIP_DISABLE_PIP_VERSION_CHECK=1",
                "-e",
                "PIP_ROOT_USER_ACTION=ignore",
                "-e",
                "PIP_NO_INPUT=1",
                cname,
                "bash",
                f"/work/setup_{name}.sh",
            ],
            log=d / "setup.log",
            timeout=1800,
        )
        meta.update(setup_rc=rc, setup_secs=secs)
        sh(["docker", "exec", cname, "pip", "freeze"], log=d / "freeze.txt", timeout=120)
        sh(["docker", "network", "disconnect", "bridge", cname], timeout=60)
        _, net, _ = sh(
            [
                "docker",
                "exec",
                cname,
                "python",
                "-c",
                "import socket;socket.setdefaulttimeout(3)\ntry:\n socket.create_connection(('pypi.org',443));print('NET-ON')\nexcept Exception as e:print('NET-OFF',type(e).__name__)",
            ],
            timeout=60,
        )
        meta["net_check"] = net.strip()
        rc, _, secs = sh(
            ["docker", "exec", cname, "bash", f"/work/test_{name}.sh"], log=d / "test.log", timeout=1200
        )
        meta.update(test_rc=rc, test_secs=secs)
    finally:
        sh(["docker", "rm", "-f", cname], timeout=120)
        shutil.rmtree(d / "src", ignore_errors=True)
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    return repo, f"setup={meta.get('setup_rc')} test={meta.get('test_rc')} {meta.get('net_check')}"


with ThreadPoolExecutor(a.jobs) as ex:
    for repo, status in ex.map(run, repos):
        print(f"{repo:44} {status}", flush=True)
