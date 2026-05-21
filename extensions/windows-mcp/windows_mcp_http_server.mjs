#!/usr/bin/env node
/**
 * Windows MCP HTTP Server — runs standalone on port 18790
 * Uses MCP StreamableHTTP transport so openclaw can connect via URL
 * Start: node windows_mcp_http_server.mjs
 */
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { execSync, exec } from "child_process";
import { randomUUID } from "crypto";
import http from "http";
import { createRequire } from "module";
import { promisify } from "util";

const require = createRequire(import.meta.url);
const execAsync = promisify(exec);

const PORT = 18790;

// ─── PowerShell Helper ──────────────────────────────────────────────────────
function runPS(command, timeoutMs = 15000) {
  try {
    const result = execSync(
      `powershell.exe -NoProfile -NonInteractive -Command "${command.replace(/"/g, '\\"')}"`,
      { timeout: timeoutMs, encoding: "utf8" },
    );
    return { success: true, output: result.trim() };
  } catch (err) {
    return { success: false, output: err.message.trim() };
  }
}

async function runPSAsync(command, timeoutMs = 30000) {
  try {
    const escaped = command.replace(/"/g, '\\"');
    const { stdout, stderr } = await execAsync(
      `powershell.exe -NoProfile -NonInteractive -Command "${escaped}"`,
      { timeout: timeoutMs, encoding: "utf8" },
    );
    return { success: true, output: (stdout + stderr).trim() };
  } catch (err) {
    return { success: false, output: err.message.trim() };
  }
}

// ─── Tool Definitions ───────────────────────────────────────────────────────
const TOOLS = [
  {
    name: "windows_shell",
    description: "Execute PowerShell commands on Windows",
    inputSchema: {
      type: "object",
      properties: {
        command: { type: "string", description: "PowerShell command to execute" },
        timeout: { type: "number", description: "Timeout in seconds (default 30)", default: 30 },
      },
      required: ["command"],
    },
  },
  {
    name: "windows_snapshot",
    description: "Take a screenshot of the Windows desktop",
    inputSchema: {
      type: "object",
      properties: {
        output_path: { type: "string", description: "Optional file path to save screenshot" },
      },
    },
  },
  {
    name: "windows_list_windows",
    description: "List all open windows on the desktop",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "windows_public_ip",
    description: "Get the public IP address of this machine",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "windows_type",
    description: "Type text into the currently focused window",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Text to type" },
      },
      required: ["text"],
    },
  },
  {
    name: "windows_shortcut",
    description: "Execute a keyboard shortcut (e.g. ctrl+c, win+r, alt+f4)",
    inputSchema: {
      type: "object",
      properties: {
        shortcut: { type: "string", description: "Key combination like ctrl+c" },
      },
      required: ["shortcut"],
    },
  },
];

// ─── Tool Execution ─────────────────────────────────────────────────────────
async function executeTool(name, args) {
  switch (name) {
    case "windows_shell": {
      const timeout = (args.timeout || 30) * 1000;
      const r = await runPSAsync(args.command, timeout);
      return r.output || "(no output)";
    }

    case "windows_snapshot": {
      const outPath =
        args.output_path || `C:\\Users\\sgarm\\.openclaw\\screenshot_${Date.now()}.png`;
      const ps = `
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b=[System.Drawing.Rectangle]::FromLTRB(0,0,[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height)
$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height)
$g=[System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left,$b.Top,0,0,$b.Size)
$bmp.Save('${outPath.replace(/\\/g, "\\\\")}')
$g.Dispose();$bmp.Dispose()
Write-Output 'OK:${outPath.replace(/\\/g, "\\\\")}'
`.trim();
      const r = runPS(ps, 10000);
      if (r.success && r.output.includes("OK:")) {
        return `Screenshot saved to: ${outPath}`;
      }
      return `Screenshot failed: ${r.output}`;
    }

    case "windows_list_windows": {
      const r = runPS(
        `Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | Select-Object Id,ProcessName,MainWindowTitle | Format-Table -AutoSize | Out-String`,
      );
      return r.success ? r.output || "(no windows found)" : `Error: ${r.output}`;
    }

    case "windows_public_ip": {
      const r = await runPSAsync(
        `(Invoke-WebRequest -Uri 'https://api.ipify.org' -UseBasicParsing -TimeoutSec 5).Content.Trim()`,
        8000,
      );
      if (r.success && r.output) return `Public IP: ${r.output}`;
      const r2 = await runPSAsync(
        `(Invoke-WebRequest -Uri 'https://ifconfig.me' -UseBasicParsing -TimeoutSec 5).Content.Trim()`,
        8000,
      );
      return r2.success ? `Public IP: ${r2.output}` : `Failed to get IP: ${r.output}`;
    }

    case "windows_type": {
      const text = args.text.replace(/'/g, "''");
      const r = runPS(
        `Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('${text}')`,
      );
      return r.success ? `Typed: ${args.text}` : `Error: ${r.output}`;
    }

    case "windows_shortcut": {
      // Map common key names to SendKeys format
      const keyMap = { ctrl: "^", alt: "%", shift: "+", win: "^{ESC}" };
      const parts = args.shortcut.toLowerCase().split("+");
      let keys = "";
      for (const part of parts) {
        if (keyMap[part]) keys += keyMap[part];
        else if (part.length === 1) keys += part;
        else keys += `{${part.toUpperCase()}}`;
      }
      const r = runPS(
        `Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('${keys}')`,
      );
      return r.success ? `Sent: ${args.shortcut}` : `Error: ${r.output}`;
    }

    default:
      return `Unknown tool: ${name}`;
  }
}

// ─── MCP Server Factory ─────────────────────────────────────────────────────
function createMCPServer() {
  const server = new Server(
    { name: "windows-mcp-http", version: "1.0.0" },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const { name, arguments: args } = req.params;
    try {
      const result = await executeTool(name, args || {});
      return { content: [{ type: "text", text: String(result) }] };
    } catch (err) {
      return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
    }
  });

  return server;
}

// ─── HTTP Server ────────────────────────────────────────────────────────────
const sessions = new Map();

const httpServer = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Health check
  if (req.method === "GET" && url.pathname === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", service: "windows-mcp-http", port: PORT }));
    return;
  }

  // MCP endpoint (Streamable HTTP)
  if (url.pathname === "/mcp" || url.pathname === "/") {
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (sessionId) => {
        sessions.set(sessionId, transport);
        console.log(`[windows-mcp-http] Session started: ${sessionId}`);
      },
    });

    transport.onclose = () => {
      const sid = transport.sessionId;
      if (sid) {
        sessions.delete(sid);
        console.log(`[windows-mcp-http] Session closed: ${sid}`);
      }
    };

    const server = createMCPServer();
    await server.connect(transport);
    await transport.handleRequest(req, res, await streamBody(req));
    return;
  }

  res.writeHead(404);
  res.end("Not found");
});

function streamBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (chunks.length === 0) {
        resolve(undefined);
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString()));
      } catch {
        resolve(undefined);
      }
    });
  });
}

httpServer.listen(PORT, "127.0.0.1", () => {
  console.log(`[windows-mcp-http] ✅ MCP server listening on http://127.0.0.1:${PORT}/mcp`);
  console.log(`[windows-mcp-http] Health: http://127.0.0.1:${PORT}/health`);
  console.log(`[windows-mcp-http] Tools: ${TOOLS.map((t) => t.name).join(", ")}`);
});

httpServer.on("error", (err) => {
  console.error(`[windows-mcp-http] Server error:`, err.message);
});
