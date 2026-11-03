from deptime.ingest.replacements import (
    PublicNames,
    clean_evidence,
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


def test_langchain_alternative_kwarg():
    assert (
        mine('since="0.1.17", alternative="langchain_openai.ChatOpenAI", removal="1.0"')[0].target
        == "langchain_openai.ChatOpenAI"
    )


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
    assert mine("Please use `from langchain_community.chat_models import ChatAnthropic` instead") == []


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
