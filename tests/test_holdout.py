"""The holdout's own tools (DT-QA-07, bench/holdout): the use reader, the labelling rules and the scorer's reading of
`diff_versions` events. Offline; nothing here installs or imports an indexed package."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deptime.models import HistoryEvent

sys.path.insert(0, str(Path(__file__).parent.parent / "bench" / "holdout"))
import labels
import score
import uses


def _uses(tmp_path: Path, code: str, *roots: str) -> set[tuple]:
    (tmp_path / "m.py").write_text(code, encoding="utf-8")
    return {(u.kind, u.path, u.npos, u.kwargs, u.via) for u in uses.uses_in_tree(tmp_path, roots)}


def test_reader_names_calls_and_instance_members(tmp_path: Path) -> None:
    found = _uses(
        tmp_path,
        "import numpy as np\nimport httpx\nfrom google import genai\n"
        "x = np.array([1], dtype=float)\n"
        "c = httpx.Client(timeout=5)\nc.get('u', params={})\n"
        "genai.Client(api_key='k').models\n"
        "np.linalg.norm(x, *y)\n",
        "numpy",
        "httpx",
        "google.genai",
    )
    assert ("call", "numpy.array", 1, ("dtype",), "import") in found
    assert ("name", "numpy.linalg.norm", None, (), "import") in found
    assert not any(k == "call" and p == "numpy.linalg.norm" for k, p, *_ in found)  # a splat has no shape
    assert ("call", "httpx.Client.get", 1, ("params",), "instance") in found
    assert ("call", "google.genai.Client", 0, ("api_key",), "import") in found
    assert ("name", "google.genai.Client.models", None, (), "instance") in found


def test_reader_forgets_a_rebound_name(tmp_path: Path) -> None:
    found = _uses(tmp_path, "import httpx\nc = httpx.Client()\nc = object()\nc.get()\n", "httpx")
    assert not any(p == "httpx.Client.get" for _, p, *_ in found)


def test_reader_skips_virtualenvs(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "x.py").write_text("import httpx\nhttpx.get('u')\n", encoding="utf-8")
    assert uses.uses_in_tree(tmp_path, ("httpx",)) == []


CALL = {"kind": "call", "path": "p.f", "npos": 0, "kwargs": ["a"]}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"resolved": True, "binds": True}, (True, "")),
        ({"resolved": False, "error": "x"}, (False, "removed")),
        (
            {"resolved": None, "error": "ImportError: no torch"},
            (None, "probe can't tell: ImportError: no torch"),
        ),
        (
            {"resolved": True, "binds": False, "bind_error": "got an unexpected keyword argument 'a'"},
            (False, "param_removed"),
        ),
        (
            {"resolved": True, "binds": False, "bind_error": "missing a required argument: 'b'"},
            (False, "param_now_required"),
        ),
        (
            {"resolved": True, "binds": False, "bind_error": "too many positional arguments"},
            (False, "positional"),
        ),
        (
            {"resolved": True, "binds": None, "bind_error": "no signature found"},
            (None, "no signature: no signature found"),
        ),
    ],
)
def test_works_reads_the_probe(result: dict, expected: tuple) -> None:
    assert labels.works({"key": "k", **result}, CALL) == expected


def test_instance_member_needs_a_class_builder() -> None:
    use = {"kind": "name", "path": "p.make.x", "via": "instance"}
    assert labels.works({"resolved": True, "builder_is_class": False}, use)[0] is None
    assert labels.works({"resolved": True, "builder_is_class": True}, use) == (True, "")


def _ev(kind: str, version: str, path: str = "p.f", **detail) -> HistoryEvent:
    return HistoryEvent(path=path, version=version, kind=kind, detail=detail)


def test_state_at_to_follows_the_last_change_upgrading() -> None:
    removed_then_back = [_ev("removed", "1.2"), _ev("added", "1.5")]
    assert score._state_at_to(removed_then_back, "p.f", True, "added", "removed") is False
    assert score._state_at_to([_ev("removed", "1.2")], "p.f", True, "added", "removed") is True
    assert score._state_at_to([], "p.f", True, "added", "removed") is None


def test_state_at_to_follows_the_first_change_downgrading() -> None:
    # to=1.0, from=2.0: added in 1.3 means 1.0 lacks it.
    assert score._state_at_to([_ev("added", "1.3")], "p.f", False, "added", "removed") is True
    assert (
        score._state_at_to([_ev("removed", "1.3"), _ev("added", "1.6")], "p.f", False, "added", "removed")
        is False
    )


def test_state_at_to_reads_one_parameter() -> None:
    evs = [_ev("param_removed", "2.0", param="a"), _ev("param_added", "2.1", param="b")]
    assert score._state_at_to(evs, "p.f", True, "param_added", "param_removed", param="a") is True
    assert score._state_at_to(evs, "p.f", True, "param_added", "param_removed", param="b") is False
    assert score._state_at_to(evs, "p.f", True, "param_added", "param_removed", param="c") is None


def test_wilson_interval() -> None:
    lo, hi = score.wilson(0, 100)  # type: ignore[misc]
    assert lo == 0.0 and 0.03 < hi < 0.04
    assert score.wilson(0, 0) is None
    assert score.rate(1, 4).startswith("25.0% (1/4; ")


def test_probe_resolves_like_python_imports(tmp_path: Path) -> None:
    """Attributes first (a function named like its own module is what calls mean), then by import (a module a package
    registers in sys.modules under a name an attribute shadows). Runs the probe's checker on two local packages only."""
    import json
    import subprocess

    import probe

    (tmp_path / "aliaspkg").mkdir()
    (tmp_path / "aliaspkg" / "base.py").write_text("class ToolResult:\n    pass\n", encoding="utf-8")
    (tmp_path / "aliaspkg" / "__init__.py").write_text(
        "import sys\nfrom . import base\nsys.modules[__name__ + '.tool'] = base\n\n\ndef tool():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "dualpkg").mkdir()
    (tmp_path / "dualpkg" / "patch.py").write_text(
        "def patch(client, mode=None):\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "dualpkg" / "__init__.py").write_text("from .patch import patch\n", encoding="utf-8")
    uses = [
        {"key": "alias", "kind": "name", "path": "aliaspkg.tool.ToolResult"},
        {"key": "dual", "kind": "call", "path": "dualpkg.patch", "npos": 0, "kwargs": ["mode"]},
        {"key": "gone", "kind": "name", "path": "dualpkg.nothing"},
    ]
    (tmp_path / "check.py").write_text(probe.CHECK, encoding="utf-8")
    (tmp_path / "uses.json").write_text(json.dumps(uses), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "check.py", "uses.json"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    got = {r["key"]: r for r in map(json.loads, out.splitlines())}
    assert got["alias"]["resolved"] is True and got["alias"]["objkind"] == "class"
    assert got["dual"]["binds"] is False and "client" in got["dual"]["bind_error"]
    assert got["gone"]["resolved"] is False


def test_probe_deprecation_is_about_the_name(tmp_path: Path) -> None:
    """DT-QA-08's truth: a warning that names the name, its own __deprecated__, a deprecation wrapper that isn't about
    a parameter, a docstring note about the object; not a parameter's note, a base's marker or another warning."""
    import json
    import subprocess

    import probe

    (tmp_path / "deppkg").mkdir()
    (tmp_path / "deppkg" / "__init__.py").write_text(
        '''import functools, warnings


def deprecated(f):
    @functools.wraps(f)
    def warning_emitting_wrapper(*a, **k):
        return f(*a, **k)
    return warning_emitting_wrapper


def deprecate_kwarg(name):
    def deco(f):
        @functools.wraps(f)
        def wrapper(*a, **k):
            return f(*a, **k)
        return wrapper
    return deco


@deprecated
def wrapped():
    pass


@deprecate_kwarg("x")
def kwarg_only(x=None):
    pass


def param_note(sep=None):
    """Read.

    Parameters
    ----------
    delim_whitespace : bool
        .. deprecated:: 2.2.0
    """


def top_note():
    """Summary.

    .. deprecated:: 1.0
    """


def calling_note(*clauses):
    """Join clauses.

    .. deprecated:: 1.4
       Calling it with no arguments is deprecated.
    """


from .deprecation import make_keyword_only


@make_keyword_only("x")
def keyword_wrapped(x=None):
    pass


class Old:
    __deprecated__ = "use New"


class Child(Old):
    pass


def __getattr__(name):
    if name == "legacy":
        warnings.warn("`legacy` is deprecated", DeprecationWarning, stacklevel=2)
        return 1
    if name == "noisy":
        warnings.warn("something else is deprecated", DeprecationWarning, stacklevel=2)
        return 2
    raise AttributeError(name)
''',
        encoding="utf-8",
    )
    (
        tmp_path / "deppkg" / "deprecation.py"
    ).write_text(  # matplotlib keeps make_keyword_only in deprecation.py
        "import functools\n\n\ndef make_keyword_only(name):\n    def deco(f):\n        @functools.wraps(f)\n"
        "        def wrapper(*a, **k):\n            return f(*a, **k)\n        return wrapper\n    return deco\n",
        encoding="utf-8",
    )
    names = [
        "wrapped",
        "kwarg_only",
        "param_note",
        "top_note",
        "calling_note",
        "keyword_wrapped",
        "Old",
        "Child",
    ]
    names += ["legacy", "noisy"]
    uses = [{"key": n, "kind": "name", "path": f"deppkg.{n}"} for n in names]
    (tmp_path / "check.py").write_text(probe.CHECK, encoding="utf-8")
    (tmp_path / "uses.json").write_text(json.dumps(uses), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "check.py", "uses.json"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    got = {r["key"]: r for r in map(json.loads, out.splitlines())}
    flagged = {k for k, r in got.items() if r["deprecation"]}
    want = {"top_note", "Old", "legacy"} | ({"wrapped"} if sys.version_info >= (3, 11) else set())
    assert flagged == want
    assert got["param_note"]["deprecation_other"] and got["noisy"]["deprecation_other"]


def test_regressions_on_shared_uses() -> None:
    base = {
        "uses": {
            "u|a": {"label": "breaks", "check_symbols": True, "diff_versions": True},
            "u|b": {"label": "works", "check_symbols": False, "diff_versions": None},
        },
        "tracebacks": {"t1": True},
    }
    same = {"uses": dict(base["uses"]), "tracebacks": {"t1": True, "t2": False}}  # a new case never fails it
    assert score.regressions(same, base) == []
    worse = {
        "uses": {
            "u|a": {"label": "breaks", "check_symbols": False, "diff_versions": True},
            "u|b": {"label": "works", "check_symbols": True, "diff_versions": None},
        },
        "tracebacks": {"t1": False},
    }
    failed = score.regressions(worse, base)
    assert any(f.startswith("check_symbols recall") for f in failed)
    assert any(f.startswith("check_symbols false-positive rate") for f in failed)
    assert any(f.startswith("explain_error") for f in failed)
    assert not any(f.startswith("diff_versions") for f in failed)
