/**
 * windows-mcp openclaw plugin — HTTP client edition
 * Connects to the standalone windows_mcp_http_server.mjs on port 18790
 * instead of spawning a subprocess.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const MCP_URL = "http://127.0.0.1:18790/mcp";
const MAX_TOOLS = 20;
const CONNECT_RETRY_MS = 3000;
const CONNECT_MAX_ATTEMPTS = 5;

let client = null;
let tools = [];
let ready = false;

async function connect(attempt = 1) {
  try {
    const transport = new StreamableHTTPClientTransport(new URL(MCP_URL));
    const c = new Client({ name: "openclaw-windows-mcp", version: "1.0.0" }, { capabilities: {} });
    await c.connect(transport);
    const result = await c.listTools();
    client = c;
    tools = result.tools || [];
    ready = true;
    console.log(
      `[windows-mcp] Connected to HTTP MCP server. ${tools.length} tools: ${tools.map((t) => t.name).join(", ")}`,
    );
  } catch (err) {
    console.error(`[windows-mcp] Connection attempt ${attempt} failed: ${err.message}`);
    if (attempt < CONNECT_MAX_ATTEMPTS) {
      console.log(`[windows-mcp] Retrying in ${CONNECT_RETRY_MS}ms...`);
      setTimeout(() => connect(attempt + 1), CONNECT_RETRY_MS);
    } else {
      console.error(`[windows-mcp] Could not connect to windows-mcp HTTP server at ${MCP_URL}`);
      console.error(`[windows-mcp] Start it with: node windows_mcp_http_server.mjs`);
    }
  }
}

// Start connecting immediately
connect();

export default function register(api) {
  console.log("[windows-mcp] Registering HTTP-backed plugin...");

  // Register a slot for each possible tool (we discover them at runtime)
  for (let i = 0; i < MAX_TOOLS; i++) {
    api.registerTool((ctx) => {
      if (ctx.sandboxed) return null;
      if (!ready) return null;
      const tool = tools[i];
      if (!tool) return null;

      return {
        name: tool.name,
        description: tool.description || `Windows tool: ${tool.name}`,
        inputSchema: tool.inputSchema || { type: "object", properties: {} },
        execute: async (args) => {
          try {
            const result = await client.callTool({ name: tool.name, arguments: args });
            const text =
              result.content
                ?.map((c) => (c.type === "text" ? c.text : JSON.stringify(c)))
                .join("\n") || "Done";
            return { content: [{ type: "text", text }] };
          } catch (err) {
            // Try reconnecting
            ready = false;
            connect();
            return { content: [{ type: "text", text: `Error (reconnecting): ${err.message}` }] };
          }
        },
      };
    });
  }
}
