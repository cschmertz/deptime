"""Standard-library module availability per Python version (DT-DATA-06)."""

from importlib import resources

from deptime import stdlib
from deptime.stdlib import LINE_RE, StdlibModule


def _tb(module: str, path: str = "/usr/lib/python3.12/runpy.py") -> str:
    return (
        "Traceback (most recent call last):\n"
        f'  File "{path}", line 1, in <module>\n'
        f"    import {module}\n"
        f"ModuleNotFoundError: No module named '{module}'\n"
    )


def test_the_shipped_table_reads_and_has_the_known_removals():
    text = resources.files("deptime").joinpath("stdlib_versions.txt").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert len(lines) > 300 and all(LINE_RE.match(line) for line in lines)
    assert stdlib.lookup("distutils") == StdlibModule("distutils", 0, 11)  # PEP 632
    assert stdlib.lookup("cgi") == StdlibModule("cgi", 0, 12)  # PEP 594
    assert stdlib.lookup("tomllib") == StdlibModule("tomllib", 11, None)
    # Only for type checkers, so `No module named '_typeshed'` is right on every Python.
    assert stdlib.lookup("_typeshed") is None and stdlib.lookup("numpy") is None


def test_a_submodule_not_listed_shares_its_parent_s_lifetime():
    assert stdlib.lookup("distutils.util").module == "distutils"
    assert stdlib.lookup("distutils.command.bdist_msi") == StdlibModule("distutils.command.bdist_msi", 0, 10)
    assert stdlib.lookup("importlib.resources.abc") == StdlibModule("importlib.resources.abc", 11, None)


def test_python_versions():
    assert [stdlib.minor_of(v) for v in ["3.12", "3.12.1", "3.13.0rc2", "3.13t", "3.9"]] == [
        12,
        12,
        13,
        13,
        9,
    ]
    assert [stdlib.minor_of(v) for v in ["2.7", "3.123", "abc", "", None]] == [None] * 5


def test_a_removed_module_on_a_later_python(dt):
    res = dt.explain_error(_tb("distutils"))
    (f,) = res.findings
    assert (f.package, f.symbol, f.issue, f.confidence) == ("python", "distutils", "module_removed", "high")
    assert (f.installed_version, f.resolved_version, f.last_version_with, f.first_version_without) == (
        "3.12",
        "3.12",
        "3.11",
        "3.12",
    )
    assert f.pin_to_restore == "python<=3.11"
    assert f.explanation == (
        "`distutils` was removed from the standard library in Python 3.12; 3.11 is the last version with it. "
        "You run Python 3.12 (from the traceback paths)."
    )
    # Not a distribution DepTime lacks, and no "inferred from the latest indexed release".
    assert res.not_covered == [] and res.note is None


def test_installed_versions_give_the_python_version(dt):
    res = dt.explain_error(_tb("distutils"), {"python": "3.11.9"})
    assert res.findings == [] and res.not_covered == []
    assert res.note.startswith(
        "`distutils` is in the standard library of Python 3.11.9 (typeshed's stdlib/VERSIONS)"
    )
    res = dt.explain_error(_tb("distutils", "/app/main.py"), pip_freeze="Python==3.13.1\n")
    assert (
        res.findings[0].installed_version == "3.13.1"
        and "You run Python 3.13.1." in res.findings[0].explanation
    )


def test_without_a_python_version_the_change_is_medium(dt):
    (f,) = dt.explain_error(_tb("imp", "/app/main.py")).findings
    assert (f.issue, f.confidence, f.installed_version, f.last_version_with) == (
        "module_removed",
        "medium",
        None,
        "3.11",
    )
    assert "If you run Python 3.12 or later, that is the cause" in f.explanation
    (f,) = dt.explain_error(_tb("tomllib", "/app/main.py")).findings
    assert (f.issue, f.confidence, f.first_version_with) == ("symbol_not_yet_added", "medium", "3.11")
    # Added before the oldest Python typeshed supports (3.9), so taken to be there.
    res = dt.explain_error(_tb("_lzma", "/app/main.py"))
    assert res.findings == [] and res.note.startswith(
        "`_lzma` is in the standard library of Python 3.3 and later"
    )
    res = dt.explain_error(_tb("_sqlite3", "/app/main.py"))
    assert res.findings == [] and res.note.startswith(
        "`_sqlite3` is in the standard library of every Python 3"
    )


def test_a_module_added_later(dt):
    (f,) = dt.explain_error(_tb("tomllib", "/usr/lib/python3.10/runpy.py")).findings
    assert (f.issue, f.confidence, f.last_version_without, f.first_version_with, f.pin_to_restore) == (
        "symbol_not_yet_added",
        "high",
        "3.10",
        "3.11",
        "python>=3.11",
    )
    assert f.explanation == (
        "`tomllib` was added to the standard library in Python 3.11. You run Python 3.10 (from the traceback paths)."
    )


def test_a_submodule_of_a_removed_module(dt):
    (f,) = dt.explain_error(_tb("distutils.msvccompiler")).findings
    assert (f.symbol, f.issue) == ("distutils", "module_removed")
    assert "`distutils.msvccompiler` is part of it." in f.explanation
    res = dt.explain_error(_tb("importlib.resources.nope"))
    assert res.findings == [] and "can't tell whether `importlib.resources.nope` ever existed" in res.note


def test_a_module_the_python_has_is_not_a_version_change(dt):
    res = dt.explain_error(_tb("_lzma"))
    assert res.findings == [] and res.not_covered == []
    assert "only when Python was built with the system library they need" in res.note
    assert "No version-history explanation found" not in res.note


def test_a_library_that_imports_a_removed_module(dt):
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/main.py", line 1, in <module>\n'
        "    import litellm\n"
        '  File "/venv/lib/python3.12/site-packages/litellm/timeout.py", line 20, in <module>\n'
        "    import imp\n"
        "ModuleNotFoundError: No module named 'imp'\n"
    )
    (f,) = dt.explain_error(tb).findings
    assert f.explanation.endswith(
        "The import is in litellm's code: a litellm release made for Python 3.12 may not need it; DepTime does not "
        "check litellm's side."
    )


def test_a_name_inside_a_standard_module_is_not_covered(dt):
    res = dt.explain_error(
        "ImportError: cannot import name 'Mapping' from 'collections' (/usr/lib/python3.10/x.py)"
    )
    assert res.findings == [] and res.not_covered == ["collections"]
    assert "`collections` is in the standard library: DepTime knows which standard modules" in res.note
