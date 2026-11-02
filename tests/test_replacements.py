from deptime.ingest.replacements import (
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
