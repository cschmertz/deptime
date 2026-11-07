"""Holdout probe (DT-QA-07): for each environment (one package at one version), install it and check at run time
whether each use resolves and whether each call shape binds. This is the ground truth the holdout scores DepTime
against, so it never asks DepTime anything.

GitHub Actions Linux only (`.github/workflows/holdout.yml`), like bench/s3/run.py: checking a use imports the package
(invariant 1 forbids that anywhere else). Per environment:
1. A fresh venv, filled with `uv pip install --only-binary :all: --exclude-newer` (wheels only, so no package code runs
   at install time), trying each listed Python until one installs.
2. CHECK runs as the throwaway user `dev` with no network (`unshare --net`), no capabilities, no new privileges and an
   empty environment, and writes one result per use.

  python3 -I bench/holdout/probe.py --envs <envs.json> --shard 0 --shards 4 --out "$RUNNER_TEMP/holdout-out"

Writes <out>/<shard>.jsonl: one line per environment with the interpreter, the venv's distributions and the results.
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
TOOLS = Path("/opt/holdout")
INSTALL_TIMEOUT_S = 15 * 60
CHECK_TIMEOUT_S = 15 * 60

# Runs inside the sandbox with the environment's interpreter: argv[1] = uses (JSON), argv[2] = results (JSON lines on
# stdout). Every use is checked on its own, so one that hangs or crashes the import of a module loses only itself.
CHECK = r'''
import importlib, inspect, json, sys, types, warnings

MAX = 300


def short(e):
    return f"{type(e).__name__}: {e}"[:MAX]


def deprecation_evidence(obj, caught):
    found = []
    for w in caught:
        cat = w.category
        if issubclass(cat, (DeprecationWarning, PendingDeprecationWarning, FutureWarning)) or "deprecat" in cat.__name__.lower():
            found.append(f"warning {cat.__name__}: {str(w.message)[:MAX]}")
    try:
        if getattr(obj, "__deprecated__", None):
            found.append(f"__deprecated__: {str(obj.__deprecated__)[:MAX]}")
    except Exception:
        pass
    try:
        doc = obj.__doc__ if not isinstance(obj, types.ModuleType) else None
        if isinstance(doc, str) and (".. deprecated::" in doc or "\nDeprecated" in doc[:400] or doc.lstrip().lower().startswith("deprecated")):
            found.append("docstring: " + next(l.strip() for l in doc.splitlines() if "eprecated" in l)[:MAX])
    except Exception:
        pass
    return found


class Missing(Exception):
    """The use's own name isn't there (as opposed to an import that fails for another reason)."""


def step(obj, name, prefix):
    """The attribute `name` of `obj`, or the submodule `prefix` when `obj` is a module without that attribute."""
    try:
        return getattr(obj, name)
    except AttributeError as e:
        if not isinstance(obj, types.ModuleType):
            raise Missing(short(e)) from None
        attr_error = e
    try:
        return importlib.import_module(prefix)
    except ModuleNotFoundError as e:
        if e.name and (e.name == prefix or prefix.startswith(e.name + ".")):
            raise Missing(short(attr_error)) from None
        raise


def resolve_attributes(path):
    """Reading attributes from the top package down, as `import pkg` then `pkg.a.b` does: (object, parent, warnings of
    the last step); raises Missing at the part that isn't there."""
    parts = path.split(".")
    parent = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            obj = importlib.import_module(parts[0])
        except ModuleNotFoundError as e:
            if e.name == parts[0]:
                raise Missing(short(e)) from None
            raise
    for i in range(1, len(parts)):
        parent = obj
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            obj = step(obj, parts[i], ".".join(parts[: i + 1]))
    return obj, parent, caught


def resolve_import(path):
    """Importing the longest prefix that is a module, then reading the rest, as `from a.b import c` does (it finds
    modules a package registers in sys.modules under another name)."""
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                obj = importlib.import_module(prefix)
            except ModuleNotFoundError as e:
                if e.name and (e.name == prefix or prefix.startswith(e.name + ".")):
                    continue
                raise
        parent = sys.modules.get(".".join(parts[: i - 1])) if i > 1 else None
        for part in parts[i:]:
            parent = obj
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    obj = getattr(obj, part)
                except AttributeError as e:
                    raise Missing(short(e)) from None
        return obj, parent, caught
    raise Missing(f"ModuleNotFoundError: No module named {parts[0]!r}")


def resolve(path):
    """Attributes first, as `from a import b` and `a.b` both read (`instructor.patch` is a function and a module, and
    calls mean the function); then by import, which finds a module registered in sys.modules under a name an attribute
    shadows (fastmcp 3's `fastmcp.tools.tool`)."""
    try:
        return resolve_attributes(path)
    except Missing as first:
        try:
            return resolve_import(path)
        except Missing:
            raise first from None


def kind_of(obj):
    if isinstance(obj, types.ModuleType):
        return "module"
    if isinstance(obj, type):
        return "class"
    if inspect.isfunction(obj) or inspect.isbuiltin(obj) or inspect.ismethod(obj):
        return "function"
    if callable(obj):
        return "callable"
    return "other"


def check(use):
    r = {"key": use["key"]}
    path = use["path"]
    try:
        obj, parent, caught = resolve(path)
    except Missing as e:
        r.update(resolved=False, error=str(e))
        return r
    except BaseException as e:  # an import that fails for another reason says nothing about this name
        r.update(resolved=None, error=short(e))
        return r
    r.update(resolved=True, objkind=kind_of(obj), deprecation=deprecation_evidence(obj, caught))
    if use.get("via") == "instance":
        r["builder_is_class"] = isinstance(parent, type)
    if use["kind"] != "call":
        return r
    self_arg = False
    if use.get("via") == "instance" and isinstance(parent, type):
        try:
            static = inspect.getattr_static(parent, path.rsplit(".", 1)[1])
        except AttributeError:
            static = None
        if isinstance(static, property):
            r.update(signature=None, binds=None, bind_error="property")
            return r
        self_arg = inspect.isfunction(obj) and not isinstance(static, staticmethod)
    try:
        sig = inspect.signature(obj)
    except BaseException as e:
        r.update(signature=None, binds=None, bind_error=short(e))
        return r
    r["signature"] = str(sig)[:MAX]
    params = list(sig.parameters.values())
    var_kw = any(p.kind is p.VAR_KEYWORD for p in params)
    named = {p.name: p.kind.name for p in params if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
    r["kwargs"] = {k: (f"named:{named[k]}" if k in named else ("var_keyword" if var_kw else "absent")) for k in use["kwargs"]}
    try:
        sig.bind(*([None] * (use["npos"] + (1 if self_arg else 0))), **{k: None for k in use["kwargs"]})
        r["binds"] = True
    except TypeError as e:
        r.update(binds=False, bind_error=str(e)[:MAX])
    return r


uses = json.load(open(sys.argv[1], encoding="utf-8"))
for use in uses:
    try:
        res = check(use)
    except BaseException as e:
        res = {"key": use["key"], "resolved": None, "error": "probe: " + short(e)}
    print(json.dumps(res), flush=True)
'''

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


def sandboxed(*cmd: str) -> list[str]:
    env = [
        f"PATH={VENV / 'bin'}:/usr/bin:/bin",
        f"HOME={HOME}",
        f"TMPDIR={HOME}",
        "LANG=C.UTF-8",
        "PYTHONDONTWRITEBYTECODE=1",
        "MPLBACKEND=Agg",
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
    sh("sudo", "rm", "-rf", str(HOME))
    sh("sudo", "install", "-d", "-m", "0755", "-o", USER, "-g", USER, str(HOME))
    sh("sudo", "install", "-d", "-m", "0755", "-o", str(os.getuid()), "-g", str(os.getgid()), str(PROJECT))


def prepare() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Linux":
        sys.exit("probe.py imports the packages under test; it runs only on a GitHub Actions Linux runner")
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


def install(env: dict) -> str | None:
    """The Python that installed the environment, or None."""
    for python in env["python"]:
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
                env["exclude_newer"],
                *env["requires"],
                timeout=INSTALL_TIMEOUT_S,
            )
            return python
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return None


def probe(env: dict) -> dict:
    base = {"id": env["id"], "requires": env["requires"]}
    python = install(env)
    if python is None:
        return {**base, "result": "install_failed", "tried": env["python"]}
    version = sh(
        str(PY), "-I", "-S", "-c", "import sys; print(sys.version.split()[0])", capture_output=True, text=True
    )
    (PROJECT / "check.py").write_text(CHECK, encoding="utf-8")
    (PROJECT / "uses.json").write_text(json.dumps(env["uses"]), encoding="utf-8")
    record = {**base, "python": version.stdout.strip(), "installed": freeze()}
    results: dict[str, dict] = {}
    remaining = list(env["uses"])
    # A use that kills the interpreter (a segfault in an extension) loses only itself: rerun the rest without it.
    for _ in range(5):
        (PROJECT / "uses.json").write_text(json.dumps(remaining), encoding="utf-8")
        try:
            p = subprocess.run(
                sandboxed(str(PY), "-I", "check.py", "uses.json"),
                cwd=PROJECT,
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT_S,
            )
            out, crashed = p.stdout, p.returncode != 0
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            crashed = True
        finally:
            sh("sudo", "pkill", "-KILL", "-u", USER, check=False)
        for line in out.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and "key" in r:
                results[r["key"]] = r
        remaining = [u for u in remaining if u["key"] not in results]
        if not crashed or not remaining:
            break
        lost = remaining.pop(0)
        results[lost["key"]] = {"key": lost["key"], "resolved": None, "error": "probe: interpreter died here"}
    for u in remaining:
        results[u["key"]] = {"key": u["key"], "resolved": None, "error": "probe: not reached"}
    record.update(result="ok", results=[results[u["key"]] for u in env["uses"]])
    shutil.rmtree(VENV, ignore_errors=True)
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--envs", type=Path, required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    envs = json.loads(args.envs.read_text(encoding="utf-8"))["envs"]
    mine = [e for i, e in enumerate(envs) if i % args.shards == args.shard]
    prepare()
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / f"{args.shard}.jsonl", "w", encoding="utf-8") as f:
        for env in mine:
            record = probe(env)
            f.write(json.dumps(record) + "\n")
            f.flush()
            print(f"{env['id']}: {record['result']}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
