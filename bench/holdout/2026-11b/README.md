# Holdout set 2026-11b (DT-QA-08)

Collected on 2026-11-08, after the 0.1.9 release, under the rules in `../README.md`, to score DT-CORE-32 to 35 and
DT-DATA-52 to 54, which were designed from 2026-11's misses. **This directory was committed before DepTime ran on any
of it.** The first scoring is `results-2026-11-08.md`.

## Uses

176 upgrades of real projects, 3,322 uses asked about:

| Source | Upgrades | Work | Break | Excluded |
|---|---|---|---|---|
| Pinned, upgrade to the newest release (Tier A 56, Tier B 48) | 104 | 1,518 | 51 | 331 |
| Pinned, the same code on a release a year older (Tier A 35, Tier B 37) | 72 | 1,024 | 95 | 303 |
| **All** | **176** | **2,542** | **146** | **634** |

Uses of all 53 covered packages are scored; 27 of the breaks are in Tier B packages (2026-11 had 5). TimeMachine-bench's
repositories were used up by 2026-11, so every upgrade here comes from a pinned repository: all 53 packages were searched
(`selection.json`: 920 candidates GitHub's code search returned, and why each skipped one was skipped; 145 were skipped
because 2026-11 had asked about them). `find_pinned.py` picked two repositories per package, except pymongo and urllib3,
where one qualified among the 100 results. The cutoff is 2026-11-08: the newest releases are those before that day.

All 218 environments installed. Eight of them needed the new fallback that lets pypika and ffmpy build from source
(`probe-envs.json`: chromadb 0.4.13 and 0.4.15, crewai 0.41.1, 0.100.0 and 0.150.0, gradio 3.35.2, 3.40.1 and 4.37.1),
which keeps the chromadb, crewai and gradio uses that 2026-11 lost.

Labels come from `holdout` run [42451607786](https://github.com/cschmertz/deptime/actions/runs/42451607786) (branch
`spike/holdout`, commit `ca14aee`). Earlier runs on the same uses
([42450816206](https://github.com/cschmertz/deptime/actions/runs/42450816206),
[42451024873](https://github.com/cschmertz/deptime/actions/runs/42451024873),
[42451249661](https://github.com/cschmertz/deptime/actions/runs/42451249661)) gave the same labels, except for the
deprecation rule: reading the labels showed it marking matplotlib's `pyplot.figure` (a `make_keyword_only` wrapper, which
is about a parameter, in matplotlib's `deprecation.py`) and sqlalchemy's `and_`/`or_` (a `.. deprecated::` note about
calling them with no arguments), so the final run counts neither kind; 6 uses changed, all `deprecated` flags. 14 working
uses are deprecated by the final rule. Two labels were changed by hand (`label-overrides.json`): crewai 0.100.0's
`Agent(mcps=)` (a pydantic model that ignores unknown keywords) and huggingface_hub 2.4.0's
`snapshot_download(local_dir_use_symlinks=)` (a decorator drops it with a warning); the five other keyword breaks were
checked and kept.

## Tracebacks

37 real tracebacks from GitHub issues, 26 packages (Tier A 23, Tier B 14). By change type, as the scoring splits them:
moved (a name or module removed, with a replacement the library states) 17, removed 10, parameter renamed 3, parameter
removed 2, now required 1, not yet added 4. Each traceback was matched against its page and each version against the thread, and each expected
answer comes from the library's changelog, migration guide or source at the two releases.

A break is one name, or one parameter of sibling classes, as in 2026-11: a release that removes several names in one
changelog entry (qdrant-client 1.16's deprecated methods, werkzeug 3.0's deprecated code, langchain 1.0's move to
langchain-classic) gives one break per name. One break is already in `bench/cases` (huggingface_hub 1.0 removing
`HfFolder`; this is its second case), which the scoring reports apart.

28 have a boundary the probe confirmed (`traceback-checks.json`, run 42451607786: 28 of 28 agree). The other 9 were
checked in the source at both tags:
- transformers ×3: importing the names needs torch.
- openai's `FileCitation.quote`: a pydantic field, which instances set.
- `openai.embeddings_utils` and `pydantic_ai.mcp`: they need optional extras.
- pandas' `to_csv(line_terminator=)`: 1.5 took it through a `deprecate_kwarg` wrapper the signature doesn't show.
- llama-index-core 0.10.68: the environment lacks pydantic.
- crewai 0.63.x: PyPI has no litellm wheels its pin allows.

fastmcp's check leaves out the installed 2.14.3, whose environment lacks `packaging`.
