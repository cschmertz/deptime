# site/ — rename-map pages and landing page (DT-GTM-01, DT-GTM-02)

Static pages generated **from the index**, never written by hand. That way every fact (removed symbol, version boundary, replacement) traces back to the same data the MCP tools serve.

Generator: `uv run python -m deptime.site build --out site/dist [--index PATH] [--base-url URL]` (`src/deptime/site.py`). For each migration below, between the newest release of the old series and the newest of the new one, it:
1. lists the names and arguments that were removed, moved or deprecated and what the index says replaces them (the library's deprecation text, the project's migration guide as a curated hint, or the same object at another path), with the evidence quoted and escaped;
2. lists the module-level names removed with no stated replacement;
3. writes one page per migration, `index.html`, `llms.txt` and, with `--base-url`, `sitemap.xml`.

| Page | Diff |
|---|---|
| MCP Python SDK 1.x → 2.x | `mcp` last 1.x → 2.x |
| LangChain 0.3 → 1.x | `langchain`, `langchain-core`, `langchain-community` |
| google-generativeai → google-genai | cross-package (needs DT-DATA-07) |
| pydantic v1 → v2 | `pydantic` 1.10.x → 2.x |
| openai 0.x → 1.x | `openai` 0.28 → 1.x |

Deploy target: Cloudflare Pages.
