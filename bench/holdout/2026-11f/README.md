# Holdout set 2026-11f (DT-QA-12)

Collected on 2026-11-11 under the rules in `../README.md`, to score what was designed from 2026-11e's misses: DT-CORE-50
to 56 (query side, not yet released when the set was picked). **This directory was committed before DepTime ran on any
of it.** The first scoring is `results-<date>.md`.

## Uses

176 upgrades of real projects, 2,850 uses asked about:

| Source | Upgrades | Work | Break | Excluded |
|---|---|---|---|---|
| Pinned, upgrade to the newest release (Tier A 56, Tier B 49) | 105 | 1,282 | 72 | 336 |
| Pinned, the same code on a release a year older (Tier A 34, Tier B 33) | 67 | 765 | 63 | 224 |
| Exercise stratum, upgrade (Tier A 3) | 3 | 38 | 17 | 13 |
| Exercise stratum, a year older (Tier A 1) | 1 | 28 | 0 | 12 |
| **All** | **176** | **2,113** | **152** | **585** |

Uses of all 53 covered packages are scored. The main stratum comes from 101 pinned repositories: all 53 packages were
searched (`selection.json`: every candidate GitHub's code search returned, and why each skipped one was skipped), and a
repository 2026-11, 2026-11b, 2026-11c, 2026-11d or 2026-11e had asked about didn't qualify. Every package has two but
urllib3, whose search found one qualifying repository in its first 500 results (444 candidates), as in 2026-11e. The
cutoff is 2026-11-11: the newest releases are those before that day.

The exercise stratum (`../README.md`) looks for uses of classes deprecated by a warning in their own `__init__`
(DT-CORE-53). huggingface_hub: one repository whose code uses `Repository`, pinned at 0.32.2. Its upgrade to 2.4.0
loses `Repository` and `HfFolder` (17 breaks), and on 0.23.2, a year older, `Repository` works but is deprecated. Of the
368 repositories its search returned, 22 more qualify but for the rule. langchain-core: two repositories that use
`RunnableWithMessageHistory`, pinned at 0.2.24 and 0.1.43, upgraded to 1.7.7, where the class's own `__init__` warns
that it's deprecated (from 1.3.3). No indexed release is a year older than either pin, so they have no second
upgrade. The probe marks the name deprecated in both. Their constructor calls are excluded because the probe can't bind
them at `from`: pydantic's field signature asks for `bound`.

214 of the 217 environments installed. The three that didn't install from wheels on any of the probe's Pythons, even
with the fallback (`probe-envs.json`): crewai 0.70.1, dspy 2.5.29 and openai 0.26.5; their uses are excluded. Eight
needed the fallback that lets pypika, ffmpy and docx2txt build from source (chromadb 0.4.18, 0.5.0, 0.5.23 and 1.0.12,
crewai 0.102.0 and 0.203.1, gradio 3.23.0 and 3.35.2). Three needed setuptools, new in this set's probe: gradio 3.23.0
and 3.35.2 and crewai 0.11.0 import `pkg_resources` without declaring it. A venv made with `python -m venv` had
setuptools until Python 3.12, and uv's venvs don't. The probe now installs an environment again with setuptools from
the same date when its checks fail on exactly that import (`added` in `probe-envs.json`). The first probe run, without
it, excluded those 94 uses.

Labels come from `holdout` run [43093446619](https://github.com/cschmertz/deptime/actions/runs/43093446619) (branch
`spike/holdout`). Run [43092814980](https://github.com/cschmertz/deptime/actions/runs/43092814980), before the
setuptools fallback, gave the same labels for every other use. 18 working uses are deprecated by the probe's rule.

Of the 6 breaks found by binding a call, one was changed by hand (`label-overrides.json`): langchain-openai 1.7.4's
`ChatOpenAI(max_tokens=)`. The field's alias is `max_completion_tokens`, so pydantic's signature lists only that, but
the model sets `populate_by_name`, so the field's own name still works. The other 5 were checked against the source at
`to`, read from the release's wheel, and kept: scikit-learn 1.9.1's `mean_squared_error(squared=)`, pydantic-ai 1.0.11's
`Tool.from_schema(args_validator=)`, crewai 0.11.0's `Crew.kickoff(inputs=)`, and gradio 6.31.0's
`Blocks.launch(enable_queue=)` and `Blocks.queue(concurrency_count=)`. Two name breaks whose error looked unusual were
checked the same way and kept:
- fastmcp 4.1.8's `fastmcp.prompts.prompt` is the `prompt` decorator, and the module of that name (2.10.1's) is gone.
- chromadb 0.5.0's `utils.embedding_functions` is one module, with no `cohere_embedding_function` inside it.

## Tracebacks

26 real tracebacks from GitHub issues and Stack Overflow, 20 packages (Tier A 8 cases, Tier B 18). By change type, as
the scoring splits them: moved (a name or module removed, with a replacement the library states) 11, removed 9,
parameter removed 3, parameter renamed 2, not yet added 1. Each traceback was cut from its page, each version quoted
from the thread, and each expected answer taken from the library's changelog, migration guide or source at the two
releases, read from the shipped wheels.

They were collected under DT-QA-01's rules, blind (DepTime wasn't run), with priority on the packages the last sets had
none for. A break already in `bench/cases`, `bench/tmb` or an earlier holdout didn't qualify, and at most two reports
per break were kept. For the fourth set running, no report passed the rules for langchain-community, langchain-openai,
weaviate-client, google-generativeai, litellm or instructor; this time none did for langchain, langchain-core or
langgraph either, nor for 24 other packages (the collectors' reject logs have 195 lines). What stopped them, most often:
- no installed version stated, or only a range ("latest", ">= 2.5.43", "1.16");
- a removal that was temporary: pydantic-settings 2.9.0's `DEFAULT_PATH` and `ConfigFileSourceMixin`, instructor
  1.11.0 to 1.11.2's `patch`, `multimodal` and `client`, openai 1.100.0's `ResponseTextConfig`, each back a release
  or two later;
- a broken environment, not an API change: a stated version that has the name, a local file shadowing the package, a
  mismatched pydantic-core;
- new this time, three reports that read as written by a coding agent (a "Root Cause Analysis" body, attached patch
  notes) with no version a person stated;
- langgraph's one candidate states only the series ("0.6"), not a release.

A break is one name or one parameter, as before, so 10 cases are a different name or parameter from the same
changelog entry as an earlier case:
- openai 1.0's `Embedding` and `Image` (beside `Completion` and `ChatCompletion`). openai 1.x still has both names, as
  proxies that raise `APIRemovedInV1` when used; the cases expect the 0.28 boundary the error states.
- django 4.0's `smart_text` (beside `force_text`), werkzeug 3.0's `url_quote_plus` (beside `url_quote`, `url_parse`,
  `url_encode` and `url_decode`), numpy 2.0's `unicode_` (beside `string_` and `float_`).
- selenium 4.10's `service_log_path` (beside `chrome_options` and `executable_path`), fastmcp 3.0's `include_tags`
  (beside `log_level`), httpx 0.28's private `_types.ProxiesTypes`, which the project imports (beside `proxies=`).
- pytest 8.1's `FixtureDef(fixturemanager=)` (beside 8.2's `FixtureDef.unittest`), streamlit 1.41's `image_to_url`
  (beside `AtomicImage`, also in this set).

mcp 1.12.3's `FastMCP(description=)` was never a named parameter: 1.12.2 took it through `**settings` and ignored it.
selenium's traceback quotes the error as `WebDriver.init()` (the page lost the underscores), kept verbatim. Eight
tracebacks are the error line alone, as their pages quote it.

All 22 with a `check` agree with the probe (`traceback-checks.json`, run 43093446619). gradio's
`encode_url_or_file_to_base64` needed the setuptools fallback; the first run couldn't tell. The other 4 were checked in
the source at both releases:
- transformers 4.48's `LlamaFlashAttention2` and 4.49's `_extract_past_from_model_output`: importing their modules
  needs torch.
- fastmcp 3's `FastMCP(include_tags=)`: `__init__` takes `**kwargs` and rejects it in `_check_removed_kwargs`, so a
  signature would bind it.
- huggingface_hub 1.0's `hf_hub_download(use_auth_token=)`: in 0.36 the `validate_hf_hub_args` wrapper takes it, not
  the function's own signature.
