"""
Cleans up the raw signal and spots blinks.

Smoothing, blink detection and quality checks live here. What to do about
the result -- drive, stop, turn -- is command_mapper's job, so thresholds can
be retuned without touching detection code.

No I/O and no threads: give it samples and a time, get state back.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional

from eeg_sources import EEGSample


class BlinkEvent(str, Enum):
    """A qualified, debounced blink gesture."""

    SINGLE = "SINGLE"
    DOUBLE = "DOUBLE"


@dataclass
class ProcessedSignal:
    """The conditioned view of the user's state at one instant."""

    timestamp: float
    connected: bool = False
    raw_attention: Optional[int] = None
    attention: Optional[float] = None
    meditation: Optional[int] = None
    poor_signal: int = 200
    quality_ok: bool = False
    window_filled: bool = False
    blink_events: List[BlinkEvent] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when the attention value may be used for driving decisions."""
        return self.connected and self.quality_ok and self.attention is not None


@dataclass
class ProcessorStats:
    """Counters surfaced on the dashboard and in the QA report."""

    samples_ingested: int = 0
    blinks_qualified: int = 0
    blinks_rejected_threshold: int = 0
    blinks_rejected_debounce: int = 0
    double_blinks: int = 0
    poor_quality_samples: int = 0
    disconnects: int = 0


class SignalProcessor:
    """Smooths attention and turns blink strengths into discrete gestures."""

    def __init__(
        self,
        attention_window: int = 5,
        blink_strength_threshold: int = 150,
        blink_debounce_ms: int = 300,
        double_blink_window_ms: int = 500,
        poor_signal_cutoff: int = 25,
        classify_double: bool = False,
        blink_from_raw: bool = False,
        raw_amplitude_threshold: int = 300,
        raw_refractory_ms: int = 400,
    ) -> None:
        if attention_window < 1:
            raise ValueError("attention_window must be >= 1")

        self.attention_window = attention_window
        self.blink_strength_threshold = blink_strength_threshold
        self.blink_debounce_s = blink_debounce_ms / 1000.0
        self.double_blink_window_s = double_blink_window_ms / 1000.0
        self.poor_signal_cutoff = poor_signal_cutoff
        # Only defer classification when the extra latency buys something:
        # in "alternate" blink mode every blink acts immediately.
        self.classify_double = classify_double
        self.blink_from_raw = blink_from_raw
        self.raw_amplitude_threshold = raw_amplitude_threshold
        self.raw_refractory_s = raw_refractory_ms / 1000.0

        self.stats = ProcessorStats()

        self._window: Deque[int] = deque(maxlen=attention_window)
        self._connected = False
        self._raw_attention: Optional[int] = None
        self._meditation: Optional[int] = None
        self._poor_signal = 200
        self._last_blink_time: Optional[float] = None
        self._last_raw_blink_time: Optional[float] = None
        self._pending_blink_time: Optional[float] = None
        self._ready_events: List[BlinkEvent] = []

    # -- input --------------------------------------------------------------

    def ingest(self, sample: EEGSample) -> None:
        """Fold one sample into the running state."""
        self.stats.samples_ingested += 1

        if not sample.connected:
            self._on_disconnect()
            return

        if not self._connected:
            self._connected = True

        self._poor_signal = sample.poor_signal
        if sample.poor_signal > self.poor_signal_cutoff:
            self.stats.poor_quality_samples += 1

        if sample.attention is not None:
            self._raw_attention = sample.attention
            self._window.append(sample.attention)
        if sample.meditation is not None:
            self._meditation = sample.meditation

        if sample.has_blink:
            self._register_blink(sample.timestamp, sample.blink_strength)

        if self.blink_from_raw and sample.raw:
            self._scan_raw_for_blink(sample)

    def _on_disconnect(self) -> None:
        """Drop stale state so a reconnect never drives on old values."""
        if self._connected:
            self.stats.disconnects += 1
        self._connected = False
        self._window.clear()
        self._raw_attention = None
        self._poor_signal = 200
        self._pending_blink_time = None

    # -- blink detection ----------------------------------------------------

    def _register_blink(self, timestamp: float, strength: Optional[int]) -> None:
        """Apply (threshold) and (debounce) to a blink row."""
        if strength is None or strength < self.blink_strength_threshold:
            self.stats.blinks_rejected_threshold += 1
            return
        self._accept_blink(timestamp)

    def _accept_blink(self, timestamp: float) -> None:
        if (
            self._last_blink_time is not None
            and timestamp - self._last_blink_time < self.blink_debounce_s
        ):
            self.stats.blinks_rejected_debounce += 1
            return

        self.stats.blinks_qualified += 1

        if not self.classify_double:
            # Lowest-latency path: every blink is an event on its own.
            self._last_blink_time = timestamp
            self._ready_events.append(BlinkEvent.SINGLE)
            return

        if self._pending_blink_time is not None:
            if timestamp - self._pending_blink_time <= self.double_blink_window_s:
                # the pair collapses into one DOUBLE gesture.
                self._pending_blink_time = None
                self._last_blink_time = timestamp
                self.stats.double_blinks += 1
                self._ready_events.append(BlinkEvent.DOUBLE)
                return
            # The earlier blink's window closed before this one
            # arrived, so it was a single after all. Emit it now rather
            # than lose it, since tick() may not have run in between.
            self._ready_events.append(BlinkEvent.SINGLE)

        self._pending_blink_time = timestamp
        self._last_blink_time = timestamp

    def _scan_raw_for_blink(self, sample: EEGSample) -> None:
        """Fallback detector: a large raw-wave excursion is a blink artefact.

        Only used when ``signal_processing.blink_from_raw.enabled`` is set,
        for headsets or firmware revisions that do not emit 0x16 rows. The
        amplitude test replaces the strength test, then the normal debounce
        applies.
        """
        peak = max((abs(value) for value in sample.raw), default=0)
        if peak < self.raw_amplitude_threshold:
            return
        if (
            self._last_raw_blink_time is not None
            and sample.timestamp - self._last_raw_blink_time < self.raw_refractory_s
        ):
            return
        self._last_raw_blink_time = sample.timestamp
        self._accept_blink(sample.timestamp)

    # -- output -------------------------------------------------------------

    def tick(self, now: float) -> ProcessedSignal:
        """Return the conditioned state, including any matured blink events.

        Called once per main-loop cycle after all pending samples have been
        ingested.
        """
        events = self._collect_events(now)

        return ProcessedSignal(
            timestamp=now,
            connected=self._connected,
            raw_attention=self._raw_attention,
            attention=self.smoothed_attention,
            meditation=self._meditation,
            poor_signal=self._poor_signal,
            quality_ok=self._connected
            and self._poor_signal <= self.poor_signal_cutoff,
            window_filled=len(self._window) >= self.attention_window,
            blink_events=events,
        )

    def _collect_events(self, now: float) -> List[BlinkEvent]:
        events, self._ready_events = self._ready_events, []
        if (
            self.classify_double
            and self._pending_blink_time is not None
            and now - self._pending_blink_time > self.double_blink_window_s
        ):
            # The window closed with no partner blink, so it was a single.
            self._pending_blink_time = None
            events.append(BlinkEvent.SINGLE)
        return events

    @property
    def smoothed_attention(self) -> Optional[float]:
        """: rolling mean over the last N attention values."""
        if not self._window:
            return None
        return sum(self._window) / len(self._window)

    @property
    def attention_window_values(self) -> List[int]:
        """The current window contents (used by the calibration routine)."""
        return list(self._window)

    def reset(self) -> None:
        """Clear all running state but keep the configuration and counters."""
        self._window.clear()
        self._connected = False
        self._raw_attention = None
        self._meditation = None
        self._poor_signal = 200
        self._last_blink_time = None
        self._last_raw_blink_time = None
        self._pending_blink_time = None
        self._ready_events.clear()


def create_processor(config) -> SignalProcessor:
    """Build a :class:`SignalProcessor` from the configuration tree."""
    section = config.section("signal_processing")
    raw = section.get("blink_from_raw", {}) or {}
    return SignalProcessor(
        attention_window=section.get("attention_window", 5),
        blink_strength_threshold=section.get("blink_strength_threshold", 150),
        blink_debounce_ms=section.get("blink_debounce_ms", 300),
        double_blink_window_ms=section.get("double_blink_window_ms", 500),
        poor_signal_cutoff=section.get("poor_signal_cutoff", 25),
        classify_double=config.get("control.blink_mode") == "single_double",
        blink_from_raw=raw.get("enabled", False),
        raw_amplitude_threshold=raw.get("amplitude_threshold", 300),
        raw_refractory_ms=raw.get("refractory_ms", 400),
    )
