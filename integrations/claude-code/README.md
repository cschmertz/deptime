# DepTime plugin for Claude Code

| Component | File | What it does |
|---|---|---|
| MCP server | `.mcp.json` | Runs `uvx deptime mcp` locally over stdio. No account, no key, no data leaves your machine. |
| Skill | `skills/deptime/SKILL.md` | Teaches when and how to use `explain_error`, `check_symbols` and `diff_versions` |
| Hook | `hooks/hooks.json` → `scripts/suggest_deptime.py` | After a Bash command prints a Python version-change error, adds a one-line hint to call `explain_error` (local, no network) |

The first run downloads the prebuilt index (one file from this repo's GitHub Releases) in the background. After that it refreshes at most once a day.

Failed Bash commands fire `PostToolUseFailure`, not `PostToolUse`, so the hook is registered for both.

## Try it from a source checkout (before the PyPI release)

```bash
claude plugin validate integrations/claude-code      # "Validation passed"
claude mcp add deptime -- uv run --directory "$PWD" deptime mcp
```

After `deptime` is published to PyPI (DT-OSS-05), `claude --plugin-dir integrations/claude-code` or a marketplace install is enough.

## Want a hosted endpoint instead?

Point Claude Code at a server started with `deptime serve` (docs/05-hosting-and-sustainability.md):

```bash
claude mcp add --transport http deptime https://your-host.example/mcp
```
