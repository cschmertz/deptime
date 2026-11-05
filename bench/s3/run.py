"""S3 sandbox driver (DT-SPIKE-03): produce real tracebacks for known breaks, on one Python version.

GitHub Actions Linux only (`.github/workflows/s3.yml`), for the same reason as bench/s1/run.py: each snippet
imports the package under test (invariant 1), so it refuses to run anywhere else.

  python3 -I bench/s3/run.py --scenarios bench/s3/scenarios.json --python 3.12 --out "$RUNNER_TEMP/s3-out"

For each scenario in scenarios.json:
1. A fresh venv at /home/dev/project/.venv, filled with `uv pip install --only-binary :all: --exclude-newer` (wheels
   only, so no package code runs at install time) with the scenario's requirements plus pytest and ipython.
2. The project files: app.py (the scenario's code) and the entry points each presentation runs.
3. Each presentation runs as the throwaway user `dev` with no network (`unshare --net`), no capabilities, no new
   privileges and an empty environment, in /home/dev/project. stdout and stderr are kept together, in order, as a
   terminal shows them.

Writes <out>/<python>.jsonl: one line per run with the scenario, presentation, interpreter version, the venv's
distributions (`uv pip freeze`), the exit code and the output. Nothing here runs DepTime.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

USER = "dev"
HOME = Path("/home/dev")
PROJECT = HOME / "project"
VENV = PROJECT / ".venv"
PY = VENV / "bin" / "python"
TOOLS = Path("/opt/s3")
INSTALL_TIMEOUT_S = 15 * 60
RUN_TIMEOUT_S = 3 * 60
MAX_OUTPUT_CHARS = 200_000

ENTRY_POINTS = {
    "test_app.py": "import app\n\n\ndef test_main():\n    app.main()\n",
    "test_unit.py": (
        "import unittest\n\nimport app\n\n\nclass AppTest(unittest.TestCase):\n    def test_main(self):\n"
        '        app.main()\n\n\nif __name__ == "__main__":\n    unittest.main()\n'
    ),
    "chain_from.py": (
        "def start():\n    try:\n        import app\n\n        app.main()\n    except Exception as exc:\n"
        '        raise RuntimeError("could not start the service") from exc\n\n\n'
        'if __name__ == "__main__":\n    start()\n'
    ),
    "chain_during.py": (
        "def start():\n    try:\n        import app\n\n        app.main()\n    except Exception:\n"
        '        raise RuntimeError("could not start the service")\n\n\n'
        'if __name__ == "__main__":\n    start()\n'
    ),
    "with_logging.py": (
        "import logging\n\nlogging.basicConfig(level=logging.INFO, "
        'format="%(asctime)s %(levelname)s %(name)s: %(message)s")\nlog = logging.getLogger("service")\n\n'
        'try:\n    import app\n\n    app.main()\nexcept Exception:\n    log.exception("startup failed")\n'
    ),
    "with_taskgroup.py": (
        "import asyncio\n\n\nasync def job():\n    import app\n\n    app.main()\n\n\nasync def run():\n"
        "    async with asyncio.TaskGroup() as tg:\n        tg.create_task(job())\n\n\nasyncio.run(run())\n"
    ),
}

# name -> (argv after the interpreter, minimum Python minor version, extra environment)
PRESENTATIONS: dict[str, tuple[list[str], int, list[str]]] = {
    "script": (["app.py"], 10, []),
    "script-color": (["app.py"], 13, ["PYTHON_COLORS=1"]),  # 3.13+ colours tracebacks
    "pytest": (["-m", "pytest", "-p", "no:cacheprovider", "test_app.py"], 10, []),
    "pytest-short": (["-m", "pytest", "-p", "no:cacheprovider", "--tb=short", "test_app.py"], 10, []),
    "unittest": (["-m", "unittest", "test_unit"], 10, []),
    "raise-from": (["chain_from.py"], 10, []),
    "during-handling": (["chain_during.py"], 10, []),
    "logging": (["with_logging.py"], 10, []),
    "taskgroup": (["with_taskgroup.py"], 11, []),  # asyncio.TaskGroup and ExceptionGroup are 3.11+
    "ipython": (["-m", "IPython", "--no-banner", "--colors=nocolor", "-c", "import app; app.main()"], 10, []),
}

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


def sh(*cmd: str, timeout: float | None = None, check: bool = True, **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), file=sys.stderr, flush=True)
    return subprocess.run(cmd, timeout=timeout, check=check, **kw)


def sandboxed(*cmd: str, extra_env: list[str] = ()) -> list[str]:
    env = [
        f"PATH={VENV / 'bin'}:/usr/bin:/bin",
        f"HOME={HOME}",
        f"TMPDIR={HOME}",
        "LANG=C.UTF-8",
        "PYTHONDONTWRITEBYTECODE=1",
        *extra_env,
    ]
    return [
        "sudo",
        "unshare",
        "--net",
        "--",
        "setpriv",
        f"--reuid={USER}",
        f"--regid={USER}",
        "--clear-groups",
        "--inh-caps=-all",
        "--no-new-privs",
        "--",
        "env",
        "-i",
        *env,
        *cmd,
    ]


def fresh_project() -> None:
    """/home/dev belongs to the sandbox user (IPython and matplotlib write there); the project and its venv
    belong to the runner, so the sandbox can read them but not change them."""
    sh("sudo", "rm", "-rf", str(HOME))
    sh("sudo", "install", "-d", "-m", "0755", "-o", USER, "-g", USER, str(HOME))
    sh("sudo", "install", "-d", "-m", "0755", "-o", str(os.getuid()), "-g", str(os.getgid()), str(PROJECT))


def prepare() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Linux":
        sys.exit("run.py imports the packages under test; it runs only on a GitHub Actions Linux runner")
    if subprocess.run(["id", USER], capture_output=True).returncode != 0:
        sh(
            "sudo",
            "useradd",
            "--system",
            "--no-create-home",
            "--home-dir",
            str(HOME),
            "--shell",
            "/usr/sbin/nologin",
            USER,
        )
    sh("sudo", "install", "-d", "-m", "0755", "-o", str(os.getuid()), "-g", str(os.getgid()), str(TOOLS))
    # Interpreters live where the sandbox user can read them but not write.
    os.environ.update(
        UV_PYTHON_INSTALL_DIR=str(TOOLS / "python"), UV_PYTHON_PREFERENCE="only-managed", UV_LINK_MODE="copy"
    )
    fresh_project()
    check = subprocess.run(
        sandboxed("/usr/bin/python3", "-I", "-c", SANDBOX_CHECK, os.getcwd()),
        capture_output=True,
        text=True,
        cwd=PROJECT,
    )
    if check.returncode != 0 or check.stdout.strip() != "sandbox-ok":
        sys.exit(f"sandbox check failed ({check.returncode}): {check.stdout.strip()} {check.stderr.strip()}")


def freeze() -> dict[str, str]:
    out = sh("uv", "pip", "freeze", "--python", str(PY), capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        name, sep, version = line.partition("==")
        if sep:
            found[name.strip()] = version.strip()
    return found


def scenario_runs(scenario: dict, doc: dict, python: str) -> list[dict]:
    minor = int(python.split(".")[1])
    names = scenario.get("presentations") or list(PRESENTATIONS)
    base = {"scenario": scenario["id"], "python": python}
    fresh_project()
    try:
        sh("uv", "venv", "--quiet", "--python", python, str(VENV), timeout=INSTALL_TIMEOUT_S)
        sh(
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(PY),
            "--only-binary",
            ":all:",
            "--exclude-newer",
            doc["exclude_newer"],
            *scenario["requires"],
            *doc["always"],
            timeout=INSTALL_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return [{**base, "presentation": None, "result": "install_failed", "error": type(exc).__name__}]
    version = sh(
        str(PY), "-I", "-S", "-c", "import sys; print(sys.version.split()[0])", capture_output=True, text=True
    )
    installed = freeze()
    files = {
        "app.py": scenario["app"] + '\n\nif __name__ == "__main__":\n    main()\n',
        **ENTRY_POINTS,
        **scenario.get("files", {}),
    }
    for name, text in files.items():
        (PROJECT / name).write_text(text, encoding="utf-8")
    runs = []
    for name in names:
        argv, min_minor, extra_env = PRESENTATIONS[name]
        if minor < min_minor:
            continue
        record = {**base, "presentation": name, "python_full": version.stdout.strip(), "installed": installed}
        try:
            p = subprocess.run(
                sandboxed(str(PY), *argv, extra_env=extra_env),
                cwd=PROJECT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=RUN_TIMEOUT_S,
            )
            output = p.stdout.decode("utf-8", errors="replace")
            record.update(result="ran", returncode=p.returncode, output=output[-MAX_OUTPUT_CHARS:])
        except subprocess.TimeoutExpired:
            record.update(result="timeout")
        finally:
            sh("sudo", "pkill", "-KILL", "-u", USER, check=False)
        runs.append(record)
        print(
            json.dumps({k: v for k, v in record.items() if k not in ("installed", "output")}), file=sys.stderr
        )
    shutil.rmtree(VENV, ignore_errors=True)
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=Path, required=True)
    ap.add_argument("--python", required=True, help="e.g. 3.12")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    doc = json.loads(args.scenarios.read_text(encoding="utf-8"))
    prepare()
    args.out.mkdir(parents=True, exist_ok=True)
    runs = []
    for scenario in doc["scenarios"]:
        runs.extend(scenario_runs(scenario, doc, args.python))
    with open(args.out / f"{args.python}.jsonl", "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")
    failed = [r for r in runs if r["result"] != "ran"]
    print(f"python {args.python}: {len(runs) - len(failed)} runs, {len(failed)} failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
