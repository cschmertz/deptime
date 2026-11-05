"""S5 (DT-SPIKE-05): drive DepTime with an MCP Python SDK 1.x client: handshake, tools/list, explain_error.

    # stdio: launch the server as a client would
    uv run --no-project --exclude-newer <date> --with "mcp==1.9.4" python bench/s5/py_client.py stdio uvx deptime mcp
    # Streamable HTTP, against `deptime serve` (mcp 1.8+)
    uv run --no-project --exclude-newer <date> --with "mcp==1.9.4" python bench/s5/py_client.py http http://127.0.0.1:8765/mcp

Pass --exclude-newer a week after the SDK release, so its dependencies resolve as they did then.
Prints one JSON line: the SDK version, the negotiated protocol version, the tools, and explain_error's top finding.
"""

import asyncio
import importlib.metadata
import json
import os
import sys

from mcp import ClientSession

TB = (
    "Traceback (most recent call last):\n"
    '  File "/app/main.py", line 3, in <module>\n'
    '    client = httpx.Client(proxies="http://localhost:8030")\n'
    "TypeError: Client.__init__() got an unexpected keyword argument 'proxies'\n"
)


async def exercise(read, write) -> dict:
    async with ClientSession(read, write) as session:
        init = await session.initialize()
        tools = await session.list_tools()
        res = await session.call_tool(
            "explain_error", {"traceback": TB, "installed_versions": {"httpx": "0.28.1"}}
        )
        body = json.loads(res.content[0].text) if res.content else {}
        top = (body.get("findings") or [{}])[0]
        return {
            "sdk": importlib.metadata.version("mcp"),
            "protocol": init.protocolVersion,
            "tools": sorted(t.name for t in tools.tools),
            "is_error": res.isError,
            "finding": [top.get("package"), top.get("symbol"), top.get("issue")],
        }


async def main() -> None:
    mode, *target = sys.argv[1:]
    if mode == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        keep = ("PATH", "HOME", "DEPTIME_HOME", "DEPTIME_OFFLINE", "UV_CACHE_DIR")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        async with stdio_client(StdioServerParameters(command=target[0], args=target[1:], env=env)) as (r, w):
            print(json.dumps(await exercise(r, w)))
    else:
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(target[0]) as (r, w, _):
            print(json.dumps(await exercise(r, w)))


asyncio.run(main())
