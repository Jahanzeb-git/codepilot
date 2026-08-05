"""
File: mcp_registry.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-08-04

Description:
Persistent MCPClient connection pool + tools/call dispatcher.

Architecture:
- MCPRegistry holds one MCPClient per registered server URL and keeps
  those connections alive for the entire agent session.
- register() does the full MCP handshake (initialize → notify → tools/list)
  and returns the discovered List[MCPTool] so the caller can embed them.
- call_tool() dispatches a tools/call JSON-RPC request to the correct
  server, validates the response, and returns either the text content or
  structuredContent depending on what the server returns.

Protocol: MCP 2025-06-18 (JSON-RPC 2.0 over HTTP + SSE)

tools/call wire format (verified live against Tavily MCP 2026-08-04):

  REQUEST
  {
    "jsonrpc": "2.0",
    "id": <int>,
    "method": "tools/call",
    "params": {
      "name": "<tool_name>",
      "arguments": { ... }          -- tool-specific kwargs
      "_meta": {                    -- OPTIONAL
        "progressToken": <str|int>  -- for long-running progress tracking
      }
    }
  }

  RESPONSE
  {
    "jsonrpc": "2.0",
    "id": <int>,
    "result": {
      "content": [
        { "type": "text",     "text": "..." }  -- most common
        { "type": "image",    "data": "...", "mimeType": "..." }
        { "type": "resource", "resource": { "uri": "...", ... } }
      ],
      "structuredContent": { ... },   -- optional; machine-readable JSON
      "isError": false                 -- true if the TOOL itself failed
    }
  }

  ERROR RESPONSE (JSON-RPC protocol error, e.g. unknown method)
  {
    "jsonrpc": "2.0",
    "id": <int>,
    "error": { "code": -32000, "message": "...", "data": { ... } }
  }

Key observations from live testing:
  - Tavily MCP does NOT issue a Mcp-Session-Id → session_id stays None.
  - notifications/initialized returns HTTP 202 with an empty body.
  - call() in MCPClient already handles both application/json and SSE.
  - isError=True means the tool ran but returned an error result;
    it is NOT a JSON-RPC protocol error.

Copyright (c) 2026 Jahanzeb Ahmed. Licensed under the MIT License.
"""

from __future__ import annotations

import json
from typing import Any

from .mcp import MCPClient, MCPTool, ServerInfo


class MCPInvokeError(Exception):
    """Raised when a tools/call returns isError=True from the MCP server."""
    pass


class MCPRegistry:
    """
    Session-scoped connection pool and tool dispatcher for MCP servers.

    Usage::

        registry = MCPRegistry()

        # On startup — connect each server from config
        tools = await registry.register(
            server_name="tavily",
            server_url="https://mcp.tavily.com/mcp/...",
        )
        # hand `tools` to MCPToolStore.index_tools()

        # During agent execution — invoke a specific tool
        result = await registry.call_tool(
            server_name="tavily",
            tool_name="tavily_search",
            arguments={"query": "..."},
        )

        # On shutdown
        await registry.close_all()
    """

    def __init__(self) -> None:
        # server_name → persistent MCPClient (connection stays open)
        self._clients: dict[str, MCPClient] = {}
        # server_name → ServerInfo (for display / logging)
        self._server_info: dict[str, ServerInfo] = {}
        # server_name → {tool_name: MCPTool} (for validation before dispatch)
        self._tools: dict[str, dict[str, MCPTool]] = {}

    # ------------------------------------------------------------------
    #  Registration
    # ------------------------------------------------------------------

    async def register(
        self,
        server_url: str,
        api_key: Optional[str] = None,
        api_key_param: Optional[str] = None,
    ) -> list["MCPTool"]:
        """
        Connect to *server_url*, perform the MCP initialize handshake, and
        fetch the tools/list manifest.

        Returns the list of MCPTool objects so the caller can pass them to
        MCPToolStore.index_tools() for embedding.

        Idempotent: if a client for the server's reported name already exists,
        it is closed and a fresh connection is opened.
        """
        client = MCPClient(server_url, api_key=api_key, api_key_param=api_key_param)

        # Trigger dual-mode transport connection (Legacy SSE vs Streamable HTTP)
        await client.connect()

        # Perform handshake.
        server_info, _capabilities = await client.initialize()
        actual_name = server_info.name
        
        # Close any existing connection for this actual server name
        if actual_name in self._clients:
            await self._clients[actual_name].close()

        tools = await client.list_tools(server_info)

        # Persist connection and metadata under the server's TRUE reported name
        self._clients[actual_name]     = client
        self._server_info[actual_name] = server_info
        self._tools[actual_name]       = {t.name: t for t in tools}

        return tools

    # ------------------------------------------------------------------
    #  Tool invocation — tools/call (MCP 2025-06-18)
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """
        Invoke *tool_name* on *server_name* via the tools/call JSON-RPC method.

        Returns a human-readable string result, extracted from the response
        content array.  Prefers ``structuredContent`` (machine-readable JSON)
        when present; falls back to concatenating all text content items.

        Raises
        ------
        KeyError
            If *server_name* is not registered or *tool_name* does not exist
            on that server (checked against the tool manifest from tools/list).
        MCPInvokeError
            If the server returns isError=True in the result, meaning the tool
            ran but signalled a tool-level failure.
        RuntimeError
            For JSON-RPC protocol-level errors (``"error"`` key in response).
        """
        if server_name not in self._clients:
            raise KeyError(
                f"MCP server '{server_name}' is not registered. "
                f"Registered servers: {list(self._clients.keys())}"
            )

        known_tools = self._tools.get(server_name, {})
        if tool_name not in known_tools:
            raise KeyError(
                f"Tool '{tool_name}' not found on server '{server_name}'. "
                f"Available tools: {list(known_tools.keys())}"
            )

        client = self._clients[server_name]

        # --- Build the tools/call request (MCP 2025-06-18 spec) ---
        # _meta / progressToken is optional; omit it unless we add progress
        # tracking later.  JSON-RPC batching was REMOVED in 2025-06-18,
        # so each call is always a single request.
        payload: dict[str, Any] = {
            "jsonrpc": MCPClient.JSONRPC,
            "id": client._next_id(),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        response = await client.call(payload)
        result   = response["body"].get("result", {})

        # --- isError: tool ran but returned an error result ---
        # This is distinct from a JSON-RPC error (handled inside client.call).
        if result.get("isError", False):
            # Extract error message from content array if present
            error_text = _extract_text(result.get("content", []))
            raise MCPInvokeError(
                f"Tool '{tool_name}' on server '{server_name}' returned an error: "
                f"{error_text or '(no message)'}"
            )

        # --- Prefer structuredContent when present ---
        # structuredContent was introduced in 2025-06-18 to return machine-
        # readable JSON alongside text.  If present, serialise it so the
        # LLM can read it as structured data.
        if "structuredContent" in result and result["structuredContent"]:
            return json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)

        # --- Fall back to text content ---
        return _extract_text(result.get("content", []))

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    async def close_all(self) -> None:
        """Close every persistent connection. Safe to call more than once."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        self._server_info.clear()
        self._tools.clear()

    # ------------------------------------------------------------------
    #  Introspection
    # ------------------------------------------------------------------

    @property
    def registered_servers(self) -> list[tuple[str, str]]:
        """
        List of (server_name, server_url) pairs for all connected servers,
        in registration order. Used to build the system-prompt server block.
        """
        return [
            (name, self._server_info[name].server_url)
            for name in self._clients
        ]

    def tool_names_for(self, server_name: str) -> list[str]:
        """Return all known tool names for *server_name*."""
        return list(self._tools.get(server_name, {}).keys())


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _extract_text(content: list[dict]) -> str:
    """
    Concatenate all ``type=text`` items from an MCP content array.

    Content array spec (MCP 2025-06-18):
    - TextContent  → {"type": "text",     "text": "..."}
    - ImageContent → {"type": "image",    "data": "...", "mimeType": "..."}
    - ResourceContent → {"type": "resource", "resource": {...}}

    Non-text types are noted as a placeholder so the LLM knows they exist
    even if we can't display them inline.
    """
    parts: list[str] = []
    for item in content:
        t = item.get("type")
        if t == "text":
            parts.append(item.get("text", ""))
        elif t == "image":
            mime = item.get("mimeType", "image")
            parts.append(f"[Image: {mime} — cannot display inline]")
        elif t == "resource":
            uri = item.get("resource", {}).get("uri", "unknown")
            parts.append(f"[Resource: {uri}]")
        else:
            parts.append(f"[{t} content]")
    return "\n".join(parts)
