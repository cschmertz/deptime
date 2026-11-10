# Holdout set 2026-11d (DT-QA-10)

Collected on 2026-11-10, after the `SNAPSHOT_FORMAT` 25 index went live, under the rules in `../README.md`, to score
what was designed from 2026-11c's misses: DT-DATA-59 to 62 and DT-CORE-41 to 45. **This directory was committed before
DepTime ran on any of it.** The first scoring is `results-2026-11-10.md`.

## Uses

182 upgrades of real projects, 3,222 uses asked about:

| Source | Upgrades | Work | Break | Excluded |
|---|---|---|---|---|
| Pinned, upgrade to the newest release (Tier A 56, Tier B 50) | 106 | 1,303 | 95 | 399 |
| Pinned, the same code on a release a year older (Tier A 32, Tier B 28) | 60 | 671 | 68 | 212 |
| Exercise stratum, upgrade (Tier A 5, Tier B 5) | 10 | 180 | 11 | 96 |
| Exercise stratum, a year older (Tier A 2, Tier B 4) | 6 | 125 | 2 | 60 |
| **All** | **182** | **2,279** | **176** | **767** |

Uses of all 53 covered packages are scored; 14 of the breaks are in Tier B packages. The main stratum comes from 97
pinned repositories: all 53 packages were searched (`selection.json`: every candidate GitHub's code search returned, and
why each skipped one was skipped), and a repository 2026-11, 2026-11b or 2026-11c had asked about didn't qualify. Every
package has two. The cutoff is 2026-11-10: the newest releases are those before that day.

The exercise stratum (new in this set, `../README.md`) adds 10 repositories, found by reading the same search results
past the main stratum, whose code has a use 2026-11c didn't exercise: `mcp.types` with mcp pinned before 2.0.0 (2
repositories, 14 such uses), crewai's `Agent` or `Task` called with keywords (2 repositories, 8 calls), and a module
dunder (numpy 2, polars 2, pandas 1, pydantic 1; 12 uses). pandas and pydantic had one such repository each in the first
500 results. All 34 of those uses work but one, excluded by hand (below).

264 of the 267 environments installed. The three that didn't are releases whose dependencies have no wheels as of 30
days after them (`probe-envs.json`): chromadb 0.3.22, openai 0.26.5, and litellm 1.77.3 (pondpond needs madoka, a C++
extension); their uses are excluded. Nine needed the fallback that lets pypika, ffmpy and docx2txt build from source
(gradio 3 and 4, chromadb 0.6.3, crewai 0.28.8, 0.76.9 and 0.83.0).

Labels come from `holdout` run [42842697123](https://github.com/cschmertz/deptime/actions/runs/42842697123) (branch
`spike/holdout`). The run before it, [42841969944](https://github.com/cschmertz/deptime/actions/runs/42841969944), lost
two things the probe can check: dspy 0.1.5 writes a log file to the working directory when imported, which the sandbox
user couldn't write (40 uses), and crewai 0.76.9 and 0.83.0 need docx2txt, a pure-Python package published only as an
sdist, so neither installed (all three crewai exercise upgrades). The probe now runs the checker from the sandbox user's
home and allows docx2txt to build. 11 working uses are deprecated by the probe's rule.

The probe's new rule for an object built to fail when called (openai 1.x's `APIRemovedInV1Proxy`) finds none among the
uses; it confirms the `openai.Completion` traceback's boundary.

Of the 22 breaks found by binding a call, three were changed by hand (`label-overrides.json`), each a call that still
works at `to` though its generated signature rejects it: llama-index-core 0.14.25's `Document(text=)` (its own
`__init__(**data)` moves `text` into `text_resource`), and langchain 0.0.109's `SQLDatabaseToolkit(llm=)` and crewai
0.1.2's `Task(expected_output=)` (pydantic v1 models that drop unknown keywords). The other 19 were checked against the
source at `to`, read from the release's wheel, and kept.

## Tracebacks

30 real tracebacks from GitHub issues, 22 packages (Tier A 17 cases, Tier B 13). By change type, as the scoring splits
them: moved (a name or module removed, with a replacement the library states) 13, removed 8, parameter renamed 4,
parameter removed 3, not yet added 2. Each traceback was cut from its page, each version quoted from the thread, and
each expected answer taken from the library's changelog, migration guide or source at the two releases, read from the
shipped wheels.

They were collected under DT-QA-01's rules, blind (DepTime wasn't run), with priority on the packages 2026-11c had none
for: a break already in `bench/cases`, `bench/tmb` or an earlier holdout didn't qualify, and at most two reports per
break were kept. In review, one report that read as agent-written (a Summary / Root cause / Impact / Fix write-up) was
dropped, and one was cut down to the error line its page quotes. No report passed the rules for langchain-community,
langchain-openai, langchain-core, weaviate-client, google-generativeai, litellm, instructor, qdrant-client, chromadb or
pinecone: their removals' reports state no exact version, contradict it, or were agent-written (the collectors' reject
logs have 269 lines).

A break is one name or one parameter, as in 2026-11c: sqlalchemy 2.1's `ScalarObjectAttributeImpl` and streamlit 1.37's
`experimental_data_editor` come from the same changelog entries as earlier cases' `ScalarAttributeImpl` and
`experimental_rerun`, and count as their own breaks.

25 have a boundary the probe confirmed (`traceback-checks.json`, run 42842697123: 25 of 25 agree). The other 5 were
checked in the source at both releases:
- transformers' `top_k_top_p_filtering` (two reports): importing transformers' top level needs torch for the name.
- mcp 2's `InitializeResult.serverInfo`: a pydantic field read off an instance.
- fastmcp 3's `FastMCP(log_level=)`: 3.x's `__init__` takes `**kwargs` and raises from a table of removed keywords, so a
  signature would bind.
- pytest 8's `CallSpec2.funcargs`: a dataclass field set on instances.
