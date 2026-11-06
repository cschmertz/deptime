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
        "What's New in Python 3.12 says to use `setuptools`. You run Python 3.12 (from the traceback paths)."
    )
    # DT-DATA-48: What's New's replacement, quoted; another distribution, so no fix the tables can check.
    assert [(r.target, r.source) for r in f.replacements] == [("setuptools", "curated")] and f.fix is None
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


def test_a_name_inside_a_standard_module_that_never_changes_is_not_covered(dt):
    # Since DT-DATA-47 a name whose presence changes is answered (below); one typeshed never lists isn't.
    res = dt.explain_error(
        "ImportError: cannot import name 'Mappings' from 'collections' (/usr/lib/python3.10/x.py)"
    )
    assert res.findings == [] and res.not_covered == ["collections"]
    assert "`collections` is in the standard library. DepTime knows which standard modules" in res.note
    assert "`Mappings` isn't one of them." in res.note


def test_the_shipped_names_table_reads_and_has_the_known_changes():
    # DT-DATA-47: names inside standard modules, from typeshed's stubs (tools/stdlib_names.py).
    text = resources.files("deptime").joinpath("stdlib_names.txt").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert len(lines) > 1000 and all(stdlib.NAME_LINE_RE.match(line) for line in lines)
    mapping = stdlib.lookup_name("collections", "Mapping")
    assert (mapping.ranges, mapping.moved) == (((9, 9),), "collections.abc.Mapping")
    assert stdlib.lookup_name("asyncio", "coroutine").ranges == ((9, 10),)
    assert stdlib.lookup_name("configparser", "SafeConfigParser").ranges == ((9, 11),)
    assert stdlib.lookup_name("ssl", "wrap_socket").ranges == ((9, 11),)
    assert stdlib.lookup_name("typing", "Self").ranges == ((11, None),)
    # Every version with the module has it, or it isn't a public name: not listed.
    assert stdlib.lookup_name("collections", "OrderedDict") is None
    assert stdlib.lookup_name("collections", "_chain") is None


def _import_tb(name: str, module: str, minor: int) -> str:
    return f"ImportError: cannot import name '{name}' from '{module}' (/usr/lib/python3.{minor}/{module}/__init__.py)\n"


def test_a_removed_standard_name(dt):
    res = dt.explain_error(_import_tb("Mapping", "collections", 10))
    (f,) = res.findings
    assert (f.package, f.symbol, f.issue, f.confidence) == (
        "python",
        "collections.Mapping",
        "symbol_removed",
        "high",
    )
    assert (f.installed_version, f.last_version_with, f.first_version_without, f.pin_to_restore) == (
        "3.10",
        "3.9",
        "3.10",
        "python<=3.9",
    )
    assert [(r.target, r.confidence, r.source) for r in f.replacements] == [
        ("collections.abc.Mapping", 0.75, "moved_heuristic")
    ]
    assert (f.fix.kind, f.fix.new, f.fix.same_object, f.fix.verified_at) == (
        "path",
        "collections.abc.Mapping",
        True,
        "3.10",
    )
    assert f.explanation.startswith(
        "`collections.Mapping` was removed from the standard library in Python 3.10; 3.9 is the last version with it"
    )
    assert res.not_covered == []


def test_a_standard_name_added_later_or_present(dt):
    (f,) = dt.explain_error(_import_tb("Self", "typing", 10)).findings
    assert (f.issue, f.last_version_without, f.first_version_with, f.pin_to_restore) == (
        "symbol_not_yet_added",
        "3.10",
        "3.11",
        "python>=3.11",
    )
    present = dt.explain_error(_import_tb("Self", "typing", 12))
    assert present.findings == [] and "isn't a version change" in present.note
    # Without a version, the change at medium; one the table doesn't list keeps the module-only note.
    (f,) = dt.explain_error("AttributeError: module 'asyncio' has no attribute 'coroutine'\n").findings
    assert (f.issue, f.last_version_with, f.confidence) == ("symbol_removed", "3.10", "medium")
    other = dt.explain_error(_import_tb("OrderedDict", "collections", 12))
    assert other.findings == [] and "`OrderedDict` isn't one of them" in other.note


def test_what_s_new_gives_the_replacement_and_the_tables_check_the_fix(dt):
    # DT-DATA-48: curated hints with package "python", applied at query time.
    (f,) = dt.explain_error(_import_tb("SafeConfigParser", "configparser", 13)).findings
    assert [(r.target, r.confidence, r.source) for r in f.replacements] == [
        ("configparser.ConfigParser", 0.9, "curated")
    ]
    assert (f.fix.new, f.fix.same_object, f.fix.verified_at) == ("configparser.ConfigParser", False, "3.13")
    assert "What's New in Python 3.12 says to use `configparser.ConfigParser`." in f.explanation
    # Two targets: no fix. A class's member: the tables can't check it, so no fix either.
    (f,) = dt.explain_error("AttributeError: module 'inspect' has no attribute 'getargspec'\n").findings
    assert [r.target for r in f.replacements] == [
        "inspect.signature",
        "inspect.getfullargspec",
    ] and f.fix is None
    (f,) = dt.explain_error("AttributeError: module 'ssl' has no attribute 'wrap_socket'\n").findings
    assert [r.target for r in f.replacements] == ["ssl.SSLContext.wrap_socket"] and f.fix is None
    # A module for a module.
    (f,) = dt.explain_error(_tb("asyncore")).findings
    assert (f.issue, f.fix.new, f.fix.verified_at) == ("module_removed", "asyncio", "3.12")
