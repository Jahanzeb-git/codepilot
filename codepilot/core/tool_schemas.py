"""
File: tool_schemas.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-10-27

Curated JSON-schema metadata for the built-in tools.

The native function-calling protocol makes the provider-side ``tools`` array
the single source of truth the model sees. Function signatures supply names,
types, and defaults; this module supplies the parts a signature cannot
express:

  - a complete description of what the tool does, when to use it, what it
    returns, and how it fails,
  - per-parameter descriptions, enums, ranges, and defaults,
  - required-parameter overrides for parameters whose Python default exists
    only to support positional/forgiving calls.

Descriptions are deliberately behavioural. They state the contract the model
must rely on; they do not document internal matching passes or implementation
details that the model neither needs nor can act on.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

from typing import Any, Dict


TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # ------------------------------------------------------------------
    # Filesystem
    # ------------------------------------------------------------------
    "view_file": {
        "description": (
            "Read a text file and return its contents with 1-indexed line "
            "numbers in the form `  42 | code`. Use start_line/end_line to "
            "read only the region you need; omit end_line to read to the end "
            "of the file. At most max_lines lines are returned per call "
            "(default 400): the footer states the last line returned and the "
            "start_line to continue from. Read a file before editing it, and "
            "re-read it after an external change. This tool never modifies "
            "the file. It fails if the path is missing, is not a file, or the "
            "range is invalid."
        ),
        "parameters": {
            "path": {
                "description": (
                    "File path, relative to the workspace root or absolute. "
                    "Check the codebase snapshot if unsure of the location."
                ),
            },
            "start_line": {
                "description": "First line to return, 1-indexed. Default 1.",
            },
            "end_line": {
                "description": (
                    "Last line to return, inclusive. Omit to read through the "
                    "end of the file."
                ),
            },
            "max_lines": {
                "description": (
                    "Maximum lines to return in this call. Default 400. "
                    "Increase only when you genuinely need a larger contiguous "
                    "region; otherwise continue with start_line."
                ),
            },
        },
    },
    "write_file": {
        "description": (
            "Create a new file or completely overwrite an existing file. "
            "Pass the entire file content in `content`; parent directories "
            "are created automatically. This replaces the whole file, so use "
            "edit_file for a partial change. On success the result is a "
            "byte/line receipt, not an echo of the content. On failure the "
            "result explains the error; if it is an OS error the content is "
            "cached and the result includes a cache_id. Fix the path or "
            "permission, then retry with that cache_id and omit content."
        ),
        "parameters": {
            "path": {
                "description": "Destination file path, relative or absolute.",
            },
            "content": {
                "description": (
                    "Complete new file content. Required on a normal write; "
                    "omit it when retrying a cached write with cache_id."
                ),
            },
            "cache_id": {
                "description": (
                    "Numeric ID returned by a previous failed write. Reuses "
                    "the cached content; do not regenerate or resend it."
                ),
            },
        },
    },
    "edit_file": {
        "description": (
            "Replace one unique region of an existing file. `old_str` "
            "identifies the region in the current file; `new_str` is the "
            "replacement. Include enough surrounding lines for a unique "
            "match, and copy old_str from the file as it currently exists. "
            "The replacement is applied exactly as written in new_str. For "
            "multiple independent regions, call edit_file once per region. "
            "On success the result includes a bounded unified diff and line "
            "counts so the change can be verified without re-reading the "
            "file. On failure the result explains why and reports that "
            "nothing was applied. If an OS error occurs, the arguments are "
            "cached and the result includes a cache_id for retry."
        ),
        "parameters": {
            "path": {
                "description": "File to edit, relative or absolute.",
            },
            "old_str": {
                "description": (
                    "Text of the region to replace, copied from the current "
                    "file with its indentation. Must match exactly one "
                    "location; add surrounding lines if ambiguous."
                ),
            },
            "new_str": {
                "description": (
                    "Replacement text. Use an empty string to delete the "
                    "matched region."
                ),
            },
            "cache_id": {
                "description": (
                    "Numeric ID returned by a previous failed edit. Reuses "
                    "the cached old_str/new_str; omit both in that case."
                ),
            },
        },
    },
    "find_and_replace_many": {
        "description": (
            "Replace every occurrence of an exact literal string in one "
            "file. Use this for mechanical whole-file replacements such as "
            "renaming a variable, constant, or import path. For a single "
            "targeted region use edit_file. `find` is literal text, not a "
            "regex. The result reports how many occurrences were replaced "
            "and the new line count; if `find` is empty or absent, nothing "
            "changes."
        ),
        "parameters": {
            "path": {
                "description": "File to modify, relative or absolute.",
            },
            "find": {
                "description": (
                    "Exact literal text to find. Every occurrence in the file "
                    "is replaced."
                ),
            },
            "replace": {
                "description": (
                    "Replacement text. Use an empty string to delete every "
                    "occurrence."
                ),
            },
        },
    },
    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------
    "execute": {
        "description": (
            "Run a shell command in a persistent terminal session and return "
            "its output. The session keeps shell state (working directory, "
            "environment, aliases) between calls. Shell options set with "
            "`set` (for example `set -euo pipefail`), `source`, `eval`, "
            "`trap`, and `exec` apply only to that one call and never leak "
            "into later calls or terminate the session. If the command finishes "
            "within `timeout`, the result is marked completed and includes "
            "the exit code; otherwise it is marked running and you must call "
            "read_output to collect more output. Use new_terminal=True with a "
            "unique session_id for a long-running server or an isolated "
            "shell. Inspect the result before taking a dependent action. "
            "Prefer view_file, edit_file, and find over shell equivalents."
        ),
        "required": ["command"],
        "parameters": {
            "session_id": {
                "description": (
                    "Terminal session name. Reuse a name to continue in the "
                    "same shell. Default `main`."
                ),
            },
            "command": {
                "description": (
                    "Shell command to run. Multi-line commands are allowed."
                ),
            },
            "timeout": {
                "description": (
                    "Seconds to wait for completion before returning a "
                    "running status. Default 10."
                ),
            },
            "new_terminal": {
                "description": (
                    "Create a new terminal session instead of reusing "
                    "session_id. Requires a unique session_id."
                ),
            },
            "shell": {
                "description": (
                    "Shell for a new terminal. Ignored for an existing "
                    "session. Default: bash on Linux/macOS, powershell on "
                    "Windows."
                ),
                "enum": ["bash", "powershell", "cmd"],
            },
        },
    },
    "read_output": {
        "description": (
            "Read new output from a command previously started with execute "
            "on a terminal session. When new output is available, returns "
            "only the new, non-overlapping portion. When the command has "
            "finished and there is no new output, returns the complete "
            "output once and clears the stored output. Use this for "
            "long-running commands, prompts, and interactive programs. Do "
            "not call it repeatedly on a completed session unless you expect "
            "new output."
        ),
        "parameters": {
            "session_id": {
                "description": "Terminal session to read from.",
            },
            "timeout": {
                "description": (
                    "Seconds to wait for new output before returning. "
                    "Default 5."
                ),
            },
        },
    },
    "send_input": {
        "description": (
            "Send raw input to an interactive process in a terminal session "
            "and return the output produced. `text` is sent exactly as "
            "given: include a newline to submit a line, or control "
            "characters to press keys (Ctrl+C = `\\u0003`, Ctrl+D = "
            "`\\u0004`, Esc = `\\u001b`, Down = `\\u001b[B`). Use this for "
            "prompts, REPLs, pagers, and TUI navigation. It does not start a "
            "new shell command; use execute for that."
        ),
        "parameters": {
            "session_id": {
                "description": "Terminal session whose foreground process "
                "should receive the input.",
            },
            "text": {
                "description": (
                    "Raw text to send. Add `\\n` to press Enter; control "
                    "characters are passed through unchanged."
                ),
            },
            "timeout": {
                "description": (
                    "Seconds to wait for output after sending. Default 5."
                ),
            },
        },
    },
    "terminate_terminal": {
        "description": (
            "Permanently kill a terminal session and every process running "
            "in it. Use only when Ctrl+C via send_input does not stop the "
            "foreground process. The session_id becomes unusable; create a "
            "new session with execute(new_terminal=True, session_id=...) if "
            "you need another shell. Returns a confirmation."
        ),
        "parameters": {
            "session_id": {
                "description": "Terminal session to terminate.",
            },
        },
    },
    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    "ask_user": {
        "description": (
            "Ask the human operator a question and block until they answer. "
            "Use only when a decision genuinely cannot be inferred from the "
            "request or the codebase: ambiguous requirements, destructive or "
            "irreversible choices, missing credentials, or a conflict "
            "between instructions. Do not use it to confirm routine work. "
            "The answer is returned as the tool result."
        ),
        "parameters": {
            "question": {
                "description": (
                    "A clear, specific question. Include the options or the "
                    "information you need so one reply is enough."
                ),
            },
        },
    },
    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    "find": {
        "description": (
            "Search for a regular expression across a file, a list of "
            "files, or the whole workspace. Returns one "
            "`file:line:matched_line` per matching line, capped by "
            "max_results. Prefer this over execute with grep/rg for ordinary "
            "text search; use shell rg only for features this tool does not "
            "expose (context lines, multiline, counts, pipes). Matching is "
            "regex, not literal. Use scope='file' with target=<path>, "
            "scope='files' with target=[...], or scope='codebase' with an "
            "optional include glob."
        ),
        "parameters": {
            "pattern": {
                "description": (
                    "Regular expression to search for. Escape regex "
                    "metacharacters, e.g. `def login\\(`."
                ),
            },
            "scope": {
                "description": (
                    "Where to search. 'file' = one path in target; 'files' = "
                    "the paths in target; 'codebase' = the whole workspace."
                ),
                "enum": ["file", "files", "codebase"],
            },
            "target": {
                "description": (
                    "File path when scope='file', or a list of paths when "
                    "scope='files'. Ignored for scope='codebase'."
                ),
            },
            "include": {
                "description": (
                    "Glob filter for scope='codebase', e.g. `*.py` or "
                    "`src/**/*.ts`. Omit to search all text files."
                ),
            },
            "max_results": {
                "description": (
                    "Maximum number of matching lines to return. Default 50."
                ),
            },
        },
    },
    "semantic_search": {
        "description": (
            "Search code by meaning using embeddings, or trace function "
            "dependencies. Modes: 'search' finds code matching a "
            "natural-language concept; 'trace_callers' finds call sites of a "
            "function; 'trace_callees' lists what a function calls; "
            "'trace_graph' returns the dependency tree up to `depth` levels. "
            "For 'search', describe intent rather than a bare identifier. "
            "Use find when you know the exact text or regex."
        ),
        "parameters": {
            "query": {
                "description": (
                    "Concept description for mode='search', or the exact "
                    "function/method name for the trace modes."
                ),
            },
            "mode": {
                "description": (
                    "Search strategy. Default 'search'."
                ),
                "enum": ["search", "trace_callers", "trace_callees", "trace_graph"],
            },
            "depth": {
                "description": (
                    "Dependency depth for mode='trace_graph'. 1 = direct "
                    "neighbors. Default 2. Ignored by other modes."
                ),
            },
            "top_k": {
                "description": (
                    "Maximum number of results for mode='search'. Default 5."
                ),
            },
        },
    },
    # ------------------------------------------------------------------
    # Sub-agents
    # ------------------------------------------------------------------
    "spawn_subagent": {
        "description": (
            "Spawn an independent worker agent and return its agent_id "
            "immediately. Use it for self-contained, bounded work that would "
            "otherwise consume many tool steps in the main conversation. The "
            "worker has no access to your history: put every needed file "
            "content, constraint, interface, and expected output format in "
            "`context`. Workspace changes are emitted as diffs. If the "
            "worker asks a question, it appears as a `[Sub-Agent #N →]` "
            "marker; call await_subagent to collect the final result."
        ),
        "parameters": {
            "task": {
                "description": (
                    "Clear, self-contained task description for the worker."
                ),
            },
            "context": {
                "description": (
                    "All context the worker needs: file contents, "
                    "constraints, interfaces, and expected output format."
                ),
            },
            "tools": {
                "description": (
                    "Allowlist of tool names for the worker. Default: "
                    "view_file, execute, read_output, send_input, find, "
                    "ask_user."
                ),
            },
            "model": {
                "description": (
                    "Optional model override, e.g. a faster/cheaper model for "
                    "a focused task. Defaults to the main agent's model."
                ),
            },
        },
    },
    "await_subagent": {
        "description": (
            "Block until a spawned sub-agent finishes and return its result "
            "summary, or return a timeout/error message. The main agent's "
            "step loop is paused while waiting, so call it only when you need "
            "the result before proceeding."
        ),
        "parameters": {
            "agent_id": {
                "description": (
                    "The integer agent_id returned by spawn_subagent."
                ),
            },
            "timeout": {
                "description": (
                    "Maximum seconds to wait before returning. Default 600."
                ),
            },
        },
    },
    # ------------------------------------------------------------------
    # Context maintenance
    # ------------------------------------------------------------------
    "archive_context": {
        "description": (
            "Replace one or more completed tasks in the conversation history "
            "with concise factual summaries. Available only during an "
            "internal context-maintenance turn. Preserve exact file paths, "
            "commands, decisions, outcomes, and unresolved issues needed by "
            "the active task. The active task cannot be archived. Positions "
            "are processed independently: invalid positions do not block "
            "valid ones, and the result reports each outcome so you can retry "
            "only the failures."
        ),
        "parameters": {
            "position": {
                "description": (
                    "Task position (integer) or list of positions to archive."
                ),
            },
            "summary": {
                "description": (
                    "Summary string, or a list with one summary per position."
                ),
            },
            "task": {
                "description": (
                    "Alias for `position`. Use either position or task, not "
                    "both."
                ),
            },
        },
    },
    # ------------------------------------------------------------------
    # MCP gateway
    # ------------------------------------------------------------------
    "mcp": {
        "description": (
            "Two-step gateway for all MCP tools. Step 1 DISCOVER: call with "
            "`query` describing the capability you need; the result lists "
            "matching servers, tool names, and input schemas. Step 2 INVOKE: "
            "call with the returned `server`, `func_name`, and `arguments` "
            "matching that tool's schema. Do not guess server or tool names; "
            "discover them first."
        ),
        "parameters": {
            "query": {
                "description": (
                    "Discovery: natural-language description of the "
                    "capability you need."
                ),
            },
            "server": {
                "description": (
                    "Invoke: exact MCP server name returned by discovery."
                ),
            },
            "func_name": {
                "description": (
                    "Invoke: exact tool name returned by discovery."
                ),
            },
            "arguments": {
                "description": (
                    "Invoke: JSON object matching the discovered tool's input "
                    "schema."
                ),
            },
        },
    },
    # ------------------------------------------------------------------
    # Legacy block retry
    # ------------------------------------------------------------------
    "retry_block": {
        "description": (
            "Retry a cached block operation after fixing the OS or apply "
            "error that made it fail. The result of the original failure "
            "contains the block_cache_id. Use the optional path only when "
            "the original target path was wrong. Not used by write_file or "
            "edit_file, which have their own cache_id retry."
        ),
        "parameters": {
            "block_cache_id": {
                "description": (
                    "The block_cache_id returned when the block operation "
                    "failed."
                ),
            },
            "path": {
                "description": (
                    "Optional corrected target path, when the original path "
                    "was wrong."
                ),
            },
        },
    },
}
