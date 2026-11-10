from deptime.ingest.replacements import (
    Hint,
    PublicNames,
    clean_evidence,
    deprecates_argument,
    mine,
    moved_to_package,
    param_alternatives,
    param_deprecations,
)


def test_use_x_instead():
    hints = mine("OldThing is deprecated, use httpx.NewThing instead.")
    assert hints[0].target == "httpx.NewThing"
    assert hints[0].confidence >= 0.85


def test_moved_to_and_backticks():
    assert mine("`Foo` has been moved to `pkg.core.Foo`.")[0].target == "pkg.core.Foo"


def test_an_import_string_names_module_dot_name():
    # DT-DATA-60: pydantic 2's `getattr_migration` warnings write paths as `module:name`, as `import_string` reads
    # them; the module alone isn't the replacement.
    moved = "`pydantic.utils:to_camel` has been moved to `pydantic.alias_generators:to_pascal`."
    assert [(h.target, h.confidence) for h in mine(moved)] == [("pydantic.alias_generators.to_pascal", 0.85)]
    redirected = "`pydantic.utils:deep_update` has been removed. We are importing from `pydantic.v1.utils:deep_update` instead."
    assert [(h.target, h.confidence) for h in mine(redirected)] == [("pydantic.v1.utils.deep_update", 0.8)]
    # A colon that ends a sentence or starts a URL isn't one.
    assert mine("Use `foo.bar`: it is faster.")[0].target == "foo.bar"
    assert mine("Moved to pkg.mod:https://example.com/x")[0].target == "pkg.mod"


def test_obj_dot_names_the_receivers_own_member():
    # DT-CORE-44: pandas 2.1-2.3's `NDFrame.fillna(method)` warning writes the object the method is called on as `obj.`.
    message = (
        "{type(self).__name__}.fillna with 'method' is deprecated and will raise in a future version. "
        "Use obj.ffill() or obj.bfill() instead."
    )
    alternatives = param_alternatives(message, ["method"], ["value", "method", "axis", "limit"])
    assert [(h.target, h.confidence) for h in alternatives] == [(".ffill", 0.8), (".bfill", 0.79)]
    assert [
        h.target for h in mine("`obj.dict()` was deprecated, you should instead use `obj.model_dump()`.")
    ] == [".model_dump"]
    # A chain or a call with arguments is an expression, not a name.
    assert param_alternatives("To group on axis=1, use obj.T.groupby(...) instead", ["axis"], []) == []
    assert param_alternatives("Use `obj = obj.set_axis(..., copy=False)` instead", ["inplace"], []) == []


def test_now_available_as():
    # DT-DATA-37: sqlalchemy's `became_legacy_20` and `moved_20` say where the name went this way.
    hints = mine("get is deprecated. The method is now available as Session.get()")
    assert [(h.target, h.confidence) for h in hints] == [("Session.get", 0.8)]
    assert (
        mine(
            "declarative_base is deprecated. The ``declarative_base()`` function is now available as"
            " sqlalchemy.orm.declarative_base()."
        )[0].target
        == "sqlalchemy.orm.declarative_base"
    )


def test_a_decorators_alternative_as_the_snapshot_writes_it():
    # DT-DATA-31: `@deprecated(since="0.1.17", alternative="langchain_openai.ChatOpenAI", removal="1.0")`.
    hints = mine(
        "OpenAI was deprecated in 0.1.17 and will be removed in 1.0. Use `langchain_openai.ChatOpenAI` instead."
    )
    assert [(h.target, h.confidence) for h in hints] == [("langchain_openai.ChatOpenAI", 0.85)]


def test_a_called_or_indexed_name_in_backticks_is_the_target():
    # DT-DATA-31: matplotlib's alternatives are code. In backticks the name counts up to the closing backtick.
    def found(message, **kw):
        return [(h.target, h.confidence) for h in mine(message, **kw)]

    assert found("Use `matplotlib.colormaps.register(name)` instead.") == [
        ("matplotlib.colormaps.register", 0.85)
    ]
    assert found("Use `callbacks.process('draw_event', DrawEvent(...))` instead.") == [
        ("callbacks.process", 0.85)
    ]
    assert found("Use `matplotlib.colormaps[name]` or `matplotlib.colormaps.get_cmap(obj)` instead.") == [
        ("matplotlib.colormaps", 0.85),
        ("matplotlib.colormaps.get_cmap", 0.84),
    ]
    # Not an expression that goes on past the call, or one outside backticks.
    assert found("Use `np.dtype(obj).type` instead") == [("np.dtype", 0.6)]
    assert found("Use `arr[index] = value` instead") == [("arr", 0.6)]
    assert found("Use foo(x) instead") == [("foo", 0.6)]
    # The traceback reader still skips the head of an expression.
    assert found("Use `matplotlib.colormaps.register(name)` instead.", skip_expressions=True) == []


def test_no_hint_from_plain_deprecation():
    assert mine("This function is deprecated and will be removed.") == []


def test_param_level_deprecation():
    msg = "The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead."
    out = param_deprecations(msg, ["auth", "proxy", "proxies", "mounts"])
    assert list(out) == ["proxies"]
    assert [h.target for h in out["proxies"]] == ["proxy", "mounts"]


def test_param_level_requires_a_real_param():
    assert param_deprecations("The 'foo' argument is deprecated.", ["bar"]) == {}


def _targets(out):
    return {p: [h.target for h in hints] for p, hints in out.items()}


def test_param_level_use_new_instead_of_old():
    # Q-04: the deprecated parameter is the one the wording marks, not the first one quoted.
    out = param_deprecations("Use 'proxy' instead of 'proxies'.", ["proxy", "proxies", "mounts"])
    assert _targets(out) == {"proxies": ["proxy"]}


def test_param_level_phrasings():
    params = ["proxy", "proxies"]
    for msg in [
        "'proxies' is deprecated in favour of 'proxy'.",
        "Deprecated argument 'proxies': pass 'proxy' instead.",
        "The `proxies` parameter will be removed in 1.0; use `proxy`.",
        'The "proxies" keyword argument is deprecated, use "proxy" instead',
    ]:
        assert _targets(param_deprecations(msg, params)) == {"proxies": ["proxy"]}, msg


def test_function_notice_quoting_a_param_stays_a_function_deprecation():
    # AU-027, real pydantic RootModel.copy text: `include` is mentioned, not deprecated.
    msg = (
        "The `copy` method is deprecated; use `model_copy` instead. See the docstring of "
        "`BaseModel.copy` for details about how to handle `include` and `exclude`."
    )
    assert param_deprecations(msg, ["include", "exclude", "update", "deep"]) == {}
    assert mine(msg)[0].target == "model_copy"


def test_an_argument_as_what_a_sentence_deprecates():
    # DT-DATA-39: a warning one call away deprecates the caller's argument only when its wording deprecates the
    # argument itself, not a value or a way of calling (messages from the radius releases).
    for message, name in [
        ("`regex` has been deprecated, please use `pattern` instead", "regex"),
        (
            "The `proxies` argument is deprecated. The `http_client` argument should be passed instead",
            "proxies",
        ),
        ("log_path has been deprecated, please use log_output", "log_path"),
        ("fingerprint is deprecated, use ssl=Fingerprint(fingerprint) instead", "fingerprint"),
        ("body argument is deprecated for http web exceptions", "body"),
        ("The 'col_count' parameter will be removed in Gradio 6.0.", "col_count"),
        ("The 'default' argument to fields is deprecated. Use 'dump_default' instead.", "default"),
        ("The 'method' keyword in {type(self).__name__}.fillna is deprecated and will be removed", "method"),
        ("The `objects` parameter of `Session.flush` is deprecated", "objects"),
        ("Argument `closed` is deprecated in favor of `inclusive`.", "closed"),
        ("The on_startup and on_shutdown parameters are deprecated, and they will be removed", "on_startup"),
        ("The on_startup and on_shutdown parameters are deprecated, and they will be removed", "on_shutdown"),
        ("`cert=...` is deprecated. Use `verify=<ssl_context>` instead.", "cert"),
        ("Setting per-request cookies=<...> is being deprecated, because the expected behaviour", "cookies"),
        ("pytest.skip(msg=...) is now deprecated, use pytest.skip(reason=...) instead", "msg"),
    ]:
        assert deprecates_argument(message, name), (message, name)
    for message, name in [
        ("Using dict as a `filter` is deprecated. Please use qdrant-client filters directly", "filter"),
        ("Passing an array containing `None` values to `s` is deprecated in NumPy 2.0", "s"),
        ("Omitting `issuer` is deprecated and it will be required in 3.0.", "issuer"),
        (
            "parameter 'timeout' of type 'float' is deprecated, please use 'timeout=ClientWSTimeout(ws_close=...)'",
            "timeout",
        ),
        (
            "float parameter 'receive_timeout' is deprecated, please use parameter 'timeout=ClientWSTimeout(...)'",
            "receive_timeout",
        ),
        ("Passing a tuple to 'row_count' will be removed in Gradio 6.0.", "row_count"),
        ("The canvas_class parameter will become required after the deprecation period", "canvas_class"),
        ("`the (unused) 'frac' key in 'arrowprops'` is deprecated.", "arrowprops"),
        ("The behavior of .astype from {self.dtype} to {dtype} is deprecated.", "dtype"),
        ("Using .astype to convert from timezone-naive dtype to timezone-aware dtype is deprecated", "dtype"),
        (
            'arrays to stack must be passed as a "sequence" type such as list or tuple. Support is deprecated',
            "arrays",
        ),
        ("`verify=<str>` is deprecated.", "verify"),
        ("Use `regex` instead; `pattern` is deprecated", "regex"),
    ]:
        assert not deprecates_argument(message, name), (message, name)


def test_param_level_mismatched_quotes():
    # Real langchain-core text opens with a backtick and closes with an apostrophe.
    msg = "`input_variables' is deprecated and ignored."
    assert _targets(param_deprecations(msg, ["template_file", "input_variables"])) == {"input_variables": []}


def test_private_names_are_never_advice():
    # numpy 2's `numpy.linalg.linalg` and `numpy.core` shims name the private module they were renamed to.
    msg = (
        "The numpy.linalg.linalg has been made private and renamed to numpy.linalg._linalg. All public functions "
        "exported by it are available from numpy.linalg. Please use numpy.linalg.det instead."
    )
    assert [h.target for h in mine(msg)] == ["numpy.linalg.det"]
    assert mine("numpy.core is deprecated and has been renamed to numpy._core.") == []
    assert mine("Use `Client.__call__` instead.")[0].target == "Client.__call__"  # a dunder isn't private


def test_moved_to_a_package_names_the_distribution():
    # DT-CORE-06: pydantic 2's PydanticImportError text names a distribution, not a dotted path.
    msg = (
        "`BaseSettings` has been moved to the `pydantic-settings` package. See "
        "https://docs.pydantic.dev/2.6/migration/#basesettings-has-moved-to-pydantic-settings for more details."
    )
    assert moved_to_package(msg) == "pydantic-settings"
    assert mine(msg) == []  # "moved to the" isn't a target, and the URL's "-moved-to-" isn't a sentence
    assert moved_to_package("`Foo` has been moved to `pkg.core.Foo`.") is None


def test_evidence_from_an_error_message_is_capped_and_has_no_control_characters():
    # DT-CORE-08 (first part): the traceback is untrusted input, and evidence is echoed back to the agent.
    assert clean_evidence("  Use `np.nan`\x00 instead.\u202e\n") == "Use `np.nan` instead."
    assert len(clean_evidence("x" * 1000)) == 300


def test_the_head_of_a_longer_expression_can_be_skipped():
    # numpy 2's "Use `np.dtype(obj).type` instead": `np.dtype` alone isn't the replacement.
    for msg in (
        "Use `np.dtype(obj).type` instead.",
        "Use `arr[index] = value` instead.",
        "Use `a.b().c` instead.",
    ):
        assert mine(msg, skip_expressions=True) == [], msg
    assert mine("Use `np.dtype(obj).type` instead.")[0].target == "np.dtype"  # deprecation text: unchanged
    assert [
        h.target for h in mine("Use `np.asarray` with a proper dtype instead.", skip_expressions=True)
    ] == ["np.asarray"]
    assert mine("Use `np.foo()` instead.", skip_expressions=True)[0].target == "np.foo"


def test_a_name_the_snapshot_could_not_read_to_the_end_is_no_hint():
    # DT-DATA-43: "…" stands for an f-string part the source doesn't say statically; `pandas.api.typing` isn't it.
    msg = "pandas.api.typing.Elsewhere is deprecated. Use pandas.api.typing.… instead."
    assert mine(msg) == []
    assert (
        mine("Use pandas.api.typing.RollingGroupBy instead.")[0].target == "pandas.api.typing.RollingGroupBy"
    )


def test_param_alternatives_named_after_a_cue():
    # DT-DATA-42: the new argument is often unquoted, or quoted with more than its name.
    def alts(msg, params):
        return [(h.target, h.confidence) for h in param_alternatives(msg, ["old"], params)]

    assert alts("log_path has been deprecated, please use log_output", ["log_path", "log_output"]) == [
        ("log_output", 0.8)
    ]
    assert alts("CheckConstraint.check is deprecated in favor of `.condition`.", ["check", "condition"]) == [
        ("condition", 0.8)
    ]
    assert alts("`cert=...` is deprecated. Use `verify=<ssl_context>` instead.", ["verify", "cert"]) == [
        ("verify", 0.8)
    ]
    # A cue followed by a word that isn't a parameter adds nothing; quoted names keep their old scores.
    assert alts("`old` is deprecated, use the 'new' one", ["old"]) == [("new", 0.6)]


def test_words_that_are_not_names_are_never_hints():
    # DT-DATA-42 made these readable (django 4.2, google-genai, langchain-community).
    msg = "django.core.files.storage.get_storage_class is deprecated in favor of using django.core.files.storage.storages."
    assert [h.target for h in mine(msg)] == ["django.core.files.storage.storages"]
    assert mine("Please use one of the more specific methods: `send_client_content` instead.") == []
    # The import statement names the class, never the word `from` (DT-CORE-23).
    assert [
        h.target
        for h in mine("Please use `from langchain_community.chat_models import ChatAnthropic` instead")
    ] == ["langchain_community.chat_models.ChatAnthropic"]


def _hints(msg):
    return [(h.target, h.confidence) for h in mine(msg)]


def test_python_keywords_are_never_hints():
    # DT-CORE-23: the published index had 1,010 such hints, mostly where "use" is a noun (numpy 2's `numpy.core`
    # shims: "its use is discouraged"), or the advice is an expression (aiohttp, polars, marshmallow, pydantic-ai).
    for msg in (
        "numpy.core is deprecated and has been renamed to numpy._core. The numpy._core namespace contains private "
        "NumPy internals and its use is discouraged, as NumPy internals can change without warning.",
        "drain method is deprecated, use await resp.write()",
        "`is_not` is deprecated. Use `not dtype.is_(...)` instead.",
        '`Field.fail` is deprecated. Use `raise self.make_error("{}", ...)` instead.',
        "`run_mcp_servers` is deprecated, use `async with agent:` instead.",
        "Using a non-boolean value for an isnull lookup is deprecated, use True or False instead.",
    ):
        assert mine(msg) == [], msg


def test_prose_after_a_cue_is_not_a_target():
    # DT-CORE-23: an unquoted lowercase word that more prose follows is English, not a name.
    for msg in (
        "Please follow our extraction use case documentation for more guidelines.",
        "Use new agent constructor methods like create_react_agent.",
        "CodeInterpreterTool is no longer available. Use dedicated sandbox services like E2B or Modal.",
        "Please use our custom component, [streamlit-bokeh](https://github.com/streamlit/streamlit-bokeh).",
        "`arange` has been replaced by two new functions: `int_range` for generating a single range.",
        "`disp` is deprecated, use your own printing function instead.",
        "This class is deprecated in favor of chaining individual prompts together.",
        "The ChatInputValue proto message should be used instead.",
    ):
        assert mine(msg) == [], msg
    # Names keep their hints: CamelCase, snake_case, a call, or a word that "instead", "or" or punctuation follows.
    assert _hints("Use FastMCP with OpenAPIProvider instead.") == [("FastMCP", 0.6)]
    assert _hints("Deprecated, use aiohttp_client fixture instead") == [("aiohttp_client", 0.6)]
    assert _hints("To keep the exact same behavior, use when/then/otherwise.") == [("when", 0.6)]
    assert _hints("apatch is deprecated, use patch instead") == [("patch", 0.85)]
    # werkzeug 3.0's `__version__`: the name is the second thing it suggests.
    msg = "The '__version__' attribute is deprecated. Use feature detection or 'importlib.metadata.version(\"werkzeug\")' instead."
    assert _hints(msg) == [("importlib.metadata.version", 0.8)]


def test_a_method_named_with_a_leading_dot_keeps_it():
    # DT-CORE-23: pandas 1.5's "Use .items instead", openai's `.content()`; the dot means a member of the same
    # class, and the resolver reads it so.
    assert _hints("iteritems is deprecated and will be removed in a future version. Use .items instead.") == [
        (".items", 0.85)
    ]
    assert _hints("'The `.content()` method should be used instead'") == [(".content", 0.8)]
    assert _hints("Please create a mask and filter using `.loc` instead") == [(".loc", 0.8)]


def test_lists_imports_and_other_verbs_name_targets():
    # DT-CORE-23: each name of "use A or B instead"; an import statement; "call/check/access X instead".
    assert _hints("`ChatMLflowAIGateway` is deprecated. Use `ChatMlflow` or `ChatDatabricks` instead.") == [
        ("ChatMlflow", 0.85),
        ("ChatDatabricks", 0.84),
    ]
    assert _hints("DataFrame.pad/Series.pad is deprecated. Use DataFrame.ffill/Series.ffill instead") == [
        ("DataFrame.ffill", 0.85),
        ("Series.ffill", 0.84),
    ]
    # pandas' shorthand for DataFrame.shift and Series.shift; a URL path isn't a list.
    assert _hints("You can use DataFrame/Series.shift instead.") == [
        ("DataFrame.shift", 0.85),
        ("Series.shift", 0.84),
    ]
    assert [h.target for h in mine("Offset pagination is deprecated. Use GET /v2/audio/voices instead.")] == [
        "GET"
    ]
    # After "the", a name is quoted or looks like code: openai's "in favor of the Responses API" names an API.
    assert mine("The Assistants API is deprecated in favor of the Responses API") == []
    assert _hints("DEPRECATED: Please use the base_font_size theme config instead:") == [
        ("base_font_size", 0.75)
    ]
    assert _hints("Use the PromptsAsTools transform instead.") == [("PromptsAsTools", 0.6)]
    assert _hints("select_entity_from is deprecated. Use the aliased() construct instead") == [
        ("aliased", 0.6)
    ]
    langchain = (
        "Importing from langchain will no longer be supported as of langchain==0.2.0. Please import from "
        "langchain-community instead:\n\n`from langchain_community.agent_toolkits import AINetworkToolkit`.\n\n"
        "To install langchain-community run `pip install -U langchain-community`."
    )
    assert _hints(langchain) == [("langchain_community.agent_toolkits.AINetworkToolkit", 0.8)]
    assert _hints("\"Use 'from crewai.events import AgentExecutionCompletedEvent' instead\"") == [
        ("crewai.events.AgentExecutionCompletedEvent", 0.8)
    ]
    # An import the wording deprecates, or the old way of a migration, isn't advice.
    assert mine("`from fastmcp.settings import settings` is deprecated.") == []
    msg = "Example migration: # Old way from instructor import from_gemini # New way from instructor import from_genai"
    assert _hints(msg) == [("instructor.from_genai", 0.8)]
    assert _hints("Index.is_all_dates is deprecated. check index.inferred_type instead.") == [
        ("index.inferred_type", 0.8)
    ]
    assert _hints("add_document is deprecated since redisearch 2.0, call hset instead") == [("hset", 0.8)]
    assert _hints("redisjson-py supported this, call get directly.") == [("get", 0.75)]
    assert _hints("getheaders() is deprecated. Instead access HTTPResponse.headers directly.") == [
        ("HTTPResponse.headers", 0.75)
    ]


def test_rst_roles_and_double_backticks():
    # DT-CORE-23: sqlalchemy writes its advice in Sphinx roles, with a private doc alias for the module.
    assert _hints("Use the :meth:`_engine.Engine.begin` context manager instead.") == [("Engine.begin", 0.6)]
    assert _hints(":meth:`_schema.Table.tometadata` is renamed to :meth:`_schema.Table.to_metadata`") == [
        ("Table.to_metadata", 0.85)
    ]
    assert _hints("Please use the :func:`.sqlalchemy.inspect` function on an Engine.") == [
        ("sqlalchemy.inspect", 0.75)
    ]
    assert _hints("Use ``astype`` as an alternative to change the dtype.") == [("astype", 0.6)]


def test_parameter_cues_take_an_article_and_double_backticks():
    # DT-CORE-23: starlette's "Use the lifespan parameter instead", pandas' "Use ``sep='\s+'`` instead".
    def alts(msg, old, params):
        return [(h.target, h.confidence) for h in param_alternatives(msg, [old], params)]

    msg = "The on_startup and on_shutdown parameters are deprecated. Use the lifespan parameter instead."
    assert alts(msg, "on_startup", ["on_startup", "on_shutdown", "lifespan"]) == [("lifespan", 0.8)]
    msg = "The 'delim_whitespace' keyword in pd.read_csv is deprecated. Use ``sep='\\s+'`` instead"
    assert alts(msg, "delim_whitespace", ["delim_whitespace", "sep"]) == [("sep", 0.8)]


def test_a_distribution_name_is_not_a_target():
    # DT-CORE-12: langchain-community's NeMoEmbeddings says "Please use langchain-nvidia-ai-endpoints …".
    assert mine("Please use langchain-nvidia-ai-endpoints NVIDIAEmbeddings interface.") == []
    assert mine("Use built-in generics such as list instead.") == []


def test_a_parameter_scores_high_only_after_a_cue():
    # DT-CORE-12: polars' `top_k(nulls_last)` names `k` in passing, not as what to pass instead.
    msg = (
        "The `nulls_last` parameter for `top_k` is deprecated. It will be removed in the next breaking release. "
        "Null values will be considered lowest priority and will only be included if `k` is larger than the "
        "number of non-null elements."
    )
    assert [
        (h.target, h.confidence) for h in param_alternatives(msg, ["nulls_last"], ["k", "nulls_last"])
    ] == [
        ("top_k", 0.6),
        ("k", 0.59),
    ]
    # A cue earlier in the same sentence covers each name after it.
    msg = "The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead."
    assert [
        (h.target, h.confidence) for h in param_alternatives(msg, ["proxies"], ["proxy", "proxies", "mounts"])
    ] == [("proxy", 0.8), ("mounts", 0.79)]
    # So does "instead" later in it (redis, streamlit), and "renamed" without "to" (scikit-learn).
    for msg, old, new in [
        (
            "`read_from_replicas` is deprecated. Please configure the 'load_balancing_strategy' instead",
            "read_from_replicas",
            "load_balancing_strategy",
        ),
        (
            "The `use_column_width` parameter has been deprecated and will be removed in a future release. Please"
            " utilize the `use_container_width` parameter instead.",
            "use_column_width",
            "use_container_width",
        ),
        (
            "`dissimilarity` was renamed `metric` in 1.8 and will be removed in 1.10.",
            "dissimilarity",
            "metric",
        ),
    ]:
        assert [(h.target, h.confidence) for h in param_alternatives(msg, [old], [old, new])] == [(new, 0.8)]
    # Not past the end of its sentence: pandas' "replace without 'value' … Explicitly specify the new values instead."
    msg = "replace without 'value' and with non-dict-like 'to_replace' is deprecated. Explicitly specify the new values instead."
    assert param_alternatives(msg, ["to_replace"], ["to_replace", "value"])[0].confidence == 0.6


# One package's paths as (path, last indexed ordinal, canonical); the deprecation is observed at ordinal 4.
SPANS = [
    ("numpy", 9, "numpy"),
    ("numpy.product", 6, "numpy.core.fromnumeric.product"),
    ("numpy.prod", 9, "numpy.core.fromnumeric.prod"),
    ("numpy.core.fromnumeric.product", 6, "numpy.core.fromnumeric.product"),
    ("numpy.core.fromnumeric.prod", 9, "numpy.core.fromnumeric.prod"),
    ("numpy.result_type", 9, "numpy.core.multiarray.result_type"),
    ("numpy.round", 9, "numpy.core.fromnumeric.round"),
    ("numpy.linalg", 9, "numpy.linalg"),
    ("numpy.linalg.linalg.transpose", 6, "numpy.linalg.linalg.transpose"),
    ("numpy.linalg.linalg.det", 6, "numpy.linalg._linalg.det"),
    ("numpy.linalg.det", 9, "numpy.linalg._linalg.det"),
    ("numpy.ma.batch_like", 2, "numpy.ma.batch_like"),
    ("numpy.transpose", 9, "numpy.core.fromnumeric.transpose"),
    ("numpy.old_name", 3, "numpy.old_name"),
    ("numpy.ma.MaskedArray.ravel", 9, "numpy.ma.core.MaskedArray.ravel"),
    ("numpy.ma.core.MaskedArray.ravel", 9, "numpy.ma.core.MaskedArray.ravel"),
    ("numpy.ndarray.ravel", 9, "numpy.ndarray.ravel"),
    ("numpy.lib.Arrayterator", 9, "numpy.lib.arrayterator.Arrayterator"),
    ("numpy.lib.arrayterator.Arrayterator", 9, "numpy.lib.arrayterator.Arrayterator"),
    ("numpy.char.chararray.find", 9, "numpy.char.chararray.find"),
    ("numpy.char.chararray.old", 6, "numpy.char.chararray.old"),
]


def test_a_hint_target_is_qualified_from_the_index():
    # DT-CORE-12: the name a deprecation suggests, as a public path the index has at that release or later.
    names = PublicNames(["numpy"], SPANS)

    def resolve(target, path="numpy.product"):
        return names.resolve(target, path, 4)

    assert resolve("prod") == ("numpy.prod", True)  # next to the deprecated name, not the builtin
    assert resolve("prod", "numpy.core.fromnumeric.product") == ("numpy.core.fromnumeric.prod", True)
    assert resolve("round") == ("numpy.round", True)
    assert resolve("np.result_type", "numpy.find_common_type") == ("numpy.result_type", True)
    assert resolve("find", "numpy.char.chararray.old") == ("numpy.char.chararray.find", True)  # same class
    # The same object at its public path: numpy 2's shims deprecate where a function is imported from.
    assert resolve("numpy.linalg.det", "numpy.linalg.linalg.det") == ("numpy.linalg.det", True)
    assert resolve("chararray.find", "numpy.char.old") == ("numpy.char.chararray.find", True)  # an ancestor's
    # Not next to it: the one object of that name anywhere in the package, by its shortest path.
    assert resolve("Arrayterator") == ("numpy.lib.Arrayterator", True)
    # Several objects of that name: it exists, but which one isn't said.
    assert resolve("ravel") == ("ravel", True)


def test_a_hint_target_the_index_contradicts():
    names = PublicNames(["numpy"], SPANS)

    def resolve(target, path="numpy.product"):
        return names.resolve(target, path, 4)

    # numpy 2's `numpy.linalg.linalg` shim suggests `numpy.linalg.<name>` for every name, public or not.
    assert resolve("numpy.linalg.transpose", "numpy.linalg.linalg.transpose") == (
        "numpy.linalg.transpose",
        False,
    )
    assert resolve("numpy.old_name") == ("numpy.old_name", False)  # gone before the deprecation
    assert resolve("newer") == ("newer", False)  # a word, not a name the package has
    assert resolve("product") == ("product", False)  # only the deprecated name itself
    # A name the package had before the deprecation but not since isn't contradicted: langchain's `batch`, which
    # Chain still inherits from langchain-core, where the index doesn't follow it.
    assert resolve("batch_like") == ("batch_like", None)


def test_a_hint_target_outside_the_package_is_not_checked():
    names = PublicNames(["numpy"], SPANS)
    for target in (
        "float",
        "string",
        "urllib.parse.quote",
        "langchain_ollama.ChatOllama",
        "name.map",
        "pd.concat",
    ):
        assert names.resolve(target, "numpy.product", 4)[1] is None, target
    assert names.resolve("pd.concat", "numpy.product", 4) == ("pandas.concat", None)


def test_a_member_of_an_instance_is_not_checked():
    # DT-DATA-31: matplotlib's `colormaps` is a ColormapRegistry the package binds as an attribute, so the index
    # has no `matplotlib.colormaps.register` to find, and can't contradict it.
    spans = [
        ("matplotlib.colormaps", 9, "matplotlib.cm._colormaps"),
        ("matplotlib.cm.register_cmap", 6, "matplotlib.cm.register_cmap"),
        ("matplotlib.cm.ScalarMappable", 9, "matplotlib.cm.ScalarMappable"),
    ]
    mpl = PublicNames(["matplotlib"], spans, instances=["matplotlib.colormaps"])
    assert mpl.resolve("matplotlib.colormaps.register", "matplotlib.cm.register_cmap", 4) == (
        "matplotlib.colormaps.register",
        None,
    )
    assert (
        mpl.check(
            Hint("matplotlib.colormaps.register", 0.85, ""), "matplotlib.cm.register_cmap", 4
        ).confidence
        == 0.85
    )
    # A member of a class the index has is checked as before.
    assert mpl.resolve("matplotlib.cm.ScalarMappable.nothing", "matplotlib.cm.register_cmap", 4)[1] is False
    # Without the attribute, the same target is contradicted.
    bare = PublicNames(["matplotlib"], spans)
    assert bare.resolve("matplotlib.colormaps.register", "matplotlib.cm.register_cmap", 4)[1] is False


def test_a_member_of_the_same_class_by_a_leading_dot_or_an_instance():
    # DT-CORE-23: ".items" on DataFrame.iteritems, "index.inferred_type" on Index.is_all_dates, "self.x", "obj.x".
    pd = PublicNames(
        ["pandas"],
        [
            ("pandas.DataFrame.iteritems", 9, "pandas.core.frame.DataFrame.iteritems"),
            ("pandas.DataFrame.items", 9, "pandas.core.frame.DataFrame.items"),
            ("pandas.Series.items", 9, "pandas.core.series.Series.items"),
            ("pandas.Index.is_all_dates", 9, "pandas.core.indexes.base.Index.is_all_dates"),
            ("pandas.Index.inferred_type", 9, "pandas.core.indexes.base.Index.inferred_type"),
            ("pandas.DataFrame.ffill", 9, "pandas.core.frame.DataFrame.ffill"),
            ("pandas.DataFrame.index", 9, "pandas.core.frame.DataFrame.index"),
            ("pandas.DataFrame.loc", 9, "pandas.core.frame.DataFrame.loc"),
        ],
    )
    assert pd.resolve(".items", "pandas.DataFrame.iteritems", 4) == ("pandas.DataFrame.items", True)
    assert pd.resolve("index.inferred_type", "pandas.Index.is_all_dates", 4) == (
        "pandas.Index.inferred_type",
        True,
    )
    assert pd.resolve("obj.ffill", "pandas.DataFrame.pad", 4) == ("pandas.DataFrame.ffill", True)
    assert pd.resolve("self.loc", "pandas.DataFrame.first", 4) == ("pandas.DataFrame.loc", True)
    # Not on that class: read as the name alone.
    assert pd.resolve(".items", "pandas.read_csv", 4) == ("items", True)
    assert pd.resolve(".nothing", "pandas.DataFrame.iteritems", 4) == ("nothing", False)
    # A member the index can't check (polars' `.str.explode()`, a namespace accessor) keeps its dot.
    assert pd.resolve(".str.split", "pandas.DataFrame.iteritems", 4) == (".str.split", None)
    # An instance named after the base class: `index` on IntervalIndex.
    sub = PublicNames(
        ["pandas"],
        [
            ("pandas.IntervalIndex.is_mixed", 9, "pandas.core.indexes.base.Index.is_mixed"),
            (
                "pandas.IntervalIndex.inferred_type",
                9,
                "pandas.core.indexes.interval.IntervalIndex.inferred_type",
            ),
        ],
    )
    assert sub.resolve("index.inferred_type", "pandas.IntervalIndex.is_mixed", 4) == (
        "pandas.IntervalIndex.inferred_type",
        True,
    )


def test_the_deprecated_object_itself_is_no_replacement():
    # DT-CORE-23: langchain-community's "Please use `HanaDB` from the langchain_hana package" found the deprecated
    # class at another path; llama-index's ReActAgent named a path the new class "will be promoted to".
    spans = [
        ("lc.vectorstores.hanavector.HanaDB", 9, "lc.vectorstores.hanavector.HanaDB"),
        ("lc.vectorstores.HanaDB", 9, "lc.vectorstores.hanavector.HanaDB"),
        ("np.linalg.linalg.det", 6, "np.linalg._linalg.det"),
        ("np.linalg.det", 9, "np.linalg._linalg.det"),
    ]
    dep = "Please use HanaDB from the langchain_hana package."
    deprecations = [
        ("lc.vectorstores.hanavector.HanaDB", 2, 9, dep),
        ("lc.vectorstores.HanaDB", 2, 9, dep),
        ("np.linalg.linalg.det", 2, 6, "np.linalg.linalg is deprecated."),
    ]
    names = PublicNames(["lc", "np"], spans, deprecations)
    hint = Hint("HanaDB", 0.75, dep)
    assert names.check(hint, "lc.vectorstores.hanavector.HanaDB", 4) == Hint(
        "lc.vectorstores.HanaDB", 0.5, dep
    )
    # The same object at a path that isn't deprecated is where to import it from (numpy 2's shims).
    hint = Hint("np.linalg.det", 0.85, "")
    assert names.check(hint, "np.linalg.linalg.det", 4).confidence == 0.85


def test_a_dotted_target_is_found_under_an_ancestor_or_as_a_sibling():
    sqla = PublicNames(
        ["sqlalchemy"],
        [
            ("sqlalchemy.orm.query.Query.get", 9, "sqlalchemy.orm.query.Query.get"),
            ("sqlalchemy.orm.Session.get", 9, "sqlalchemy.orm.session.Session.get"),
        ],
    )
    # sqlalchemy 2.0: "The method is now available as Session.get()".
    assert sqla.resolve("Session.get", "sqlalchemy.orm.query.Query.get", 4) == (
        "sqlalchemy.orm.Session.get",
        True,
    )
    # langchain-community names a top-level re-export that doesn't exist; the class is next to the old one.
    lc = PublicNames(
        ["langchain_community"],
        [
            ("langchain_community.embeddings.ClovaEmbeddings", 9, "x.ClovaEmbeddings"),
            ("langchain_community.embeddings.ClovaXEmbeddings", 9, "x.ClovaXEmbeddings"),
        ],
    )
    assert lc.resolve(
        "langchain_community.ClovaXEmbeddings", "langchain_community.embeddings.ClovaEmbeddings", 4
    ) == ("langchain_community.embeddings.ClovaXEmbeddings", True)


def test_an_argument_to_pass_to_another_callable_instead():
    # gradio 5.50 (DT-CORE-27 case) and scikit-learn 1.1: the same argument, given to another call.
    message = (
        "The 'theme' parameter in the Blocks constructor will be removed in Gradio 6.0. "
        "You will need to pass 'theme' to Blocks.launch() instead."
    )
    assert [(h.target, h.confidence) for h in param_alternatives(message, ["theme"], ["theme", "css"])] == [
        ("Blocks.launch(theme)", 0.8)
    ]
    message = "Deprecated since version 1.1: Pass the parameter `pdp_lim` to sklearn.inspection.PartialDependenceDisplay.plot() instead."
    assert param_alternatives(message, ["pdp_lim"], ["pdp_lim"])[0].target == (
        "sklearn.inspection.PartialDependenceDisplay.plot(pdp_lim)"
    )
    # Another argument passed to something else is not this one's replacement.
    message = "'cache' is deprecated. Pass 'store' to Session.open() instead."
    assert not [h for h in param_alternatives(message, ["cache"], ["cache", "store"]) if "open" in h.target]
    assert param_alternatives(
        "Pass `Embeddings` instance to `embeddings` instead.", ["embedding_function"], []
    ) == [
        Hint("Embeddings", 0.6, "Pass `Embeddings` instance to `embeddings` instead."),
        Hint("embeddings", 0.59, "Pass `Embeddings` instance to `embeddings` instead."),
    ]


def test_a_recommended_name_and_the_library_it_comes_from():
    # DT-CORE-40: transformers 4.42's `shard_checkpoint` warning, a name in another distribution.
    hints = mine(
        "Note that `shard_checkpoint` is deprecated and will be removed in v4.44. We recommend you using"
        " split_torch_state_dict_into_shards from huggingface_hub library"
    )
    assert [(h.target, h.confidence) for h in hints] == [
        ("huggingface_hub.split_torch_state_dict_into_shards", 0.75)
    ]
    hints = mine(
        "KGTableRetriever is deprecated, it is recommended to use PropertyGraphIndex and associated retrievers."
    )
    assert [h.target for h in hints] == ["PropertyGraphIndex"]
    assert [
        h.target for h in mine("Please use the `WeaveTracer` from the `weave` package instead of this.")
    ] == ["weave.WeaveTracer"]
    # Prose after the cue isn't a name.
    for msg in (
        "The class `TFTrainer` is deprecated. We recommend using native Keras instead, by calling `fit()`.",
        "The NeptuneCallback is deprecated. We recommend using other supported experiment tracking integrations.",
    ):
        assert mine(msg) == [], msg


def test_refer_to_a_name_that_looks_like_code():
    # DT-CORE-40: sqlalchemy 1.4's "Please refer to Inspector.get_table_names()".
    hints = mine(
        "The Engine.table_names() method is deprecated and will be removed in a future release. Please refer to"
        " Inspector.get_table_names()."
    )
    assert [(h.target, h.confidence) for h in hints] == [("Inspector.get_table_names", 0.75)]
    assert [h.target for h in mine("`x` is deprecated; refer to `new_x` for details.")] == ["new_x"]
    # Prose, a URL, a product, and langchain's RST link aren't names.
    for msg in (
        "`x` is deprecated. Please refer to the documentation.",
        "`x` is deprecated; refer to docs.",
        "`x` is deprecated, refer to https://example.com/migrate",
        "`x` is deprecated. Please refer to LangGraph for agents.",
        "Agent was deprecated in 0.1.0. For details, refer to the `LangGraph documentation <https://x.y/>`_.",
    ):
        assert mine(msg) == [], msg


def test_a_keyword_argument_written_out_names_the_parameter_not_its_value():
    # DT-CORE-40: gradio 5.50 advises `buttons`, a parameter of 6.0; "copy" is its value.
    def alts(msg, deprecated, params):
        return [(h.target, h.confidence) for h in param_alternatives(msg, deprecated, params)]

    assert alts(
        "The 'show_copy_button' parameter will be removed in Gradio 6.0. You will need to use 'buttons=[\"copy\"]'"
        " instead.",
        ["show_copy_button"],
        ["value", "show_copy_button"],
    ) == [("buttons", 0.8)]
    assert alts(
        "The 'app' shortcut is now deprecated. Use the explicit style 'transport=WSGITransport(app=...)' instead.",
        ["app"],
        ["app", "transport"],
    ) == [("transport", 0.8)]
    # The same parameter with another value: nothing to name, and neither value is a parameter.
    assert (
        alts(
            "`type='protected'` is deprecated and will be removed in a future release. Use `type='authenticated'`"
            " instead.",
            ["type"],
            ["type"],
        )
        == []
    )
    # Not after a cue: a way of calling, not advice.
    assert alts("Passing `cert=...` is deprecated.", ["verify"], ["verify", "cert"]) == []


def test_a_name_something_is_passed_as_is_not_what_is_deprecated():
    # DT-CORE-40: transformers' "using `EvaluationStrategy` for `eval_strategy`" deprecates a value of
    # `eval_strategy`, the argument to use, and "Passing a tuple of `past_key_values`" a type of value.
    for msg, param in (
        ("using `EvaluationStrategy` for `eval_strategy` is deprecated and will be removed", "eval_strategy"),
        (
            "Passing a tuple of `past_key_values` is deprecated and will be removed in Transformers v4.47.",
            "past_key_values",
        ),
        (
            "Passing raw bytes as `body` is deprecated. Please pass raw bytes via the `content` parameter.",
            "body",
        ),
        ("Using dict as a `filter` is deprecated.", "filter"),
    ):
        assert param_deprecations(msg, [param]) == {}, msg
    # Its subject: "Use of `x`", "Support for `x`", "Passing `x`".
    for msg in ("Use of `x` is deprecated.", "Support for `x` is deprecated.", "Passing `x` is deprecated."):
        assert list(param_deprecations(msg, ["x"])) == ["x"], msg
