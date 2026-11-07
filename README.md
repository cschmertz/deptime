<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cschmertz/deptime/main/docs/assets/deptime-wordmark-dark.svg">
    <img alt="DepTime" src="https://raw.githubusercontent.com/cschmertz/deptime/main/docs/assets/deptime-wordmark-light.svg" width="340">
  </picture>
</h1>

<p align="center"><b>An API time machine for Python libraries, built for coding agents.</b></p>

<div align="center">

[![PyPI](https://img.shields.io/pypi/v/deptime)](https://pypi.org/project/deptime/)
[![Python](https://img.shields.io/pypi/pyversions/deptime)](https://pypi.org/project/deptime/)
[![CI](https://github.com/cschmertz/deptime/actions/workflows/ci.yml/badge.svg)](https://github.com/cschmertz/deptime/actions/workflows/ci.yml)
[![Index build](https://github.com/cschmertz/deptime/actions/workflows/build-index.yml/badge.svg)](https://github.com/cschmertz/deptime/actions/workflows/build-index.yml)
[![License: MIT](https://img.shields.io/github/license/cschmertz/deptime)](https://github.com/cschmertz/deptime/blob/main/LICENSE)
[![Glama](https://glama.ai/mcp/servers/cschmertz/deptime/badges/score.svg)](https://glama.ai/mcp/servers/cschmertz/deptime)

</div>

When an agent hits `ImportError`, `AttributeError` or "unexpected keyword argument" in a fast-moving library, one call to DepTime says **what changed, in which release, and what to use instead**. Answers are computed from static diffs of every indexed release, not from docs or model memory.

Free, open source (MIT), and **local**: your code and errors never leave your machine.

<p>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cschmertz/deptime/main/docs/assets/how-it-works-dark.svg">
  <img alt="How DepTime works. Built nightly on GitHub: new PyPI releases are read statically and diffed release to release; packages are never imported or run. The only thing that crosses to your machine is one file, index.sqlite.xz. Answered on your machine: deptime mcp answers your agent locally, and your code and errors stay there." src="https://raw.githubusercontent.com/cschmertz/deptime/main/docs/assets/how-it-works-light.svg">
</picture>
</p>

Every night, GitHub Actions reads each new release's public API with [griffe](https://mkdocstrings.github.io/griffe/), which parses the source without running it, diffs it against the previous release, and publishes the result as one file, `index.sqlite.xz`, on this repo's Releases. On your machine, `deptime mcp` checks that file's sha256 and schema before swapping it in, then answers every question locally in milliseconds. The full design is in [docs/04-architecture.md](https://github.com/cschmertz/deptime/blob/main/docs/04-architecture.md), and the decisions are in [docs/adr/](https://github.com/cschmertz/deptime/tree/main/docs/adr).

```text
TypeError: Client.__init__() got an unexpected keyword argument 'proxies'
```

```json
{
  "symbol": "httpx.Client.__init__(proxies)",
  "issue": "param_removed",
  "installed_version": "0.28.1",
  "last_version_with": "0.27.2",
  "first_version_without": "0.28.0",
  "deprecated_in": "0.26.0",
  "replacements": [
    {"target": "proxy", "confidence": 0.8, "source": "deprecation_message"},
    {"target": "mounts", "confidence": 0.79, "source": "deprecation_message"}
  ],
  "pin_to_restore": "httpx<=0.27.2",
  "confidence": "high"
}
```

That's the real `explain_error` answer from the published index, trimmed. The full answer also quotes the deprecation message DepTime found in httpx's own source, and adds a one-sentence explanation for your agent to pass on.

> **Status:** alpha. The prebuilt index is rebuilt twice a day. Known gaps are tracked in [BACKLOG.md](https://github.com/cschmertz/deptime/blob/main/BACKLOG.md).

## How well it works

Each check's right answers were written down before DepTime first ran on it ([bench/](https://github.com/cschmertz/deptime/tree/main/bench)). Judge DepTime by the first run: the fixes since then were designed from its misses, so the later numbers are flattering.

| Check | Cases | First run | Now |
|---|---|---|---|
| Right package, name and breaking release for a traceback from a GitHub issue, on the [holdout of 2026-11-07](https://github.com/cschmertz/deptime/blob/main/bench/holdout/2026-11/results-2026-11-07.md) | 35, after every fix so far | 88.6% | 88.6% |
| Code that still works after an upgrade, flagged as broken by `check_symbols` (lower is better) | 3,600 uses from 292 upgrades of real projects, labelled by running them | 3.8% | 3.8% |
| The same, by `diff_versions(used_symbols=...)` | 2,399 it answers | 0.3% | 0.3% |
| Code that breaks, flagged by `check_symbols` / `diff_versions` | 130 uses | 97.7% / 76.9% | 97.7% / 76.9% |
| Right package, name and breaking release for a traceback from a GitHub issue | 32 collected 2026-11-05 | 68.8% | 96.9% |
| The same, on a holdout fixed in advance from [TimeMachine-bench](https://github.com/tohoku-nlp/timemachine-bench) | 29 | 72.4% | 96.6% |
| Error kind and package read from the traceback: scripts, pytest, chained exceptions, `ExceptionGroup`, IPython, Python 3.10–3.14 | 736, holdout fixed in advance | 93.8% | 93.8% |
| Replacement hints at confidence ≥ 0.75 that are right | hints for 50 documented migrations | 11 of 11 | 42 of 45 |
| Documented migrations that get a right hint at ≥ 0.75 | 50 | 11 | 42 (32 without hand-checked hints from migration guides) |
| Names that exist at runtime but read as absent | 2,428 sampled at runtime from 10 dynamic packages | 5.1% | 1.2% |

Across all 237 real tracebacks in the benchmark, including the ones fixes were designed from, DepTime names the right package, name and release for 97.9%, and a right replacement is among its first three for 66.7%. CI re-scores them and both holdouts on every code change and every published index, and fails on a drop of more than 2 points (or a false-positive rate up by more than 1).

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
# Claude Code. Cursor, Codex, VS Code and Claude Desktop take the same
# command (uvx deptime mcp) in their MCP config.
claude mcp add deptime -- uvx deptime mcp
```

On first launch DepTime downloads its prebuilt index (one file from this repo's GitHub Releases) in the background, then checks for a newer one at most once a day. Set `DEPTIME_OFFLINE=1` to never touch the network. Each index carries a signed record of the CI run that built it; `deptime index pull --verify` checks it ([SECURITY.md](https://github.com/cschmertz/deptime/blob/main/SECURITY.md#verifying-the-index)).

Claude Code users can also install the plugin in [`integrations/claude-code`](https://github.com/cschmertz/deptime/tree/main/integrations/claude-code) with `/plugin marketplace add cschmertz/deptime`, then `/plugin install deptime@deptime`. It adds a skill and a local hook that suggests DepTime when a Python version-change error appears in a command's output.

**First launch timing out?** `uvx` installs DepTime the first time a client starts it. On macOS with an x86_64 `uv` (an Intel Mac, or Intel Homebrew on Apple silicon), one dependency, `cryptography`, has no prebuilt wheel and is compiled, which can take longer than a client waits for a server to start. Run `uvx deptime --help` once beforehand.

### Client compatibility

Checked 2026-11-05 with `deptime` 0.1.4 ([details](https://github.com/cschmertz/deptime/tree/main/bench/s5)):

| Client | Result |
|---|---|
| Claude Code 2.1.318 | ✅ |
| Codex CLI 0.166.0 | ✅ |
| MCP Python SDK clients 1.0–1.30 and TypeScript SDK clients 1.0–1.32 (protocols 2024-11-05 to 2025-11-25), over stdio and against `deptime serve` | ✅ |
| Cursor, VS Code, Claude Desktop | not checked yet; they take the same `uvx deptime mcp` command |

## Tools

| Tool | Use it when |
|---|---|
| `explain_error(traceback, installed_versions?, pip_freeze?)` | An import or call into a library just failed |
| `symbol_history(package, path)` | Before trusting an API you remember |
| `check_symbols(package, version, symbols[{path, kwargs}])` | Pre-flight check of the symbols and kwargs you're about to use |
| `diff_versions(package, from_version \| date, to_version?, used_symbols?)` | Upgrading, or "what changed since my training cutoff?" |
| `coverage()` | What's indexed (53 fast-moving packages: AI/agent stack + libraries with known breaking changes; [list](https://github.com/cschmertz/deptime/blob/main/docs/coverage.md)) |

Every answer carries evidence and a confidence level. If a package isn't covered, DepTime says so instead of guessing. The contracts are in [docs/03-product-spec.md](https://github.com/cschmertz/deptime/blob/main/docs/03-product-spec.md).

## CLI

```bash
uvx deptime explain --installed httpx==0.28.1 < traceback.txt
uvx deptime history httpx Client.__init__
uvx deptime check httpx 0.28.1 'httpx.Client:proxies,proxy'
uvx deptime diff httpx 0.27.2 0.28.1
uvx deptime diff pydantic 2024-06-01              # everything since a date
uvx deptime index status | pull
```

## Hosting (optional)

Local mode covers coding agents. For web clients that only accept a URL, run `deptime serve` (MCP at `/mcp`, JSON at `/v1/*`) on any container host. The repo includes a `Dockerfile` and `fly.toml`, and the server applies anonymous rate limits. See [docs/05-self-hosting.md](https://github.com/cschmertz/deptime/blob/main/docs/05-self-hosting.md).

## Prior art and credits

- [griffe](https://github.com/mkdocstrings/griffe) does the static analysis.
- [since-cutoff](https://github.com/MohammadHijjawi97/since-cutoff) diffs your pinned dependencies against a model's training cutoff, locally and on demand. DepTime differs by shipping a prebuilt cross-version index and starting from a traceback.
- [Context7](https://context7.com) and [Ref](https://ref.tools) serve version-specific docs. DepTime answers symbol-level "when and what replaced it" questions.

## Contributing

The most useful contributions are wrong-answer reports, which become benchmark cases. See [CONTRIBUTING.md](https://github.com/cschmertz/deptime/blob/main/CONTRIBUTING.md). Development setup:

```bash
uv sync && uv run pytest
# build a small local index from real PyPI wheels
uv run deptime ingest httpx --versions 0.27.2 0.28.1
```

## Docs

[Product spec](https://github.com/cschmertz/deptime/blob/main/docs/03-product-spec.md) · [Architecture](https://github.com/cschmertz/deptime/blob/main/docs/04-architecture.md) · [Self-hosting](https://github.com/cschmertz/deptime/blob/main/docs/05-self-hosting.md) · [Risks & spikes](https://github.com/cschmertz/deptime/blob/main/docs/07-risks-and-spikes.md) · [Privacy & security](https://github.com/cschmertz/deptime/blob/main/docs/08-privacy-and-security.md) · [Changelog](https://github.com/cschmertz/deptime/blob/main/CHANGELOG.md)

## License

[MIT](https://github.com/cschmertz/deptime/blob/main/LICENSE)

<!-- mcp-name: io.github.cschmertz/deptime -->
