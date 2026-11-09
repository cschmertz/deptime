# Holdout set 2026-11c (DT-QA-09)

Collected on 2026-11-09, after the 0.1.10 release, under the rules in `../README.md`, to score what was designed from
2026-11b's misses: DT-CORE-36 to 40, DT-DATA-55 to 58, and DT-DATA-45 and DT-DATA-52's transformers rules. **This
directory was committed before DepTime ran on any of it.** The first scoring is `results-2026-11-09.md`.

## Uses

169 upgrades of real projects, 2,637 uses asked about:

| Source | Upgrades | Work | Break | Excluded |
|---|---|---|---|---|
| Pinned, upgrade to the newest release (Tier A 56, Tier B 50) | 106 | 1,172 | 92 | 402 |
| Pinned, the same code on a release a year older (Tier A 25, Tier B 38) | 63 | 669 | 36 | 266 |
| **All** | **169** | **1,841** | **128** | **668** |

Uses of all 53 covered packages are scored; 11 of the breaks are in Tier B packages. Every upgrade comes from a pinned
repository: all 53 packages were searched (`selection.json`: 1,644 candidates GitHub's code search returned, and why
each skipped one was skipped), and a repository 2026-11 or 2026-11b had asked about didn't qualify. `find_pinned.py`
takes the first 100 results, then up to 500 when those give fewer than two repositories, so every package has two
(pymongo and urllib3 had one each in 2026-11b). The cutoff is 2026-11-09: the newest releases are those before that day.

204 of the 209 environments installed. The five that didn't are old releases whose dependencies have no wheels as of
30 days after them (`probe-envs.json`): chromadb 0.3.22 and 0.3.29, openai 0.26.5, fastmcp 2.11.3 and dspy 2.5.40;
their uses are excluded. Two needed the fallback that lets pypika and ffmpy build from source (gradio 3.40.1, crewai
0.100.1).

Labels come from `holdout` run [42611030964](https://github.com/cschmertz/deptime/actions/runs/42611030964) (branch
`spike/holdout`, commit `c86568e`). The run before it,
[42610365540](https://github.com/cschmertz/deptime/actions/runs/42610365540), checked no use on Python 3.9-3.11: since
`23ae8a8` the probe had a backslash inside an f-string expression, which those Pythons can't parse, so it died before
the first use; `c86568e` fixes it. The probe now also looks for `self.<name> =` in the source of a class's MRO when a
built object's member is missing on the class, and such a use is excluded (2026-11b's six label errors, gradio 3's
event listeners). 24 working uses are deprecated by the probe's rule.

Four labels were changed by hand (`label-overrides.json`), each a call that still works at `to` though its generated
signature rejects it: langchain-openai 1.7.4's `AzureChatOpenAI(max_tokens=)` (a field whose alias the signature shows,
with `populate_by_name`), crewai 0.1.32's `Agent(cache=)` and `Task(expected_output=)` (pydantic models that drop
unknown keywords), and streamlit 1.67's `components.v1.html(_, height=)` (a deprecation wrapper around a bound method,
which `inspect.signature` unwraps to the plain function). The other ten breaks found by binding a call were checked
against the source at `to` and kept.

## Tracebacks

35 real tracebacks from GitHub issues, 21 packages (Tier A 24 cases, Tier B 11). By change type, as the scoring splits
them: moved (a name or module removed, with a replacement the library states) 6, removed 15, parameter renamed 2,
parameter removed 2, now required 1, not yet added 9. Each traceback was cut from its page, each version quoted from the
thread, and each expected answer taken from the library's changelog, migration guide or source at the two releases.

They were collected under DT-QA-01's rules for the packages 2026-11b had none for and others: a break already in
`bench/cases`, `bench/tmb` or an earlier holdout didn't qualify, and at most two reports per break were kept. Three
candidates were dropped in review: a version inferred from a lockfile rather than stated in the thread, a version
inferred from a dependency range, and a report an AI agent wrote. No report passed the rules for langchain-community,
langchain-openai, weaviate-client, google-generativeai, litellm or instructor.

A break is one name or one parameter: langchain 1.0's removed modules (`prompts`, `callbacks`, `docstore`,
`text_splitter`) give one break each, as in 2026-11b.

32 have a boundary the probe confirmed (`traceback-checks.json`, run 42611030964: 32 of 32 agree). The other 3 were
checked in the source at both tags:
- mcp 2's `CallToolResult.isError` and mistralai 1.5's `Mistral.ocr`: a pydantic field and a client set in `__init__`,
  which the class doesn't show.
- `celery.backends.database.retry`: importing the module needs sqlalchemy, which the environment lacks.
