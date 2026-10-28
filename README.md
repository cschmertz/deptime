<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cschmertz/deptime/main/docs/assets/deptime-wordmark-dark.svg">
    <img alt="DepTime" src="https://raw.githubusercontent.com/cschmertz/deptime/main/docs/assets/deptime-wordmark-light.svg" width="340">
  </picture>
</h1>

[![CI](https://github.com/cschmertz/deptime/actions/workflows/ci.yml/badge.svg)](https://github.com/cschmertz/deptime/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/cschmertz/deptime)](https://github.com/cschmertz/deptime/blob/main/LICENSE)

**An API time machine for Python libraries, built for coding agents.** When an agent hits `ImportError`, `AttributeError` or "unexpected keyword argument" in a fast-moving library, one call to DepTime says **what changed, in which release, and what to use instead**. Answers are computed from static diffs of every indexed release, not from docs or model memory.

Free, open source (MIT), and **local**: your code and errors never leave your machine.

```text
TypeError: Client.__init__() got an unexpected keyword argument 'proxies'      (httpx 0.28.1 installed)

→ explain_error
  httpx.Client.__init__ accepted `proxies` up to httpx 0.27.2; it is gone from 0.28.1 onward.
  Deprecated in or before 0.27.2 ("The 'proxies' argument is now deprecated. Use 'proxy' or 'mounts' instead.").
  Use `proxy` or `mounts` instead, or pin httpx<=0.27.2.          issue=param_removed  confidence=high
```

That output is real. It came from the actual httpx 0.27.2/0.28.1 wheels, and the deprecation text was recovered from httpx's own source code.

> **Status:** pre-alpha (2026-10-17). The pipeline, local mode and plugin work end to end; the public index isn't published yet. See [BACKLOG.md](BACKLOG.md).

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
# Claude Code (Cursor, Codex, VS Code and Claude Desktop take the same command in their MCP config)
claude mcp add deptime -- uvx deptime mcp
```

On first launch DepTime downloads its prebuilt index (one file from this repo's GitHub Releases) in the background, then checks for a newer one at most once a day. Set `DEPTIME_OFFLINE=1` to never touch the network.

Claude Code users can also install the plugin in [`integrations/claude-code`](integrations/claude-code) with `/plugin marketplace add cschmertz/deptime`, then `/plugin install deptime@deptime`. It adds a skill and a local hook that suggests DepTime when a Python version-change error appears in a command's output.

> Until the first PyPI release, run from a checkout: `claude mcp add deptime -- uv run --directory /path/to/deptime deptime mcp`.

## Tools

| Tool | Use it when |
|---|---|
| `explain_error(traceback, installed_versions?, pip_freeze?)` | An import or call into a library just failed |
| `symbol_history(package, path)` | Before trusting an API you remember |
| `check_symbols(package, version, symbols[{path, kwargs}])` | Pre-flight check of the symbols and kwargs you're about to use |
| `diff_versions(package, from_version \| date, to_version?, used_symbols?)` | Upgrading, or "what changed since my training cutoff?" |
| `coverage()` | What's indexed (52 fast-moving packages: AI/agent stack + libraries with known breaking changes) |

Every answer carries evidence and a confidence level. If a package isn't covered, DepTime says so instead of guessing. The contracts are in [docs/03-product-spec.md](docs/03-product-spec.md).

## CLI

```bash
uvx deptime explain --installed httpx==0.28.1 < traceback.txt
uvx deptime history httpx Client.__init__
uvx deptime check httpx 0.28.1 'httpx.Client:proxies,proxy'
uvx deptime diff httpx 0.27.2 0.28.1              # or: diff pydantic 2024-06-01 (since a date)
uvx deptime index status | pull
```

## How it works

1. **Nightly in GitHub Actions:** download every selected release of each covered package from PyPI, extract its public API with [griffe](https://mkdocstrings.github.io/griffe/) (static analysis, no code is ever executed), and diff consecutive releases. Results go into a compact SQLite index. Snapshots are cached, so only new releases are analysed.
2. The index is published as `index.sqlite.gz` + `manifest.json` (sha256, schema version) on a rolling `index` release.
3. **On your machine,** `deptime mcp` verifies and installs that file, then answers every question locally in milliseconds.

The full design is in [docs/04-architecture.md](docs/04-architecture.md) and the decisions are in [docs/adr/](docs/adr/).

## Hosting (optional)

Local mode covers coding agents. For web clients that only accept a URL, or for a public demo, run `deptime serve` (MCP at `/mcp`, JSON at `/v1/*`) on Cloud Run (likely free) or Fly.io (about $3–6/month). It has a `Dockerfile` and `fly.toml`, plus anonymous rate limits. See [docs/05-hosting-and-sustainability.md](docs/05-hosting-and-sustainability.md).

## Prior art and credits

- [griffe](https://github.com/mkdocstrings/griffe) does the static analysis.
- [since-cutoff](https://github.com/MohammadHijjawi97/since-cutoff) diffs your pinned dependencies against a model's training cutoff, locally and on demand. DepTime differs by shipping a prebuilt cross-version index and starting from a traceback.
- [Context7](https://context7.com) and [Ref](https://ref.tools) serve version-specific docs. DepTime answers symbol-level "when and what replaced it" questions.

## Contributing

The most useful contributions are wrong-answer reports, which become benchmark cases. See [CONTRIBUTING.md](CONTRIBUTING.md). Development setup:

```bash
uv sync && uv run pytest
uv run deptime ingest httpx --versions 0.27.2 0.28.1   # build a small local index from real PyPI wheels
```

## Docs

[Product spec](docs/03-product-spec.md) · [Architecture](docs/04-architecture.md) · [Hosting & sustainability](docs/05-hosting-and-sustainability.md) · [Launch & adoption](docs/06-launch-and-adoption.md) · [Risks & spikes](docs/07-risks-and-spikes.md) · [Privacy & security](docs/08-privacy-and-security.md)

## License

[MIT](LICENSE)
