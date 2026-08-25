"""
Non-blocking keyboard input.

Requirement coverage:
    UI-03  Arrow keys manually send commands, for testing without a headset
           and as Fallback Level 1 on demo day (plan section 11.3).

Works on Windows (``msvcrt``) and POSIX (``termios`` + ``select``) with no
third-party dependency. It normalises keys to short names, so the caller
never deals with escape sequences:

    "UP" "DOWN" "LEFT" "RIGHT" "SPACE" "ESC" "ENTER" and single characters.

Use it as a context manager. It restores the terminal on the way out,
including after an exception or a Ctrl-C.
"""

from __future__ import annotations

import os
import sys
from typing import List

IS_WINDOWS = os.name == "nt"

#: Command keys shared by both platforms.
ARROW_UP = "UP"
ARROW_DOWN = "DOWN"
ARROW_LEFT = "LEFT"
ARROW_RIGHT = "RIGHT"


class KeyboardReader:
    """Polls stdin for keypresses without blocking the main loop."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stdin is not None
        self.active = False
        self._fd = None
        self._saved_attrs = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        """Put the terminal into raw/cbreak mode. Returns True on success."""
        if not self.enabled:
            return False
        if IS_WINDOWS:
            try:
                import msvcrt  # noqa: F401
            except ImportError:  # pragma: no cover - not reachable on Windows
                self.enabled = False
                return False
            self.active = True
            return True

        try:  # pragma: no cover - POSIX-only path
            import termios
            import tty

            if not sys.stdin.isatty():
                self.enabled = False
                return False
            self._fd = sys.stdin.fileno()
            self._saved_attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self.active = True
            return True
        except Exception:
            self.enabled = False
            return False

    def stop(self) -> None:
        """Restore the terminal. Safe to call more than once."""
        if not IS_WINDOWS and self._saved_attrs is not None:  # pragma: no cover
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)
            except Exception:
                pass
            self._saved_attrs = None
        self.active = False

    def __enter__(self) -> "KeyboardReader":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- polling ------------------------------------------------------------

    def poll(self) -> List[str]:
        """Return every key pressed since the last call (possibly empty)."""
        if not self.active:
            return []
        return self._poll_windows() if IS_WINDOWS else self._poll_posix()

    def _poll_windows(self) -> List[str]:
        import msvcrt

        keys: List[str] = []
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                # Extended key: the scan code follows.
                if not msvcrt.kbhit():
                    break
                code = msvcrt.getwch()
                mapped = {
                    "H": ARROW_UP,
                    "P": ARROW_DOWN,
                    "K": ARROW_LEFT,
                    "M": ARROW_RIGHT,
                }.get(code)
                if mapped:
                    keys.append(mapped)
                continue
            keys.append(self._normalise(char))
        return keys

    def _poll_posix(self) -> List[str]:  # pragma: no cover - POSIX-only path
        import select

        keys: List[str] = []
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                break
            char = sys.stdin.read(1)
            if not char:
                break
            if char != "\x1b":
                keys.append(self._normalise(char))
                continue

            # Possible arrow key: ESC [ A|B|C|D
            ready, _, _ = select.select([sys.stdin], [], [], 0.02)
            if not ready:
                keys.append("ESC")
                continue
            bracket = sys.stdin.read(1)
            if bracket != "[":
                keys.append("ESC")
                keys.append(self._normalise(bracket))
                continue
            ready, _, _ = select.select([sys.stdin], [], [], 0.02)
            if not ready:
                keys.append("ESC")
                continue
            code = sys.stdin.read(1)
            mapped = {
                "A": ARROW_UP,
                "B": ARROW_DOWN,
                "D": ARROW_LEFT,
                "C": ARROW_RIGHT,
            }.get(code)
            if mapped:
                keys.append(mapped)
        return keys

    @staticmethod
    def _normalise(char: str) -> str:
        if char == " ":
            return "SPACE"
        if char in ("\r", "\n"):
            return "ENTER"
        if char == "\x1b":
            return "ESC"
        if char == "\x03":
            return "CTRL_C"
        return char.lower()


#: Keys that map straight onto a vehicle command name.
KEY_COMMANDS = {
    ARROW_UP: "FORWARD",
    "w": "FORWARD",
    ARROW_LEFT: "LEFT",
    "a": "LEFT",
    ARROW_RIGHT: "RIGHT",
    "d": "RIGHT",
    ARROW_DOWN: "STOP",
    "s": "STOP",
    "SPACE": "STOP",
}
