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
                        # A bare fence line (context or unprefixed) safely terminates
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
# Phase 1: Region detection
# ---------------------------------------------------------------------------
#
# ALGORITHM
# ---------
# For a hunk with N remove (-) lines:
#
#   Step 1 — Find candidate regions
#       Locate every file position of the FIRST remove line and every file
#       position of the LAST remove line.  For each (first_pos, last_pos)
#       pair where first_pos <= last_pos, verify that every intermediate
#       remove line appears in the file between those positions IN ORDER
#       (but not necessarily contiguously — anything can sit between them).
#       Each valid pair defines a candidate region [first_pos, last_pos].
#
#   Step 2 — Unique? done.  Multiple? → context scoring.
#       For each candidate region, count how many of the diff's context
#       lines appear in a window around that region (above, within, below).
#       Highest score wins.  Tie → reject with a precise message.
#
# This correctly handles:
#   - Disjoint removes (context between - lines in the diff) ← trace_2 fix
#   - Missing context lines (model omitted some context)
#   - Fully unique single-line edits
# ---------------------------------------------------------------------------


def _find_remove_positions(file_lines: list[str], text: str) -> list[int]:
    """Return all file line indices where *text* matches (normalised)."""
    needle = _normal(text)
    return [i for i, fl in enumerate(file_lines) if _normal(fl) == needle]


def _find_region_candidates(
    file_lines: list[str],
    remove_texts: list[str],
) -> list[tuple[int, int]]:
    """Return (region_start, region_end) pairs for every valid placement.

    A valid placement satisfies:
      - remove_texts[0] is found at region_start in the file.
      - remove_texts[-1] is found at region_end (>= region_start) in the file.
      - Every intermediate remove line appears in the file between
        region_start and region_end, in order (gaps are allowed).

    Returns a list of (region_start, region_end) tuples.
    If there is only one remove line, region_start == region_end.
    """
    if not remove_texts:
        return []

    first_text = remove_texts[0]
    last_text = remove_texts[-1]
    middle_texts = remove_texts[1:-1] if len(remove_texts) > 1 else []

    first_positions = _find_remove_positions(file_lines, first_text)
    if not first_positions:
        return []

    # Single remove line: every occurrence is a candidate region
    if len(remove_texts) == 1:
        return [(p, p) for p in first_positions]

    last_positions = _find_remove_positions(file_lines, last_text)
    if not last_positions:
        return []

    candidates: list[tuple[int, int]] = []
    for fp in first_positions:
        for lp in last_positions:
            if lp < fp:
                continue
            if lp == fp and first_text != last_text:
                # Same physical line can only be first==last if the texts match
                continue
            # Verify all intermediate removes appear in file[fp+1 .. lp-1] in order
            search_from = fp + 1
            valid = True
            for mid in middle_texts:
                mid_needle = _normal(mid)
                found = False
                for k in range(search_from, lp):
                    if _normal(file_lines[k]) == mid_needle:
                        search_from = k + 1
                        found = True
                        break
                if not found:
                    valid = False
                    break
            if valid:
                candidates.append((fp, lp))

    return candidates


def _score_region(
    file_lines: list[str],
    region_start: int,
    region_end: int,
    hunk: "DiffHunk",
) -> int:
    """Score a region by how many diff context lines match the file around it.

    Context lines are split into three groups based on their position in the
    hunk relative to the remove lines:

      above_ctx  — context lines before the first remove line.
                   Matched against file lines immediately above region_start.

      below_ctx  — context lines after the last remove line.
                   Matched against file lines immediately below region_end.

      inner_ctx  — context lines between remove lines (interleaved).
                   Matched anywhere within the region span.

    Each matching context line adds 1 point.  Out-of-bounds or missing lines
    add 0.  This position-aware approach prevents two identical regions from
    tying when one has a unique function name above it and the other doesn't.
    """
    WINDOW = 8

    # Classify context lines by their hunk position relative to removes
    above_ctx: list[str] = []
    below_ctx: list[str] = []
    inner_ctx: list[str] = []

    seen_first_remove = False
    last_remove_idx = max(
        (i for i, dl in enumerate(hunk.lines) if dl.kind == "remove"),
        default=-1,
    )

    for i, dl in enumerate(hunk.lines):
        if dl.kind == "remove":
            seen_first_remove = True
        elif dl.kind == "context":
            if not seen_first_remove:
                above_ctx.append(dl.text)
            elif i > last_remove_idx:
                below_ctx.append(dl.text)
            else:
                inner_ctx.append(dl.text)

    score = 0

    # Score above-context: file lines immediately above region_start
    for j, ctx in enumerate(reversed(above_ctx)):
        file_idx = region_start - 1 - j
        if 0 <= file_idx < len(file_lines):
            if _normal(file_lines[file_idx]) == _normal(ctx):
                score += 1

    # Score below-context: file lines immediately below region_end
    for j, ctx in enumerate(below_ctx):
        file_idx = region_end + 1 + j
        if 0 <= file_idx < len(file_lines):
            if _normal(file_lines[file_idx]) == _normal(ctx):
                score += 1

    # Score inner-context: search within the region span
    if inner_ctx:
        region_set = {_normal(fl) for fl in file_lines[region_start:region_end + 1]}
        for ctx in inner_ctx:
            if _normal(ctx) in region_set:
                score += 1

    return score


# ---------------------------------------------------------------------------
# Phase 2: Apply the hunk to the confirmed region
# ---------------------------------------------------------------------------


def _apply_hunk(
    file_lines: list[str],
    hunk: "DiffHunk",
    region_start: int,
    region_end: int,
) -> list[str]:
    """Apply *hunk* to *file_lines* using the confirmed region [region_start, region_end].

    Walk strategy
    -------------
    Maintain a file cursor starting at region_start.

    For each diff line (in order):

      (-) remove line:
          Scan forward from cursor to region_end until we find the matching
          file line.  Emit as-is any file lines we skip over (they are not
          mentioned in the diff → implicitly kept).  Buffer the matched line
          for removal; advance cursor past it.

      (+) add line:
          Accumulate into pending_adds.  These are flushed (emitted) the next
          time we hit a non-add diff line or reach the end of the hunk.

      ( ) context line (advisory only):
          Flush any pending buffer/adds first, then skip (Phase 2 does not
          re-verify context — Phase 1 already located the region).

    After all diff lines are processed:
      - Flush any remaining buffer/adds.
      - Emit any remaining file lines within the region (implicitly kept).
      - Append everything after region_end.
    """
    result: list[str] = list(file_lines[:region_start])
    cursor = region_start
    remove_buffer: list[int] = []  # file indices held for removal
    pending_adds: list[str] = []   # + line texts to emit on flush

    def flush() -> None:
        for add_text in pending_adds:
            result.append(add_text + "\n")
        remove_buffer.clear()
        pending_adds.clear()

    def emit_upto(stop: int) -> None:
        nonlocal cursor
        while cursor < stop:
            result.append(file_lines[cursor])
            cursor += 1

    for dl in hunk.lines:
        if dl.kind == "remove":
            # Flush any pure-insertion pending adds before starting a new removal
            if not remove_buffer and pending_adds:
                flush()
            # Scan forward within the region to find this - line
            target = _normal(dl.text)
            found_at = None
            for k in range(cursor, region_end + 1):
                if _normal(file_lines[k]) == target:
                    found_at = k
                    break
            if found_at is None:
                # Phase 1 verified this exists; skip defensively if somehow missing
                continue
            # Keep any file lines we skip over (implicitly not in the diff)
            emit_upto(found_at)
            # Buffer this file line for removal; advance cursor
            remove_buffer.append(cursor)
            cursor += 1

        elif dl.kind == "add":
            pending_adds.append(dl.text)

        else:  # context — advisory only
            # Flush before advancing past this context position
            if remove_buffer or pending_adds:
                flush()

    # End of hunk: flush remaining buffer/adds
    if remove_buffer or pending_adds:
        flush()

    # Emit any remaining file lines within the region (not consumed by removes)
    emit_upto(region_end + 1)

    # Append everything after the region
    result.extend(file_lines[region_end + 1:])
    return result


# ---------------------------------------------------------------------------
# Public apply entry-point
# ---------------------------------------------------------------------------


def apply_operation(operation: DiffOperation, current: str, exists: bool) -> str:
    """Return the new file content or raise a precise, non-mutating rejection.

    Phase 1 — Locate the region
        Find every file position of the first and last remove (-) line.
        Build candidate regions (first_pos → last_pos), verifying all
        intermediate - lines appear in order between them (gaps allowed).
        Unique region → Phase 2.  Multiple → context scoring tiebreak.
        Tie or zero candidates → reject with a precise actionable message.

    Phase 2 — Apply the edit
        Walk the hunk.  - lines are found in the region and buffered.
        + lines flush the buffer (replacement).  File lines inside the
        region that are not mentioned by any - line are kept as-is.
        Context lines in the diff are advisory (Phase 1 already used them).
    """
    if operation.is_creation:
        if any(line.kind != "add" for hunk in operation.hunks for line in hunk.lines):
            raise DiffProtocolError(
                f"Creation diff for '{operation.path}' must contain only + lines."
            )
        return "".join(line.text + "\n" for h in operation.hunks for line in h.new_lines)

    working = current
    for number, hunk in enumerate(operation.hunks, start=1):
        file_lines = working.splitlines(keepends=True)
        remove_texts = [dl.text for dl in hunk.lines if dl.kind == "remove"]
        context_texts = [dl.text for dl in hunk.lines if dl.kind == "context"]

        # ── Pure insertion: no remove lines ───────────────────────────────────
        if not remove_texts:
            add_lines = [dl.text for dl in hunk.lines if dl.kind == "add"]
            if context_texts:
                # Use context lines as a contiguous needle to find insertion point
                ctx_needle = [_normal(t) for t in context_texts]
                ctx_matches = [
                    s for s in range(len(file_lines) - len(ctx_needle) + 1)
                    if [_normal(file_lines[s + i]) for i in range(len(ctx_needle))] == ctx_needle
                ]
                if not ctx_matches:
                    raise DiffProtocolError(
                        f"Hunk {number} for '{operation.path}': context lines were not found "
                        "in the file. Cannot locate the insertion point."
                    )
                if len(ctx_matches) != 1:
                    raise DiffProtocolError(
                        f"Hunk {number} for '{operation.path}' is ambiguous: context lines "
                        f"match {len(ctx_matches)} locations. Add more unique context lines "
                        "to pinpoint the insertion point."
                    )
                # Count context lines before the first + to find the split point
                pre_ctx = 0
                for dl in hunk.lines:
                    if dl.kind == "context":
                        pre_ctx += 1
                    elif dl.kind == "add":
                        break
                ip = ctx_matches[0] + pre_ctx
                working = "".join(
                    file_lines[:ip] + [t + "\n" for t in add_lines] + file_lines[ip:]
                )
            else:
                # No context, no removes: plain append
                working = working + "".join(t + "\n" for t in add_lines)
            continue

        # ── Phase 1: find candidate regions ───────────────────────────────────
        candidates = _find_region_candidates(file_lines, remove_texts)

        if not candidates:
            raise DiffProtocolError(
                f"Hunk {number} for '{operation.path}': the deleted lines (-) were not found "
                "in the file. Verify the - lines exactly match the current file content "
                "and regenerate the diff."
            )

        if len(candidates) == 1:
            region_start, region_end = candidates[0]
        else:
            # Disambiguate with context scoring
            scores = [
                _score_region(file_lines, rs, re, hunk)
                for rs, re in candidates
            ]
            max_score = max(scores)
            winners = [
                (rs, re) for (rs, re), s in zip(candidates, scores) if s == max_score
            ]

            if len(winners) > 1:
                # Tie-breaker: prefer the tightest (shortest) region
                min_len = min(re - rs for rs, re in winners)
                winners = [(rs, re) for rs, re in winners if re - rs == min_len]

            if len(winners) != 1:
                raise DiffProtocolError(
                    f"Hunk {number} for '{operation.path}' is ambiguous: "
                    f"{len(candidates)} regions match the - lines and "
                    f"multiple of them tie on context score ({max_score} point(s)). "
                    "Add more unique space-prefixed context lines above or below the "
                    "changed block to make one region score higher than the rest."
                )
            region_start, region_end = winners[0]

        # ── Phase 2: apply the edit ────────────────────────────────────────────
        working = "".join(_apply_hunk(file_lines, hunk, region_start, region_end))

    return working
