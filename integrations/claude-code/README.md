# DepTime plugin for Claude Code

Finds what a Python dependency update breaks in your code, and has Claude fix those lines. After you or Dependabot
bump a version, `/deptime:fix-upgrade` lists each use the new release breaks (a removed name, a keyword it no longer
accepts, a newly required argument) with the release that changed it and what replaces it, and Claude fixes them.
When Claude hits an import or argument error, the same index tells it what changed in the library, in which release,
and what to use instead.

It covers fast-moving Python libraries: the AI and agent stack (openai, langchain, pydantic, mcp, transformers
and more) and widely used web and data libraries (fastapi, django, sqlalchemy, pandas, numpy, pillow and more).
Answers come from a prebuilt index of each release's public API, read without importing or running the library. No
AI model is involved, and your code and errors never leave your machine.

## Try it

- `/deptime:fix-upgrade` after updating your lockfile: checks every package whose pinned version changed.
- `/deptime:fix-upgrade httpx 0.28.1`: checks one update before you make it.
- "Why does `httpx.Client(proxies=...)` fail with an unexpected keyword argument?": Claude asks DepTime's
  `explain_error` tool, which says the keyword went in httpx 0.28.0 and to pass `proxy` instead.

## What it runs, fetches and stores

| Component | File | What it does |
|---|---|---|
| MCP server | `.mcp.json` | Runs DepTime locally over stdio with `uvx`, pinned to this plugin's version. No account, no key, no data leaves your machine. |
| Skill | `skills/deptime/SKILL.md` | Teaches when and how to use `explain_error`, `check_symbols` and `diff_versions` |
| Command | `commands/fix-upgrade.md` | `/deptime:fix-upgrade [package version [old-version]]` runs `uvx deptime==<this plugin's version> check-upgrade` in the project, which writes its report to `.deptime-report.md`; Claude reads it, fixes the lines it reports as broken, checks the uses it can't tell about, runs it again, then deletes the report. It reads the project's files and lockfiles locally, running `git` to read the lockfiles at `HEAD` or the branch's base; nothing is sent |
| Hook | `hooks/hooks.json` → `scripts/suggest_deptime.py` | After a Bash command prints a Python version-change error, adds a one-line hint to call `explain_error` (local, no network) |

The hint hook uses Python 3. On Windows with Git Bash, put a Python installation's `python.exe` or
`python3.exe` on PATH. The hook skips Microsoft Store aliases in `WindowsApps` and stays silent if no
interpreter is available. On macOS and Linux it uses `python3`.

The first run downloads the prebuilt index (one file from this repo's GitHub Releases) in the background. After that it refreshes at most once a day.

Failed Bash commands fire `PostToolUseFailure`, not `PostToolUse`, so the hook is registered for both. So that it suggests each error once, the hook keeps a short hash of each error line it has hinted at, per session, in the plugin's own data folder. It stores no error text and sends nothing.

## Install

This repo is also a plugin marketplace (`.claude-plugin/marketplace.json`). In Claude Code:

```text
/plugin marketplace add cschmertz/deptime
/plugin install deptime@deptime
```

From a terminal: `claude plugin marketplace add cschmertz/deptime && claude plugin install deptime@deptime`.

The plugin's MCP server runs `uvx deptime==<this plugin's version> mcp`, which installs that exact DepTime release from PyPI.

## Try it from a source checkout

```bash
claude plugin validate integrations/claude-code      # "Validation passed"
claude mcp add deptime -- uv run --directory "$PWD" deptime mcp
```

## Want a hosted endpoint instead?

Point Claude Code at a server started with `deptime serve` (docs/05-self-hosting.md):

```bash
claude mcp add --transport http deptime https://your-host.example/mcp
```
