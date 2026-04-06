"""
codepilot/core/vt.py

Virtual VT100 / VT102 headless terminal emulator for CodePilot shell sessions.

Replaces regex-based ANSI stripping with a proper state-machine emulator that
maintains a 2-D character grid identical to what a human sees in a real
terminal.  The LLM therefore receives only the *final rendered state* of the
screen, not the raw escape-laden byte stream.

Key features
────────────
• Scrollback buffer   — lines that scroll off the top are kept (up to
                        MAX_SCROLLBACK) and included in every snapshot.
• Alternate screen    — apps like vim / top / htop switch buffers; the
                        emulator tracks main and alt independently and
                        restores the main screen when the app exits.
• Delta snapshots     — delta_snapshot() returns only new scrollback lines
                        + current visible screen, enabling cheap polling of
                        still-running commands without duplicating output.
• Thread safety       — a reentrant lock guards all state mutation so the
                        emulator is safe to feed from a reader thread while
                        the main thread takes snapshots.
• Large PTY geometry  — default 220 × 50 suppresses most line-wrapping that
                        would break structured output for smaller ttys.

Supported escape sequences
──────────────────────────
CSI final bytes:  A B C D E F G H J K L M P S T X @ d f m r s u
ESC sequences:    7 8 c M E  and charset / keypad variants
OSC:              ignored (terminal title, hyperlinks, …)
DCS / PM / APC:   ignored until String Terminator
C1 8-bit:         9B (CSI), 9D (OSC), 90 (DCS) recognised
"""

from __future__ import annotations

import threading
from typing import List, Optional, Tuple

# ── defaults ──────────────────────────────────────────────────────────────────

DEFAULT_COLS   = 220
DEFAULT_ROWS   = 50
MAX_SCROLLBACK = 10_000   # hard cap — prevents unbounded memory growth

# ── parser states (int constants are faster than Enum in hot loops) ───────────

_NORMAL  = 0   # ordinary text
_ESC     = 1   # received ESC, waiting for dispatch byte
_CSI     = 2   # ESC [ — collecting params + final byte
_CSIPV   = 3   # ESC [ ? (or < > =) — DEC private CSI
_OSC     = 4   # ESC ] — ignore until BEL / ST
_IGNORE  = 5   # DCS / PM / APC — ignore until ST
_CHARSET = 6   # ESC ( or ) — consume one more byte then done


# ── VirtualScreen ─────────────────────────────────────────────────────────────

class VirtualScreen:
    """
    Headless VT100/VT102 terminal emulator.

    Typical usage (one instance per ShellSession)::

        screen = VirtualScreen()

        # before every new command:
        screen.reset()

        # after receiving raw PTY bytes:
        screen.feed(process.before)

        # to get what the LLM should see (full):
        clean_text = screen.snapshot()

        # to get only new content during polling:
        new_text = screen.delta_snapshot()
    """

    # ── construction ──────────────────────────────────────────────────────────

    def __init__(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> None:
        self.cols = cols
        self.rows = rows
        self._lock = threading.RLock()
        self._init_state()

    def _init_state(self) -> None:
        """Initialise (or re-initialise) all mutable state."""
        # Main screen
        self._grid: List[List[str]] = self._blank_grid()
        self._scrollback: List[str] = []

        # Cursor
        self._row: int = 0
        self._col: int = 0
        self._saved_cursor: Tuple[int, int] = (0, 0)

        # Scrolling region (inclusive, 0-based row indices)
        self._scroll_top: int = 0
        self._scroll_bot: int = self.rows - 1

        # Alternate screen
        self._in_alt: bool = False
        self._alt_grid: Optional[List[List[str]]] = None
        self._alt_scrollback: Optional[List[str]] = None
        self._alt_saved_cursor: Optional[Tuple[int, int]] = None

        # Parser state machine
        self._state: int = _NORMAL
        self._param_buf: str = ""
        self._inter_buf: str = ""

        # Delta watermark (index into self._scrollback)
        self._delta_mark: int = 0

    # ── public API ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all terminal state — call before each new shell command."""
        with self._lock:
            self._init_state()

    def feed(self, text: str) -> None:
        """Feed raw PTY bytes (as a decoded string) into the emulator."""
        with self._lock:
            for ch in text:
                self._process(ch)

    def snapshot(self) -> str:
        """
        Return the full current terminal state as plain text.

        Includes all scrollback lines followed by visible screen rows.
        Trailing blank lines are stripped.
        """
        with self._lock:
            return self._render(self._scrollback, self._grid)

    def delta_snapshot(self) -> str:
        """
        Return only content *new since the last delta_snapshot()* call.

        Includes newly scrolled-off lines + the current visible screen.
        Advances the internal watermark so the next call won't repeat them.
        Used for token-efficient polling of still-running commands.
        """
        with self._lock:
            new_sb = self._scrollback[self._delta_mark:]
            self._delta_mark = len(self._scrollback)
            return self._render(new_sb, self._grid)

    def reset_delta(self) -> None:
        """Advance the delta watermark to the current position (skip pending lines)."""
        with self._lock:
            self._delta_mark = len(self._scrollback)

    # ── rendering ─────────────────────────────────────────────────────────────

    @staticmethod
    def _render(scrollback: List[str], grid: List[List[str]]) -> str:
        lines = list(scrollback)
        for row in grid:
            lines.append("".join(row).rstrip())
        # Drop trailing blank lines
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    # ── grid helpers ──────────────────────────────────────────────────────────

    def _blank_grid(self) -> List[List[str]]:
        return [[" "] * self.cols for _ in range(self.rows)]

    def _blank_row(self) -> List[str]:
        return [" "] * self.cols

    # ── scroll helpers ────────────────────────────────────────────────────────

    def _scroll_up(self, n: int = 1) -> None:
        """Scroll the scrolling region *up* by n lines (content moves up/off)."""
        for _ in range(n):
            # Capture the top line of the scrolling region
            top_line = "".join(self._grid[self._scroll_top]).rstrip()
            # Only push to scrollback when the region starts at row 0
            if self._scroll_top == 0 and not self._in_alt:
                if len(self._scrollback) >= MAX_SCROLLBACK:
                    self._scrollback.pop(0)
                    if self._delta_mark > 0:
                        self._delta_mark -= 1
                self._scrollback.append(top_line)
            # Remove top row of region; insert blank at bottom
            del self._grid[self._scroll_top]
            self._grid.insert(self._scroll_bot, self._blank_row())

    def _scroll_down(self, n: int = 1) -> None:
        """Scroll the scrolling region *down* by n lines (content moves down)."""
        for _ in range(n):
            del self._grid[self._scroll_bot]
            self._grid.insert(self._scroll_top, self._blank_row())

    # ── printable character ───────────────────────────────────────────────────

    def _write_char(self, ch: str) -> None:
        if self._col >= self.cols:
            # Auto-wrap to next line
            self._row += 1
            if self._row > self._scroll_bot:
                self._row = self._scroll_bot
                self._scroll_up()
            self._col = 0
        self._grid[self._row][self._col] = ch
        self._col += 1

    # ── C0 control codes ──────────────────────────────────────────────────────

    def _handle_c0(self, ch: str) -> bool:
        """Process a C0 control character. Returns True if consumed."""
        if ch == "\r":
            self._col = 0
            return True
        if ch in ("\n", "\x0b", "\x0c"):          # LF / VT / FF
            self._row += 1
            if self._row > self._scroll_bot:
                self._row = self._scroll_bot
                self._scroll_up()
            return True
        if ch == "\t":
            stop = ((self._col // 8) + 1) * 8
            self._col = min(stop, self.cols - 1)
            return True
        if ch == "\b":
            if self._col > 0:
                self._col -= 1
            return True
        if ch in ("\x07", "\x00", "\x18", "\x1a"):  # BEL / NUL / CAN / SUB
            return True
        return False

    # ── C1 8-bit controls (optional — sent by some terminals) ─────────────────

    def _handle_c1(self, ch: str) -> bool:
        o = ord(ch)
        if o == 0x9B:   # CSI
            self._state = _CSI
            self._param_buf = self._inter_buf = ""
            return True
        if o == 0x9D:   # OSC
            self._state = _OSC
            self._param_buf = ""
            return True
        if o in (0x90, 0x9E, 0x9F):  # DCS / PM / APC
            self._state = _IGNORE
            return True
        return False

    # ── state machine entry point ──────────────────────────────────────────────

    def _process(self, ch: str) -> None:  # noqa: C901 (complexity — it's a state machine)
        state = self._state

        # ── NORMAL ──────────────────────────────────────────────────────────
        if state == _NORMAL:
            if ch == "\x1b":
                self._state = _ESC
                return
            if self._handle_c0(ch):
                return
            if 0x80 <= ord(ch) <= 0x9F and self._handle_c1(ch):
                return
            if ch >= " ":
                self._write_char(ch)
            return

        # ── ESC ─────────────────────────────────────────────────────────────
        if state == _ESC:
            if ch == "[":
                self._state = _CSI
                self._param_buf = self._inter_buf = ""
            elif ch == "]":
                self._state = _OSC
                self._param_buf = ""
            elif ch in ("P", "^", "_"):
                self._state = _IGNORE
            elif ch in ("(", ")"):
                self._state = _CHARSET
            elif ch == "7":
                self._saved_cursor = (self._row, self._col)
                self._state = _NORMAL
            elif ch == "8":
                row, col = self._saved_cursor
                self._row = max(0, min(self.rows - 1, row))
                self._col = max(0, min(self.cols - 1, col))
                self._state = _NORMAL
            elif ch == "M":                             # RI reverse index
                if self._row == self._scroll_top:
                    self._scroll_down()
                else:
                    self._row = max(0, self._row - 1)
                self._state = _NORMAL
            elif ch == "E":                             # NEL next line
                self._col = 0
                self._row += 1
                if self._row > self._scroll_bot:
                    self._row = self._scroll_bot
                    self._scroll_up()
                self._state = _NORMAL
            elif ch == "c":                             # RIS full reset
                self._init_state()
            else:
                # Ignore: =, >, <, H (tab-stop), N, O, etc.
                self._state = _NORMAL
            return

        # ── CSI ─────────────────────────────────────────────────────────────
        if state == _CSI:
            # First byte: check for DEC private prefix
            if ch in ("?", ">", "<", "=") and not self._param_buf and not self._inter_buf:
                self._state = _CSIPV
                self._param_buf = ch
                return
            # Intermediate bytes 0x20–0x2F
            if "\x20" <= ch <= "\x2f":
                self._inter_buf += ch
                return
            # Parameter bytes 0x30–0x3F
            if "\x30" <= ch <= "\x3f":
                self._param_buf += ch
                return
            # Final byte 0x40–0x7E
            if "\x40" <= ch <= "\x7e":
                self._dispatch_csi(self._param_buf, ch)
                self._state = _NORMAL
            return

        # ── CSI private (ESC [ ? …) ──────────────────────────────────────────
        if state == _CSIPV:
            if "\x30" <= ch <= "\x3f":
                self._param_buf += ch
                return
            if "\x40" <= ch <= "\x7e":
                self._dispatch_csi_private(self._param_buf, ch)
                self._state = _NORMAL
            return

        # ── OSC ─────────────────────────────────────────────────────────────
        if state == _OSC:
            if ch == "\x07":                            # BEL terminates OSC
                self._state = _NORMAL
            elif ch == "\x1b":
                self._state = _ESC                      # will consume ST backslash
            # else: accumulate and ignore
            return

        # ── IGNORE (DCS / PM / APC) ──────────────────────────────────────────
        if state == _IGNORE:
            if ch == "\x07":
                self._state = _NORMAL
            elif ch == "\x1b":
                self._state = _ESC                      # consume ESC of ST
            return

        # ── CHARSET — consume designator byte and return ──────────────────────
        if state == _CHARSET:
            self._state = _NORMAL

    # ── CSI dispatch ──────────────────────────────────────────────────────────

    def _params(self, buf: str, count: int, default: int) -> List[int]:
        """Parse semicolon-separated CSI parameter string into a fixed-length list."""
        parts = buf.split(";") if buf else []
        result: List[int] = []
        for i in range(count):
            raw = parts[i] if i < len(parts) else ""
            try:
                result.append(int(raw) if raw else default)
            except ValueError:
                result.append(default)
        return result

    def _dispatch_csi(self, buf: str, final: str) -> None:  # noqa: C901
        p = self._params(buf, 8, 0)
        # Helper: treat 0 as 1 for movement/count commands
        p1 = p[0] if p[0] > 0 else 1

        if final == "A":                                # CUU — cursor up
            self._row = max(self._scroll_top, self._row - p1)

        elif final == "B":                              # CUD — cursor down
            self._row = min(self._scroll_bot, self._row + p1)

        elif final == "C":                              # CUF — cursor right
            self._col = min(self.cols - 1, self._col + p1)

        elif final == "D":                              # CUB — cursor left
            self._col = max(0, self._col - p1)

        elif final == "E":                              # CNL — cursor next line
            self._row = min(self._scroll_bot, self._row + p1)
            self._col = 0

        elif final == "F":                              # CPL — cursor prev line
            self._row = max(self._scroll_top, self._row - p1)
            self._col = 0

        elif final == "G":                              # CHA — horizontal absolute
            self._col = max(0, min(self.cols - 1, (p[0] - 1) if p[0] > 0 else 0))

        elif final in ("H", "f"):                       # CUP — cursor position (1-based)
            row = max(1, p[0]) - 1
            col = max(1, p[1]) - 1 if len(p) > 1 else 0
            self._row = max(0, min(self.rows - 1, row))
            self._col = max(0, min(self.cols - 1, col))

        elif final == "J":                              # ED — erase display
            n = p[0]
            if n == 0:       # erase below (cursor inclusive)
                for c in range(self._col, self.cols):
                    self._grid[self._row][c] = " "
                for r in range(self._row + 1, self.rows):
                    self._grid[r] = self._blank_row()
            elif n == 1:     # erase above (cursor inclusive)
                for c in range(0, self._col + 1):
                    self._grid[self._row][c] = " "
                for r in range(0, self._row):
                    self._grid[r] = self._blank_row()
            elif n == 2:     # erase all visible
                self._grid = self._blank_grid()
            elif n == 3:     # erase all + scrollback
                self._grid = self._blank_grid()
                self._scrollback.clear()
                self._delta_mark = 0

        elif final == "K":                              # EL — erase line
            n = p[0]
            if n == 0:       # erase to end of line
                for c in range(self._col, self.cols):
                    self._grid[self._row][c] = " "
            elif n == 1:     # erase to beginning of line
                for c in range(0, self._col + 1):
                    self._grid[self._row][c] = " "
            elif n == 2:     # erase whole line
                self._grid[self._row] = self._blank_row()

        elif final == "L":                              # IL — insert lines
            n = max(1, p[0])
            for _ in range(n):
                if self._row <= self._scroll_bot:
                    del self._grid[self._scroll_bot]
                    self._grid.insert(self._row, self._blank_row())

        elif final == "M":                              # DL — delete lines
            n = max(1, p[0])
            for _ in range(n):
                if self._row <= self._scroll_bot:
                    del self._grid[self._row]
                    self._grid.insert(self._scroll_bot, self._blank_row())

        elif final == "P":                              # DCH — delete chars
            n = max(1, p[0])
            row = self._grid[self._row]
            del row[self._col: self._col + n]
            row.extend([" "] * n)

        elif final == "S":                              # SU — scroll up
            self._scroll_up(max(1, p[0]))

        elif final == "T":                              # SD — scroll down
            self._scroll_down(max(1, p[0]))

        elif final == "X":                              # ECH — erase chars
            n = max(1, p[0])
            for c in range(self._col, min(self._col + n, self.cols)):
                self._grid[self._row][c] = " "

        elif final == "@":                              # ICH — insert blank chars
            n = max(1, p[0])
            row = self._grid[self._row]
            row[self._col:self._col] = [" "] * n
            del row[self.cols:]                         # trim to width

        elif final == "d":                              # VPA — line position absolute
            self._row = max(0, min(self.rows - 1, max(1, p[0]) - 1))

        elif final == "r":                              # DECSTBM — set scrolling region
            top = max(1, p[0]) - 1
            bot = (max(1, p[1]) - 1) if p[1] > 0 else self.rows - 1
            if top < bot:
                self._scroll_top = max(0, min(self.rows - 1, top))
                self._scroll_bot = max(0, min(self.rows - 1, bot))
            self._row, self._col = 0, 0                 # VT100: cursor to home

        elif final == "s":                              # SCP — save cursor (ANSI)
            self._saved_cursor = (self._row, self._col)

        elif final == "u":                              # RCP — restore cursor (ANSI)
            r, c = self._saved_cursor
            self._row = max(0, min(self.rows - 1, r))
            self._col = max(0, min(self.cols - 1, c))

        elif final == "m":                              # SGR — select graphic rendition
            pass                                        # colours/attrs ignored cleanly

        # Any unrecognised final byte: silently ignored.

    def _dispatch_csi_private(self, buf: str, final: str) -> None:
        """Handle CSI ? … sequences (DEC private modes)."""
        code_str = buf.lstrip("?><= ")
        try:
            code = int(code_str) if code_str else 0
        except ValueError:
            return

        if code in (47, 1047, 1049):                    # alternate screen
            if final == "h":
                self._enter_alt()
            elif final == "l":
                self._leave_alt()
        # All other DEC private modes (mouse tracking, bracketed paste, …): ignored.

    # ── alternate screen ──────────────────────────────────────────────────────

    def _enter_alt(self) -> None:
        if self._in_alt:
            return
        # Save main screen
        self._alt_grid          = self._grid
        self._alt_scrollback    = self._scrollback
        self._alt_saved_cursor  = (self._row, self._col)
        # Switch to blank alternate screen
        self._grid              = self._blank_grid()
        self._scrollback        = []
        self._row               = 0
        self._col               = 0
        self._scroll_top        = 0
        self._scroll_bot        = self.rows - 1
        self._in_alt            = True

    def _leave_alt(self) -> None:
        if not self._in_alt:
            return
        # Restore main screen
        self._grid           = self._alt_grid         or self._blank_grid()
        self._scrollback     = self._alt_scrollback   or []
        r, c                 = self._alt_saved_cursor or (0, 0)
        self._row            = max(0, min(self.rows - 1, r))
        self._col            = max(0, min(self.cols - 1, c))
        self._alt_grid       = None
        self._alt_scrollback = None
        self._alt_saved_cursor = None
        self._in_alt         = False
