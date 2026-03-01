"""ANSI escape code stripper for clean LLM output."""

import re

# Covers:
# - CSI sequences: colors, bold, cursor movement, screen clear  (\x1b[...X)
# - OSC sequences: terminal title, hyperlinks                   (\x1b]...\x07 or \x1b]...\x1b\\)
# - Character set selection                                     (\x1b(B)
# - Keypad mode                                                 (\x1b= / \x1b>)
# - Bare carriage returns from progress bars                    (\r not followed by \n)
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'       # CSI sequences
    r'|\x1b\][^\x07]*(?:\x07|\x1b\\)'  # OSC sequences
    r'|\x1b\(B'                     # Character set
    r'|\x1b[=>]'                    # Keypad mode
    r'|\r(?!\n)'                    # Bare \r (progress bar overwrites)
)


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape codes from text, returning clean plaintext."""
    return _ANSI_RE.sub('', text)
