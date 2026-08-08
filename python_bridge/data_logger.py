"""
Logging and session recording.

Requirement coverage:
    EEG-04  Every EEG value is logged with a timestamp for debugging.
    NFR 3.5 Log destinations come from ``config.json``.

Two independent outputs:

``neurodrive.log``
    Standard Python logging (events, warnings, connection changes). Written
    to file always; echoed to the console only when
    ``logging.console_log`` is set, because the dashboard owns the terminal.

``session_<timestamp>.csv``
    One row per main-loop cycle. This is the file M7 uses for latency and
    reliability analysis, and the file ``ReplaySource`` plays back for the
    fallback demo -- so the column names here and in
    ``eeg_sources.ReplaySource`` must stay in step.
"""

from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime
from typing import Iterable, Optional

#: Columns of the session CSV. ReplaySource reads elapsed_s / attention /
#: meditation / poor_signal / blink_strength; the rest is analysis material.
CSV_COLUMNS = [
    "wall_clock",
    "elapsed_s",
    "attention",
    "smoothed_attention",
    "meditation",
    "poor_signal",
    "blink_strength",
    "blink_event",
    "quality_ok",
    "connected",
    "command",
    "reason",
]


def setup_logging(config, run_id: str) -> str:
    """Configure the root ``neurodrive`` logger. Returns the log file path."""
    directory = _resolve_dir(config.get("logging.dir", "logs"))
    path = os.path.join(directory, f"neurodrive_{run_id}.log")

    level = getattr(logging, str(config.get("logging.level", "INFO")).upper(), logging.INFO)
    logger = logging.getLogger("neurodrive")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s")
    )
    logger.addHandler(file_handler)

    if config.get("logging.console_log", False):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
        logger.addHandler(console)

    return path


def _resolve_dir(directory: str) -> str:
    if not os.path.isabs(directory):
        directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), directory)
    os.makedirs(directory, exist_ok=True)
    return directory


def make_run_id() -> str:
    """A filename-safe stamp shared by the log file and the session CSV."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class SessionRecorder:
    """Appends one CSV row per cycle; safe to use when disabled."""

    def __init__(self, config, run_id: str, enabled: Optional[bool] = None) -> None:
        if enabled is None:
            enabled = bool(config.get("logging.csv_data_log", True))
        self.enabled = enabled
        self.path: Optional[str] = None
        self._handle = None
        self._writer = None
        self._start = time.monotonic()
        self._rows = 0

        if self.enabled:
            directory = _resolve_dir(config.get("logging.dir", "logs"))
            self.path = os.path.join(directory, f"session_{run_id}.csv")
            self._handle = open(self.path, "w", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._handle, fieldnames=CSV_COLUMNS)
            self._writer.writeheader()

    @property
    def rows_written(self) -> int:
        return self._rows

    def log_cycle(
        self,
        processed,
        command,
        reason: str,
        samples: Iterable = (),
    ) -> None:
        """Record one main-loop cycle.

        ``samples`` is the batch of raw :class:`~eeg_sources.EEGSample`
        objects consumed this cycle; the strongest blink in the batch is
        preserved so a recording can be replayed faithfully.
        """
        if not self.enabled or self._writer is None:
            return

        blink_strengths = [
            sample.blink_strength for sample in samples if sample.has_blink
        ]
        smoothed = processed.attention

        self._writer.writerow(
            {
                "wall_clock": datetime.now().isoformat(timespec="milliseconds"),
                "elapsed_s": f"{time.monotonic() - self._start:.3f}",
                "attention": "" if processed.raw_attention is None else processed.raw_attention,
                "smoothed_attention": "" if smoothed is None else f"{smoothed:.2f}",
                "meditation": "" if processed.meditation is None else processed.meditation,
                "poor_signal": processed.poor_signal,
                "blink_strength": max(blink_strengths) if blink_strengths else "",
                "blink_event": ",".join(e.value for e in processed.blink_events),
                "quality_ok": int(processed.quality_ok),
                "connected": int(processed.connected),
                "command": getattr(command, "value", command),
                "reason": reason,
            }
        )
        self._rows += 1
        if self._rows % 100 == 0:  # bound data loss if the process is killed
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                self._handle.close()
            finally:
                self._handle = None
                self._writer = None

    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
