"""The MCP surface: five read-only tools over the DepTime query layer.

Tool names, descriptions and parameter docs are product copy: agents decide whether to call a tool from
these strings alone. Keep them concrete, keep the "when to call" first, and re-run the benchmark when editing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from deptime import __version__
from deptime.models import (
    CheckSymbolsResult,
    CoverageResult,
    DiffResult,
    ExplainResult,
    SymbolHistory,
    SymbolQuery,
)
from deptime.query import DepTime
from deptime.tracebacks import MAX_ACCEPTED_CHARS, MAX_INPUT_CHARS

INSTRUCTIONS = """\
DepTime answers "what changed in this Python library, in which release, and what replaces it?" from static
analysis of every indexed release (no docs scraping, no LLM guesses). Use it when:
- a third-party import or call fails with ImportError, ModuleNotFoundError, AttributeError, or TypeError
  (unexpected/missing keyword argument): call `explain_error` with the traceback and `pip freeze` output;
- you are about to use an API you remember from training data: call `check_symbols` with the installed version;
- you are upgrading a dependency: call `diff_versions` (from may be a date such as your training cutoff).
Call `coverage` to see which packages and versions are indexed. Answers include evidence and confidence;
"never_existed" means no indexed release ever had that name; at low confidence the name may be created at
runtime or predate the earliest indexed release, so read its explanation before calling it misremembered.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def build_mcp(
    deptime: DepTime, *, middleware: Sequence[Any] = (), website_url: str | None = None
) -> MCPServer:
    mcp = MCPServer(
        name="deptime",
        title="DepTime",
        description="API time machine for Python libraries: what changed, when, and what to use instead.",
        instructions=INSTRUCTIONS,
        website_url=website_url,
        version=__version__,
        middleware=list(middleware),
    )

    @mcp.tool(
        name="explain_error",
        title="Explain a Python import/attribute/signature error",
        description=(
            "Call this when a third-party Python library raises ImportError ('cannot import name'), ModuleNotFoundError, "
            "AttributeError ('has no attribute'), or TypeError ('unexpected keyword argument' / 'missing required argument'). "
            "Returns what changed, in which release, the deprecation message, replacement candidates with evidence, and the "
            "version pin that restores the old API. Pass installed versions for an exact answer."
        ),
        annotations=READ_ONLY,
    )
    def explain_error(
        traceback: Annotated[
            str,
            Field(
                max_length=MAX_ACCEPTED_CHARS,
                description="Traceback text, or at least the final 'SomeError: message' line. "
                f"Long logs are fine: only the last {MAX_INPUT_CHARS:,} characters are analysed.",
            ),
        ],
        installed_versions: Annotated[
            dict[str, str] | None, Field(description='Installed versions, e.g. {"httpx": "0.28.1"}.')
        ] = None,
        pip_freeze: Annotated[
            str | None,
            Field(max_length=100_000, description="Output of `pip freeze` / `uv pip freeze` / `pip list`."),
        ] = None,
    ) -> ExplainResult:
        return deptime.explain_error(traceback, installed_versions, pip_freeze)

    @mcp.tool(
        name="symbol_history",
        title="Version history of one symbol",
        description=(
            "Version history of one public symbol, e.g. package='httpx', path='httpx.Client.__init__': first and last release "
            "it exists in, signature changes, deprecations, removals, and replacement candidates. Use before writing code "
            "against an API you remember from training data."
        ),
        annotations=READ_ONLY,
    )
    def symbol_history(
        package: Annotated[
            str, Field(max_length=100, description="PyPI distribution name, e.g. 'langchain-core'.")
        ],
        path: Annotated[
            str, Field(max_length=300, description="Public dotted path; the package prefix may be omitted.")
        ],
    ) -> SymbolHistory:
        return deptime.symbol_history(package, path)

    @mcp.tool(
        name="check_symbols",
        title="Check symbols and kwargs against an installed version",
        description=(
            "Batch-check that symbols, and the keyword arguments you plan to pass, exist in a specific version of a package. "
            "Returns present/missing per symbol, rejected kwargs, deprecation notes, and hints."
        ),
        annotations=READ_ONLY,
    )
    def check_symbols(
        package: Annotated[str, Field(max_length=100)],
        version: Annotated[str, Field(max_length=50, description="Installed version, e.g. '0.28.1'.")],
        symbols: Annotated[list[SymbolQuery], Field(max_length=100)],
    ) -> CheckSymbolsResult:
        return deptime.check_symbols(package, version, symbols)

    @mcp.tool(
        name="diff_versions",
        title="Breaking changes between versions",
        description=(
            "Breaking changes and deprecations of a package between two versions, or since a date such as your training "
            "cutoff ('YYYY-MM-DD'). Optionally restrict to the symbols your code uses. Most severe first."
        ),
        annotations=READ_ONLY,
    )
    def diff_versions(
        package: Annotated[str, Field(max_length=100)],
        from_version: Annotated[
            str,
            Field(max_length=50, description="Version, or a date 'YYYY-MM-DD' (nearest release on/before)."),
        ],
        to_version: Annotated[
            str | None, Field(max_length=50, description="Defaults to the latest indexed release.")
        ] = None,
        used_symbols: Annotated[list[str] | None, Field(max_length=500)] = None,
        include_added: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> DiffResult:
        return deptime.diff_versions(package, from_version, to_version, used_symbols, include_added, limit)

    @mcp.tool(
        name="coverage",
        title="What DepTime indexes",
        description="Lists indexed packages and version ranges. If a package is missing, DepTime cannot answer about it.",
        annotations=READ_ONLY,
    )
    def coverage() -> CoverageResult:
        return deptime.coverage_report()

    return mcp
