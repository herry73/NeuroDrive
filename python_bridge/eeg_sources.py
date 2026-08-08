"""
EEG acquisition sources.

Requirement coverage:
    EEG-01  Bluetooth connection to the MindWave Mobile 2.
    NFR 3.6 The acquisition layer sits behind one small interface
            (:class:`EEGSource`) so the headset can be swapped for another
            device -- or for a simulator -- without touching the rest of the
            application.

Three sources ship with the project:

    ``SerialThinkGearSource``  real headset over a Bluetooth SPP serial port
    ``MockSource``             synthetic signal, no hardware needed
    ``ReplaySource``           plays back a CSV recorded by ``data_logger.py``

``MockSource`` synthesises genuine ThinkGear packets rather than fabricating
samples directly, so running in mock mode still exercises the real parser.
"""

from __future__ import annotations

import csv
import math
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from thinkgear import (
    ThinkGearParser,
    build_blink_packet,
    build_esense_packet,
    build_raw_packet,
)


class EEGConnectionError(RuntimeError):
    """Raised when a source cannot be opened or its link drops."""


@dataclass
class EEGSample:
    """One decoded observation of the user's EEG state.

    ``attention``/``meditation`` update at roughly 1 Hz (the headset's eSense
    rate). ``blink_strength`` is set only on the sample carrying a blink
    event, and is ``None`` otherwise -- callers must not treat a missing
    blink as "strength 0".
    """

    timestamp: float
    attention: Optional[int] = None
    meditation: Optional[int] = None
    poor_signal: int = 200
    blink_strength: Optional[int] = None
    raw: List[int] = field(default_factory=list)
    connected: bool = True

    @property
    def has_blink(self) -> bool:
        return self.blink_strength is not None


class EEGSource(ABC):
    """Minimal interface every acquisition backend implements."""

    #: Short name shown in the console dashboard.
    name = "eeg"

    @abstractmethod
    def open(self) -> None:
        """Establish the link. Raises :class:`EEGConnectionError` on failure."""

    @abstractmethod
    def poll(self) -> List[EEGSample]:
        """Return every sample available since the previous call.

        Must not block for longer than a few hundred milliseconds so the
        reader thread stays responsive (NFR 3.3).
        """

    def close(self) -> None:
        """Release the link. Safe to call when already closed."""


class _ThinkGearByteSource(EEGSource):
    """Shared plumbing for sources that produce a ThinkGear byte stream."""

    def __init__(self) -> None:
        self._parser = ThinkGearParser()
        self._attention: Optional[int] = None
        self._meditation: Optional[int] = None
        self._poor_signal: int = 200
        self._pending_raw: List[int] = []

    @property
    def parser_stats(self):
        return self._parser.stats

    @abstractmethod
    def _read_bytes(self) -> bytes:
        """Return the bytes received since the previous call (may be empty)."""

    def poll(self) -> List[EEGSample]:
        return self._decode(self._read_bytes())

    def _decode(self, data: bytes) -> List[EEGSample]:
        if not data:
            return []

        samples: List[EEGSample] = []
        dirty = False

        for row in self._parser.feed(data):
            if row.name == "blink_strength":
                # Emit blinks immediately and on their own so two blinks in
                # quick succession are never collapsed into one sample.
                samples.append(self._snapshot(blink_strength=int(row.value)))
            elif row.name == "attention":
                self._attention = int(row.value)
                dirty = True
            elif row.name == "meditation":
                self._meditation = int(row.value)
                dirty = True
            elif row.name == "poor_signal":
                self._poor_signal = int(row.value)
                dirty = True
            elif row.name == "raw_wave" and row.value is not None:
                self._pending_raw.append(int(row.value))

        if dirty or self._pending_raw:
            samples.append(self._snapshot())
        return samples

    def _snapshot(self, blink_strength: Optional[int] = None) -> EEGSample:
        raw, self._pending_raw = self._pending_raw, []
        return EEGSample(
            timestamp=time.monotonic(),
            attention=self._attention,
            meditation=self._meditation,
            poor_signal=self._poor_signal,
            blink_strength=blink_strength,
            raw=raw,
            connected=True,
        )


class SerialThinkGearSource(_ThinkGearByteSource):
    """Real MindWave Mobile 2 over a Bluetooth Classic serial port.

    On Windows the headset appears as an outgoing COM port once paired
    ("MindWave Mobile" -> Bluetooth settings -> COM ports). On Linux, bind it
    first with ``sudo rfcomm bind 0 <MAC> 1`` and use ``/dev/rfcomm0``.
    """

    name = "serial"

    def __init__(
        self,
        port: str,
        baudrate: int = 57600,
        read_timeout_s: float = 0.2,
    ) -> None:
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.read_timeout_s = read_timeout_s
        self._serial = None

    def open(self) -> None:
        try:
            import serial  # imported lazily: only needed for real hardware
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EEGConnectionError(
                "pyserial is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.read_timeout_s,
            )
        except Exception as exc:  # serial.SerialException and friends
            raise EEGConnectionError(
                f"cannot open EEG serial port {self.port!r}: {exc}"
            ) from exc
        self._parser.reset()

    def _read_bytes(self) -> bytes:
        if self._serial is None:
            raise EEGConnectionError("serial port is not open")
        try:
            waiting = self._serial.in_waiting
            # read(1) blocks up to read_timeout_s, which paces the reader
            # thread without a sleep when the headset is streaming.
            return self._serial.read(waiting if waiting else 1)
        except Exception as exc:
            raise EEGConnectionError(f"EEG serial read failed: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None


class MockSource(_ThinkGearByteSource):
    """Synthetic headset: no hardware, deterministic, useful for CI and demos.

    Produces a slow attention sweep (so FORWARD/STOP alternate naturally),
    periodic blinks above the trigger threshold, and occasional bursts of
    poor signal quality so the SF-03 gate can be exercised.
    """

    name = "mock"

    def __init__(
        self,
        seed: int = 42,
        blink_interval_s: float = 8.0,
        attention_period_s: float = 20.0,
        emit_raw: bool = False,
        esense_interval_s: float = 1.0,
    ) -> None:
        super().__init__()
        self._rng = random.Random(seed)
        self.blink_interval_s = blink_interval_s
        self.attention_period_s = max(1.0, attention_period_s)
        self.emit_raw = emit_raw
        self.esense_interval_s = esense_interval_s
        self._t0 = 0.0
        self._next_esense = 0.0
        self._next_blink = 0.0
        self._next_raw = 0.0

    def open(self) -> None:
        now = time.monotonic()
        self._t0 = now
        self._next_esense = now
        self._next_blink = now + self.blink_interval_s
        self._next_raw = now
        self._parser.reset()

    def _read_bytes(self) -> bytes:
        now = time.monotonic()
        out = bytearray()

        while self._next_esense <= now:
            elapsed = self._next_esense - self._t0
            phase = 2 * math.pi * elapsed / self.attention_period_s
            attention = 55 + 35 * math.sin(phase) + self._rng.gauss(0, 4)
            meditation = 50 + 20 * math.cos(phase) + self._rng.gauss(0, 4)
            # Roughly one poor-signal burst every ~25 s.
            poor = 0 if self._rng.random() > 0.04 else self._rng.choice([26, 51, 200])
            out += build_esense_packet(
                poor_signal=poor,
                attention=int(max(0, min(100, attention))),
                meditation=int(max(0, min(100, meditation))),
            )
            self._next_esense += self.esense_interval_s

        while self._next_blink <= now:
            out += build_blink_packet(self._rng.randint(140, 230))
            self._next_blink += self.blink_interval_s

        if self.emit_raw:
            # 512 Hz raw wave, generated in catch-up batches.
            period = 1.0 / 512.0
            budget = 0
            while self._next_raw <= now and budget < 512:
                elapsed = self._next_raw - self._t0
                sample = 40 * math.sin(2 * math.pi * 10 * elapsed) + self._rng.gauss(
                    0, 12
                )
                out += build_raw_packet(int(sample))
                self._next_raw += period
                budget += 1
            if self._next_raw < now:  # fell too far behind; resynchronise
                self._next_raw = now

        return bytes(out)


class ReplaySource(EEGSource):
    """Replays a CSV recorded by :mod:`data_logger`, paced in real time.

    This is Fallback Level 2 from the demo strategy: a known-good session can
    drive the vehicle exactly as a live user would, with no headset attached.
    """

    name = "replay"

    #: Columns the recorder writes; extra columns are ignored.
    REQUIRED_COLUMNS = ("elapsed_s", "attention", "poor_signal")

    def __init__(self, csv_path: str, loop: bool = True, speed: float = 1.0) -> None:
        self.csv_path = csv_path
        self.loop = loop
        self.speed = speed if speed > 0 else 1.0
        self._rows: List[dict] = []
        self._index = 0
        self._start = 0.0
        self._lap_offset = 0.0

    def open(self) -> None:
        if not os.path.exists(self.csv_path):
            raise EEGConnectionError(f"replay file not found: {self.csv_path}")
        with open(self.csv_path, "r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            raise EEGConnectionError(f"replay file is empty: {self.csv_path}")
        missing = [c for c in self.REQUIRED_COLUMNS if c not in rows[0]]
        if missing:
            raise EEGConnectionError(
                f"replay file {self.csv_path} is missing column(s): "
                f"{', '.join(missing)}"
            )
        self._rows = rows
        self._index = 0
        self._lap_offset = 0.0
        self._start = time.monotonic()

    def poll(self) -> List[EEGSample]:
        if not self._rows:
            return []

        now = time.monotonic()
        elapsed = (now - self._start) * self.speed
        samples: List[EEGSample] = []

        while self._index < len(self._rows):
            row = self._rows[self._index]
            row_time = self._to_float(row.get("elapsed_s"), 0.0) + self._lap_offset
            if row_time > elapsed:
                break
            self._index += 1
            samples.append(
                EEGSample(
                    timestamp=now,
                    attention=self._to_int(row.get("attention")),
                    meditation=self._to_int(row.get("meditation")),
                    poor_signal=self._to_int(row.get("poor_signal")) or 0,
                    blink_strength=self._to_int(row.get("blink_strength")),
                    connected=True,
                )
            )

        if self._index >= len(self._rows) and self.loop:
            last = self._to_float(self._rows[-1].get("elapsed_s"), 0.0)
            self._lap_offset += last + 1.0
            self._index = 0

        return samples

    @staticmethod
    def _to_int(text) -> Optional[int]:
        if text is None or text == "":
            return None
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(text, default: float) -> float:
        try:
            return float(text)
        except (TypeError, ValueError):
            return default


def create_source(config) -> EEGSource:
    """Build the source named by ``eeg.source`` in the configuration."""
    kind = config.get("eeg.source", "mock")

    if kind == "serial":
        settings = config.section("eeg.serial")
        return SerialThinkGearSource(
            port=settings.get("port", "COM5"),
            baudrate=settings.get("baudrate", 57600),
            read_timeout_s=settings.get("read_timeout_s", 0.2),
        )

    if kind == "mock":
        settings = config.section("eeg.mock")
        return MockSource(
            seed=settings.get("seed", 42),
            blink_interval_s=settings.get("blink_interval_s", 8.0),
            attention_period_s=settings.get("attention_period_s", 20.0),
            emit_raw=settings.get("emit_raw", False),
        )

    if kind == "replay":
        settings = config.section("eeg.replay")
        path = settings.get("csv_path", "logs/recorded_session.csv")
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        return ReplaySource(
            csv_path=path,
            loop=settings.get("loop", True),
            speed=settings.get("speed", 1.0),
        )

    raise ValueError(f"unknown eeg.source: {kind!r}")
