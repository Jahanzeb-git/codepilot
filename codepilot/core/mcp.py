"""
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file is responsible for handling MCP client upon JSON-RPC 2.0 protocol (Required MCP version: 2025-06-18)
version: 1.1.0
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass(slots=True)
class SSEEvent:
    event: str | None = None
    data: str | None = ""
    id: str | None = None
    retry: int | None = None


@dataclass
class ServerCapabilities:
    logging: bool
    tools_list_changed: bool
    prompts_list_changed: bool
    resource_list_changed: bool
    resource_subscription: bool
    experimental: dict
    extensions: dict


@dataclass
class ServerInfo:
    name: str
    version: str
    server_url: str


@dataclass
class MCPTool:
    server: ServerInfo
    name: str
    title: str | None
    description: str | None
    inputSchema: dict
    outputSchema: dict | None
    annotation: dict | None
    meta: dict | None


class ProtocolMismatchError(Exception):
    "Raised when there's a protocol version mismatch"
    pass


class SSEParser:

    @staticmethod
    def parse(text: str) -> list[SSEEvent]:
        events = []
        current = SSEEvent()

        for line in text.splitlines():

            # blank line = event finished
            if line == "":
                if current.event or current.data:
                    events.append(current)
                    current = SSEEvent()
                continue

            field, _, value = line.partition(":")
            value = value.lstrip()

            if field == "event":
                current.event = value

            elif field == "data":
                if current.data:
                    current.data += "\n"
                current.data += value

            elif field == "id":
                current.id = value

            elif field == "retry":
                current.retry = int(value)

        if current.event or current.data:
            events.append(current)

        return events


class MCPClient:
    """
    One MCPClient instance = one persistent connection to one MCP server.

    This is the fix for the thing I flagged: call_mcp() was opening and
    tearing down a brand new httpx.AsyncClient (and therefore a new TCP
    connection, new TLS handshake, the works) on every single JSON-RPC call.
    For a tool discovery flow that's initialize -> notify -> tools/list
    -> N cursor pages, that's N+2 handshakes for what should be one
    connection. Now the AsyncClient is created once and reused for the
    life of this object.
    """

    PROTOCOL_VERSION = "2025-06-18"  # MCP client version (deterministic)
    CLIENT_VERSION = "1.0.0"
    CLIENT_NAME = "codepilot_mcp_client:v1"
    JSONRPC = "2.0"

    def __init__(
        self,
        mcp_url: str,
        api_key: str | None = None,
        api_key_param: str | None = None,
    ):
        self.mcp_url = mcp_url
        self.api_key = api_key
        self.api_key_param = api_key_param  # -- fixed: this was a `:` in the draft, not `=`, so it never actually assigned --

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        self.timeout = httpx.Timeout(
            connect=10,
            read=60,
            write=10,
            pool=10,
        )

        self.params = {}

        # Intelligently route the API key to either headers or query params
        if self.api_key and self.api_key_param:
            param_lower = self.api_key_param.lower()
            if param_lower == "authorization" or param_lower.startswith("x-"):
                val = self.api_key
                # Automatically format Bearer tokens if needed
                if param_lower == "authorization" and not val.lower().startswith(("bearer ", "basic ")):
                    val = f"Bearer {val}"
                self.headers[self.api_key_param] = val
            else:
                self.params[self.api_key_param] = self.api_key

        # -- don't open the connection here, __init__ can't be async and we --
        # -- don't want a half-built client lying around if construction fails --
        # -- partway through, so the actual AsyncClient gets created lazily --
        # -- either on __aenter__ or on the first call() if used without --
        # -- the context manager --
        self.client: httpx.AsyncClient | None = None

        # -- MCP servers can hand back a session id on the initialize response --
        self.session_id: str | None = None

        # -- auto-incrementing request id --
        self._request_id = 0

        # -- True SSE (Legacy) state --
        self._post_url: str | None = None
        self._sse_task: asyncio.Task | None = None
        self._pending_requests: dict[int, asyncio.Future] = {}

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def __aenter__(self) -> "MCPClient":
        self._ensure_client()
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    def _ensure_client(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(
                params=self.params,
                headers=self.headers,
                timeout=self.timeout,
            )

    async def connect(self) -> None:
        """
        Attempt to establish the True SSE transport connection.
        If the server accepts a GET request and returns an SSE stream with an 'endpoint' event,
        we are in Legacy SSE mode. Otherwise, we seamlessly fallback to Streamable HTTP.
        """
        self._ensure_client()
        if not self._post_url:
            await self._connect_sse()

    async def _connect_sse(self) -> bool:
        try:
            request = self.client.build_request("GET", self.mcp_url, headers={"Accept": "text/event-stream"})
            response = await self.client.send(request, stream=True)
            if response.status_code in (404, 405):
                await response.aclose()
                return False
                
            response.raise_for_status()
            
            loop = asyncio.get_running_loop()
            self._endpoint_future = loop.create_future()
            self._sse_task = asyncio.create_task(self._read_sse_loop(response))
            
            # Wait for endpoint event
            self._post_url = await asyncio.wait_for(self._endpoint_future, timeout=10.0)
            return True
        except Exception:
            return False

    async def _read_sse_loop(self, response: httpx.Response) -> None:
        try:
            current = SSEEvent()
            async for line in response.aiter_lines():
                if line == "":
                    if current.event or current.data:
                        self._handle_sse_event(current)
                        current = SSEEvent()
                    continue
                
                field, _, value = line.partition(":")
                value = value.lstrip()
                
                if field == "event":
                    current.event = value
                elif field == "data":
                    if current.data:
                        current.data += "\n"
                    current.data += value
                elif field == "id":
                    current.id = value
                    
            if current.event or current.data:
                self._handle_sse_event(current)
        except Exception:
            pass
        finally:
            await response.aclose()

    def _handle_sse_event(self, event: SSEEvent) -> None:
        if event.event == "endpoint":
            url = event.data
            if url.startswith("/"):
                base = str(self.client.base_url).rstrip("/")
                url = base + url
            elif not url.startswith("http"):
                base = self.mcp_url.rstrip("/")
                url = base + "/" + url
                
            if not self._endpoint_future.done():
                self._endpoint_future.set_result(url)
                
        elif event.event == "message" or not event.event:
            try:
                rpc = json.loads(event.data)
                if "id" in rpc and rpc["id"] in self._pending_requests:
                    if not self._pending_requests[rpc["id"]].done():
                        self._pending_requests[rpc["id"]].set_result(rpc)
            except Exception:
                pass

    async def close(self) -> None:
        """Tear down the shared connection. Safe to call more than once."""
        if self._sse_task:
            self._sse_task.cancel()
            self._sse_task = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Send one JSON-RPC request (or notification) to the MCP server over
        the shared connection. This is the method version of the old
        module-level call_mcp() function.
        """
        self._ensure_client()

        # -- once initialize() has run and we've got a session id back, every --
        # -- following call needs to carry it, notifications included --
        request_headers = (
            {"Mcp-Session-Id": self.session_id} if self.session_id else None
        )

        # -- True SSE (Legacy) Flow --
        if self._post_url:
            req_id = payload.get("id")
            fut = None
            if req_id is not None:
                fut = asyncio.get_running_loop().create_future()
                self._pending_requests[req_id] = fut
                
            try:
                response = await self.client.post(
                    self._post_url,
                    json=payload,
                    headers=request_headers,
                )
                response.raise_for_status()
                
                if req_id is None:
                    return {"status": response.status_code, "session_id": self.session_id, "body": {}}
                    
                # Wait for response on SSE stream
                rpc = await asyncio.wait_for(fut, timeout=self.timeout.read)
                
                if "error" in rpc:
                    raise RuntimeError(rpc["error"])
                    
                return {
                    "status": 200,
                    "session_id": self.session_id,
                    "body": rpc,
                }
            except asyncio.TimeoutError:
                if req_id is not None and req_id in self._pending_requests:
                    del self._pending_requests[req_id]
                raise TimeoutError("SSE response timed out")
            finally:
                if req_id is not None and req_id in self._pending_requests:
                    del self._pending_requests[req_id]

        # -- Streamable HTTP (Fallback) Flow --
        try:
            response = await self.client.post(
                self.mcp_url,
                json=payload,
                headers=request_headers,
            )

            content_type = response.headers.get("Content-Type", "")
            response.raise_for_status()

            session_header = response.headers.get("Mcp-Session-Id")
            if session_header:
                self.session_id = session_header

            is_notification = "id" not in payload
            body_empty = not response.text.strip()

            if response.status_code in (202, 204) or (is_notification and body_empty):
                return {
                    "status": response.status_code,
                    "session_id": self.session_id,
                    "body": {},
                }

            if content_type.startswith("application/json"):
                rpc = response.json()
            elif content_type.startswith("text/event-stream"):
                events = SSEParser.parse(response.text)
                if not events:
                    raise RuntimeError(f"Server at '{self.mcp_url}' returned an empty SSE stream.")
                rpc = json.loads(events[0].data)
            else:
                raise RuntimeError(f"Unexpected Content-Type '{content_type}' from '{self.mcp_url}'.")

            if "error" in rpc:
                raise RuntimeError(rpc["error"])

            return {
                "status": response.status_code,
                "session_id": self.session_id,
                "body": rpc,
            }

        except httpx.ConnectError as e:
            raise ConnectionError(f"Failed to connect to '{self.mcp_url}'.") from e

        except httpx.TimeoutException as e:
            raise TimeoutError("The request timed out.") from e

        except httpx.HTTPStatusError:
            raise

        except httpx.HTTPError:
            raise

    async def initialize(self) -> tuple[ServerInfo, ServerCapabilities]:
        """
        Do the MCP handshake: send `initialize`, check the protocol version
        matches, then fire off `notifications/initialized`. Returns the
        server's identity and what it says it can do.
        """
        supported_versions = ["2025-06-18", "2024-11-05"]
        rpc = None
        init_payload = None
        
        for attempt_version in supported_versions:
            init_payload = {
                "jsonrpc": self.JSONRPC,
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": attempt_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self.CLIENT_NAME,
                        "version": self.CLIENT_VERSION,
                    },
                },
            }
            
            try:
                response = await self.call(init_payload)
                rpc = response["body"]
                break  # Success! Exit retry loop
            except RuntimeError as e:
                if attempt_version == supported_versions[-1]:
                    raise  # We are out of fallback versions, crash
                print(f"MCP Client: Server rejected version {attempt_version}, attempting downgrade...")
                continue

        if rpc.get("id") != init_payload["id"]:
            raise RuntimeError(
                f"Response id {rpc.get('id')!r} doesn't match request id {init_payload['id']!r}."
            )

        result = rpc["result"]
        rpc_protocol_version = result.get("protocolVersion")
        
        if rpc_protocol_version not in supported_versions:
            raise ProtocolMismatchError(
                f"Unsupported protocol version from server: {rpc_protocol_version}. "
                f"Client supports: {supported_versions}"
            )
            
        if rpc_protocol_version != self.PROTOCOL_VERSION:
            print(f"MCP Client: Server negotiated protocol downgrade to {rpc_protocol_version}")

        server_info = ServerInfo(
            name=result["serverInfo"]["name"],
            version=result["serverInfo"]["version"],
            server_url=self.mcp_url,
        )

        # -- the MCP spec doesn't require a server to advertise every --
        # -- capability, so pulling these out with hard [] indexing like the --
        # -- draft did will KeyError on any server that omits one. .get() --
        # -- your way through instead and default to "not supported" --
        caps = result.get("capabilities", {})
        resources_cap = caps.get("resources", {})

        capabilities = ServerCapabilities(
            # -- "logging" was literally set to the string "logging" in the --
            # -- draft, which is always truthy no matter what the server said. --
            # -- what we actually want is: did the server advertise it at all --
            logging="logging" in caps,
            tools_list_changed=caps.get("tools", {}).get("listChanged", False),
            prompts_list_changed=caps.get("prompts", {}).get("listChanged", False),
            resource_list_changed=resources_cap.get("listChanged", False),
            resource_subscription=resources_cap.get("subscribe", False),
            experimental=caps.get("experimental", {}),
            extensions=caps.get("extensions", {}),
        )

        # -- handshake isn't complete until this notification goes out; it has --
        # -- no "id" and the server won't (and shouldn't) reply to it --
        await self.call(
            {"jsonrpc": self.JSONRPC, "method": "notifications/initialized"}
        )

        return server_info, capabilities

    async def list_tools(self, server_info: ServerInfo) -> list[MCPTool]:
        """
        Pull every tool this server exposes, following `nextCursor` pages
        until the server stops handing them out. This is what feeds the
        embedding step: each MCPTool here is one chunk (name, description,
        schema, server identity) that gets embedded and dropped into LanceDB.
        """

        async def _page(cursor: str | None = None) -> list[MCPTool]:
            payload = {
                "jsonrpc": self.JSONRPC,
                "id": self._next_id(),
                "method": "tools/list",
                "params": {"cursor": cursor} if cursor else {},
            }

            response = await self.call(payload)
            result = response["body"]["result"]

            tools = [
                MCPTool(
                    server=server_info,
                    name=tool["name"],
                    title=tool.get("title"),
                    description=tool.get("description"),
                    inputSchema=tool["inputSchema"],
                    outputSchema=tool.get("outputSchema"),
                    annotation=tool.get("annotations"),
                    meta=tool.get("_meta"),
                )
                for tool in result["tools"]
            ]

            # -- draft pulled the cursor from tool_response["body"]["nextCursor"] --
            # -- but checked its existence one level deeper at --
            # -- tool_response["body"]["result"]["nextCursor"] -- those two paths --
            # -- don't agree, so pagination silently broke after page one. --
            # -- also: the draft called _parse_tool() with no args and no await, --
            # -- so it never ran at all, just handed back an unawaited coroutine. --
            next_cursor = result.get("nextCursor")
            if next_cursor is not None:
                tools += await _page(next_cursor)

            return tools

        return await _page()


async def connect_mcp_server(
    mcp_url: str,
    api_key: str | None = None,
    api_key_param: str | None = None,
) -> tuple[ServerInfo, ServerCapabilities, list[MCPTool]]:
    """
    One-shot convenience wrapper for the embedding pipeline: open a
    connection, do the full handshake, pull every tool, then close up.

    If a caller needs the connection to stay open afterward (e.g. to
    actually invoke a tool once the embedding layer has picked it), use
    MCPClient directly inside `async with MCPClient(...) as client:`
    instead of this function.
    """
    client = MCPClient(mcp_url, api_key=api_key, api_key_param=api_key_param)
    try:
        server_info, capabilities = await client.initialize()
        tools = await client.list_tools(server_info)
        return server_info, capabilities, tools
    finally:
        await client.close()