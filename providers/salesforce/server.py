"""
Salesforce MCP Server — JSON-RPC 2.0 over HTTP.

Exposes all Salesforce tools as an MCP-compatible endpoint.
The existing MCPClient (protocol="mcp") can call this server directly.

Usage:
  uvicorn providers.salesforce.server:app --port 8083

Env vars required:
  SF_CLIENT_ID       - Salesforce Connected App consumer key
  SF_CLIENT_SECRET   - Salesforce Connected App consumer secret
  SF_LOGIN_URL       - default: https://login.salesforce.com
  SF_API_VERSION     - default: v59.0

Then add to config/mcp_servers.json:
  {
    "name": "salesforce",
    "url": "http://localhost:8083",
    "protocol": "mcp",
    "timeout": 20
  }
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from providers.salesforce.auth import SalesforceAuth
from providers.salesforce.client import SalesforceClient
from providers.salesforce.tools import ALL_TOOLS, list_tool_schemas

logger = logging.getLogger(__name__)

app = FastAPI(title="Salesforce MCP Server")

# Module-level singletons — initialized on first request (lazy) so the server
# starts even if SF env vars are missing (useful for health checks).
_auth: SalesforceAuth | None = None
_client: SalesforceClient | None = None


def _get_client() -> SalesforceClient:
    global _auth, _client
    if _client is None:
        _auth = SalesforceAuth()
        _client = SalesforceClient(_auth)
    return _client


def _ok(id_, result: dict):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code: int, message: str):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


@app.post("/")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    id_ = body.get("id", 1)

    # ── tools/list ───────────────────────────────────────────────────────────
    if method == "tools/list":
        return JSONResponse(_ok(id_, {"tools": list_tool_schemas()}))

    # ── tools/call ───────────────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name not in ALL_TOOLS:
            return JSONResponse(_err(id_, -32601, f"Unknown tool: {tool_name}"))

        tool = ALL_TOOLS[tool_name]
        try:
            client = _get_client()
            result = await tool["handler"](client, arguments)
            return JSONResponse(_ok(id_, {
                "content": [{"type": "text", "text": str(result)}],
                "data": result,
            }))
        except Exception as e:
            logger.exception(f"Tool '{tool_name}' failed")
            return JSONResponse(_err(id_, -32000, str(e)))

    return JSONResponse(_err(id_, -32601, f"Method not found: {method}"))


@app.get("/health")
async def health():
    return {"status": "ok", "tools": list(ALL_TOOLS.keys())}
