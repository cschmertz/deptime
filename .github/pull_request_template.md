## What and why

<!-- Backlog ID if any, e.g. DT-CORE-05 -->

## Checklist

- [ ] `uv run ruff format src tests && uv run ruff check src tests && uv run pytest` passes
- [ ] New behaviour has a test (parser/diff changes: failing test first)
- [ ] No public field or tool name renamed (additions only)
- [ ] Nothing prints to stdout in `deptime mcp` code paths
- [ ] Docs/BACKLOG updated if behaviour or plans changed
