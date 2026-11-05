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
        "pydantic.errors.PydanticUserError: A non-annotated attribute was detected: `x = 1`."
    )
    assert (s.kind, s.exception_type) == ("unknown", "PydanticUserError")


def test_library_migration_errors_name_the_removed_api():
    # DT-CORE-05: messages copied from numpy 2.0.2, pydantic 2.9.2 and openai 1.55.0 wheels.
    cases = {
        "AttributeError: `np.float_` was removed in the NumPy 2.0 release. Use `np.float64` instead.. "
        "Did you mean: 'float16'?": ("module_attribute", "numpy", "float_"),
        "pydantic.errors.PydanticImportError: `BaseSettings` has been moved to the `pydantic-settings` package. "
        "See https://docs.pydantic.dev/2.6/migration/#basesettings-has-moved-to-pydantic-settings for more details."
        "\n\nFor further information visit https://errors.pydantic.dev/2.6/u/import-error": (
            "import_name",
            "pydantic",
            "BaseSettings",
        ),
        "pydantic.errors.PydanticImportError: `pydantic:ConstrainedInt` has been removed in V2.": (
            "import_name",
            "pydantic",
            "ConstrainedInt",
        ),
        # No "Error" suffix, and the message starts on a later line.
        "openai.lib._old_api.APIRemovedInV1: \n\nYou tried to access openai.ChatCompletion, but this is no longer "
        "supported in openai>=1.0.0 - see the README at https://github.com/openai/openai-python for the API.\n": (
            "module_attribute",
            "openai",
            "ChatCompletion",
        ),
    }
    for text, expected in cases.items():
        s = parse_traceback(text)
        assert s is not None, text
        assert (s.kind, s.module, s.name) == expected, text


def test_numpy_2_class_member_removals_read_like_the_standard_message():
    # DT-CORE-21: numpy 2.0.0-2.3.5 raise these from C (strings in _multiarray_umath); from 2.4.0 the same access
    # raises "'numpy.ndarray' object has no attribute 'itemset'", so both must give the same signal.
    cases = {
        "AttributeError: `itemset` was removed from the ndarray class in NumPy 2.0. Use `arr[index] = value` instead.": (
            "numpy.ndarray",
            "itemset",
        ),
        "AttributeError: `ptp` was removed from the ndarray class in NumPy 2.0. Use np.ptp(arr, ...) instead.": (
            "numpy.ndarray",
            "ptp",
        ),
        "AttributeError: `newbyteorder` was removed from scalar types in NumPy 2.0. "
        "Use `sc.view(sc.dtype.newbyteorder(order))` instead.": ("numpy.generic", "newbyteorder"),
        "E   AttributeError: `itemset` was removed from scalar types in NumPy 2.0 because scalars are immutable.": (
            "numpy.generic",
            "itemset",
        ),
    }
    for text, (owner, name) in cases.items():
        s = parse_traceback(text)
        assert s is not None, text
        assert (s.kind, s.owner, s.name, s.module) == ("object_attribute", owner, name, None), text
        assert s.exception_type == "AttributeError"
    standard = parse_traceback("AttributeError: 'numpy.ndarray' object has no attribute 'itemset'")
    assert (standard.kind, standard.owner, standard.name) == ("object_attribute", "numpy.ndarray", "itemset")


def test_pydantic_removed_kwargs_take_the_raising_function():
    s = parse_traceback(
        "Traceback (most recent call last):\n"
        '  File "/env/lib/python3.10/site-packages/pydantic/types.py", line 894, in conlist\n'
        "    raise PydanticUserError(\n"
        "pydantic.errors.PydanticUserError: `unique_items` is removed, use `Set` instead(this feature is discussed)\n"
    )
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "conlist", "unique_items")
    # Without frames, Field(): pydantic documents `removed-kwargs` as Field()'s error.
    s = parse_traceback("pydantic.errors.PydanticUserError: `regex` is removed. use `pattern` instead")
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "Field", "regex")


def test_pluggy_hook_argument_behind_the_internalerror_prefix():
    s = parse_traceback(
        "INTERNALERROR> Traceback (most recent call last):\n"
        'INTERNALERROR>   File "/venv/lib/python3.14/site-packages/pluggy/_manager.py", line 343, in _verify_hook\n'
        "INTERNALERROR>     raise PluginValidationError(\n"
        "INTERNALERROR> pluggy._manager.PluginValidationError: Plugin '140630964876496' for hook 'pytest_collect_file'\n"
        "INTERNALERROR> hookimpl definition: pytest_collect_file(path, parent)\n"
        "INTERNALERROR> Argument(s) {'path'} are declared in the hookimpl but can not be found in the hookspec\n"
    )
    assert (s.kind, s.owner, s.kwarg, s.exception_type) == (
        "unexpected_kwarg",
        "pytest_collect_file",
        "path",
        "PluginValidationError",
    )
    assert s.frames[0].module == "pluggy._manager"


# S3 (DT-SPIKE-03): shapes from bench/s3/cases.jsonl, real output of Python 3.10-3.14.
CHAINED_FROM_TB = """\
Traceback (most recent call last):
  File "/home/dev/project/chain_from.py", line 3, in start
    import app
  File "/home/dev/project/app.py", line 1, in <module>
    from langchain.chains import LLMChain
ModuleNotFoundError: No module named 'langchain.chains'

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/home/dev/project/chain_from.py", line 11, in <module>
    start()
    ~~~~~^^
  File "/home/dev/project/chain_from.py", line 7, in start
    raise RuntimeError("could not start the service") from exc
RuntimeError: could not start the service
"""

CHAINED_DURING_TB = """\
Traceback (most recent call last):
  File "/home/dev/project/chain_during.py", line 3, in start
    import app
  File "/home/dev/project/app.py", line 1, in <module>
    from pydantic import BaseSettings
  File "/home/dev/project/.venv/lib/python3.13/site-packages/pydantic/__init__.py", line 442, in __getattr__
    return _getattr_migration(attr_name)
  File "/home/dev/project/.venv/lib/python3.13/site-packages/pydantic/_migration.py", line 305, in wrapper
    raise PydanticImportError(
    ...<3 lines>...
    )
pydantic.errors.PydanticImportError: `BaseSettings` has been moved to the `pydantic-settings` package. See https://pydantic.dev/docs/validation/2.14/migration/#basesettings-has-moved-to-pydantic-settings

For further information visit https://errors.pydantic.dev/2.14/u/import-error

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/home/dev/project/chain_during.py", line 11, in <module>
    start()
    ~~~~~^^
  File "/home/dev/project/chain_during.py", line 7, in start
    raise RuntimeError("could not start the service")
RuntimeError: could not start the service
"""

TASKGROUP_TB = """\
  + Exception Group Traceback (most recent call last):
  |   File "/home/dev/project/with_taskgroup.py", line 15, in <module>
  |     asyncio.run(run())
  |   File "/home/dev/project/with_taskgroup.py", line 11, in run
  |     async with asyncio.TaskGroup() as tg:
  |                ^^^^^^^^^^^^^^^^^^^
  |   File "/opt/s3/python/cpython-3.12.16-linux-x86_64-gnu/lib/python3.12/asyncio/taskgroups.py", line 164, in _aexit
  |     raise BaseExceptionGroup(
  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
  +-+---------------- 1 ----------------
    | Traceback (most recent call last):
    |   File "/home/dev/project/with_taskgroup.py", line 7, in job
    |     app.main()
    |   File "/home/dev/project/app.py", line 5, in main
    |     return np.float_(1.5)
    |            ^^^^^^^^^
    |   File "/home/dev/project/.venv/lib/python3.12/site-packages/numpy/__init__.py", line 763, in __getattr__
    |     raise AttributeError(
    | AttributeError: `np.float_` was removed in the NumPy 2.0 release. Use `np.float64` instead.
    +------------------------------------
"""


def test_raise_from_reads_the_cause_when_the_wrapper_is_not_a_version_symptom():
    s = parse_traceback(CHAINED_FROM_TB)
    assert (s.kind, s.module, s.exception_type) == (
        "module_missing",
        "langchain.chains",
        "ModuleNotFoundError",
    )
    assert s.wrapped_in == "RuntimeError"
    assert [f.function for f in s.frames] == ["start", "<module>"]  # the cause's own block


def test_during_handling_reads_the_library_exception_it_interrupted():
    s = parse_traceback(CHAINED_DURING_TB)
    assert (s.kind, s.module, s.name) == ("import_name", "pydantic", "BaseSettings")
    assert s.wrapped_in == "RuntimeError"


def test_a_classified_last_exception_is_kept_over_its_cause():
    tb = CHAINED_FROM_TB.replace(
        "RuntimeError: could not start the service", "AttributeError: module 'app' has no attribute 'serve'"
    )
    s = parse_traceback(tb)
    assert (s.kind, s.module, s.name, s.wrapped_in) == ("module_attribute", "app", "serve", None)


def test_an_unrelated_earlier_exception_is_not_a_cause():
    # Two separate errors in one log: no chain marker joins them, so the last one stands alone.
    tb = CHAINED_FROM_TB.replace("The above exception was the direct cause of the following exception:", "")
    s = parse_traceback(tb)
    assert (s.kind, s.exception_type, s.wrapped_in) == ("unknown", "RuntimeError", None)


def test_exception_group_sub_exception_keeps_library_message_and_frames():
    s = parse_traceback(TASKGROUP_TB)
    assert (s.kind, s.module, s.name, s.exception_type) == (
        "module_attribute",
        "numpy",
        "float_",
        "AttributeError",
    )
    assert s.library_modules == ["numpy"]


def test_lazy_loader_attribute_message():
    # huggingface_hub (and scientific-python's lazy_loader) word a missing attribute their own way.
    s = parse_traceback(
        "app.py:5: in main\n"
        '    return huggingface_hub.cached_download("https://huggingface.co/gpt2/resolve/main/config.json")\n'
        ".venv/lib/python3.12/site-packages/huggingface_hub/__init__.py:1251: in __getattr__\n"
        '    raise AttributeError(f"No {package_name} attribute {name}")\n'
        "E   AttributeError: No huggingface_hub attribute cached_download. Did you mean: 'hf_hub_download'?\n"
    )
    assert (s.kind, s.module, s.name, s.suggestion) == (
        "module_attribute",
        "huggingface_hub",
        "cached_download",
        "hf_hub_download",
    )


def test_python39_constructor_message_takes_the_class_from_the_call():
    # DT-CORE-26: before 3.10 a constructor's TypeError names no class.
    s = parse_traceback(
        "Traceback (most recent call last):\n"
        '  File "/venv/lib/python3.9/site-packages/langchain_google_genai/chat_models.py", line 645, in _prepare_chat\n'
        "    client = genai.GenerativeModel(\n"
        "TypeError: __init__() got an unexpected keyword argument 'tools'\n"
    )
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "GenerativeModel.__init__", "tools")
    assert "genai.GenerativeModel" in s.referenced_names


def test_python39_constructor_message_keeps_init_without_a_class_call():
    s = parse_traceback(
        "Traceback (most recent call last):\n"
        '  File "/app/x.py", line 3, in <module>\n'
        "    super().__init__(**kwargs)\n"
        "TypeError: __init__() got an unexpected keyword argument 'tools'\n"
    )
    assert s.owner == "__init__"


def test_import_name_without_its_module_takes_it_from_the_import_line():
    # DT-QA-01: a pasted message trimmed to `cannot import name 'X'`; the frame's import names the module.
    s = parse_traceback(
        'File ".../llama_index/vector_stores/qdrant/base.py", line 54, in <module>\n'
        "    from qdrant_client.qdrant_fastembed import IDF_EMBEDDING_MODELS\n"
        "ImportError: cannot import name 'IDF_EMBEDDING_MODELS'\n"
    )
    assert (s.kind, s.module, s.name) == (
        "import_name",
        "qdrant_client.qdrant_fastembed",
        "IDF_EMBEDDING_MODELS",
    )


GRADIO_TB = (
    "Traceback (most recent call last):\n"
    '  File "/venv/lib/python3.12/site-packages/smolagents/gradio_ui.py", line 419, in create_app\n'
    '    with gr.Blocks(theme="ocean", fill_height=True) as demo:\n'
    "         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
    '  File "/venv/lib/python3.12/site-packages/gradio/blocks.py", line 1071, in __init__\n'
    "    super().__init__(render=False, **kwargs)\n"
    "TypeError: BlockContext.__init__() got an unexpected keyword argument 'theme'\n"
)


def test_an_argument_forwarded_by_kwargs_is_also_the_called_class_s():
    # DT-CORE-27: gradio 6 moved `theme` from Blocks() to launch(); Blocks.__init__ passes **kwargs to its parent.
    s = parse_traceback(GRADIO_TB)
    assert (s.kind, s.owner, s.kwarg) == ("unexpected_kwarg", "BlockContext.__init__", "theme")
    assert s.forwarded_owners == ["Blocks.__init__"]
    # Only an __init__ that passes **kwargs on, and only a class the frame before it calls.
    assert parse_traceback(GRADIO_TB.replace("**kwargs", "theme=theme")).forwarded_owners == []
    assert parse_traceback(GRADIO_TB.replace("in __init__", "in render")).forwarded_owners == []
    assert parse_traceback(GRADIO_TB.replace("gr.Blocks(", "gr.blocks(")).forwarded_owners == []


def _frame(path: str) -> str:
    return f'Traceback (most recent call last):\n  File "{path}", line 1, in <module>\n    import x\n'


def test_the_python_version_comes_from_the_frames_paths():
    # DT-DATA-06: which standard-library modules exist depends on it.
    end = "ModuleNotFoundError: No module named 'distutils'\n"
    for path, version in [
        ("/venv/lib/python3.12/site-packages/numpy/distutils/core.py", "3.12"),
        ("/home/u/.pyenv/versions/3.11.9/lib/python3.11/importlib/__init__.py", "3.11"),
        ("/usr/local/lib64/python3.13t/site-packages/a.py", "3.13"),  # free-threaded build
        (r"C:\Users\u\AppData\Local\Programs\Python\Python310\lib\site-packages\a.py", "3.10"),
        (
            r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.9_3.9.13_x64__qbz5n2kfra8p0\lib\a.py",
            "3.9",
        ),
    ]:
        assert parse_traceback(_frame(path) + end).python_version == version, path
    # No minor version in the path (a Windows venv, Debian's dist-packages), or paths that disagree: unknown.
    for text in [
        _frame(r"D:\project\venv\lib\site-packages\agents\__init__.py") + end,
        _frame("/usr/lib/python3/dist-packages/apt/__init__.py") + end,
        _frame("/home/u/python3.10-notes/app.py") + end,
        _frame("/a/lib/python3.11/x.py") + _frame("/b/lib/python3.12/y.py").split("\n", 1)[1] + end,
        end,
    ]:
        assert parse_traceback(text).python_version is None, text
    assert (
        parse_traceback(_frame("/venv/lib/python3.12/site-packages/a.py") + end).to_model().python_version
        == "3.12"
    )


def test_the_python_version_also_comes_from_ipython_s_frame_lines():
    # IPython 8 prints `File <path>:<line>`, and JupyterLab links each file (natasha/corus#86).
    end = "\nModuleNotFoundError: No module named 'cgi'\n"
    ipython = (
        "File ~/.cache/pypoetry/virtualenvs/x-py3.12/lib/python3.12/site-packages/skdatasets/cran.py:14\n"
    )
    jupyter = (
        "http://localhost:8888/app/.venv/lib/python3.13/site-packages/corus/third/WikiExtractor.py#line=62\n"
    )
    assert parse_traceback(ipython + "---> 14 import cgi" + end).python_version == "3.12"
    assert parse_traceback(jupyter + "---> 63 import cgi" + end).python_version == "3.13"
    # Other text that names a path doesn't count.
    assert parse_traceback("see /usr/lib/python3.12/cgi.py" + end).python_version is None
