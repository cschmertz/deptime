// S5 (DT-SPIKE-05): drive DepTime with an MCP TypeScript SDK client: handshake, tools/list, explain_error.
//
//   npm install --prefix <dir> @modelcontextprotocol/sdk@1.13.0
//   cd <dir> && node <repo>/bench/s5/ts_client.mjs stdio uvx deptime mcp
//   cd <dir> && node <repo>/bench/s5/ts_client.mjs http http://127.0.0.1:8765/mcp   # SDK 1.10+
//
// Run from the directory whose node_modules holds the SDK version under test. Prints one JSON line.
import { existsSync, readFileSync } from "node:fs";

const root = process.cwd() + "/node_modules/@modelcontextprotocol/sdk";
const dist = existsSync(root + "/dist/esm") ? root + "/dist/esm" : root + "/dist"; // 1.0.x has no esm/
const sdk = JSON.parse(readFileSync(root + "/package.json", "utf8")).version;
const { Client } = await import(dist + "/client/index.js");
const TB =
  'Traceback (most recent call last):\n  File "/app/main.py", line 3, in <module>\n' +
  '    client = httpx.Client(proxies="http://localhost:8030")\n' +
  "TypeError: Client.__init__() got an unexpected keyword argument 'proxies'\n";

const [mode, ...target] = process.argv.slice(2);
let transport;
if (mode === "stdio") {
  const { StdioClientTransport } = await import(dist + "/client/stdio.js");
  const env = {};
  for (const k of ["PATH", "HOME", "DEPTIME_HOME", "DEPTIME_OFFLINE", "UV_CACHE_DIR"]) if (process.env[k]) env[k] = process.env[k];
  transport = new StdioClientTransport({ command: target[0], args: target.slice(1), env });
} else {
  const { StreamableHTTPClientTransport } = await import(dist + "/client/streamableHttp.js");
  transport = new StreamableHTTPClientTransport(new URL(target[0]));
}
const client = new Client({ name: "deptime-s5", version: "0" }, { capabilities: {} });
await client.connect(transport);
const tools = await client.listTools();
const res = await client.callTool({
  name: "explain_error",
  arguments: { traceback: TB, installed_versions: { httpx: "0.28.1" } },
});
const top = (JSON.parse(res.content[0].text).findings || [{}])[0];
console.log(JSON.stringify({
  sdk,
  tools: tools.tools.map((t) => t.name).sort(),
  is_error: res.isError ?? false,
  finding: [top.package, top.symbol, top.issue],
}));
await client.close();
