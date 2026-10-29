"""S1 sandbox driver (DT-SPIKE-01): install each planned release and run probe.py on it, in isolation.

GitHub Actions only (`.github/workflows/s1.yml`); it refuses to run anywhere else, because the probe imports the
package under test (invariant 1). For one package:

  python3 -I bench/s1/run.py --plan bench/s1/plan.json --package langchain --out "$RUNNER_TEMP/s1-out"

1. `uv pip install --only-binary :all: --exclude-newer <release + 7 days>` into a fresh venv. Wheels only, so
   no package code runs at install time; dependencies are resolved as of the release, not today.
2. probe.py runs as a throwaway system user with no network (`unshare --net`), no capabilities, no new
   privileges and an empty environment (`env -i`), so it can't reach the runner's files, tokens or the internet.
   It writes only its JSON output.
3. The venv is deleted. A slot whose release won't install moves on to the next Python, then the next release.

Writes <out>/<package>/<version>.json from each probe, and <out>/<package>/attempts.json.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

WORK = Path("/tmp/s1")
PROBE_USER = "s1probe"
INSTALL_TIMEOUT_S = 15 * 60
PROBE_TIMEOUT_S = 40 * 60
SANDBOX_CHECK = """
import os, socket, sys
assert os.getuid() != 0 and os.geteuid() != 0, "running as root"
assert not os.access(sys.argv[1], os.W_OK), "checkout is writable"
try:
    socket.create_connection(("1.1.1.1", 443), 5)
except OSError:
    print("sandbox-ok")
else:
    print("network reachable")
"""


def sh(*cmd: str, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), file=sys.stderr, flush=True)
    return subprocess.run(cmd, timeout=timeout, check=check)


def sandboxed(*cmd: str, home: Path) -> list[str]:
    env = [
        "PATH=/usr/bin:/bin",
        f"HOME={home}",
        f"TMPDIR={home}",
        "LANG=C.UTF-8",
        "PYTHONDONTWRITEBYTECODE=1",
        "LITELLM_LOCAL_MODEL_COST_MAP=True",  # skip litellm's import-time fetch (it would fail anyway)
    ]
    return [
        "sudo",
        "unshare",
        "--net",
        "--",
        "setpriv",
        f"--reuid={PROBE_USER}",
        f"--regid={PROBE_USER}",
        "--clear-groups",
        "--inh-caps=-all",
        "--no-new-privs",
        "--",
        "env",
        "-i",
        *env,
        *cmd,
    ]


def prepare() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Linux":
        sys.exit("run.py imports untrusted packages; it runs only on a GitHub Actions Linux runner")
    if subprocess.run(["id", PROBE_USER], capture_output=True).returncode != 0:
        sh("sudo", "useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", PROBE_USER)
    WORK.mkdir(parents=True, exist_ok=True)
    os.chmod(WORK, 0o755)
    shutil.copy(Path(__file__).with_name("probe.py"), WORK / "probe.py")
    os.chmod(WORK / "probe.py", 0o644)
    # Interpreters and venvs live under WORK, where the probe user can read them but not write.
    os.environ.update(
        UV_PYTHON_INSTALL_DIR=str(WORK / "python"), UV_PYTHON_PREFERENCE="only-managed", UV_LINK_MODE="copy"
    )
    # Prove the sandbox from inside it: not root, can't write the checkout, and no network.
    check = subprocess.run(
        sandboxed("/usr/bin/python3", "-I", "-c", SANDBOX_CHECK, os.getcwd(), home=fresh_home()),
        capture_output=True,
        text=True,
    )
    if check.returncode != 0 or check.stdout.strip() != "sandbox-ok":
        sys.exit(f"sandbox check failed ({check.returncode}): {check.stdout.strip()} {check.stderr.strip()}")


def fresh_home() -> Path:
    home = WORK / "home"
    sh("sudo", "rm", "-rf", str(home))
    sh("sudo", "install", "-d", "-m", "0700", "-o", PROBE_USER, "-g", PROBE_USER, str(home))
    return home


def attempt(package: str, roots: list[str], entry: dict, python: str, dest: Path) -> dict:
    version = entry["version"]
    record = {"version": version, "python": python}
    venv = WORK / "venv"
    shutil.rmtree(venv, ignore_errors=True)
    cutoff = datetime.fromisoformat(entry["released_at"]).astimezone(UTC) + timedelta(days=7)
    try:
        sh("uv", "venv", "--quiet", "--python", python, str(venv), timeout=INSTALL_TIMEOUT_S)
        sh(
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(venv / "bin" / "python"),
            "--only-binary",
            ":all:",
            "--exclude-newer",
            cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{package}=={version}",
            timeout=INSTALL_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(venv, ignore_errors=True)
        return {**record, "result": "install_failed", "error": type(exc).__name__}
    home = fresh_home()
    out = home / "names.json"
    try:
        probe = sh(
            *sandboxed(
                str(venv / "bin" / "python"),
                "-I",
                str(WORK / "probe.py"),
                "--roots",
                *roots,
                "--package",
                package,
                "--version",
                version,
                "--out",
                str(out),
                home=home,
            ),
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
        code = probe.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    finally:
        # Nothing the probe started may outlive it (a timeout kills only sudo) and write the next result.
        sh("sudo", "pkill", "-KILL", "-u", PROBE_USER, check=False)
        shutil.rmtree(venv, ignore_errors=True)
    if code != 0:
        return {**record, "result": "probe_failed", "error": str(code)}
    sh("sudo", "cp", str(out), str(dest / f"{version}.json"))
    sh("sudo", "chown", f"{os.getuid()}:{os.getgid()}", str(dest / f"{version}.json"))
    return {**record, "result": "ok"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--package", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    spec = next((p for p in plan["packages"] if p["name"] == args.package), None)
    if spec is None:
        sys.exit(f"{args.package} is not in {args.plan}")
    prepare()
    dest = args.out / args.package
    dest.mkdir(parents=True, exist_ok=True)
    attempts = []
    for slot in spec["slots"]:
        done = False
        for entry in slot:
            for python in entry["python"]:
                attempts.append(attempt(args.package, spec["import_names"], entry, python, dest))
                print(json.dumps(attempts[-1]), file=sys.stderr, flush=True)
                if attempts[-1]["result"] == "ok":
                    done = True
                    break
            if done:
                break
    (dest / "attempts.json").write_text(json.dumps(attempts, indent=1), encoding="utf-8")
    probed = sum(a["result"] == "ok" for a in attempts)
    print(f"{args.package}: {probed}/{len(spec['slots'])} slots probed", file=sys.stderr)
    return 0 if probed == len(spec["slots"]) else 1


if __name__ == "__main__":
    sys.exit(main())
