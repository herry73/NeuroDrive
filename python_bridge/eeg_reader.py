"""
Threaded EEG reader.

Requirement coverage:
    EEG-01/02  Connect to the headset and parse ThinkGear packets.
    EEG-03     Report the time taken to reach a stable connection.
    EEG-05     Detect signal loss and surface it so the bridge can safe-stop.
    NFR 3.1    Automatic reconnection with a 3-attempt policy.
    NFR 3.3    Never block the main loop: acquisition runs on its own thread
               and hands samples over through a thread-safe queue.

The reader owns an :class:`~eeg_sources.EEGSource` and nothing else. It does
no thresholding and no smoothing -- that is the signal processor's job.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

from eeg_sources import EEGConnectionError, EEGSample, EEGSource

LOG = logging.getLogger("neurodrive.eeg")


class ReaderStatus(str, Enum):
    """Lifecycle state of the acquisition link, shown on the dashboard."""

    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    SIGNAL_LOST = "SIGNAL_LOST"
    FAILED = "FAILED"
    STOPPED = "STOPPED"

    @property
    def is_usable(self) -> bool:
        """True when samples can be trusted to be arriving."""
        return self is ReaderStatus.CONNECTED


@dataclass
class ReaderInfo:
    """Snapshot of reader health for the dashboard and the test report."""

    status: ReaderStatus = ReaderStatus.IDLE
    source_name: str = "-"
    connect_seconds: Optional[float] = None
    reconnect_count: int = 0
    samples_received: int = 0
    samples_dropped: int = 0
    last_sample_age_s: Optional[float] = None
    last_error: str = ""


class EEGReader:
    """Runs an :class:`EEGSource` on a background thread.

    ``source_factory`` is called each time a (re)connection is attempted, so
    a dead serial handle is never reused.
    """

    def __init__(
        self,
        source_factory: Callable[[], EEGSource],
        signal_timeout_ms: int = 2000,
        reconnect_attempts: int = 3,
        reconnect_delay_s: float = 2.0,
        queue_size: int = 256,
        poll_interval_s: float = 0.02,
    ) -> None:
        self._source_factory = source_factory
        self._signal_timeout_s = max(0.1, signal_timeout_ms / 1000.0)
        self._reconnect_attempts = max(1, reconnect_attempts)
        self._reconnect_delay_s = reconnect_delay_s
        self._poll_interval_s = poll_interval_s

        self._queue: "queue.Queue[EEGSample]" = queue.Queue(maxsize=queue_size)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._info = ReaderInfo()
        self._source: Optional[EEGSource] = None
        self._last_sample_time: Optional[float] = None
        self._signal_lost_announced = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("EEGReader already started")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="eeg-reader", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._close_source()
        self._update(status=ReaderStatus.STOPPED)

    def __enter__(self) -> "EEGReader":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- consumer API -------------------------------------------------------

    def read_all(self) -> List[EEGSample]:
        """Drain every queued sample without blocking.

        The main loop calls this once per cycle; returning a list (rather
        than one sample) means a slow cycle can never fall behind the headset.
        """
        samples: List[EEGSample] = []
        while True:
            try:
                samples.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return samples

    def wait_for_connection(self, timeout: float = 30.0) -> bool:
        """Block until the link is up. Returns False on timeout (EEG-03)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.info.status is ReaderStatus.CONNECTED:
                return True
            if self._stop_event.is_set():
                return False
            time.sleep(0.05)
        return self.info.status is ReaderStatus.CONNECTED

    @property
    def info(self) -> ReaderInfo:
        with self._lock:
            info = ReaderInfo(**vars(self._info))
        if self._last_sample_time is not None:
            info.last_sample_age_s = time.monotonic() - self._last_sample_time
        return info

    # -- internals ----------------------------------------------------------

    def _update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._info, key, value)

    def _bump(self, field: str) -> None:
        with self._lock:
            setattr(self._info, field, getattr(self._info, field) + 1)

    def _close_source(self) -> None:
        if self._source is not None:
            try:
                self._source.close()
            except Exception:  # pragma: no cover - best effort
                LOG.debug("error closing EEG source", exc_info=True)
            self._source = None

    def _publish(self, sample: EEGSample) -> None:
        """Enqueue a sample, discarding the oldest if the consumer stalled."""
        try:
            self._queue.put_nowait(sample)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._bump("samples_dropped")
                self._queue.put_nowait(sample)
            except (queue.Empty, queue.Full):  # pragma: no cover - race
                self._bump("samples_dropped")
                return
        self._bump("samples_received")

    def _run(self) -> None:
        attempt = 0
        connect_started = time.monotonic()

        while not self._stop_event.is_set():
            # --- (re)connect -----------------------------------------------
            if self._source is None:
                attempt += 1
                self._update(status=ReaderStatus.CONNECTING)
                try:
                    source = self._source_factory()
                    source.open()
                except Exception as exc:
                    self._close_source()
                    LOG.warning(
                        "EEG connect attempt %d/%d failed: %s",
                        attempt,
                        self._reconnect_attempts,
                        exc,
                    )
                    self._update(last_error=str(exc))
                    if attempt >= self._reconnect_attempts:
                        # Policy exhausted: report failure, then keep trying
                        # slowly so a demo can recover without a restart.
                        self._update(status=ReaderStatus.FAILED)
                        self._emit_disconnected()
                        self._sleep(self._reconnect_delay_s * 5)
                    else:
                        self._sleep(self._reconnect_delay_s)
                    continue

                self._source = source
                elapsed = time.monotonic() - connect_started
                attempt = 0
                self._last_sample_time = None
                self._signal_lost_announced = False
                self._update(
                    status=ReaderStatus.CONNECTED,
                    source_name=source.name,
                    connect_seconds=elapsed,
                    last_error="",
                )
                LOG.info("EEG source %r connected in %.2f s", source.name, elapsed)

            # --- poll -------------------------------------------------------
            try:
                samples = self._source.poll()
            except EEGConnectionError as exc:
                LOG.warning("EEG link lost: %s", exc)
                self._update(last_error=str(exc), status=ReaderStatus.SIGNAL_LOST)
                self._bump("reconnect_count")
                self._emit_disconnected()
                self._close_source()
                connect_started = time.monotonic()
                self._sleep(self._reconnect_delay_s)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                LOG.exception("unexpected EEG source error")
                self._update(last_error=str(exc), status=ReaderStatus.SIGNAL_LOST)
                self._emit_disconnected()
                self._close_source()
                connect_started = time.monotonic()
                self._sleep(self._reconnect_delay_s)
                continue

            now = time.monotonic()
            if samples:
                self._last_sample_time = now
                if self._signal_lost_announced:
                    self._signal_lost_announced = False
                    LOG.info("EEG stream recovered")
                self._update(status=ReaderStatus.CONNECTED)
                for sample in samples:
                    self._publish(sample)
            else:
                # EEG-05: the port is open but nothing is arriving (headset
                # switched off, taken off the head, out of range).
                if (
                    self._last_sample_time is not None
                    and now - self._last_sample_time > self._signal_timeout_s
                    and not self._signal_lost_announced
                ):
                    self._signal_lost_announced = True
                    self._update(status=ReaderStatus.SIGNAL_LOST)
                    LOG.warning(
                        "no EEG samples for %.1f s -- signalling loss",
                        now - self._last_sample_time,
                    )
                    self._emit_disconnected()

            self._sleep(self._poll_interval_s)

        self._close_source()

    def _emit_disconnected(self) -> None:
        """Push a sentinel sample so the control chain safe-stops (EEG-05)."""
        self._publish(
            EEGSample(
                timestamp=time.monotonic(),
                attention=None,
                poor_signal=200,
                connected=False,
            )
        )

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep so ``stop()`` is always prompt."""
        if seconds > 0:
            self._stop_event.wait(seconds)
