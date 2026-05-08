"""
Isolated test for SemanticTools — no agent loop, no LLM.

What this tests:
  1. validate_config() — config pre-flight check
  2. _bootstrap()     — binary resolution + grepai init + grepai watch --background
  3. semantic_search()— query once indexing is underway

Run from the repo root:
  VOYAGE_API_KEY=<your-key> python3 test_semantic_search.py

The initial indexing takes a few minutes (one-time cost). After that, the
grepai background daemon keeps the index up to date incrementally.
"""

import os
import time
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from codepilot.tools.semantic import SemanticTools

# ── Config ─────────────────────────────────────────────────────────────────
VOYAGE_API_KEY = "pa-xmv1doIrQEku8nfFTAAAKQ30hpQUMEC60wMC9-qTm7X"
WORKSPACE_DIR  = os.path.expanduser("~/test-grepai")   # 1 file → 1 chunk → fits free tier
SEARCH_QUERY   = "hello program"
WAIT_BEFORE_SEARCH = 30   # seconds — tiny repo, should be indexed in <10s


def make_mock_runtime(workspace_dir: str) -> MagicMock:
    """Build a minimal Runtime mock — only the fields SemanticTools touches."""
    mock = MagicMock()
    mock.config.runtime.work_dir = workspace_dir

    def _append_exec(msg):
        print(f"  [Runtime] {msg}")

    def _hook_emit(event_type, **kwargs):
        print(f"  [Hook]    {kwargs.get('label', str(event_type))}")

    mock._append_execution = _append_exec
    mock.hooks.emit        = _hook_emit

    def _tool_config(name):
        if name == "semantic_search":
            return {
                "provider":      "openai",
                "model":         "voyage-code-3",
                "base_url":      "https://api.voyageai.com/v1",
                "api_key_env":   "VOYAGE_API_KEY",
                "parallelism":   1,
                "timeout":       60,
                "max_results":   3,
                "max_output_chars": 4000,
            }
        return {}

    mock._tool_config = _tool_config
    return mock


def main():
    print("=" * 60)
    print("  SemanticTools Isolated Test")
    print("=" * 60)

    # Inject API key into environment
    os.environ["VOYAGE_API_KEY"] = VOYAGE_API_KEY

    # Build mock runtime
    mock_runtime = make_mock_runtime(WORKSPACE_DIR)

    # Instantiate tool
    print("\n[1/4] Instantiating SemanticTools…")
    tool = SemanticTools(mock_runtime)

    # Validate config (this triggers _bootstrap: download → init → watch --background)
    print("\n[2/4] Running validate_config() — this starts the background indexer…")
    try:
        tool.validate_config()
        print("  ✅  Config valid. Background indexer started.")
    except Exception as exc:
        print(f"  ❌  Config error: {exc}")
        sys.exit(1)

    print(f"\n[3/4] Waiting {WAIT_BEFORE_SEARCH}s for initial indexing to begin…")
    print("      (First-time indexing embeds every file via Voyage AI — takes a few minutes)")
    print("      (The agent is NOT blocked — it just returns a status message if called early)")
    for i in range(WAIT_BEFORE_SEARCH, 0, -1):
        print(f"      {i}s remaining…", end="\r", flush=True)
        time.sleep(1)
    print()

    # Run semantic search
    print(f"\n[4/4] Running semantic_search() — query: {SEARCH_QUERY!r}")
    result = tool.semantic_search(query=SEARCH_QUERY, mode="search", top_k=3)

    print("\n" + "=" * 60)
    print("  Result")
    print("=" * 60)
    print(result)

    # Cleanup
    print("\n[Cleanup] Stopping background daemon…")
    tool.cleanup()
    print("  Done.")


if __name__ == "__main__":
    main()
