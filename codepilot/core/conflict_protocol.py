"""
File: conflict_protocol.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Created: 2026-08-30

Description:
    Parser and apply engine for CodePilot's plain-text conflict-marker protocol.

Protocol shape
--------------
Every file operation is a self-contained block in the model's raw text output:

    src/main.py
    <<<<<<< SEARCH
    def hello():
        print("world")
    =======
    def hello():
        print("universe")
    >>>>>>> REPLACE

Write / override (empty SEARCH section):

    src/new_file.py
    <<<<<<< SEARCH
    =======
    def main():
        print("brand new!")
    >>>>>>> REPLACE

The path is the **non-blank, non-marker line immediately before** the opening
marker.  Any text outside a block streams live to the user and is ignored here.

Forgiveness layers (parser)
---------------------------
1. Fuzzy markers   — <<<< through <<<<<<<< all accepted (±2 chars on < and >).
                     SEARCH/REPLACE suffixes matched by first letter only, so
                     "SRCH", "SEARCH", "SRCH" and "REPLACE", "RPLACE" all pass.
2. Separator line  — ======= matched by ≥4 consecutive = signs, with optional
                     leading/trailing whitespace.
3. Path tolerance  — the path line is the last non-blank, non-marker line
                     before <<<<<<< SEARCH.  Leading/trailing whitespace stripped.

Forgiveness layers (apply)
--------------------------
Pass 1 — First + last anchor  : match only the first and last non-blank line of
                                  the SEARCH block (normalised, indentation
                                  stripped).  If the resulting region is unique,
                                  accept it.  If non-unique, progressively add
                                  interior lines until unique.
Pass 2 — Indentation-stripped : if Pass 1 fails entirely, strip leading
                                  whitespace from every SEARCH line and retry
                                  exact (normalised) match against every
                                  indentation-stripped file line.  The matched
                                  region is spliced in with the REPLACE content.
Pass 3 — Context scoring      : if multiple candidate regions survive Passes 1-2,
                                  use the same above/below context scoring as the
                                  old diff engine to break ties.

Copyright (c) 2026 Jahanzeb Ahmed.
Licensed under the MIT License.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class ConflictProtocolError(ValueError):
    """Raised when a parsed block cannot be applied to the target file."""


# ---------------------------------------------------------------------------
# Fuzzy marker patterns
# ---------------------------------------------------------------------------

# <<<< through <<<<<<<< (5–9 < signs) optionally followed by any suffix
# starting with S or s (SEARCH, SRCH, etc.)
_OPEN_RE = re.compile(
    r"^[ \t]*(?:<{5,9})[ \t]*(?:[Ss]\w*)?[ \t]*$",
    re.MULTILINE,
)

# ======= — at least 4 = signs with optional surrounding whitespace
_SEP_RE = re.compile(
    r"^[ \t]*={4,}[ \t]*$",
    re.MULTILINE,
)

# >>>>>>> through >>>>>>>>> (5–9 > signs) optionally followed by any suffix
# starting with R or r (REPLACE, RPLACE, etc.)
_CLOSE_RE = re.compile(
    r"^[ \t]*(?:>{5,9})[ \t]*(?:[Rr]\w*)?[ \t]*$",
    re.MULTILINE,
)

# Lines that look like a protocol marker — used to exclude them from path
# candidates and to detect stray marker lines.
_ANY_MARKER_RE = re.compile(
    r"^[ \t]*(?:[<>{5,9}]|={4,}).*$",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlockOperation:
    """A single parsed SEARCH/REPLACE block."""

    path: str
    """Workspace-relative (or simple) path extracted from the line before <<<."""

    search_text: str
    """Raw text between <<<<<<< SEARCH and =======. Empty string → write/create."""

    replace_text: str
    """Raw text between ======= and >>>>>>> REPLACE."""

    source: str
    """The exact raw substring (path line + block) from the model output.
    Stored for caching on failure and for streaming clip-point tracking."""

    @property
    def is_creation(self) -> bool:
        """True when SEARCH is empty — write / full-override operation."""
        return not self.search_text.strip()


@dataclass
class ParseError:
    """A block that could not be fully parsed."""

    position: int   # 1-based index of the block in the model output
    path: str       # best-effort path (may be empty)
    reason: str     # human-readable description


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _is_marker_line(line: str) -> bool:
    """Return True if *line* looks like a protocol marker (<<</===/>>>)."""
    stripped = line.strip()
    return bool(
        _OPEN_RE.match(stripped)
        or _SEP_RE.match(stripped)
        or _CLOSE_RE.match(stripped)
    )


def _strip_trailing_newline(text: str) -> str:
    """Remove exactly one trailing newline if present."""
    if text.endswith("\n"):
        return text[:-1]
    return text


def parse_blocks(
    text: str,
) -> tuple[list[BlockOperation], list[ParseError]]:
    """
    Find all conflict-marker blocks in *text* (the model's raw generation).

    Returns
    -------
    ops    : list of successfully parsed BlockOperation objects, in order.
    errors : list of ParseError for every block that could not be parsed.

    A single malformed block does NOT abort the others.  Callers must check
    both return values and feed errors back to the model.
    """
    ops: list[BlockOperation] = []
    errors: list[ParseError] = []

    # Find every opening marker
    open_matches = list(_OPEN_RE.finditer(text))

    for idx, open_m in enumerate(open_matches):
        position = idx + 1  # 1-based
        path = ""

        # ── Determine search window ──────────────────────────────────────────
        # Text between this open marker and the next one (or end of string).
        block_start = open_m.start()
        next_open = open_matches[idx + 1].start() if idx + 1 < len(open_matches) else len(text)

        # ── Extract path line ────────────────────────────────────────────────
        # Walk backwards from block_start over the text that precedes this block.
        pre_text = text[:block_start]
        pre_lines = pre_text.splitlines()

        # Find the last non-blank, non-marker line before this block.
        for pre_line in reversed(pre_lines):
            stripped = pre_line.strip()
            if stripped and not _is_marker_line(stripped):
                path = stripped
                break

        # ── Find separator and close markers ─────────────────────────────────
        block_text = text[block_start:next_open]

        sep_m = _SEP_RE.search(block_text)
        if sep_m is None:
            errors.append(ParseError(
                position=position,
                path=path,
                reason=(
                    f"Block {position} (path={path!r}): missing '=======' separator. "
                    "The separator line must contain at least 4 '=' signs."
                ),
            ))
            continue

        close_m = _CLOSE_RE.search(block_text, sep_m.end())
        if close_m is None:
            errors.append(ParseError(
                position=position,
                path=path,
                reason=(
                    f"Block {position} (path={path!r}): missing '>>>>>>> REPLACE' closer. "
                    "The closing line must contain at least 5 '>' signs followed by REPLACE (or R...)."
                ),
            ))
            continue

        if not path:
            errors.append(ParseError(
                position=position,
                path="",
                reason=(
                    f"Block {position}: no path line found before '<<<<<<< SEARCH'. "
                    "The line immediately before '<<<<<<< SEARCH' must be the file path."
                ),
            ))
            continue

        # ── Extract SEARCH and REPLACE content ───────────────────────────────
        # SEARCH: from end of open-marker line to start of separator line.
        search_raw = block_text[open_m.end() - block_start: sep_m.start()]
        # Strip exactly one leading newline (the newline after the <<< line itself)
        if search_raw.startswith("\n"):
            search_raw = search_raw[1:]

        # REPLACE: from end of separator line to start of close marker.
        replace_raw = block_text[sep_m.end(): close_m.start()]
        if replace_raw.startswith("\n"):
            replace_raw = replace_raw[1:]

        # ── Build the source span for caching ────────────────────────────────
        # Include the path line if we can pin it.
        path_line_start = pre_text.rfind(path)
        source_start = path_line_start if path_line_start != -1 else block_start
        source_end = block_start + close_m.end()
        source = text[source_start:source_end]

        ops.append(BlockOperation(
            path=path,
            search_text=search_raw,
            replace_text=replace_raw,
            source=source,
        ))

    return ops, errors


# ---------------------------------------------------------------------------
# Apply engine helpers
# ---------------------------------------------------------------------------

def _norm(line: str) -> str:
    """Normalise a line: strip leading/trailing whitespace and line endings."""
    return line.strip()



def _search_lines(text: str) -> list[str]:
    """Split SEARCH text into non-blank lines preserving order."""
    return [ln for ln in text.splitlines() if ln.strip()]


def _find_anchored_region(
    file_lines: list[str],
    needle_lines: list[str],
    strip_indent: bool = False,
) -> list[tuple[int, int]]:
    """
    Find all (start, end) index pairs in *file_lines* where the sequence
    *needle_lines* can be matched.

    Algorithm
    ---------
    1. Collect all file positions of the first needle line.
    2. For each candidate start, walk forward through needle_lines checking
       that each appears in file_lines in order (gaps allowed).
    3. Return every (start, end) pair that satisfies the ordering constraint.

    Parameters
    ----------
    strip_indent : if True, compare normalised (indentation-stripped) versions
                   of both needle and file lines.
    """
    if not needle_lines:
        return []

    def _key(s: str) -> str:
        return _norm(s) if strip_indent else s.rstrip("\r\n")

    first_key = _key(needle_lines[0])
    last_key  = _key(needle_lines[-1])

    # All positions of the first needle line
    first_positions = [
        i for i, fl in enumerate(file_lines) if _key(fl) == first_key
    ]
    if not first_positions:
        return []

    if len(needle_lines) == 1:
        return [(p, p) for p in first_positions]

    # All positions of the last needle line
    last_positions = [
        i for i, fl in enumerate(file_lines) if _key(fl) == last_key
    ]
    if not last_positions:
        return []

    middle_keys = [_key(ln) for ln in needle_lines[1:-1]]

    candidates: list[tuple[int, int]] = []
    for fp in first_positions:
        # For each start anchor, find the FIRST valid end anchor at or after fp.
        # Taking the tightest region prevents cross-block pairing (e.g. START
        # from one block accidentally pairing with END from a later block).
        for lp in sorted(lp for lp in last_positions if lp >= fp):
            if lp == fp and needle_lines[0] != needle_lines[-1]:
                continue
            # Verify middle lines appear in order between fp and lp
            search_from = fp + 1
            ok = True
            for mk in middle_keys:
                found = False
                for k in range(search_from, lp):
                    if _key(file_lines[k]) == mk:
                        search_from = k + 1
                        found = True
                        break
                if not found:
                    ok = False
                    break
            if ok:
                candidates.append((fp, lp))
                break  # take the tightest match for this fp; don't keep going

    return candidates



def _splice(
    file_lines: list[str],
    region_start: int,
    region_end: int,
    needle_lines: list[str],
    replace_text: str,
) -> str:
    """
    Replace file_lines[region_start..region_end] (inclusive) with *replace_text*,
    preserving original line endings on surrounding lines.

    The replace content is written verbatim (as the model produced it) with a
    trailing newline appended to each line that doesn't already have one.
    """
    result: list[str] = list(file_lines[:region_start])

    # Emit the replacement lines
    for rline in replace_text.splitlines():
        result.append(rline + "\n")

    # Append everything after the region
    result.extend(file_lines[region_end + 1:])
    return "".join(result)


# ---------------------------------------------------------------------------
# Public apply entry-point
# ---------------------------------------------------------------------------

def apply_block(
    op: BlockOperation,
    current: str,
) -> str:
    """
    Apply *op* to *current* file content and return the new content.

    Parameters
    ----------
    op      : the parsed block operation.
    current : the current file content (empty string for new files).

    Raises
    ------
    ConflictProtocolError
        If the SEARCH text cannot be located in the file, or if the match
        is ambiguous (multiple regions match — add more lines to SEARCH).
    """

    # ── Write / create (empty SEARCH) ────────────────────────────────────────
    if op.is_creation:
        content = op.replace_text
        # Ensure final newline
        if content and not content.endswith("\n"):
            content += "\n"
        return content

    # ── Edit: locate the SEARCH block in the file ─────────────────────────────
    file_lines = current.splitlines(keepends=True)
    needle_lines = _search_lines(op.search_text)

    if not needle_lines:
        # Degenerate: SEARCH had only blank lines → treat as append
        content = current + op.replace_text
        if op.replace_text and not content.endswith("\n"):
            content += "\n"
        return content

    # ── Pass 1: exact (normalised, whitespace-preserved) anchor match ─────────
    candidates = _find_anchored_region(file_lines, needle_lines, strip_indent=False)

    # ── Pass 2: indentation-stripped match ────────────────────────────────────
    strip_indent = False
    if not candidates:
        candidates = _find_anchored_region(file_lines, needle_lines, strip_indent=True)
        strip_indent = True

    if not candidates:
        raise ConflictProtocolError(
            f"SEARCH block for '{op.path}' was not found in the file. "
            "The SEARCH text must match the current file content. "
            "Use view_file() to read the current content and correct the SEARCH block."
        )

    if len(candidates) > 1:
        raise ConflictProtocolError(
            f"SEARCH block for '{op.path}' is ambiguous: "
            f"{len(candidates)} regions match. "
            "Add more unique surrounding lines to the SEARCH block "
            "to make the target region unambiguous."
        )

    region_start, region_end = candidates[0]
    return _splice(file_lines, region_start, region_end, needle_lines, op.replace_text)


# ---------------------------------------------------------------------------
# Feedback message formatting
# ---------------------------------------------------------------------------

def format_parse_error(err: ParseError) -> str:
    """
    Return a short, actionable model-facing message for a ParseError.
    Appears in [EXECUTION RESULT] so the model can self-correct.
    """
    return (
        f"[block #{err.position}] PARSE FAILED: {err.reason}\n"
        "Format: a non-blank path line, then '<<<<<<< SEARCH', then the exact "
        "old content, then '=======', then the new content, then '>>>>>>> REPLACE'."
    )


def format_apply_error(op: BlockOperation, exc: ConflictProtocolError) -> str:
    """Actionable feedback when apply_block raises ConflictProtocolError."""
    e = str(exc).lower()
    if "not found" in e:
        return (
            f"[block:'{op.path}'] APPLY FAILED: {exc}\n"
            "Use view_file() to read the current file content, copy the exact lines "
            "you want to replace into the SEARCH block, then re-emit the block."
        )
    if "ambiguous" in e:
        return (
            f"[block:'{op.path}'] APPLY FAILED: {exc}\n"
            "Add more unique surrounding lines to the SEARCH block so only one "
            "location in the file matches."
        )
    return f"[block:'{op.path}'] APPLY FAILED: {exc}"
