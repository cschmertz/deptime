# Holdout set 2026-11f (DT-QA-12)

Collected on 2026-11-11 under the rules in `../README.md`, to score what was designed from 2026-11e's misses: DT-CORE-50
to 56 (query side, not yet released when the set was picked). **This directory was committed before DepTime ran on any
of it.** The first scoring is `results-<date>.md`.

## Uses

UPGRADES_TABLE

Uses of all 53 covered packages are scored. The main stratum comes from 101 pinned repositories: all 53 packages were
searched (`selection.json`: every candidate GitHub's code search returned, and why each skipped one was skipped), and a
repository 2026-11, 2026-11b, 2026-11c, 2026-11d or 2026-11e had asked about didn't qualify. Every package has two but
urllib3, whose search found one qualifying repository in its first 500 results (444 candidates), as in 2026-11e. The
cutoff is 2026-11-11: the newest releases are those before that day.

EXERCISE_PARAGRAPH

ENVS_PARAGRAPH

LABELS_PARAGRAPH

## Tracebacks

26 real tracebacks from GitHub issues and Stack Overflow, 20 packages (Tier A 8 cases, Tier B 18). By change type, as the
scoring splits them: moved (a name or module removed, with a replacement the library states) 11, removed 9, parameter
removed 3, parameter renamed 2, not yet added 1. Each traceback was cut from its page, each version quoted from the
thread, and each expected answer taken from the library's changelog, migration guide or source at the two releases,
read from the shipped wheels.

They were collected under DT-QA-01's rules, blind (DepTime wasn't run), with priority on the packages the last sets had
none for. A break already in `bench/cases`, `bench/tmb` or an earlier holdout didn't qualify, and at most two reports per
break were kept. For the fourth set running, no report passed the rules for langchain-community, langchain-openai,
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
- pytest 8.1's `FixtureDef(fixturemanager=)` (beside 8.2's `FixtureDef.unittest`), streamlit 1.41's `image_to_url` (beside
  `AtomicImage`, also in this set). mcp 1.12.3's
`FastMCP(description=)` was never a named parameter: 1.12.2 took it through `**settings` and ignored it. selenium's
traceback quotes the error as `WebDriver.init()` (the page lost the underscores), kept verbatim. Eight tracebacks are the
error line alone, as their pages quote it.

TRACEBACK_CHECKS
