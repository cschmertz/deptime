from deptime.ingest.replacements import mine, param_deprecations


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
