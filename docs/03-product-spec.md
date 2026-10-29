# 03 — Product spec: DepTime v1

## Positioning

> When a coding agent hits an `ImportError`, `AttributeError` or "unexpected keyword argument" in a fast-moving Python library, DepTime tells it in one call **what changed, in which release, and what to use instead**. Answers are computed from static diffs of every indexed release, not from docs or model memory.

DepTime is **free and open source (MIT)** and **runs locally** (`uvx deptime mcp`): code and errors never leave the machine ([ADR 0007](adr/0007-open-source-local-first.md)).

**Name:** DepTime. As of 2026-10-17 it is free on PyPI, npm and GitHub; DT-OSS-03 reserves it.

## Problem

- Models produce plausible-but-wrong library calls. Reported API-usage plausibility is under 30% across popular Python libraries, and deprecated usage reaches 70–90% when the surrounding code is already outdated ([The Deprecated API Trap](https://tianpan.co/blog/2026-04-17-deprecated-api-trap-ai-coding-agents)).
- The churn is real and recent:
  - LangChain 0.x → 1.0
  - `google-generativeai` → `google-genai`
  - pydantic v1 → v2
  - `openai` 0.x → 1.x
  - `mcp` 1.x → 2.x (`FastMCP` renamed `MCPServer`; verified on `mcp==2.3.0`)
- Today an agent's loop is: error → web search or changelog reading (thousands of tokens) → a guess → retry. The installed environment only shows the *current* API. It can't say when something was removed, what replaced it, or which pin restores it.

## Users and jobs

| Who | Job | Tool |
|---|---|---|
| Coding agents (Claude Code, Codex, Cursor, Copilot agent) mid-task | "This import/call just failed. Why, and what's the fix?" | `explain_error` |
| Coding agents writing new code from memory | "Does this API (and these kwargs) exist in the version installed here?" | `check_symbols`, `symbol_history` |
| Upgrade bots (Dependabot/Renovate PR fixers) | "What breaks for *my* usage between v1 and v2?" | `diff_versions` with `used_symbols` |
| Humans (secondary) | The same questions, from a terminal or curl | `deptime` CLI, `/v1/*` |

## How it runs

| Mode | Command | For |
|---|---|---|
| **Local (default)** | `uvx deptime mcp` (stdio) | Claude Code, Cursor, Codex, VS Code, Claude Desktop: private and free |
| CLI | `deptime explain / history / check / diff` | Humans, scripts |
| Hosted (optional) | `deptime serve`: `/mcp` + `/v1/*` | Web clients that need a URL; demos ([05](05-hosting-and-sustainability.md)) |

## Why DepTime vs. what exists

| Alternative | What it gives | What it lacks that DepTime adds |
|---|---|---|
| Introspecting the installed env (`dir()`, pyright) | The current version only | No history, no "renamed to", no pin |
| Context7 / Ref (versioned docs) | Docs prose for a version | No symbol-level boundaries, token-heavy, docs lag the code |
| [since-cutoff](https://github.com/MohammadHijjawi97/since-cutoff) | Local griffe diff of your pins vs. the model cutoff | Not hosted, needs per-project setup, has no traceback entry point, and names replacements only when deprecation text says so explicitly |
| dep-diff-mcp | Upgrade plans from release notes | Notes are incomplete; DepTime reads the code |
| Web search | Anything | Slow, expensive, unverified |

## Tool contract (v1)

All tools are read-only and idempotent, and return structured output (pydantic models in `src/deptime/models.py`). **Field names are a public contract: add fields, don't rename them.**

### `explain_error(traceback, installed_versions?, pip_freeze?) → ExplainResult` (main tool)

- **Input:**
  - `traceback`: the full traceback, or just the final exception line. Max 20,000 chars.
  - Versions: `installed_versions` as a dict, or `pip_freeze` as `pip freeze`/`pip list` text.
- **Signal kinds it handles:**
  - `import_name` (`cannot import name 'X' from 'm'`)
  - `module_missing`
  - `module_attribute`
  - `object_attribute` and `class_attribute` (resolved by class-name suffix)
  - `unexpected_kwarg`
  - `missing_argument`
  - `circular_import` and `unknown` are recognized and declined.
- **Output:** `findings[]`, best first. Each finding carries:
  - `issue`: `symbol_removed`, `param_removed`, `never_existed`, `symbol_not_yet_added`, `param_not_yet_added`, `param_now_required`, `module_removed`, or `present_in_installed_version`
  - `last_version_with` and `first_version_without`
  - `deprecated_in` and `deprecation_message`
  - `replacements[]`, each with `confidence`, `source` and `evidence`
  - `pin_to_restore`
  - `confidence`
  - `explanation`
- Real output, from the actual httpx 0.27.2/0.28.1 wheels:

```json
{
  "package": "httpx",
  "symbol": "httpx.Client.__init__(proxies)",
  "issue": "param_removed",
  "installed_version": "0.28.1",
  "last_version_with": "0.27.2",
  "first_version_without": "0.28.1",
  "deprecated_in": "<=0.27.2",
  "deprecation_message": "The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead.",
  "replacements": [{"target": "proxy", "confidence": 0.8, "source": "deprecation_message"}, {"target": "mounts", "confidence": 0.79, "source": "deprecation_message"}],
  "pin_to_restore": "httpx<=0.27.2",
  "confidence": "high"
}
```

### `symbol_history(package, path) → SymbolHistory`

- Reports first and last indexed release, `present_in_latest`, the signature at the last release where the symbol was seen, events (added, removed, deprecated, param changes, moved), and replacements.
- `ever_seen: false` means no indexed release ever exposed that path. This is the "you probably hallucinated this" signal.

### `check_symbols(package, version, symbols[{path, kwargs[]}]) → CheckSymbolsResult`

- A batch pre-flight check before writing code. Each symbol gets `present`, `missing` or `not_indexed`, plus `rejected_kwargs` and a deprecation note.
- If `version` isn't indexed, the answer uses the nearest earlier indexed release and says so in the response.

### `diff_versions(package, from_version, to_version?, used_symbols?, include_added?, limit?) → DiffResult`

- Events in `(from, to]`, most severe first.
- `from_version` can be a date (`YYYY-MM-DD`), which covers "everything since my training cutoff."
- `used_symbols` filters the results to your own usage.

### `coverage() → CoverageResult` (free, never metered)

- Lists the indexed packages and version ranges, the version policy, and packages that are configured but not indexed yet.

## Answer semantics (guarantees)

1. **Deterministic, no LLM at serve time** ([ADR 0006](adr/0006-no-llm-at-serve-time.md)). The same index and the same input always give the same answer.
2. **Evidence on every claim.** Version boundaries come from the indexed releases. Replacements carry their source text.
3. **Confidence:**
   - `high`: the installed version is indexed exactly.
   - `medium`: answered from the nearest indexed release, or from the window between two indexed releases.
   - `low`: no installed version was given.
4. **Honest boundaries.**
   - `first_version_without` is the *first indexed* release without the symbol; unindexed releases may sit in between.
   - `pin_to_restore` therefore uses `<=last_version_with`, which is always correct, rather than `<first_version_without`.
   - `deprecated_in = "<=X"` when the deprecation already existed in the earliest indexed release X.
5. **Say "don't know" instead of guessing.** A package outside coverage goes into `not_covered`. Unsupported error shapes return a `note`, not a guess.
6. **Mined text is untrusted data.** Deprecation messages come from third-party source code: they're length-capped and must be quoted, never followed ([08-privacy-and-security.md](08-privacy-and-security.md)).

## Coverage v1

- **Policy** (`src/deptime/coverage.toml`): for every minor series active since 2023-01-01, index every release, so a patch-level break gets its exact boundary (DT-DATA-19). No pre-releases and no yanked releases.
- **Tier A (AI and agent stack, 27 packages):** openai, anthropic, google-genai, google-generativeai, mistralai, litellm, langchain (+core, community, openai), langgraph, llama-index-core, mcp, fastmcp, pydantic (+settings), pydantic-ai-slim, openai-agents, crewai, dspy, instructor, huggingface-hub, chromadb, qdrant-client, pinecone, weaviate-client, ollama.
- **Tier B (libraries with documented breaking changes, 25 packages):** fastapi, starlette, httpx, sqlalchemy, sqlmodel, pandas, numpy (via its stubs), polars, scikit-learn, matplotlib, streamlit, gradio, django, flask, werkzeug, celery, redis, pymongo, selenium, urllib3, aiohttp, pytest, typer, click, marshmallow.
- **Standard library:** module availability per Python version, from typeshed `stdlib/VERSIONS` (DT-DATA-06). This catches removals like `distutils`, `imp` and `cgi`.
- **Skipped in v1:** boto3 (generated code), torch and tensorflow (compiled), apache-airflow (too large), transformers (about 1.5M public paths per release, mostly inherited methods; it returns with DT-DATA-16).

## Non-goals for v1

- npm or any other ecosystem.
- Docs, code examples or semantic search.
- Codemods or auto-fix PRs.
- CVE or license data.
- Dependency resolution.
- Private packages and teams.
- A CI app.
- LLM-written summaries.
- Type inference through arbitrary instance chains. v1 resolves `Class.attr` by class name.
- Compiled APIs without stubs. These are reported as not covered rather than guessed.
- Python older than 3.8.

## Quality bar (measured by `bench/`, DT-QA-*)

| Metric | Target |
|---|---|
| Correct root cause + version boundary on the traceback benchmark | ≥ 80% |
| Correct replacement, when one exists | ≥ 60% |
| False "removed" or "never_existed" rate in spike S1 sampling | ≤ 10% (**stop the project if this is breached**) |
| Server p95 latency per tool call | < 150 ms |
| New PyPI release indexed (p95) | < 24 h |

## Open questions

- ~~Licensing~~: decided as MIT, local-first ([ADR 0007](adr/0007-open-source-local-first.md)).
- Should rename maps also be exposed as MCP *resources* for clients that prefetch context?
- Should `explain_error` also mine replacement hints from the live error text? Some libraries now embed migration hints in the exception itself; `mcp` 2.x does (DT-CORE-06).
