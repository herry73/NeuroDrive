"""
Startup calibration.

Requirement coverage:
    UI-02   A calibration phase (default 15 s) during which the vehicle does
            not move.
    NFR 3.8 The whole startup procedure completes in under 60 seconds.

What it actually does: holds the mapper disarmed, watches the incoming
attention values, and reports what it saw. The measured baseline is turned
into *suggested* thresholds using the guidance in Appendix B of the project
plan (keep the stop threshold 15-20 below the forward threshold). The
suggestion is only applied when the operator asks for it -- silently
retuning the vehicle between runs would make demo behaviour unreproducible.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import List, Optional

LOG = logging.getLogger("neurodrive.calibration")

#: Bounds for suggested thresholds, so a bad baseline cannot produce a
#: vehicle that is either impossible to start or impossible to stop.
FORWARD_MIN, FORWARD_MAX = 45.0, 85.0
STOP_GAP = 20.0
STOP_MIN = 15.0


@dataclass
class CalibrationResult:
    """Everything the calibration phase learned about this user."""

    completed: bool = False
    duration_s: float = 0.0
    samples: int = 0
    good_quality_samples: int = 0
    blinks_seen: int = 0
    attention_values: List[int] = field(default_factory=list)
    mean: Optional[float] = None
    stdev: Optional[float] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    suggested_forward: Optional[float] = None
    suggested_stop: Optional[float] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def quality_ratio(self) -> float:
        if not self.samples:
            return 0.0
        return self.good_quality_samples / self.samples

    def summary(self) -> str:
        """A short human-readable block for the console and the log."""
        if not self.attention_values:
            return (
                "  Calibration: no attention values received. Check the headset "
                "fit and the Bluetooth link before driving."
            )
        lines = [
            f"  Calibration complete ({self.duration_s:.0f} s, {self.samples} samples)",
            f"    attention   mean {self.mean:.1f}  sd {self.stdev:.1f}  "
            f"range {self.minimum}-{self.maximum}",
            f"    signal      {self.quality_ratio * 100:.0f}% good quality, "
            f"{self.blinks_seen} blink(s) detected",
        ]
        if self.suggested_forward is not None:
            lines.append(
                f"    suggested   forward >= {self.suggested_forward:.0f}, "
                f"stop < {self.suggested_stop:.0f}"
                "   (apply with --apply-calibration)"
            )
        for warning in self.warnings:
            lines.append(f"    warning     {warning}")
        return "\n".join(lines)


class Calibrator:
    """Collects a baseline while the vehicle is held stationary.

    Driven by the main loop: :meth:`feed` once per cycle, then :meth:`finish`
    when :meth:`remaining` reaches zero.
    """

    def __init__(self, duration_s: float = 15.0) -> None:
        self.duration_s = max(0.0, duration_s)
        self._start = time.monotonic()
        self._values: List[int] = []
        self._samples = 0
        self._good = 0
        self._blinks = 0

    def restart(self) -> None:
        """Begin a fresh calibration (bound to the 'c' key at runtime)."""
        self._start = time.monotonic()
        self._values.clear()
        self._samples = 0
        self._good = 0
        self._blinks = 0

    def feed(self, processed) -> None:
        """Fold one conditioned signal into the baseline."""
        self._samples += 1
        if processed.quality_ok:
            self._good += 1
        self._blinks += len(processed.blink_events)
        if processed.raw_attention is not None and processed.quality_ok:
            self._values.append(processed.raw_attention)

    def remaining(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self.duration_s - (now - self._start))

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def is_done(self) -> bool:
        return self.remaining() <= 0.0

    def finish(self) -> CalibrationResult:
        """Summarise the baseline and derive suggested thresholds."""
        result = CalibrationResult(
            completed=True,
            duration_s=self.elapsed,
            samples=self._samples,
            good_quality_samples=self._good,
            blinks_seen=self._blinks,
            attention_values=list(self._values),
        )

        if result.quality_ratio < 0.5 and self._samples:
            result.warnings.append(
                "signal quality was poor for most of the calibration -- "
                "re-seat the forehead sensor and the ear clip"
            )

        if not self._values:
            result.warnings.append("no usable attention values were captured")
            return result

        result.mean = statistics.fmean(self._values)
        result.stdev = statistics.pstdev(self._values) if len(self._values) > 1 else 0.0
        result.minimum = min(self._values)
        result.maximum = max(self._values)

        # Sit the forward threshold just above the user's resting band so
        # deliberate concentration crosses it but idle attention does not.
        forward = result.mean + max(5.0, 0.75 * result.stdev)
        forward = min(FORWARD_MAX, max(FORWARD_MIN, forward))
        stop = max(STOP_MIN, forward - STOP_GAP)
        result.suggested_forward = round(forward)
        result.suggested_stop = round(stop)

        if result.stdev is not None and result.stdev < 2.0:
            result.warnings.append(
                "attention barely varied -- the headset may not be reading "
                "the user (check the ear clip)"
            )
        if result.maximum is not None and result.maximum < FORWARD_MIN:
            result.warnings.append(
                f"attention never reached {FORWARD_MIN:.0f}; the user may find "
                "it hard to trigger FORWARD"
            )

        LOG.info(
            "calibration: mean=%.1f sd=%.1f range=%d-%d suggest fwd>=%s stop<%s",
            result.mean,
            result.stdev,
            result.minimum,
            result.maximum,
            result.suggested_forward,
            result.suggested_stop,
        )
        return result


def apply_result(config, result: CalibrationResult) -> bool:
    """Write the suggested thresholds into the in-memory configuration.

    Returns True if anything changed. The on-disk ``config.json`` is left
    alone -- persisting a threshold is a deliberate act by M2, not a side
    effect of starting the bridge.
    """
    if result.suggested_forward is None or result.suggested_stop is None:
        return False
    config.set("control.attention_forward_threshold", result.suggested_forward)
    config.set("control.attention_stop_threshold", result.suggested_stop)
    LOG.info(
        "applied calibrated thresholds: forward=%s stop=%s",
        result.suggested_forward,
        result.suggested_stop,
    )
    return True
