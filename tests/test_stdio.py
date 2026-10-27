"""`deptime mcp` over real stdio, launched the way MCP clients launch it.

This guards the most fragile property of local mode: stdout must carry only protocol messages.
"""

import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from tests.test_tracebacks import HTTPX_TB


@pytest.mark.anyio
async def test_stdio_server_end_to_end(index_file, settings):
    env = {
        **os.environ,
        "DEPTIME_INDEX_PATH": str(index_file),
        "DEPTIME_COVERAGE_FILE": str(settings.coverage_file),
        "DEPTIME_OFFLINE": "1",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "deptime.cli", "mcp"], env=env)
    async with Client(params) as client:
        tools = await client.list_tools()
        assert "explain_error" in {t.name for t in tools.tools}
        result = await client.call_tool(
            "explain_error", {"traceback": HTTPX_TB, "installed_versions": {"httpx": "0.28.1"}}
        )
        assert result.structured_content["findings"][0]["symbol"] == "httpx.Client.__init__(proxies)"
