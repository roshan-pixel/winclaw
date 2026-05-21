"""
mem0 MCP Server for OpenClaw
============================
Repo: github.com/mem0ai/mem0
Replaces flat MEMORY.md with smart vector memory.
Works with BOTH Ollama/DeepSeek (free, local) AND Gemini (cloud).

Run by mcp-cli-tool.py — same pattern as windows_mcp_server.py
Author: Claude Sonnet 4.6 — 2026-03-28
"""

import json, os, sys, logging

log_handlers = [logging.StreamHandler(sys.stderr)]
log_path = r"C:\Users\sgarm\.openclaw\logs\mem0_mcp.log"
try:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log_handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
except Exception:
    # Do not fail server startup if log file cannot be opened.
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mem0-mcp] %(levelname)s: %(message)s",
    handlers=log_handlers,
)
log = logging.getLogger("mem0-mcp")

DEFAULT_USER_ID = "sgarm"
MEM0_DATA_DIR   = r"C:\Users\sgarm\.openclaw\memory\mem0_store"
OLLAMA_BASE_URL  = "http://localhost:11434"
OLLAMA_LLM_MODEL = "deepseek-r1-godmode"
OLLAMA_EMBED_MODEL = "nomic-embed-text"
TOP_K = 5

TOOLS = {
    "mem0-search": {
        "description": "Search relevant memories before every reply. Returns top 5 relevant memories (~100 tokens). MUCH cheaper than injecting full MEMORY.md.",
        "inputSchema": {"type":"object","properties":{"query":{"type":"string"},"user_id":{"type":"string","default":DEFAULT_USER_ID},"limit":{"type":"integer","default":TOP_K}},"required":["query"]},
    },
    "mem0-add": {
        "description": "Store a new memory. Call after reply when user shares a preference, fact, task or correction. mem0 auto-deduplicates.",
        "inputSchema": {"type":"object","properties":{"content":{"type":"string"},"user_id":{"type":"string","default":DEFAULT_USER_ID}},"required":["content"]},
    },
    "mem0-get-all": {
        "description": "Get ALL memories. Use ONLY when user asks 'what do you remember about me?'. Always use mem0-search for normal replies.",
        "inputSchema": {"type":"object","properties":{"user_id":{"type":"string","default":DEFAULT_USER_ID}},"required":[]},
    },
    "mem0-delete": {
        "description": "Delete a specific memory by its ID.",
        "inputSchema": {"type":"object","properties":{"memory_id":{"type":"string"}},"required":["memory_id"]},
    },
    "mem0-clear": {
        "description": "Clear ALL memories for user. Use only when user explicitly says 'forget everything'.",
        "inputSchema": {"type":"object","properties":{"user_id":{"type":"string","default":DEFAULT_USER_ID}},"required":[]},
    },
}

def get_mem():
    if not hasattr(get_mem, "_m"):
        from mem0 import Memory
        os.makedirs(MEM0_DATA_DIR, exist_ok=True)
        get_mem._m = Memory.from_config({
            "llm":{"provider":"ollama","config":{"model":OLLAMA_LLM_MODEL,"ollama_base_url":OLLAMA_BASE_URL,"temperature":0.1,"max_tokens":512}},
            "embedder":{"provider":"ollama","config":{"model":OLLAMA_EMBED_MODEL,"ollama_base_url":OLLAMA_BASE_URL}},
            "vector_store":{"provider":"chroma","config":{"collection_name":"openclaw_memories","path":MEM0_DATA_DIR}},
        })
        log.info("mem0 client ready")
    return get_mem._m

def tool_search(a):
    r = get_mem().search(query=a["query"], user_id=a.get("user_id",DEFAULT_USER_ID), limit=int(a.get("limit",TOP_K)))
    mems = [{"id":x.get("id"),"memory":x.get("memory"),"score":round(x.get("score",0),3)} for x in (r.get("results") or [])]
    return json.dumps({"memories":mems,"count":len(mems)})

def tool_add(a):
    r = get_mem().add(messages=[{"role":"user","content":a["content"]}], user_id=a.get("user_id",DEFAULT_USER_ID))
    added = r.get("results",[])
    return json.dumps({"status":"stored","memories_updated":len(added)})

def tool_get_all(a):
    r = get_mem().get_all(user_id=a.get("user_id",DEFAULT_USER_ID))
    mems = [{"id":x.get("id"),"memory":x.get("memory")} for x in (r.get("results") or [])]
    return json.dumps({"memories":mems,"count":len(mems)})

def tool_delete(a):
    get_mem().delete(memory_id=a["memory_id"]); return json.dumps({"status":"deleted","memory_id":a["memory_id"]})

def tool_clear(a):
    get_mem().delete_all(user_id=a.get("user_id",DEFAULT_USER_ID)); return json.dumps({"status":"cleared"})

HANDLERS = {"mem0-search":tool_search,"mem0-add":tool_add,"mem0-get-all":tool_get_all,"mem0-delete":tool_delete,"mem0-clear":tool_clear}

def send(obj): sys.stdout.write(json.dumps(obj)+"\n"); sys.stdout.flush()

def handle(req):
    m, rid, p = req.get("method",""), req.get("id"), req.get("params",{})
    if m=="initialize":
        return {"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"mem0-mcp","version":"1.0.0"}}}
    if m=="notifications/initialized": return None
    if m=="tools/list":
        return {"jsonrpc":"2.0","id":rid,"result":{"tools":[{"name":n,**s} for n,s in TOOLS.items()]}}
    if m=="tools/call":
        name, args = p.get("name",""), p.get("arguments",{})
        if name not in HANDLERS:
            return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Unknown tool: {name}"}}
        try:
            txt = HANDLERS[name](args)
            return {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":txt}],"isError":False}}
        except Exception as e:
            log.error(f"{name} error: {e}", exc_info=True)
            return {"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":f"ERROR: {e}"}],"isError":True}}
    return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Unknown method: {m}"}}

if __name__=="__main__":
    log.info("mem0 MCP Server starting")
    for line in sys.stdin:
        line=line.strip()
        if not line: continue
        try: req=json.loads(line)
        except: continue
        resp=handle(req)
        if resp: send(resp)
