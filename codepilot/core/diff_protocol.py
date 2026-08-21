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
                    if raw.strip() == "```":
                        # A closing markdown fence safely terminates the hunk.
                        # This prevents trailing conversational text from being
                        # parsed as malformed hunk lines.
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


def _render(hunk: DiffHunk, file_lines: list[str], match_start: int) -> list[str]:
    """Context takes indentation from the matched file; additions keep model text."""
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
    """Return the new content or raise a precise, non-mutating rejection."""
    if operation.is_creation:
        if any(line.kind != "add" for hunk in operation.hunks for line in hunk.lines):
            raise DiffProtocolError(
                f"Creation diff for '{operation.path}' must contain only + lines."
            )
        return "".join(line.text + "\n" for h in operation.hunks for line in h.new_lines)

    working = current
    for number, hunk in enumerate(operation.hunks, start=1):
        old = hunk.old_lines
        new = hunk.new_lines
        if not old:
            return "".join(line.text + "\n" for h in operation.hunks for line in h.new_lines)
        file_lines = working.splitlines(keepends=True)
        needle = [_normal(line.text) for line in old]
        matches = [
            start for start in range(len(file_lines) - len(needle) + 1)
            if [_normal(file_lines[start + offset]) for offset in range(len(needle))] == needle
        ]
        if not matches:
            raise DiffProtocolError(
                f"Hunk {number} for '{operation.path}' was not found. Include more current surrounding context."
            )
        if len(matches) != 1:
            raise DiffProtocolError(
                f"Hunk {number} for '{operation.path}' is ambiguous: found {len(matches)} matches. "
                "Include more surrounding context."
            )
        start = matches[0]
        replacement = _render(hunk, file_lines, start)
        working = "".join(file_lines[:start] + replacement + file_lines[start + len(old):])
    return working
