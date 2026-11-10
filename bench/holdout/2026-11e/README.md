# Holdout set 2026-11e (DT-QA-11)

Collected on 2026-11-10, while the `SNAPSHOT_FORMAT` 26 index was being built, under the rules in `../README.md`, to
score what was designed from 2026-11d's misses: DT-DATA-63 to 67 and 69, and DT-CORE-46 to 48. **This directory was
committed before DepTime ran on any of it.** The first scoring is `results-<date>.md`.

## Uses

168 upgrades of real projects, 2,747 uses asked about:

| Source | Upgrades | Work | Break | Excluded |
|---|---|---|---|---|
| Pinned, upgrade to the newest release (Tier A 56, Tier B 49) | 105 | 1,228 | 70 | 359 |
| Pinned, the same code on a release a year older (Tier A 28, Tier B 31) | 59 | 734 | 55 | 179 |
| Exercise stratum, upgrade (Tier A 2) | 2 | 39 | 13 | 9 |
| Exercise stratum, a year older (Tier A 2) | 2 | 51 | 2 | 8 |
| **All** | **168** | **2,052** | **140** | **555** |

Uses of all 53 covered packages are scored; one of the breaks is in a Tier B package. The main stratum comes from 102
pinned repositories: all 53 packages were searched (`selection.json`: every candidate GitHub's code search returned, and
why each skipped one was skipped), and a repository 2026-11, 2026-11b, 2026-11c or 2026-11d had asked about didn't
qualify. Every package has two but urllib3, whose search found one qualifying repository in its first 500 results (it
was run again after a GitHub 503 cut the first run short). The cutoff is 2026-11-10: the newest releases are those
before that day.

The exercise stratum (`../README.md`) looks for uses of what 2026-11d's misses fixed. huggingface_hub: 2 repositories
whose code calls the functions it binds at its top level to a method of its `HfApi()` instance, with keywords
(`create_repo`, `create_commit`, `model_info(use_auth_token=)`, `upload_folder`, `list_repo_files`; DT-DATA-66).
polars: none. Of the 259 repositories its search returned, 57 qualify but for the rule, and none of them uses a
top-level exception such as `polars.ComputeError` with polars pinned before 2.0.0 (DT-DATA-64).

209 of the 212 environments installed. The three that didn't install from wheels on any of the probe's Pythons, even
with the fallback (`probe-envs.json`): chromadb 0.4.6, dspy 2.5.41 and fastmcp 2.11.2; their uses are excluded. Four
needed the fallback that lets pypika, ffmpy and docx2txt build from source (chromadb 0.6.3, crewai 0.30.11 and
0.126.0, gradio 3.45.2).

Labels come from `holdout` run [42918236483](https://github.com/cschmertz/deptime/actions/runs/42918236483) (branch
`spike/holdout`). 22 working uses are deprecated by the probe's rule.

The probe's rule for an object built to fail when called fires on openai 3.48.0's `openai.ChatCompletion.create`
(`APIRemovedInV1Proxy`): one project reads and calls it, so the name is a break (its two calls are excluded, because
in `from` only `**kwargs` takes their keywords); another only reads it, which still works.

Of the 11 breaks found by binding a call, three were changed by hand (`label-overrides.json`): huggingface_hub 2.4.0's
`hf_hub_download(proxies=)`, called by two projects. Its `validate_hf_hub_args` wrapper drops `proxies` and
`resume_download` with a warning before calling the function, so the call still works though the signature lacks both.
The other 8 were checked against the source at `to`, read from the release's wheel, and kept.

## Tracebacks

28 real tracebacks from GitHub issues, 21 packages (Tier A 16 cases, Tier B 12). By change type, as the scoring splits
them: moved (a name or module removed, with a replacement the library states) 12, removed 9, not yet added 4,
parameter renamed 2, parameter removed 1. Each traceback was cut from its page, each version quoted from the thread, and
each expected answer taken from the library's changelog, migration guide or source at the two releases, read from the
shipped wheels.

They were collected under DT-QA-01's rules, blind (DepTime wasn't run), with priority on the packages the last two sets
had none for. A break already in `bench/cases`, `bench/tmb` or an earlier holdout didn't qualify, and at most two reports
per break were kept. New in this set: a maintainer's reply that confirms the reporter's version counts as stating it
(langchain-core's `messages.content`). For the third set running, no report passed the rules for langchain-community,
langchain-openai, weaviate-client, google-generativeai, litellm or instructor; nor this time for google-genai,
mistralai, ollama, crewai, openai-agents, mcp, fastmcp, streamlit or pydantic-settings (the collectors' reject logs have
271 lines). The collectors compared each release's names to find the removals there are:
- langchain-openai removes almost no public names, and an unknown constructor keyword goes into `model_kwargs`, so it
  fails as an openai error.
- langchain-community 0.4.2 (2026-05) removed about 40 modules, and no report of one states a version yet.
- litellm's, instructor's and google-generativeai's removals have no report with a version.
- weaviate-client 4.x still serves the v3 top-level names through a deprecation `__getattr__`, and its `Client` stub
  takes no arguments.

A break is one name or one parameter, as before. flask 3.0's `Markup` and werkzeug 3.0's `url_parse` and `url_encode`
come from the same changelog entries as earlier cases' `escape` and `url_quote`, and count as their own breaks;
qdrant-client's `upload_records` and chromadb's `api.API` are the second report of a break with one earlier case. The
claudette case's error names `anthropic.types.beta`, which 0.27.0 dropped whole and 0.34.0 brought back without `tools`;
the case expects the module its code imports, `anthropic.types.beta.tools`. pinecone 3.0.1 and later add an `init()`
that only raises, saying to use the `Pinecone` class; the case is at 3.0.0, where the name is gone. Four tracebacks are
the error line alone, as their pages quote it.

24 have a boundary the probe confirmed (`traceback-checks.json`, run 42918236483: 24 of the 25 with a `check` agree).
The 25th is pydantic's `NoneStr`, which the probe can't tell: pydantic 2's import shim raises `PydanticImportError`
("`pydantic:NoneStr` has been removed in V2"), which is the removal itself. The other 3 were checked in the source at
both releases:
- chromadb 0.4.15's `chromadb.api.API`: chromadb 0.4.x doesn't install from wheels alone.
- transformers 5's `cache_utils.SlidingWindowCache`: importing `cache_utils` needs torch.
- pytest 8.2's `FixtureDef.unittest`: an attribute set on instances in `__init__`.
