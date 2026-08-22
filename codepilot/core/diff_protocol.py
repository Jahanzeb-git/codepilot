"""Content-addressed application of model-generated unified diffs.

This is deliberately *not* a ``patch`` implementation.  Hunk ranges are a
recognisable delimiter for models, not an authority on where a change belongs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$", re.MULTILINE)
_HUNK = re.compile(r"^@@.*@@(?:.*)?$", re.MULTILINE)


class DiffProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class DiffLine:
    kind: str  # context, add, remove
    text: str


@dataclass(frozen=True)
class DiffHunk:
    lines: tuple[DiffLine, ...]

    @property
    def old_lines(self) -> list[DiffLine]:
        return [line for line in self.lines if line.kind != "add"]

    @property
    def new_lines(self) -> list[DiffLine]:
        return [line for line in self.lines if line.kind != "remove"]


@dataclass(frozen=True)
class DiffOperation:
    path: str
    old_path: str
    new_path: str
    hunks: tuple[DiffHunk, ...]
    source: str

    @property
    def is_creation(self) -> bool:
        return self.old_path == "/dev/null"


def _path_from_marker(line: str, prefix: str) -> str | None:
    if not line.startswith(prefix):
        return None
    value = line[len(prefix):].strip().split("\t", 1)[0]
    return value


def parse_operations(
    text: str,
) -> tuple[list[DiffOperation], list[tuple[int, str, DiffProtocolError]]]:
    """Find all ``diff --git`` operations in arbitrary Markdown.

    Returns
    -------
    ops    : list of successfully parsed DiffOperation objects.
    errors : list of (position, target_path_or_raw, DiffProtocolError) for
             every operation that could not be parsed.  The position is the
             1-based index of the ``diff --git`` block in the response.

    A single malformed block does NOT abort the others.  Callers must check
    both return values and feed errors back to the model.
    """
    headers = list(_HEADER.finditer(text))
    ops: list[DiffOperation] = []
    errors: list[tuple[int, str, DiffProtocolError]] = []

    for index, header in enumerate(headers):
        position = index + 1  # 1-based for feedback messages
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        source = text[header.start():end]
        old_header, new_header = header.group(1), header.group(2)
        old_path, new_path = f"a/{old_header}", f"b/{new_header}"
        marker_lines = source.splitlines()
        for line in marker_lines[1:]:
            old_path = _path_from_marker(line, "--- ") or old_path
            new_path = _path_from_marker(line, "+++ ") or new_path
            if line.startswith("@@"):
                break

        # Resolve the target path as early as possible so error messages name
        # the file even when later validation fails.
        raw_target = new_header  # best-effort name for error reporting

        try:
            if new_path == "/dev/null":
                raise DiffProtocolError(
                    f"Deletion diff for '{old_header}' is not supported by this runtime."
                )
            target = new_path[2:] if new_path.startswith("b/") else new_path
            if not target or target == "/dev/null":
                raise DiffProtocolError("Diff is missing a writable +++ target path.")

            raw_target = target  # update once we have the real resolved target

            # Whether to guard against closing-fence bleed on + lines.
            # Markdown files may legitimately emit fenced code blocks as content.
            is_markdown = target.lower().endswith((".md", ".mdx", ".markdown"))

            hunk_marks = list(_HUNK.finditer(source))
            if not hunk_marks:
                raise DiffProtocolError(f"Diff for '{target}' has no @@ hunk marker.")

            hunks: list[DiffHunk] = []
            for hunk_index, mark in enumerate(hunk_marks):
                body_end = (
                    hunk_marks[hunk_index + 1].start()
                    if hunk_index + 1 < len(hunk_marks)
                    else len(source)
                )
                body = source[mark.end():body_end]
                if body.startswith("\n"):
                    body = body[1:]
                lines: list[DiffLine] = []
                for raw in body.splitlines():
                    stripped = raw.strip()
                    if stripped.startswith("```"):
                        # A bare fence line (context or unpreixed) safely terminates
                        # the hunk — prevents trailing markdown fences polluting the
                        # parsed diff.
                        break
                    if not is_markdown and raw.startswith("+") and not raw.startswith("++"):
                        # Closing-fence bleed: model emitted a ``` line prefixed with
                        # '+' as the last line of a write/creation diff.  For non-
                        # markdown targets this is always hallucinated punctuation from
                        # the surrounding ```diff fence — strip it and stop the hunk.
                        inner = raw[1:].strip()  # text after the leading '+'
                        if inner.startswith("```"):
                            break
                    if raw.startswith("\\ No newline at end of file"):
                        continue
                    if raw.startswith("+") and not raw.startswith("+++"):
                        lines.append(DiffLine("add", raw[1:]))
                    elif raw.startswith("-") and not raw.startswith("---"):
                        lines.append(DiffLine("remove", raw[1:]))
                    elif raw.startswith(" "):
                        lines.append(DiffLine("context", raw[1:]))
                    elif raw == "":
                        # A model occasionally omits the required context
                        # prefix on blank lines.  Treat as context; unambiguous.
                        lines.append(DiffLine("context", ""))
                    else:
                        raise DiffProtocolError(
                            f"Malformed hunk line in '{target}': {raw!r} "
                            "(expected space, +, or - prefix)."
                        )
                if not lines:
                    raise DiffProtocolError(f"Empty hunk in diff for '{target}'.")
                hunks.append(DiffHunk(tuple(lines)))

            ops.append(DiffOperation(target, old_path, new_path, tuple(hunks), source))

        except DiffProtocolError as exc:
            errors.append((position, raw_target, exc))

    return ops, errors


def _normal(line: str) -> str:
    return line.lstrip().rstrip("\r\n")


# ---------------------------------------------------------------------------
# Phase 1: Hard Anchors + Soft Context Scoring
# ---------------------------------------------------------------------------

def _find_hard_anchor_matches(
    file_lines: list[str],
    remove_lines: list[str],
) -> list[int]:
    """Return every start index where *remove_lines* appear contiguously in *file_lines*.

    Comparison is normalised (leading whitespace and line endings stripped).
    If *remove_lines* is empty (pure-insertion hunk) every file position is a
    candidate: returns [0 .. len(file_lines)] inclusive so the caller can still
    disambiguate via context scoring.
    """
    if not remove_lines:
        return list(range(len(file_lines) + 1))
    needle = [_normal(ln) for ln in remove_lines]
    n = len(needle)
    return [
        start
        for start in range(len(file_lines) - n + 1)
        if [_normal(file_lines[start + i]) for i in range(n)] == needle
    ]


def _score_context(
    file_lines: list[str],
    candidate_start: int,
    candidate_end: int,
    hunk: "DiffHunk",
) -> int:
    """Score how many context lines from *hunk* match the file around [candidate_start, candidate_end).

    Context lines that appear before the first remove line are matched against
    file lines immediately above the candidate region (going upward).  Context
    lines that appear after the last remove line are matched against file lines
    immediately below.  Interleaved context lines (between remove groups) are
    matched at their implicit cursor position within the region.

    Each matching context line adds 1 point.  Missing or out-of-bounds context
    lines add 0.  The total is the score for this candidate.
    """
    score = 0
    above_ctx: list[str] = []   # context lines before the first - line
    below_ctx: list[str] = []   # context lines after the last - line
    inner_ctx: list[tuple[int, str]] = []  # (file_offset_within_region, text)

    seen_remove = False
    remove_finished = False
    remove_offset = 0  # cursor within the candidate region

    for dl in hunk.lines:
        if dl.kind == "remove":
            seen_remove = True
            remove_offset += 1
        elif dl.kind == "add":
            # additions don't occupy file positions
            pass
        else:  # context
            if not seen_remove:
                above_ctx.append(dl.text)
            elif remove_finished or not any(
                d.kind == "remove" for d in hunk.lines[hunk.lines.index(dl):]
            ):
                below_ctx.append(dl.text)
            else:
                inner_ctx.append((remove_offset, dl.text))
            remove_offset += 1

    # Detect "remove_finished": context lines after all removes
    # Re-derive above/below cleanly without the ambiguous mid-loop state above.
    above_ctx = []
    below_ctx = []
    inner_ctx = []
    remove_offset = 0
    first_remove_seen = False
    last_remove_offset = -1

    # First pass: find last remove position
    tmp_offset = 0
    for dl in hunk.lines:
        if dl.kind == "remove":
            last_remove_offset = tmp_offset
            tmp_offset += 1
        elif dl.kind == "context":
            tmp_offset += 1

    # Second pass: classify context lines
    cur_offset = 0
    for dl in hunk.lines:
        if dl.kind == "remove":
            first_remove_seen = True
            cur_offset += 1
        elif dl.kind == "context":
            if not first_remove_seen:
                above_ctx.append(dl.text)
            elif cur_offset > last_remove_offset:
                below_ctx.append(dl.text)
            else:
                inner_ctx.append((cur_offset, dl.text))
            cur_offset += 1

    # Score above-context lines (match file lines just above candidate_start)
    for i, ctx in enumerate(reversed(above_ctx)):
        file_idx = candidate_start - 1 - i
        if 0 <= file_idx < len(file_lines):
            if _normal(file_lines[file_idx]) == _normal(ctx):
                score += 1

    # Score below-context lines (match file lines just below candidate_end)
    for i, ctx in enumerate(below_ctx):
        file_idx = candidate_end + i
        if 0 <= file_idx < len(file_lines):
            if _normal(file_lines[file_idx]) == _normal(ctx):
                score += 1

    # Score inner context lines (interleaved within the candidate region)
    for offset, ctx in inner_ctx:
        file_idx = candidate_start + offset
        if 0 <= file_idx < len(file_lines):
            if _normal(file_lines[file_idx]) == _normal(ctx):
                score += 1

    return score


# ---------------------------------------------------------------------------
# Phase 2: buffer / flush cursor walk
# ---------------------------------------------------------------------------

def _apply_hunk_at(
    file_lines: list[str],
    hunk: "DiffHunk",
    region_start: int,
) -> list[str]:
    """Apply *hunk* at the confirmed region starting at *region_start*.

    Walk the hunk line by line:
    - context line  → keep the corresponding file line as-is; advance cursor.
    - remove line   → collect into buffer; advance cursor.
    - add line      → if buffer non-empty: flush (replace buffered file lines
                       with this + line and any immediately following + lines);
                       if buffer empty: insert before cursor position.

    This handles N→M replacements correctly regardless of the ratio.
    """
    result_before = list(file_lines[:region_start])
    result_after_start = region_start
    file_cursor = region_start
    buffer_start: int | None = None   # file index where current - run started
    buffer_len = 0                    # number of - lines accumulated
    additions: list[str] = []        # + lines waiting to flush with the buffer

    output: list[str] = []

    i = 0
    lines = hunk.lines
    while i < len(lines):
        dl = lines[i]
        if dl.kind == "context":
            # Flush any pending buffer first (- lines with no trailing + yet)
            if buffer_start is not None:
                # Remove buffered file lines, emit additions collected so far
                for a in additions:
                    output.append(a + "\n")
                additions = []
                buffer_start = None
                buffer_len = 0
            output.append(file_lines[file_cursor])
            file_cursor += 1
        elif dl.kind == "remove":
            if buffer_start is None:
                buffer_start = file_cursor
                buffer_len = 0
            buffer_len += 1
            file_cursor += 1
        else:  # add
            if buffer_start is not None:
                # Accumulate + lines for this buffer run
                additions.append(dl.text)
            else:
                # Pure insertion: no preceding - lines
                output.append(dl.text + "\n")
        i += 1

    # End of hunk: flush any remaining buffer
    if buffer_start is not None:
        for a in additions:
            output.append(a + "\n")

    # Append the remainder of the file after the consumed region
    remove_count = sum(1 for dl in hunk.lines if dl.kind == "remove")
    result_tail = list(file_lines[region_start + remove_count + sum(
        1 for dl in hunk.lines if dl.kind == "context"
    ):])
    return result_before + output + result_tail


# ---------------------------------------------------------------------------
# Public apply entry-point
# ---------------------------------------------------------------------------

def _render(hunk: "DiffHunk", file_lines: list[str], match_start: int) -> list[str]:
    """Reconstruct output lines from a confirmed match region.

    Context lines take their indentation from the matched file lines;
    addition lines keep the model's text verbatim.
    """
    rendered: list[str] = []
    old_offset = 0
    for line in hunk.lines:
        if line.kind == "remove":
            old_offset += 1
        elif line.kind == "context":
            rendered.append(file_lines[match_start + old_offset])
            old_offset += 1
        else:
            rendered.append(line.text + "\n")
    return rendered


def apply_operation(operation: DiffOperation, current: str, exists: bool) -> str:
    """Return the new content or raise a precise, non-mutating rejection.

    Uses the two-phase Hard Anchors + Soft Context Scoring algorithm:

    Phase 1 — Locate the region
        Extract the ``-`` lines as hard anchors.  Search the file for that
        exact sequence.  If found exactly once → proceed.  If found multiple
        times → disambiguate with context-line scoring.  Tie → reject with a
        precise feedback message naming the count of matching regions.

    Phase 2 — Apply the edit
        Walk the hunk with a buffer/flush cursor.  ``-`` lines accumulate in a
        buffer; the first ``+`` line (or end of hunk) flushes the buffer as a
        block replacement.  Context lines advance the cursor without re-checking
        (Phase 1 already located the region uniquely).
    """
    if operation.is_creation:
        if any(line.kind != "add" for hunk in operation.hunks for line in hunk.lines):
            raise DiffProtocolError(
                f"Creation diff for '{operation.path}' must contain only + lines."
            )
        return "".join(line.text + "\n" for h in operation.hunks for line in h.new_lines)

    working = current
    for number, hunk in enumerate(operation.hunks, start=1):
        remove_lines_text = [dl.text for dl in hunk.lines if dl.kind == "remove"]
        context_lines_text = [dl.text for dl in hunk.lines if dl.kind == "context"]
        file_lines = working.splitlines(keepends=True)

        if not remove_lines_text:
            # ── Pure insertion: no hard anchors ───────────────────────────────
            # Match the context lines as a needle to find insertion point, then
            # insert + lines immediately after the matched context block.
            if context_lines_text:
                ctx_needle = [_normal(t) for t in context_lines_text]
                ctx_matches = [
                    start for start in range(len(file_lines) - len(ctx_needle) + 1)
                    if [_normal(file_lines[start + i]) for i in range(len(ctx_needle))] == ctx_needle
                ]
                if not ctx_matches:
                    raise DiffProtocolError(
                        f"Hunk {number} for '{operation.path}': the context lines were not found "
                        "in the file. Cannot locate the insertion point."
                    )
                if len(ctx_matches) != 1:
                    raise DiffProtocolError(
                        f"Hunk {number} for '{operation.path}' is ambiguous: context lines match "
                        f"{len(ctx_matches)} locations. Add more unique context lines to pinpoint "
                        "the insertion point."
                    )
                # Find the first + line position within the hunk to split context
                # into before-insertion and after-insertion groups.
                add_lines = [dl.text for dl in hunk.lines if dl.kind == "add"]
                ctx_start = ctx_matches[0]
                # Count context lines before the first + line in the hunk
                pre_ctx_count = 0
                for dl in hunk.lines:
                    if dl.kind == "context":
                        pre_ctx_count += 1
                    elif dl.kind == "add":
                        break
                insertion_point = ctx_start + pre_ctx_count
                new_lines = [t + "\n" for t in add_lines]
                working = "".join(
                    file_lines[:insertion_point] + new_lines + file_lines[insertion_point:]
                )
            else:
                # No context at all: append to end of file
                add_lines = [dl.text + "\n" for dl in hunk.lines if dl.kind == "add"]
                working = working + "".join(add_lines)
            continue

        # ── Phase 1: locate the region via hard anchors ──────────────────────
        candidates = _find_hard_anchor_matches(file_lines, remove_lines_text)

        if not candidates:
            raise DiffProtocolError(
                f"Hunk {number} for '{operation.path}': the deleted lines (-) were not found "
                "verbatim in the file. Verify the - lines exactly match the current file "
                "content and regenerate the diff."
            )

        if len(candidates) == 1:
            winner = candidates[0]
        else:
            # Disambiguate using soft context scoring
            region_len = len(remove_lines_text)
            scores = [
                _score_context(file_lines, c, c + region_len, hunk)
                for c in candidates
            ]
            max_score = max(scores)
            winners = [c for c, s in zip(candidates, scores) if s == max_score]

            if len(winners) != 1:
                raise DiffProtocolError(
                    f"Hunk {number} for '{operation.path}' is ambiguous: "
                    f"{len(candidates)} regions match the - lines and "
                    f"{len(winners)} of them tie on context score ({max_score} point(s)). "
                    "Add more unique space-prefixed context lines above or below the "
                    "changed block to make one region score higher than the rest."
                )
            winner = winners[0]

        # ── Phase 2: apply the edit at the confirmed region ──────────────────
        # winner = index of the first - line.  Compute render_start by counting
        # leading context lines (those before the first - in the hunk).
        leading_ctx = 0
        for dl in hunk.lines:
            if dl.kind == "context":
                leading_ctx += 1
            elif dl.kind == "remove":
                break

        render_start = winner - leading_ctx
        # Total file lines consumed: all non-add hunk lines
        total_old_span = sum(1 for dl in hunk.lines if dl.kind != "add")

        replacement = _render(hunk, file_lines, render_start)
        working = "".join(
            file_lines[:render_start] + replacement + file_lines[render_start + total_old_span:]
        )
    return working
