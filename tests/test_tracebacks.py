from deptime.tracebacks import MAX_INPUT_CHARS, parse_traceback

HTTPX_TB = """\
Traceback (most recent call last):
  File "/home/u/app/main.py", line 3, in <module>
    client = httpx.Client(proxies="http://localhost:8030")
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: Client.__init__() got an unexpected keyword argument 'proxies'
"""


def test_unexpected_kwarg_with_call_site_names():
    s = parse_traceback(HTTPX_TB)
    assert s is not None
    assert (s.kind, s.owner, s.kwarg, s.exception_type) == (
        "unexpected_kwarg",
        "Client.__init__",
        "proxies",
        "TypeError",
    )
    assert "httpx.Client" in s.referenced_names
    assert s.frames[0].code.startswith("client = httpx.Client(")
    assert s.frames[0].module is None  # user code, not site-packages


def test_import_name_from_module():
    s = parse_traceback(
        "Traceback (most recent call last):\n"
        '  File "/app/x.py", line 1, in <module>\n'
        "    from langchain.chains import LLMChain\n"
        "ImportError: cannot import name 'LLMChain' from 'langchain.chains' (/venv/lib/python3.12/site-packages/langchain/chains/__init__.py)\n"
    )
    assert (s.kind, s.module, s.name) == ("import_name", "langchain.chains", "LLMChain")
    assert "langchain.chains.LLMChain" in s.referenced_names


def test_module_not_found_submodule():
    s = parse_traceback("ModuleNotFoundError: No module named 'langchain.chat_models'")
    assert (s.kind, s.module) == ("module_missing", "langchain.chat_models")


def test_module_attribute_with_suggestion_suffix():
    s = parse_traceback(
        "AttributeError: module 'openai' has no attribute 'ChatCompletion'. Did you mean: 'completions'?"
    )
    assert (s.kind, s.module, s.name, s.suggestion) == (
        "module_attribute",
        "openai",
        "ChatCompletion",
        "completions",
    )


def test_object_attribute_and_library_frames():
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/etl.py", line 9, in run\n'
        "    df = df.append(row)\n"
        '  File "/venv/lib/python3.12/site-packages/pandas/core/generic.py", line 6299, in __getattr__\n'
        "    return object.__getattribute__(self, name)\n"
        "AttributeError: 'DataFrame' object has no attribute 'append'\n"
    )
    s = parse_traceback(tb)
    assert (s.kind, s.owner, s.name) == ("object_attribute", "DataFrame", "append")
    assert s.library_modules == ["pandas.core.generic"]


def test_missing_required_argument():
    s = parse_traceback("TypeError: Agent.__init__() missing 1 required keyword-only argument: 'model'")
    assert (s.kind, s.owner, s.missing_args) == ("missing_argument", "Agent.__init__", ["model"])


def test_chained_exceptions_use_the_last_one():
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/a.py", line 1, in <module>\n'
        "    import foo\n"
        "ModuleNotFoundError: No module named 'foo'\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\n"
        '  File "/app/a.py", line 3, in <module>\n'
        "    from bar import baz\n"
        "ImportError: cannot import name 'baz' from 'bar' (/x/bar.py)\n"
    )
    s = parse_traceback(tb)
    assert (s.kind, s.module, s.name) == ("import_name", "bar", "baz")
    assert len(s.frames) == 1


def test_pytest_prefix_and_ansi_are_stripped():
    s = parse_traceback("\x1b[31mE   AttributeError: module 'numpy' has no attribute 'float'\x1b[0m")
    assert (s.kind, s.module, s.name) == ("module_attribute", "numpy", "float")


def test_circular_import_is_flagged():
    s = parse_traceback(
        "ImportError: cannot import name 'x' from partially initialized module 'app.models' (most likely due to a circular import)"
    )
    assert s.kind == "circular_import"


def test_no_exception_line():
    assert parse_traceback("everything is fine") is None


def test_long_log_keeps_the_final_exception():
    # Q-02: the exception line comes last, so a long log must be cut from the front, not the back.
    noise = "ValueError: earlier unrelated\n" + "collected test output line\n" * 1200  # ~31K chars
    assert len(noise) > MAX_INPUT_CHARS
    s = parse_traceback(noise + HTTPX_TB)
    assert s is not None
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "Client.__init__", "proxies")
