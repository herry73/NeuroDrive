"""
Live console dashboard.

Requirement coverage:
    EEG-06  Display live attention / blink values.
    UI-01   Show connection status, EEG values and the current command.

No dependencies, on purpose. It redraws a fixed block of lines in place
using ANSI escapes, which works in Windows Terminal, PowerShell 7, VS Code
and every POSIX terminal. If the output is not a TTY (piped to a file, run
under CI) it degrades to plain periodic lines instead of scribbling escape
codes into the capture.

This exists for plan section 12.3, "make the invisible visible". The
attention bar on a projector is what lets an audience follow what the
operator's brain is doing.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import List, Optional

CSI = "\x1b["
RESET = "\x1b[0m"

COLOURS = {
    "grey": "\x1b[90m",
    "red": "\x1b[91m",
    "green": "\x1b[92m",
    "yellow": "\x1b[93m",
    "blue": "\x1b[94m",
    "magenta": "\x1b[95m",
    "cyan": "\x1b[96m",
    "white": "\x1b[97m",
    "bold": "\x1b[1m",
}

COMMAND_COLOURS = {
    "FORWARD": "green",
    "LEFT": "yellow",
    "RIGHT": "yellow",
    "STOP": "red",
}

STATUS_COLOURS = {
    "CONNECTED": "green",
    "CONNECTING": "yellow",
    "SIGNAL_LOST": "red",
    "FAILED": "red",
    "IDLE": "grey",
    "STOPPED": "grey",
}


#: Glyphs used by the dashboard, and their ASCII stand-ins. Redirecting
#: output on Windows drops stdout to the locale codepage (cp1252), which
#: cannot encode box-drawing characters. Without a fallback, piping the
#: bridge to a file kills the process mid-run.
GLYPHS_UNICODE = {
    "bar_full": "█",
    "bar_empty": "·",
    "corner_top": "┌",
    "corner_bottom": "└",
    "rule": "─",
}

GLYPHS_ASCII = {
    "bar_full": "#",
    "bar_empty": ".",
    "corner_top": "+",
    "corner_bottom": "+",
    "rule": "-",
}


def supports_unicode(stream=None) -> bool:
    """True if the output encoding can represent the dashboard's glyphs."""
    stream = stream if stream is not None else sys.stdout
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "".join(GLYPHS_UNICODE.values()).encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def enable_ansi() -> bool:
    """Turn on virtual-terminal processing. Returns True if ANSI is usable."""
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:  # pragma: no cover - Windows-only path
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Dashboard:
    """Renders the bridge state as a redrawing block of text."""

    def __init__(self, enabled: bool = True, colour: bool = True, refresh_hz: float = 10.0) -> None:
        self.enabled = enabled
        self.interactive = enable_ansi()
        self.colour = colour and self.interactive
        self.glyphs = GLYPHS_UNICODE if supports_unicode() else GLYPHS_ASCII
        self.min_interval = 1.0 / max(1.0, refresh_hz)
        self._last_render = 0.0
        self._lines_drawn = 0
        self._notice = ""
        self._notice_until = 0.0

    # -- helpers ------------------------------------------------------------

    def _c(self, text: str, colour: Optional[str]) -> str:
        if not self.colour or not colour:
            return text
        return f"{COLOURS.get(colour, '')}{text}{RESET}"

    def notify(self, message: str, seconds: float = 3.0) -> None:
        """Flash a transient message on the status line."""
        self._notice = message
        self._notice_until = time.monotonic() + seconds

    def message(self, text: str) -> None:
        """Print a line above the dashboard (startup / shutdown notices)."""
        self._teardown_block()
        print(text, flush=True)

    # -- rendering ----------------------------------------------------------

    def render(
        self,
        reader_info,
        processed,
        mapper_state,
        sender_stats,
        transport_description: str,
        elapsed_s: float,
        loop_hz: float,
        override_active: bool,
        vision_info=None,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last_render < self.min_interval:
            return
        self._last_render = now

        lines = self._build_lines(
            reader_info,
            processed,
            mapper_state,
            sender_stats,
            transport_description,
            elapsed_s,
            loop_hz,
            override_active,
            now,
            vision_info,
        )
        self._draw(lines)

    def _build_lines(
        self,
        reader_info,
        processed,
        mapper_state,
        sender_stats,
        transport_description,
        elapsed_s,
        loop_hz,
        override_active,
        now,
        vision_info=None,
    ) -> List[str]:
        width = min(shutil.get_terminal_size((100, 24)).columns, 100)
        rule = self.glyphs["rule"] * max(20, width - 2)

        status = reader_info.status.value
        status_text = self._c(f"{status:<12}", STATUS_COLOURS.get(status, "white"))

        attention = processed.attention
        attention_text = "--" if attention is None else f"{attention:5.1f}"
        raw_text = "--" if processed.raw_attention is None else f"{processed.raw_attention:3d}"

        quality = "OK " if processed.quality_ok else "POOR"
        quality_text = self._c(quality, "green" if processed.quality_ok else "red")

        command = mapper_state.command.value
        command_text = self._c(
            f"{command:<8}", COMMAND_COLOURS.get(command, "white")
        )

        rtt = sender_stats.last_rtt_ms
        avg_rtt = sender_stats.avg_rtt_ms
        rtt_text = "--" if rtt is None else f"{rtt:5.1f}ms"
        avg_text = "--" if avg_rtt is None else f"{avg_rtt:5.1f}ms"

        mode = self._c("KEYBOARD", "magenta") if override_active else self._c("EEG", "cyan")
        notice = self._notice if now < self._notice_until else ""

        return [
            self._c(
                f"{self.glyphs['corner_top']}{self.glyphs['rule']} NeuroDrive Bridge "
                f"{rule[:max(0, width - 22)]}",
                "bold",
            ),
            f"  EEG      {status_text} src={reader_info.source_name:<8} "
            f"samples={reader_info.samples_received:<6} drops={reader_info.samples_dropped}",
            f"  Signal   quality {quality_text} (poor={processed.poor_signal:>3})   "
            f"attention {attention_text} (raw {raw_text})  med "
            f"{'--' if processed.meditation is None else processed.meditation:>3}",
            f"  {self._attention_bar(attention, mapper_state, width)}",
            f"  Command  {command_text} {self._c(mapper_state.reason[:max(10, width - 22)], 'grey')}",
            f"  Link     {transport_description:<26} sent={sender_stats.packets_sent:<6} "
            f"ack={sender_stats.acks_received:<6} rtt={rtt_text} avg={avg_text}",
            f"  Session  mode={mode}  up={self._duration(elapsed_s)}  loop={loop_hz:4.1f}Hz  "
            f"turns={mapper_state.turns_issued}  safe-stops={mapper_state.safe_stops}",
            *self._vision_line(vision_info),
            f"  Keys     {self._c('up', 'white')}=fwd {self._c('left/right', 'white')}=turn "
            f"{self._c('down/space', 'white')}=stop  k=override  c=recalibrate  q=quit"
            + (f"   {self._c(notice, 'yellow')}" if notice else ""),
            self._c(
                self.glyphs["corner_bottom"] + rule[: max(0, width - 2)], "bold"
            ),
        ]

    def _vision_line(self, info) -> List[str]:
        """One row for the camera, or nothing at all when it is switched off.

        Returned as a list so the caller can splat it: a disabled camera adds
        no row rather than an empty one.
        """
        if info is None or not getattr(info, "enabled", False):
            return []

        if not info.running:
            label, colour = "OFFLINE", "red"
            detail = info.last_error[:40] or "starting"
        elif info.raised:
            label, colour = f"{info.raised} HAND", "green"
            detail = "raised"
        elif info.pose_frames:
            label, colour = "WATCHING", "cyan"
            detail = "no hand raised"
        else:
            label, colour = "NO USER", "yellow"
            detail = "step into the frame"

        # Pad inside the colour call: the escape codes are invisible on screen
        # but very much visible to a format spec's width count.
        return [
            f"  Vision   {self._c(f'{label:<12}', colour)} "
            f"{self._c(f'{detail:<22}', 'grey')} "
            f"gestures={info.gestures:<4} {info.fps:4.1f}fps"
        ]

    def _attention_bar(self, attention, mapper_state, width: int) -> str:
        """A 0-100 bar coloured by the command it is currently producing."""
        span = max(20, min(60, width - 30))
        filled = 0 if attention is None else int(round(attention / 100.0 * span))
        filled = max(0, min(span, filled))
        bar = (
            self.glyphs["bar_full"] * filled
            + self.glyphs["bar_empty"] * (span - filled)
        )

        colour = "grey"
        if attention is not None:
            command = mapper_state.command.value
            colour = COMMAND_COLOURS.get(command, "grey")
        value = "  --" if attention is None else f"{attention:4.0f}"
        return f"Attention [{self._c(bar, colour)}] {value}/100"

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = int(seconds)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    # -- terminal handling --------------------------------------------------

    def _draw(self, lines: List[str]) -> None:
        if not self.interactive:
            # Non-TTY: emit a single compact line so logs stay readable.
            print(" | ".join(line.strip() for line in lines[1:-1]), flush=True)
            return

        out = []
        if self._lines_drawn:
            out.append(f"{CSI}{self._lines_drawn}A")  # cursor up
        for line in lines:
            out.append(f"\r{CSI}K{line}\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._lines_drawn = len(lines)

    def _teardown_block(self) -> None:
        self._lines_drawn = 0

    def close(self) -> None:
        """Leave the cursor below the dashboard so the shell prompt is clean."""
        if self.enabled and self.interactive and self._lines_drawn:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self._lines_drawn = 0


def print_banner(config, transport_description: str, log_path: str) -> None:
    """One-time startup summary printed above the dashboard."""
    print()
    print("  NeuroDrive - EEG-controlled vehicle bridge")
    print("  " + "-" * 52)
    print(f"  EEG source        : {config.get('eeg.source')}")
    print(f"  Transport         : {transport_description}")
    print(f"  Forward / stop    : {config.get('control.attention_forward_threshold')}"
          f" / {config.get('control.attention_stop_threshold')} attention")
    print(f"  Blink threshold   : {config.get('signal_processing.blink_strength_threshold')}"
          f" ({config.get('control.blink_mode')} mode)")
    print(f"  Log file          : {log_path}")
    print("  " + "-" * 52)
    print()
