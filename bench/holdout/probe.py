"""Holdout probe (DT-QA-07): for each environment (one package at one version), install it and check at run time
whether each use resolves and whether each call shape binds. This is the ground truth the holdout scores DepTime
against, so it never asks DepTime anything.

GitHub Actions Linux only (`.github/workflows/holdout.yml`), like bench/s3/run.py: checking a use imports the package
(invariant 1 forbids that anywhere else). Per environment:
1. A fresh venv, filled with `uv pip install --only-binary :all: --exclude-newer` (wheels only, so no package code runs
   at install time), trying each listed Python until one installs; if none does, again allowing only SOURCE_BUILDS (two
   pure-Python sdist-only dependencies) to build.
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
# Pure-Python dependencies published only as sdists, which some environments need (chromadb's pypika, gradio 3's
# ffmpy; DT-QA-08). An environment that doesn't install from wheels alone is retried allowing just these to build.
# Nothing else builds, and the packages under test always come from wheels.
SOURCE_BUILDS = ["pypika", "ffmpy"]

# Runs inside the sandbox with the environment's interpreter: argv[1] = uses (JSON), argv[2] = results (JSON lines on
# stdout). Every use is checked on its own, so one that hangs or crashes the import of a module loses only itself.
CHECK = r'''
import ast, importlib, inspect, itertools, json, re, sys, textwrap, types, warnings

MAX = 300


def short(e):
    return f"{type(e).__name__}: {e}"[:MAX]


SECTION = re.compile(
    r"(Parameters|Params|Args|Arguments|Keyword Arguments|Other Parameters|Returns|Yields|Raises|Attributes|"
    r"Examples?|Notes?|See Also|Warnings?)\s*:?\s*$"
)
THIS = re.compile(r"\bthis (function|class|method|module|property|attribute|object|model|api|decorator)\b", re.I)
PARAM_LEVEL = ("arg", "param", "keyword", "positional")
ARG_LEVEL = re.compile(r"\b(arguments?|parameters?|keywords?|kwargs?|positional)\b", re.I)


def _functions(obj):
    """The functions behind a name, unwrapped: a function or method, a property's getter, a class's own __init__ and
    __new__; each with the chain of __wrapped__ below it."""
    if isinstance(obj, property):
        heads = [obj.fget]
    elif isinstance(obj, type):
        heads = [vars(obj).get("__init__"), vars(obj).get("__new__")]
    elif inspect.ismethod(obj):
        heads = [obj.__func__]
    else:
        heads = [obj]
    out = []
    for f in heads:
        if isinstance(f, (staticmethod, classmethod)):
            f = f.__func__
        for _ in range(10):
            if f is None or not (inspect.isfunction(f) or hasattr(f, "_self_wrapper")):
                break
            out.append(f)
            f = getattr(f, "__wrapped__", None)
    return out


def _decorator(obj):
    """The qualified name of a deprecation wrapper around the name itself, not one of its parameters: a wrapper
    function defined in a function or module whose name says `deprecat` (the package's own @deprecated), or a wrapt
    wrapper whose adapter does."""
    for f in _functions(obj):
        if not hasattr(f, "__wrapped__"):  # the function itself, not a wrapper around it (a helper in deprecation.py)
            continue
        try:
            if hasattr(f, "_self_wrapper"):
                w = f._self_wrapper  # the `deprecated` package's ClassicAdapter.__call__.<locals>.wrapper_function
                mod = getattr(w, "__module__", None) or getattr(type(w), "__module__", "")
                where = f"{mod}.{getattr(w, '__qualname__', None) or getattr(type(w), '__qualname__', '')}"
            elif hasattr(f.__code__, "co_qualname"):  # Python 3.11+: functools.wraps copies __qualname__ over
                code = f.__code__
                file = re.split(r"[\\/]", code.co_filename)[-1]  # no backslash in an f-string before 3.12
                where = f"{file}:{code.co_qualname}"
            else:
                continue
        except Exception:
            continue
        parts = re.split(r"[.:<>/]", where.lower())
        # `deprecate_kwarg`, `_deprecate_positional_args`, matplotlib's `make_keyword_only` and `rename_parameter`
        # (in its deprecation.py) are about a parameter.
        if any("deprecat" in s for s in parts) and not any(p in s for s in parts for p in PARAM_LEVEL):
            return where
    return None


def _own_deprecated(obj):
    """PEP 702's __deprecated__ set on the object itself (a class's own, not inherited from a base)."""
    try:
        if isinstance(obj, type):
            return vars(obj).get("__deprecated__")
        if inspect.ismethod(obj):
            obj = obj.__func__
        if isinstance(obj, property):  # langchain marks the property object, typing_extensions the getter
            if (d := getattr(obj, "__deprecated__", None)):
                return d
            obj = obj.fget
        if inspect.isfunction(obj):
            return getattr(obj, "__deprecated__", None)
    except Exception:
        pass
    return None


def _doc_note(obj, leaf):
    """A deprecation note in the docstring that is about the object: a `.. deprecated::` directive at the docstring's
    own indentation before its first section (not one indented under a parameter), or a first paragraph that says
    deprecated and starts with it, says "this function/class/...", or names the object."""
    doc = getattr(obj, "__doc__", None) if not isinstance(obj, type) else vars(obj).get("__doc__")
    if not isinstance(doc, str) or "deprecat" not in doc.lower():
        return None
    lines = inspect.cleandoc(doc).splitlines()
    for i, line in enumerate(lines):
        if SECTION.match(line):
            break
        if line.startswith(".. deprecated::"):
            block = [line] + [l for l in itertools.takewhile(lambda l: not l or l[:1].isspace(), lines[i + 1 :])]
            if not ARG_LEVEL.search(" ".join(block)):  # sqlalchemy's and_(): calling it with no arguments
                return line.strip()
    first = " ".join(l.strip() for l in lines[: next((i for i, l in enumerate(lines) if not l.strip()), len(lines))])
    low = first.lower()
    if "deprecat" in low and not ARG_LEVEL.search(first) and (
        low.lstrip("*[(!_ ").startswith("deprecat") or THIS.search(first) or re.search(rf"\b{re.escape(leaf)}\b", first)
    ):
        return first
    return None


def deprecation_evidence(obj, caught, path):
    """(evidence that the name itself is deprecated, other deprecation notes seen on the way). A warning counts when
    reading the name raises it and it names the name; the others are kept apart (a parameter's note, an unrelated
    module's import warning)."""
    leaf = path.rsplit(".", 1)[-1]
    found, other = [], []
    for w in caught:
        cat = w.category
        if issubclass(cat, (DeprecationWarning, PendingDeprecationWarning, FutureWarning)) or "deprecat" in cat.__name__.lower():
            text = f"warning {cat.__name__}: {str(w.message)[:MAX]}"
            (found if re.search(rf"\b{re.escape(leaf)}\b", str(w.message)) else other).append(text)
    if (d := _own_deprecated(obj)):
        found.append(f"__deprecated__: {str(d)[:MAX]}")
    try:
        if (dec := _decorator(obj)):
            found.append(f"decorator: {dec[:MAX]}")
    except Exception:
        pass
    try:
        if (note := _doc_note(obj, leaf)):
            found.append(f"docstring: {note[:MAX]}")
        else:
            doc = getattr(obj, "__doc__", None)
            if isinstance(doc, str) and "deprecat" in doc.lower() and not isinstance(obj, types.ModuleType):
                other.append("docstring: " + next(l.strip() for l in doc.splitlines() if "deprecat" in l.lower())[:MAX])
    except Exception:
        pass
    return found, other


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


def _raises_only(f):
    """Whether a function's body, after its docstring, is one `raise` of something other than NotImplementedError
    (an abstract method's)."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(f)))
    except Exception:
        return False
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Raise) or body[0].exc is None:
        return False
    exc = body[0].exc.func if isinstance(body[0].exc, ast.Call) else body[0].exc
    name = exc.attr if isinstance(exc, ast.Attribute) else getattr(exc, "id", "")
    return name != "NotImplementedError"


def built_to_fail(obj):
    """Whether calling the object can only raise: a function whose body is one `raise`, or an instance whose class's
    `__call__` is (openai 1.x keeps `openai.ChatCompletion` as an `APIRemovedInV1Proxy`, whose attributes are the proxy
    again and whose `__call__` raises `APIRemovedInV1`; DT-QA-10). `type()`, not `__class__`, which a proxy fakes."""
    if isinstance(obj, (type, types.ModuleType)):
        return False
    if inspect.ismethod(obj):
        obj = obj.__func__
    if inspect.isfunction(obj):
        return _raises_only(obj)
    call = getattr(type(obj), "__call__", None)
    return inspect.isfunction(call) and _raises_only(call)


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


def set_on_instances(path):
    """Whether a class in the MRO of the class `path`'s parent names assigns `self.<name> = ...` in its source: an
    attribute each instance sets (gradio 3's `self.click = EventListenerMethod(self, "click")` in an event mixin's
    `__init__`), which reading the class can't see (DT-QA-09)."""
    owner, _, name = path.rpartition(".")
    try:
        cls = resolve(owner)[0]
    except BaseException:
        return False
    if not isinstance(cls, type):
        return False
    assign = re.compile(rf"\bself\.{re.escape(name)}\s*(?::[^=\n]+)?=(?!=)")
    for c in cls.__mro__:
        try:
            if assign.search(inspect.getsource(c)):
                return True
        except (OSError, TypeError):
            continue
    return False


def check(use):
    r = {"key": use["key"]}
    path = use["path"]
    try:
        obj, parent, caught = resolve(path)
    except Missing as e:
        r.update(resolved=False, error=str(e))
        if use.get("via") == "instance" and set_on_instances(path):
            r["set_on_instances"] = True
        return r
    except BaseException as e:  # an import that fails for another reason says nothing about this name
        r.update(resolved=None, error=short(e))
        return r
    found, other = deprecation_evidence(obj, caught, path)
    r.update(resolved=True, objkind=kind_of(obj), deprecation=found)
    try:
        if built_to_fail(obj):
            r["raises_when_called"] = True
    except Exception:
        pass
    if other:
        r["deprecation_other"] = other
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


def install(env: dict) -> tuple[str, list[str]] | None:
    """(the Python that installed the environment, the dependencies built from source), or None. Wheels only first,
    on each listed Python; then the same allowing SOURCE_BUILDS to build from their sdists."""
    for builds in ([], SOURCE_BUILDS):
        allow = [a for b in builds for a in ("--no-binary", b)]
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
                    *allow,
                    "--exclude-newer",
                    env["exclude_newer"],
                    *env["requires"],
                    timeout=INSTALL_TIMEOUT_S,
                )
                return python, list(builds)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
    return None


def probe(env: dict) -> dict:
    base = {"id": env["id"], "requires": env["requires"]}
    installed = install(env)
    if installed is None:
        return {**base, "result": "install_failed", "tried": env["python"]}
    _, built = installed
    version = sh(
        str(PY), "-I", "-S", "-c", "import sys; print(sys.version.split()[0])", capture_output=True, text=True
    )
    (PROJECT / "check.py").write_text(CHECK, encoding="utf-8")
    (PROJECT / "uses.json").write_text(json.dumps(env["uses"]), encoding="utf-8")
    record = {**base, "python": version.stdout.strip(), "installed": freeze()}
    if built:
        record["source_builds_allowed"] = built
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
