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


# DT-CORE-19: real-world error text, each shape taken from a benchmark case.


def test_indented_exception_line():
    s = parse_traceback(
        "     super(Chrome, self).__init__(\n"
        "     TypeError: WebDriver.__init__() got an unexpected keyword argument 'executable_path'"
    )
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "WebDriver.__init__", "executable_path")


def test_pytest_long_e_prefix_and_location_line():
    s = parse_traceback(
        "E           TypeError: skip() got an unexpected keyword argument 'msg'\n"
        "venv/lib/python3.12/site-packages/odl/test/ray_trafo_test.py:362: TypeError"
    )
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "skip", "msg")


def test_compose_log_prefix_and_trailing_warning():
    s = parse_traceback(
        "api-1  | AttributeError: 'QdrantClient' object has no attribute 'search'\n"
        "/usr/local/lib/python3.12/site-packages/qdrant_client/qdrant_remote.py:282: UserWarning: incompatible"
    )
    assert (s.kind, s.owner, s.name) == ("object_attribute", "QdrantClient", "search")


def test_commented_exception_line():
    s = parse_traceback(
        "router = Router(on_startup=on_startup)\n"
        "# TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'"
    )
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "Router.__init__", "on_startup")


def test_plain_exception_type_wrapping_a_known_message():
    s = parse_traceback("Exception: module 'dspy' has no attribute 'Assert'")
    assert (s.kind, s.module, s.name, s.exception_type) == ("module_attribute", "dspy", "Assert", "Exception")


def test_message_without_exception_line_infers_the_type():
    cases = {
        "INFO - Server initializing\nERROR - Error starting server: 'FastMCP' object has no attribute 'get_tools'": (
            "object_attribute",
            "AttributeError",
        ),
        "Error during analysis: Client.chat() got an unexpected keyword argument 'tools'": (
            "unexpected_kwarg",
            "TypeError",
        ),
        '{"error": "TypeError", "detail": "Messages.create() got an unexpected keyword argument \'temperature\'"}': (
            "unexpected_kwarg",
            "TypeError",
        ),
        "No module named 'langgraph.graph.graph'": ("module_missing", "ModuleNotFoundError"),
    }
    for text, (kind, exc_type) in cases.items():
        s = parse_traceback(text)
        assert s is not None, text
        assert (s.kind, s.exception_type) == (kind, exc_type), text


def test_unquoted_module_attribute():
    s = parse_traceback("AttributeError: module aiohttp has no attribute ClientWSTimeout")
    assert (s.kind, s.module, s.name) == ("module_attribute", "aiohttp", "ClientWSTimeout")


def test_unexpected_kwarg_without_a_callable_takes_it_from_the_call_site():
    s = parse_traceback(
        "km.mean_squared_error(true, pred, squared=False)\n\n"
        "TypeError: got an unexpected keyword argument 'squared'"
    )
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "mean_squared_error", "squared")
    assert "km.mean_squared_error" in s.referenced_names


def test_an_unrecognised_exception_line_is_not_replaced_by_an_earlier_message():
    s = parse_traceback(
        "WARNING: module 'x' has no attribute 'y'\n"
        "pydantic.errors.PydanticUserError: `regex` is removed. use `pattern` instead"
    )
    assert (s.kind, s.exception_type) == ("unknown", "PydanticUserError")
