"""
File: mcp_tool.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-08-04

Description:
The mcp() meta-tool — the single tool the LLM calls to interact with any
registered MCP server.  Two dispatch modes:

  DISCOVER  mcp(query="…")
    Embeds the query, cosine-searches the MCPToolStore, returns a formatted
    markdown block of the top-K matching tools so the LLM can read their
    signatures before deciding which to invoke.

  INVOKE    mcp(server="…", func_name="…", arguments={…})
    Looks up the MCPClient via MCPRegistry, fires the tools/call JSON-RPC
    call (MCP 2025-06-18), and returns the result as a string.

The LLM always knows it's working through MCP (transparency = better
decisions), and the two-step design prevents token bloat: tool schemas
only appear in context when the LLM explicitly asks for them.

Copyright (c) 2026 Jahanzeb Ahmed. Licensed under the MIT License.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..engine.runtime import AsyncRuntime


class MCPTools:
    """
    Hosts the mcp() meta-tool and owns the MCPRegistry + MCPToolStore.

    Lifecycle
    ---------
    1. Runtime.__init__ creates MCPTools(runtime) if 'mcp' is in enabled tools.
    2. MCPTools.__init__ reads agent.yaml config, starts async setup.
    3. MCPTools.setup() must be awaited before the first run() call.
       It registers each server, indexes tools, and closes one-shot clients.
    4. Each mcp() call during the agentic loop dispatches via the open registry.
    5. On runtime teardown, MCPTools.close() awaits registry.close_all().
    """

    def __init__(self, runtime: "AsyncRuntime") -> None:
        self._runtime = runtime
        self._cfg     = runtime._tool_config("mcp")

        # Lazily imported to avoid pulling numpy at module load time
        from ..core.mcp_registry import MCPRegistry
        from ..core.mcp_store    import MCPToolStore

        # Resolve embedding API key from env
        emb_key_env = self._cfg.get("embedding_api_key_env", "VOYAGE_API_KEY")
        emb_key     = os.environ.get(emb_key_env, "")

        self._registry = MCPRegistry()
        self._store    = MCPToolStore(
            api_key=emb_key,
            model=self._cfg.get("embedding_model",    "voyage-code-3"),
            base_url=self._cfg.get("embedding_base_url", "https://api.voyageai.com/v1"),
        )
        self._top_k: int = int(self._cfg.get("top_k", 3))
        self._ready: bool = False
        self._main_loop = None

    # ------------------------------------------------------------------
    #  Setup — must be awaited before first run()
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """
        Connect every server listed in agent.yaml and index their tools.

        Called once at the start of the first run() call (deferred from
        __init__ because __init__ is synchronous).
        """
        import asyncio
        self._main_loop = asyncio.get_running_loop()
        
        servers: list[dict] = self._cfg.get("servers", [])
        for srv in servers:
            name           = srv.get("name", "unknown")
            url            = srv.get("url", "")
            api_key_env    = srv.get("api_key_env")
            api_key_param  = srv.get("api_key_param")

            if not url:
                continue

            # Resolve API key from env
            api_key = os.environ.get(api_key_env) if api_key_env else None

            # Connect and index (will raise exception if server is unreachable or key is invalid)
            tools = await self._registry.register(
                server_url=url,
                api_key=api_key,
                api_key_param=api_key_param,
            )
            await self._store.index_tools(tools)

        self._ready = True

    # ------------------------------------------------------------------
    #  The meta-tool
    # ------------------------------------------------------------------

    def mcp(
        self,
        *,
        query: Optional[str] = None,
        server: Optional[str] = None,
        func_name: Optional[str] = None,
        arguments: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Two-step gateway for all MCP tools.

        Step 1: DISCOVER
          mcp(query="A tool that retrieves web page content from a URL")
          -> Returns server, func_name, and schema of matching tools.

        Step 2: INVOKE
          mcp(server="tavily-mcp", func_name="tavily_search", arguments={"query": "python"})
          -> Executes the tool and returns the result.

        Parameters
        ----------
        query     : For DISCOVER. Describe the tool you need (see example).
        server    : For INVOKE. The exact server name returned by DISCOVER.
        func_name : For INVOKE. The exact tool name returned by DISCOVER.
        arguments : For INVOKE. Dict matching the tool's input schema.
        """
        import asyncio
        import concurrent.futures
        from ..engine.hooks import EventType

        # Emit TOOL_CALL so external listeners/logs can see the mcp() invocation
        args_log = {}
        if query: args_log["query"] = query
        if server: args_log["server"] = server
        if func_name: args_log["func_name"] = func_name
        if arguments: args_log["arguments"] = arguments
        
        if self._runtime:
            self._runtime.hooks.emit(
                EventType.TOOL_CALL,
                tool="mcp",
                args=args_log,
                label=f"DISCOVER: {query}" if query else f"INVOKE: {server}.{func_name}"
            )

        # exec() runs synchronously inside an already-running event loop's worker thread
        # We must submit the async work back to the main event loop because our httpx.AsyncClient
        # and other resources are bound to the main event loop.
        
        if not self._ready or self._main_loop is None:
            return "[MCP error] MCP is not initialized. Please ensure setup() is complete."

        future = asyncio.run_coroutine_threadsafe(
            self._dispatch(
                query=query,
                server=server,
                func_name=func_name,
                arguments=arguments,
            ),
            self._main_loop
        )
        
        try:
            res = future.result(timeout=120)
        except concurrent.futures.TimeoutError:
            res = "[MCP] Request timed out after 120 seconds."
        except Exception as exc:
            res = f"[MCP error] {exc}"
            
        if self._runtime:
            self._runtime._append_execution(res)
            self._runtime.hooks.emit(EventType.TOOL_RESULT, tool="mcp", result=res)
            
        return res

    async def _dispatch(
        self,
        *,
        query:     Optional[str],
        server:    Optional[str],
        func_name: Optional[str],
        arguments: Optional[dict[str, Any]],
    ) -> str:
        if not self._ready:
            await self.setup()

        # ---- INVOKE mode ----
        if server is not None and func_name is not None:
            return await self._invoke(server, func_name, arguments or {})

        # ---- DISCOVER mode ----
        if query is not None:
            return await self._discover(query)

        return (
            "mcp() requires either:\n"
            "  • query='...'                                    (DISCOVER)\n"
            "  • server='...', func_name='...', arguments={...} (INVOKE)"
        )

    # ------------------------------------------------------------------
    #  DISCOVER
    # ------------------------------------------------------------------

    async def _discover(self, query: str) -> str:
        """Cosine search the store and return a formatted tool-discovery block."""
        if self._store.total_tools == 0:
            return (
                "No MCP tools are indexed yet. "
                "Check that at least one MCP server is configured under "
                "tools > mcp > servers in agent.yaml and that it is reachable."
            )

        matches = await self._store.search(query, k=self._top_k)

        if not matches:
            return f"No MCP tools matched the query: {query!r}"

        lines: list[str] = [
            f"**Discovered MCP Tools (Top {len(matches)} matches for query: {query!r}):**\n"
        ]

        for i, tool in enumerate(matches, start=1):
            lines.append(f"{i}. Server: `{tool.server_name}` | Tool: `{tool.tool_name}`")

            if tool.title and tool.title != tool.tool_name:
                lines.append(f"   Title: {tool.title}")

            if tool.description:
                lines.append(f"   Description: {tool.description}")

            # Pretty-print the input schema
            try:
                schema = json.loads(tool.schema_json)
            except Exception:
                schema = {}

            props    = schema.get("properties", {})
            required = tool.required or []

            if props:
                lines.append("   Schema:")
                schema_out = json.dumps(props, indent=4, ensure_ascii=False)
                # indent each schema line for visual nesting
                for schema_line in schema_out.splitlines():
                    lines.append(f"   {schema_line}")

            if required:
                lines.append(f"   Required: {json.dumps(required)}")

            lines.append("")  # blank line between tools

        lines.append(
            "To call a tool use:\n"
            "```codepilot\n"
            "mcp(server=\"<server_name>\", func_name=\"<tool_name>\", arguments={...})\n"
            "```"
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  INVOKE
    # ------------------------------------------------------------------

    async def _invoke(
        self,
        server: str,
        func_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Dispatch a tools/call to the named server and return the result."""
        from ..core.mcp_registry import MCPInvokeError

        try:
            result = await self._registry.call_tool(
                server_name=server,
                tool_name=func_name,
                arguments=arguments,
            )
            return result

        except MCPInvokeError as exc:
            return f"[MCP tool error] {exc}"

        except KeyError as exc:
            return f"[MCP error] {exc}"

        except Exception as exc:
            return f"[MCP unexpected error calling {server}.{func_name}] {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close all server connections. Called on runtime teardown."""
        await self._registry.close_all()

    # ------------------------------------------------------------------
    #  System-prompt injection helper
    # ------------------------------------------------------------------

    def build_server_block(self) -> str:
        """
        Returns a compact Markdown block listing all registered MCP servers.
        Injected into the static system prompt so the LLM knows what MCP
        context is available without seeing individual tool schemas.

        Example output::

            ## Connected MCP Servers
            - `tavily`    https://mcp.tavily.com/mcp/...  (5 tools)
            - `office-db` https://internal.hr.com/mcp     (12 tools)

            Use mcp(query="…") to discover tools. Once you know the right tool,
            call it with mcp(server="…", func_name="…", arguments={…}).
        """
        servers = self._registry.registered_servers
        if not servers:
            return ""

        lines: list[str] = ["## Connected MCP Servers"]
        for name, url in servers:
            n_tools = len(self._registry.tool_names_for(name))
            lines.append(f"- `{name}`  {url}  ({n_tools} tools)")

        lines.append("")
        return "\n".join(lines)
