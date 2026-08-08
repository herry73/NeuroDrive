"""
Unit tests for the acquisition layer.

Covers EEG-01/02 (sources produce samples), EEG-03 (connection timing),
EEG-05 (signal loss is surfaced so the vehicle can safe-stop), NFR 3.1
(3-attempt reconnection) and NFR 3.6 (any source behind one interface).
"""

import _bootstrap  # noqa: F401

import os
import tempfile
import threading
import time
import unittest

import config as config_module
from eeg_reader import EEGReader, ReaderStatus
from eeg_sources import (
    EEGConnectionError,
    EEGSample,
    EEGSource,
    MockSource,
    ReplaySource,
    SerialThinkGearSource,
    create_source,
)
from mock_eeg_generator import generate_samples, write_csv


class ScriptedSource(EEGSource):
    """A source whose behaviour the test dictates."""

    name = "scripted"

    def __init__(self, fail_opens=0, fail_after_polls=None):
        self.fail_opens = fail_opens
        self.fail_after_polls = fail_after_polls
        self.open_count = 0
        self.poll_count = 0
        self.closed = 0
        self._lock = threading.Lock()
        self._queued = []

    def open(self):
        with self._lock:
            self.open_count += 1
            if self.fail_opens > 0:
                self.fail_opens -= 1
                raise EEGConnectionError("scripted open failure")

    def poll(self):
        with self._lock:
            self.poll_count += 1
            if (
                self.fail_after_polls is not None
                and self.poll_count > self.fail_after_polls
            ):
                raise EEGConnectionError("scripted link drop")
            samples, self._queued = self._queued, []
        return samples

    def close(self):
        with self._lock:
            self.closed += 1

    def push(self, **kwargs):
        with self._lock:
            self._queued.append(EEGSample(timestamp=time.monotonic(), **kwargs))


class TestMockSource(unittest.TestCase):
    def test_produces_parsable_samples(self):
        source = MockSource(seed=1, blink_interval_s=0.05)
        source.open()

        deadline = time.monotonic() + 3.0
        samples = []
        while time.monotonic() < deadline and len(samples) < 3:
            samples.extend(source.poll())
            time.sleep(0.02)

        self.assertGreaterEqual(len(samples), 2)
        self.assertEqual(source.parser_stats.packets_bad_checksum, 0)
        with_attention = [s for s in samples if s.attention is not None]
        self.assertTrue(with_attention)
        self.assertTrue(all(0 <= s.attention <= 100 for s in with_attention))

    def test_emits_blinks(self):
        source = MockSource(seed=1, blink_interval_s=0.05)
        source.open()

        blinks = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(blinks) < 2:
            blinks.extend(s for s in source.poll() if s.has_blink)
            time.sleep(0.02)

        self.assertGreaterEqual(len(blinks), 2)
        self.assertTrue(all(s.blink_strength >= 140 for s in blinks))


class TestReplaySource(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="neurodrive-replay-")
        self.path = os.path.join(self.tmpdir, "session.csv")
        write_csv(self.path, generate_samples(duration_s=10, scenario="smooth", seed=4))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_replays_in_order_and_in_real_time(self):
        source = ReplaySource(self.path, loop=False, speed=20.0)
        source.open()

        samples = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(samples) < 10:
            samples.extend(source.poll())
            time.sleep(0.01)

        self.assertGreaterEqual(len(samples), 10)
        self.assertTrue(all(s.attention is not None for s in samples[:10]))

    def test_missing_file_is_a_clear_error(self):
        source = ReplaySource(os.path.join(self.tmpdir, "nope.csv"))
        with self.assertRaises(EEGConnectionError) as caught:
            source.open()
        self.assertIn("not found", str(caught.exception))

    def test_file_without_the_required_columns_is_rejected(self):
        path = os.path.join(self.tmpdir, "wrong.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("time,value\n1,2\n")

        source = ReplaySource(path)
        with self.assertRaises(EEGConnectionError) as caught:
            source.open()
        self.assertIn("missing column", str(caught.exception))

    def test_empty_file_is_rejected(self):
        path = os.path.join(self.tmpdir, "empty.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("elapsed_s,attention,poor_signal\n")

        with self.assertRaises(EEGConnectionError):
            ReplaySource(path).open()


class TestSourceFactory(unittest.TestCase):
    """NFR 3.6: swapping the acquisition backend is a config change."""

    def test_each_source_kind(self):
        config = config_module.load()

        config.set("eeg.source", "mock")
        self.assertIsInstance(create_source(config), MockSource)

        config.set("eeg.source", "serial")
        self.assertIsInstance(create_source(config), SerialThinkGearSource)

        config.set("eeg.source", "replay")
        self.assertIsInstance(create_source(config), ReplaySource)

    def test_unknown_source_is_rejected(self):
        config = config_module.load()
        config.set("eeg.source", "telepathy")
        with self.assertRaises(ValueError):
            create_source(config)


class TestReaderLifecycle(unittest.TestCase):
    def test_samples_reach_the_consumer(self):
        source = ScriptedSource()
        reader = EEGReader(lambda: source, poll_interval_s=0.005)
        reader.start()
        try:
            self.assertTrue(reader.wait_for_connection(timeout=2.0))
            source.push(attention=71, poor_signal=0)

            deadline = time.monotonic() + 2.0
            received = []
            while time.monotonic() < deadline and not received:
                received = reader.read_all()
                time.sleep(0.01)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].attention, 71)
            self.assertIs(reader.info.status, ReaderStatus.CONNECTED)
            self.assertIsNotNone(reader.info.connect_seconds)
        finally:
            reader.stop()

    def test_read_all_drains_and_does_not_block(self):
        source = ScriptedSource()
        reader = EEGReader(lambda: source, poll_interval_s=0.005)
        reader.start()
        try:
            reader.wait_for_connection(timeout=2.0)
            self.assertEqual(reader.read_all(), [])  # empty, immediately

            for value in (10, 20, 30):
                source.push(attention=value, poor_signal=0)

            deadline = time.monotonic() + 2.0
            received = []
            while time.monotonic() < deadline and len(received) < 3:
                received.extend(reader.read_all())
                time.sleep(0.01)

            self.assertEqual([s.attention for s in received], [10, 20, 30])
        finally:
            reader.stop()

    def test_retries_a_failing_connection_then_reports_failure(self):
        """NFR 3.1: three attempts, then FAILED rather than a silent hang."""
        source = ScriptedSource(fail_opens=99)
        reader = EEGReader(
            lambda: source,
            reconnect_attempts=3,
            reconnect_delay_s=0.02,
            poll_interval_s=0.005,
        )
        reader.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if reader.info.status is ReaderStatus.FAILED:
                    break
                time.sleep(0.01)

            self.assertIs(reader.info.status, ReaderStatus.FAILED)
            self.assertGreaterEqual(source.open_count, 3)
            self.assertIn("scripted open failure", reader.info.last_error)
        finally:
            reader.stop()

    def test_failed_connection_still_publishes_a_disconnected_sample(self):
        """EEG-05: downstream must learn there is no signal, not just stall."""
        source = ScriptedSource(fail_opens=99)
        reader = EEGReader(
            lambda: source,
            reconnect_attempts=1,
            reconnect_delay_s=0.02,
            poll_interval_s=0.005,
        )
        reader.start()
        try:
            deadline = time.monotonic() + 3.0
            samples = []
            while time.monotonic() < deadline and not samples:
                samples = reader.read_all()
                time.sleep(0.01)

            self.assertTrue(samples)
            self.assertFalse(samples[0].connected)
        finally:
            reader.stop()

    def test_link_drop_reconnects(self):
        source = ScriptedSource(fail_after_polls=3)
        reader = EEGReader(
            lambda: source,
            reconnect_attempts=3,
            reconnect_delay_s=0.02,
            poll_interval_s=0.005,
        )
        reader.start()
        try:
            self.assertTrue(reader.wait_for_connection(timeout=2.0))

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if reader.info.reconnect_count >= 1:
                    break
                time.sleep(0.01)

            self.assertGreaterEqual(reader.info.reconnect_count, 1)
            self.assertGreaterEqual(source.closed, 1)
        finally:
            reader.stop()

    def test_signal_timeout_reports_loss(self):
        """EEG-05: the port is open but the headset went quiet."""
        source = ScriptedSource()
        reader = EEGReader(
            lambda: source, signal_timeout_ms=150, poll_interval_s=0.005
        )
        reader.start()
        try:
            self.assertTrue(reader.wait_for_connection(timeout=2.0))
            source.push(attention=50, poor_signal=0)
            time.sleep(0.1)
            reader.read_all()

            deadline = time.monotonic() + 3.0
            lost = []
            while time.monotonic() < deadline and not lost:
                lost = [s for s in reader.read_all() if not s.connected]
                time.sleep(0.01)

            self.assertTrue(lost, "no disconnected sample was published")
            self.assertIs(reader.info.status, ReaderStatus.SIGNAL_LOST)
        finally:
            reader.stop()

    def test_stop_is_idempotent_and_closes_the_source(self):
        source = ScriptedSource()
        reader = EEGReader(lambda: source, poll_interval_s=0.005)
        reader.start()
        reader.wait_for_connection(timeout=2.0)

        reader.stop()
        reader.stop()

        self.assertIs(reader.info.status, ReaderStatus.STOPPED)
        self.assertGreaterEqual(source.closed, 1)

    def test_double_start_is_refused(self):
        reader = EEGReader(lambda: ScriptedSource(), poll_interval_s=0.005)
        reader.start()
        try:
            with self.assertRaises(RuntimeError):
                reader.start()
        finally:
            reader.stop()

    def test_context_manager(self):
        source = ScriptedSource()
        with EEGReader(lambda: source, poll_interval_s=0.005) as reader:
            self.assertTrue(reader.wait_for_connection(timeout=2.0))
        self.assertGreaterEqual(source.closed, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
