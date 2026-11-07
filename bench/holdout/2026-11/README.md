# Holdout set 2026-11 (DT-QA-07)

Collected on 2026-11-07, before the release after 0.1.8, under the rules in `../README.md`. **This directory was
committed before DepTime ran on any of it.** The first scoring is `results-2026-11-07.md`.

## Uses

303 upgrades of real projects, 5,018 uses asked about:

| Source | Upgrades | Work | Break | Excluded |
|---|---|---|---|---|
| TimeMachine-bench (Tier B 162, Tier A 2) | 164 | 1,905 | (not counted) | 664 |
| Pinned, upgrade to the newest release (Tier A 56, Tier B 28) | 84 | 983 | 54 | 387 |
| Pinned, the same code on a release a year older (Tier A 39, Tier B 16) | 55 | 712 | 76 | 237 |
| **All** | **303** | **3,600** | **130** | **1,288** |

Uses of 50 of the 53 covered packages are scored. 42 packages were searched for pinned repositories (`selection.json`:
every candidate GitHub's code search returned, and why each skipped one was skipped); `find_pinned.py` picked two per
package where two qualified. Seven environments didn't install from wheels (`probe-envs.json`: chromadb 0.3.21, 0.4.13
and 0.5.0, crewai 0.30.11 and 0.108.0, dspy 2.5.29, gradio 3.50.2), so those upgrades' uses are excluded.

Labels come from `holdout` run [42384611061](https://github.com/cschmertz/deptime/actions/runs/42384611061) (branch
`spike/holdout`, commit `b0c692c`). Earlier runs resolved paths another way: attributes only
([42382883731](https://github.com/cschmertz/deptime/actions/runs/42382883731) for the uses,
[42384212116](https://github.com/cschmertz/deptime/actions/runs/42384212116) for the tracebacks), which got one
traceback's boundary wrong (fastmcp 3.4's `fastmcp.tools.tool`, a module alias behind a function of the same name); then
import first ([42384424878](https://github.com/cschmertz/deptime/actions/runs/42384424878)), which changed 6 uses (two
transformers modules that need torch, and `instructor.patch`, a function and a module, read as the module). The final
order, attributes first and then by import, gives the same use labels as the first run and confirms all 27 boundaries. One label was changed by hand
(`label-overrides.json`: llama-index's `Document(text=)` still works through `__init__(**data)`).

## Tracebacks

35 real tracebacks from GitHub issues, 20 packages (Tier A 22, Tier B 13). By change type: removed 10, moved (removed,
with a replacement the library states) 9, parameter renamed 4, parameter removed 2, not yet added 10. Each traceback was
matched against its page and each version against the thread, and each expected answer comes from the library's
changelog, migration guide or source at the two releases. 27 have a boundary the probe confirmed
(`traceback-checks.json`, run 42384611061: 27 of 27 agree); the other 8 need torch or name an attribute each instance
sets, and were checked in the source at both tags. One break is already in `bench/cases` (mcp 2.0 removing
`streamablehttp_client`; this is its second case), which the scoring reports apart.
