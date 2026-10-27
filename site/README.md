# site/ — rename-map pages and landing page (DT-GTM-01, DT-GTM-02)

Static pages generated **from the index**, never written by hand. That way every fact (removed symbol, version boundary, replacement) traces back to the same data the MCP tools serve.

Planned generator: `uv run python -m deptime.site build --out site/dist`. It will:
1. Run `diff_versions` for each migration below and group events by symbol.
2. Render one page per migration: a table of old → new symbols, version boundaries, replacement evidence, and a "paste your traceback into DepTime" call to action with install instructions.
3. Emit `sitemap.xml` and `llms.txt` so search engines and AI assistants can cite the pages.

| Page | Diff |
|---|---|
| MCP Python SDK 1.x → 2.x | `mcp` last 1.x → 2.x |
| LangChain 0.3 → 1.x | `langchain`, `langchain-core`, `langchain-community` |
| google-generativeai → google-genai | cross-package (needs DT-DATA-07) |
| pydantic v1 → v2 | `pydantic` 1.10.x → 2.x |
| openai 0.x → 1.x | `openai` 0.28 → 1.x |

Deploy target: Cloudflare Pages (free).
