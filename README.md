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
  <img alt="How DepTime works. Built nightly on GitHub: new PyPI releases are read statically and diffed release to release; packages are never imported or run. The only thing that crosses to your machine is one file, index.sqlite.gz. Answered on your machine: deptime mcp answers your agent locally, and your code and errors stay there." src="https://raw.githubusercontent.com/cschmertz/deptime/main/docs/assets/how-it-works-light.svg">
</picture>
</p>

Every night, GitHub Actions reads each new release's public API with [griffe](https://mkdocstrings.github.io/griffe/), which parses the source without running it, diffs it against the previous release, and publishes the result as one file, `index.sqlite.gz`, on this repo's Releases. On your machine, `deptime mcp` checks that file's sha256 and schema before swapping it in, then answers every question locally in milliseconds. The full design is in [docs/04-architecture.md](https://github.com/cschmertz/deptime/blob/main/docs/04-architecture.md), and the decisions are in [docs/adr/](https://github.com/cschmertz/deptime/tree/main/docs/adr).

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

> **Status:** alpha. The prebuilt index is rebuilt nightly. Known gaps are tracked in [BACKLOG.md](https://github.com/cschmertz/deptime/blob/main/BACKLOG.md).

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
# Claude Code. Cursor, Codex, VS Code and Claude Desktop take the same
# command (uvx deptime mcp) in their MCP config.
claude mcp add deptime -- uvx deptime mcp
```

On first launch DepTime downloads its prebuilt index (one file from this repo's GitHub Releases) in the background, then checks for a newer one at most once a day. Set `DEPTIME_OFFLINE=1` to never touch the network.

Claude Code users can also install the plugin in [`integrations/claude-code`](https://github.com/cschmertz/deptime/tree/main/integrations/claude-code) with `/plugin marketplace add cschmertz/deptime`, then `/plugin install deptime@deptime`. It adds a skill and a local hook that suggests DepTime when a Python version-change error appears in a command's output.

## Tools

| Tool | Use it when |
|---|---|
| `explain_error(traceback, installed_versions?, pip_freeze?)` | An import or call into a library just failed |
| `symbol_history(package, path)` | Before trusting an API you remember |
| `check_symbols(package, version, symbols[{path, kwargs}])` | Pre-flight check of the symbols and kwargs you're about to use |
| `diff_versions(package, from_version \| date, to_version?, used_symbols?)` | Upgrading, or "what changed since my training cutoff?" |
| `coverage()` | What's indexed (52 fast-moving packages: AI/agent stack + libraries with known breaking changes) |

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

Local mode covers coding agents. For web clients that only accept a URL, or for a public demo, run `deptime serve` (MCP at `/mcp`, JSON at `/v1/*`) on Cloud Run (likely free) or Fly.io (about $3–6/month). It has a `Dockerfile` and `fly.toml`, plus anonymous rate limits. See [docs/05-hosting-and-sustainability.md](https://github.com/cschmertz/deptime/blob/main/docs/05-hosting-and-sustainability.md).

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

[Product spec](https://github.com/cschmertz/deptime/blob/main/docs/03-product-spec.md) · [Architecture](https://github.com/cschmertz/deptime/blob/main/docs/04-architecture.md) · [Hosting & sustainability](https://github.com/cschmertz/deptime/blob/main/docs/05-hosting-and-sustainability.md) · [Launch & adoption](https://github.com/cschmertz/deptime/blob/main/docs/06-launch-and-adoption.md) · [Risks & spikes](https://github.com/cschmertz/deptime/blob/main/docs/07-risks-and-spikes.md) · [Privacy & security](https://github.com/cschmertz/deptime/blob/main/docs/08-privacy-and-security.md)

## License

[MIT](https://github.com/cschmertz/deptime/blob/main/LICENSE)

<!-- mcp-name: io.github.cschmertz/deptime -->
