"""
File: mcp_store.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-08-04

Description:
Embedding store for MCP tools — no LanceDB, no heavy vector-DB deps.
Persists tool metadata to ~/.codepilot/mcp_tools.json and
embeddings to ~/.codepilot/mcp_embeddings.npz.

Design:
- Each MCPTool is one chunk. The chunk text is a dense prose
  representation of the tool's full surface (name, server, description,
  arguments with their descriptions) so the embedding captures both
  semantic intent and schema signal.
- Voyage AI embeddings are L2-normalised by the API, so cosine
  similarity reduces to a plain numpy dot product — one matrix multiply.
- Per-server upsert: when a server reconnects, its rows are deleted then
  re-inserted atomically. Other servers are untouched.
- .npz format stores both the float32 matrix and a matching string-id
  array so row ↔ tool mapping survives across sessions without any DB.

Copyright (c) 2026 Jahanzeb Ahmed. Licensed under the MIT License.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import httpx
import numpy as np

if TYPE_CHECKING:
    from .mcp import MCPTool


# ---------------------------------------------------------------------------
#  Serialisable snapshot of one MCPTool
# ---------------------------------------------------------------------------

@dataclass
class StoredTool:
    """
    Flat, JSON-serialisable view of one MCPTool.

    All fields are plain Python scalars or lists so the object can be
    round-tripped through json.dumps / json.loads without a custom encoder.
    """
    server_name: str
    server_url:  str
    tool_name:   str
    title:       Optional[str]
    description: Optional[str]
    schema_json: str   # json.dumps(inputSchema)
    required:    list  # inputSchema.get("required", [])
    chunk_text:  str   # the text that was embedded


# ---------------------------------------------------------------------------
#  Chunk-text builder
# ---------------------------------------------------------------------------

def _build_chunk_text(tool: "MCPTool") -> str:
    """
    Flatten one MCPTool into a single dense paragraph for embedding.

    Format::

        Tool: <name>
        Server: <server_name> (<server_url>)
        Description: <description>
        Arguments: arg1 (type, required) — description.
                   arg2 (type, enum: a|b, default: x) — description.

    The richer the text, the more schema-level signal the embedding
    captures — improving retrieval precision for argument-level queries
    like "a tool that takes an employee ID".
    """
    lines: list[str] = []

    lines.append(f"Tool: {tool.name}")
    lines.append(f"Server: {tool.server.name} ({tool.server.server_url})")

    if tool.description:
        lines.append(f"Description: {tool.description}")

    props = tool.inputSchema.get("properties", {})
    required_set = set(tool.inputSchema.get("required", []))

    if props:
        arg_lines: list[str] = []
        for i, (arg_name, arg_schema) in enumerate(props.items()):
            parts: list[str] = []

            arg_type = arg_schema.get("type", "any")
            parts.append(arg_type)

            enum_vals = arg_schema.get("enum")
            if enum_vals:
                parts.append(f"enum: {'|'.join(str(v) for v in enum_vals)}")

            if "default" in arg_schema:
                parts.append(f"default: {arg_schema['default']}")

            if arg_name in required_set:
                parts.append("required")

            qualifier = f"({', '.join(parts)})" if parts else ""
            arg_desc  = arg_schema.get("description", "")
            sep       = f" — {arg_desc}" if arg_desc else ""

            prefix = "Arguments: " if i == 0 else "           "
            arg_lines.append(f"{prefix}{arg_name} {qualifier}{sep}")

        lines.extend(arg_lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Embedding call — raw HTTP, no voyageai SDK
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 1024  # voyage-code-3 output dimension


async def _embed_texts(
    texts: list[str],
    api_key: str,
    model: str = "voyage-code-3",
    base_url: str = "https://api.voyageai.com/v1",
    *,
    input_type: str = "document",
) -> np.ndarray:
    """
    Embed a batch of texts via the Voyage AI OpenAI-compatible endpoint.

    Returns a float32 ndarray of shape (len(texts), 1024).
    Voyage embeddings are already L2-normalised — cosine similarity equals
    the dot product, so no post-processing is needed.
    """
    url = base_url.rstrip("/") + "/embeddings"
    payload: dict = {
        "model": model,
        "input": texts,
        "input_type": input_type,
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10)
    ) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    # OpenAI-compat format: {"data": [{"index": i, "embedding": [...]}]}
    ordered = sorted(data["data"], key=lambda x: x["index"])
    vectors  = [item["embedding"] for item in ordered]
    return np.array(vectors, dtype=np.float32)


# ---------------------------------------------------------------------------
#  MCPToolStore
# ---------------------------------------------------------------------------

class MCPToolStore:
    """
    Persistent embedding store for MCP tools.

    Storage layout (under ``~/.codepilot/``)::

        mcp_tools.json       — list[StoredTool] as dicts, insertion order
        mcp_embeddings.npz   — {"vectors": float32 (N, 1024),
                                "ids":     object  (N,)}

    ``ids[i]`` is ``"{server_url}::{tool_name}"`` and maps row *i* back
    to the corresponding entry in ``_tools``.

    Both files are always in sync: every upsert writes to ``.tmp`` then
    uses ``os.replace`` so a mid-write crash never corrupts the store.
    """

    _CODEPILOT_DIR = Path.home() / ".codepilot"
    _TOOLS_FILE    = _CODEPILOT_DIR / "mcp_tools.json"
    _EMBED_FILE    = _CODEPILOT_DIR / "mcp_embeddings.npz"

    def __init__(
        self,
        *,
        api_key:  str,
        model:    str = "voyage-code-3",
        base_url: str = "https://api.voyageai.com/v1",
    ):
        self._api_key  = api_key
        self._model    = model
        self._base_url = base_url

        # In-memory mirrors
        self._tools:   list[StoredTool]  = []
        self._vectors: np.ndarray | None = None  # (N, 1024) float32
        self._ids:     list[str]         = []     # parallel to self._tools

        self._CODEPILOT_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    #  Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load existing store from disk. No-ops silently if files are absent."""
        if self._TOOLS_FILE.exists():
            try:
                raw = json.loads(self._TOOLS_FILE.read_text(encoding="utf-8"))
                self._tools = [StoredTool(**r) for r in raw]
            except Exception:
                self._tools = []
        else:
            self._tools = []

        if self._EMBED_FILE.exists():
            try:
                npz           = np.load(self._EMBED_FILE, allow_pickle=True)
                self._vectors = npz["vectors"].astype(np.float32)
                self._ids     = list(npz["ids"])
            except Exception:
                self._vectors = None
                self._ids     = []
        else:
            self._vectors = None
            self._ids     = []

        # Guard against corrupt state: lengths must always match
        if len(self._ids) != len(self._tools):
            self._tools   = []
            self._vectors = None
            self._ids     = []

    def _save(self) -> None:
        """Atomically persist both files."""
        # --- mcp_tools.json ---
        tmp_j = self._TOOLS_FILE.with_suffix(".tmp")
        tmp_j.write_text(
            json.dumps([asdict(t) for t in self._tools], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_j, self._TOOLS_FILE)

        # --- mcp_embeddings.npz ---
        if self._vectors is not None and len(self._ids) > 0:
            tmp_n_str = str(self._EMBED_FILE) + ".tmp"
            np.savez_compressed(
                tmp_n_str,
                vectors=self._vectors,
                ids=np.array(self._ids, dtype=object),
            )
            # np.savez_compressed appends .npz if not already present
            tmp_n_actual = tmp_n_str + ".npz"
            os.replace(tmp_n_actual, self._EMBED_FILE)

    @staticmethod
    def _make_id(server_url: str, tool_name: str) -> str:
        return f"{server_url}::{tool_name}"

    # ------------------------------------------------------------------
    #  Upsert — idempotent at the server granularity
    # ------------------------------------------------------------------

    async def index_tools(self, tools: list["MCPTool"]) -> None:
        """
        Embed every tool in *tools* and upsert them into the store.

        All tools are expected to belong to the same server. Existing rows
        for that server are deleted before the new batch is inserted, so
        this is idempotent. Calling this on reconnect or tool-list refresh
        is always safe.

        Does nothing if *tools* is empty.
        """
        if not tools:
            return

        server_url = tools[0].server.server_url

        # 1. Remove all existing rows for this server
        keep_mask = [t.server_url != server_url for t in self._tools]
        self._tools = [t for t, keep in zip(self._tools, keep_mask) if keep]
        self._ids   = [i for i, keep in zip(self._ids,   keep_mask) if keep]

        if self._vectors is not None:
            if self._vectors.shape[0] == len(keep_mask):
                keep_arr      = np.array(keep_mask, dtype=bool)
                self._vectors = self._vectors[keep_arr]
                if self._vectors.shape[0] == 0:
                    self._vectors = None
            else:
                # Shape mismatch → reset to clean state
                self._vectors = None
                self._ids     = []
                self._tools   = []

        # 2. Build chunk texts
        chunk_texts = [_build_chunk_text(t) for t in tools]

        # 3. Embed in one batch
        new_vectors = await _embed_texts(
            chunk_texts,
            api_key=self._api_key,
            model=self._model,
            base_url=self._base_url,
            input_type="document",
        )  # shape: (len(tools), 1024)

        # 4. Build StoredTool records
        new_stored = [
            StoredTool(
                server_name=t.server.name,
                server_url=t.server.server_url,
                tool_name=t.name,
                title=t.title,
                description=t.description,
                schema_json=json.dumps(t.inputSchema),
                required=t.inputSchema.get("required", []),
                chunk_text=chunk_texts[i],
            )
            for i, t in enumerate(tools)
        ]
        new_ids = [self._make_id(server_url, t.name) for t in tools]

        # 5. Append to in-memory mirrors
        self._tools.extend(new_stored)
        self._ids.extend(new_ids)
        self._vectors = (
            new_vectors
            if self._vectors is None
            else np.concatenate([self._vectors, new_vectors], axis=0)
        )

        # 6. Flush to disk
        self._save()

    # ------------------------------------------------------------------
    #  Cosine search
    # ------------------------------------------------------------------

    async def search(self, query: str, k: int = 3) -> list[StoredTool]:
        """
        Embed *query* and return the top-k most similar stored tools.

        Voyage embeddings are L2-normalised so dot product == cosine
        similarity. No explicit normalisation step is needed.

        Returns an empty list if the store has no indexed tools.
        """
        if self._vectors is None or not self._tools:
            return []

        k = min(k, len(self._tools))

        query_vec = await _embed_texts(
            [query],
            api_key=self._api_key,
            model=self._model,
            base_url=self._base_url,
            input_type="query",   # asymmetric: document vs query input type
        )  # (1, 1024)
        q = query_vec[0]  # (1024,)

        # One matmul: (N, 1024) @ (1024,) → (N,) cosine scores
        scores      = self._vectors @ q
        top_indices = np.argsort(scores)[::-1][:k]

        return [self._tools[int(i)] for i in top_indices]

    # ------------------------------------------------------------------
    #  Convenience
    # ------------------------------------------------------------------

    def get_tool(self, server_url: str, tool_name: str) -> StoredTool | None:
        """Exact lookup by (server_url, tool_name). Returns None if not found."""
        target = self._make_id(server_url, tool_name)
        for tool_id, stored in zip(self._ids, self._tools):
            if tool_id == target:
                return stored
        return None

    @property
    def total_tools(self) -> int:
        """Total number of indexed tools across all servers."""
        return len(self._tools)

    @property
    def server_names(self) -> list[str]:
        """Deduplicated list of registered server *names*, insertion-ordered."""
        seen: dict[str, None] = {}
        for t in self._tools:
            seen[t.server_name] = None
        return list(seen)

    def servers_info(self) -> list[tuple[str, str]]:
        """
        List of (server_name, server_url) pairs, one entry per unique server,
        in insertion order. Used for system prompt injection.
        """
        seen: dict[str, str] = {}
        for t in self._tools:
            if t.server_url not in seen:
                seen[t.server_url] = t.server_name
        return [(name, url) for url, name in seen.items()]
