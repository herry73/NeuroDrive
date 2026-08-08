"""
Unit tests for signal conditioning.

Covers SP-01 (smoothing), SP-04 (blink threshold), SP-05 (double blink),
SP-06 (debounce) and SF-03 (signal quality gate).
"""

import _bootstrap  # noqa: F401

import unittest

from eeg_sources import EEGSample
from signal_processor import BlinkEvent, SignalProcessor, create_processor


def sample(t, attention=None, poor_signal=0, blink=None, connected=True, raw=None):
    return EEGSample(
        timestamp=t,
        attention=attention,
        poor_signal=poor_signal,
        blink_strength=blink,
        connected=connected,
        raw=list(raw or []),
    )


class TestSmoothing(unittest.TestCase):
    """SP-01: rolling average over the last N attention values."""

    def test_average_of_a_partial_window(self):
        processor = SignalProcessor(attention_window=5)
        for index, value in enumerate([40, 60]):
            processor.ingest(sample(index, attention=value))

        state = processor.tick(2.0)
        self.assertAlmostEqual(state.attention, 50.0)
        self.assertFalse(state.window_filled)

    def test_window_holds_only_the_last_n_values(self):
        processor = SignalProcessor(attention_window=5)
        for index, value in enumerate([0, 0, 0, 0, 0, 100, 100, 100, 100, 100]):
            processor.ingest(sample(index, attention=value))

        state = processor.tick(10.0)
        self.assertAlmostEqual(state.attention, 100.0)
        self.assertTrue(state.window_filled)

    def test_smoothing_damps_a_single_spike(self):
        """The point of SP-01: one bad reading must not launch the vehicle."""
        processor = SignalProcessor(attention_window=5)
        for index in range(4):
            processor.ingest(sample(index, attention=30))
        processor.ingest(sample(4, attention=100))

        state = processor.tick(5.0)
        self.assertAlmostEqual(state.attention, 44.0)
        self.assertEqual(state.raw_attention, 100)

    def test_no_attention_yet_reports_none(self):
        processor = SignalProcessor()
        processor.ingest(sample(0, poor_signal=0))
        self.assertIsNone(processor.tick(0.1).attention)

    def test_rejects_a_window_below_one(self):
        with self.assertRaises(ValueError):
            SignalProcessor(attention_window=0)


class TestQualityGate(unittest.TestCase):
    """SF-03: a poor-signal value above the cutoff makes samples unusable."""

    def test_good_signal_is_usable(self):
        processor = SignalProcessor(poor_signal_cutoff=25)
        processor.ingest(sample(0, attention=70, poor_signal=0))

        state = processor.tick(0.1)
        self.assertTrue(state.quality_ok)
        self.assertTrue(state.usable)

    def test_poor_signal_marks_the_sample_unusable(self):
        processor = SignalProcessor(poor_signal_cutoff=25)
        processor.ingest(sample(0, attention=70, poor_signal=51))

        state = processor.tick(0.1)
        self.assertFalse(state.quality_ok)
        self.assertFalse(state.usable)
        self.assertEqual(processor.stats.poor_quality_samples, 1)

    def test_cutoff_is_inclusive(self):
        processor = SignalProcessor(poor_signal_cutoff=25)
        processor.ingest(sample(0, attention=70, poor_signal=25))
        self.assertTrue(processor.tick(0.1).quality_ok)

    def test_disconnect_clears_the_window(self):
        """Stale values must not drive the vehicle after a reconnect."""
        processor = SignalProcessor(attention_window=5)
        for index in range(5):
            processor.ingest(sample(index, attention=90))
        self.assertAlmostEqual(processor.tick(5.0).attention, 90.0)

        processor.ingest(sample(6, connected=False))
        state = processor.tick(6.0)

        self.assertFalse(state.connected)
        self.assertIsNone(state.attention)
        self.assertEqual(processor.stats.disconnects, 1)


class TestBlinkDetection(unittest.TestCase):
    """SP-04 and SP-06."""

    def test_blink_above_threshold_is_reported(self):
        processor = SignalProcessor(blink_strength_threshold=150)
        processor.ingest(sample(1.0, attention=50, blink=180))

        self.assertEqual(processor.tick(1.0).blink_events, [BlinkEvent.SINGLE])
        self.assertEqual(processor.stats.blinks_qualified, 1)

    def test_blink_below_threshold_is_ignored(self):
        processor = SignalProcessor(blink_strength_threshold=150)
        processor.ingest(sample(1.0, attention=50, blink=149))

        self.assertEqual(processor.tick(1.0).blink_events, [])
        self.assertEqual(processor.stats.blinks_rejected_threshold, 1)

    def test_threshold_is_inclusive(self):
        processor = SignalProcessor(blink_strength_threshold=150)
        processor.ingest(sample(1.0, blink=150))
        self.assertEqual(processor.tick(1.0).blink_events, [BlinkEvent.SINGLE])

    def test_debounce_suppresses_a_rapid_second_blink(self):
        processor = SignalProcessor(blink_debounce_ms=300)
        processor.ingest(sample(1.00, blink=200))
        processor.ingest(sample(1.15, blink=200))  # 150 ms later

        self.assertEqual(processor.tick(1.2).blink_events, [BlinkEvent.SINGLE])
        self.assertEqual(processor.stats.blinks_rejected_debounce, 1)

    def test_blink_after_the_debounce_window_is_accepted(self):
        processor = SignalProcessor(blink_debounce_ms=300)
        processor.ingest(sample(1.0, blink=200))
        processor.tick(1.0)
        processor.ingest(sample(1.4, blink=200))

        self.assertEqual(processor.tick(1.4).blink_events, [BlinkEvent.SINGLE])
        self.assertEqual(processor.stats.blinks_qualified, 2)

    def test_events_are_consumed_once(self):
        processor = SignalProcessor()
        processor.ingest(sample(1.0, blink=200))

        self.assertEqual(len(processor.tick(1.0).blink_events), 1)
        self.assertEqual(processor.tick(1.1).blink_events, [])


class TestDoubleBlink(unittest.TestCase):
    """SP-05, in the mode where single and double blinks differ."""

    def make(self):
        return SignalProcessor(
            classify_double=True, blink_debounce_ms=100, double_blink_window_ms=500
        )

    def test_two_close_blinks_become_one_double(self):
        processor = self.make()
        processor.ingest(sample(1.0, blink=200))
        self.assertEqual(processor.tick(1.0).blink_events, [])  # still deciding

        processor.ingest(sample(1.3, blink=200))
        self.assertEqual(processor.tick(1.3).blink_events, [BlinkEvent.DOUBLE])
        self.assertEqual(processor.stats.double_blinks, 1)

    def test_lone_blink_resolves_to_single_once_the_window_closes(self):
        processor = self.make()
        processor.ingest(sample(1.0, blink=200))

        self.assertEqual(processor.tick(1.2).blink_events, [])
        self.assertEqual(processor.tick(1.6).blink_events, [BlinkEvent.SINGLE])

    def test_blinks_further_apart_than_the_window_are_two_singles(self):
        processor = self.make()
        processor.ingest(sample(1.0, blink=200))
        processor.tick(1.0)
        processor.ingest(sample(2.0, blink=200))

        events = processor.tick(2.0).blink_events + processor.tick(2.6).blink_events
        self.assertEqual(events, [BlinkEvent.SINGLE, BlinkEvent.SINGLE])
        self.assertEqual(processor.stats.double_blinks, 0)

    def test_alternate_mode_never_defers(self):
        """With classify_double off there is no added latency (COM-03)."""
        processor = SignalProcessor(classify_double=False)
        processor.ingest(sample(1.0, blink=200))
        self.assertEqual(processor.tick(1.0).blink_events, [BlinkEvent.SINGLE])


class TestRawBlinkFallback(unittest.TestCase):
    """Optional detector for headsets that do not emit 0x16 rows."""

    def test_large_raw_excursion_produces_a_blink(self):
        processor = SignalProcessor(
            blink_from_raw=True, raw_amplitude_threshold=300, raw_refractory_ms=400
        )
        processor.ingest(sample(1.0, attention=50, raw=[10, -20, 450, 30]))

        self.assertEqual(processor.tick(1.0).blink_events, [BlinkEvent.SINGLE])

    def test_small_raw_values_do_not(self):
        processor = SignalProcessor(blink_from_raw=True, raw_amplitude_threshold=300)
        processor.ingest(sample(1.0, attention=50, raw=[10, -20, 299]))
        self.assertEqual(processor.tick(1.0).blink_events, [])

    def test_refractory_period_suppresses_the_tail_of_one_artefact(self):
        processor = SignalProcessor(
            blink_from_raw=True,
            raw_amplitude_threshold=300,
            raw_refractory_ms=400,
            blink_debounce_ms=0,
        )
        processor.ingest(sample(1.0, raw=[500]))
        processor.ingest(sample(1.1, raw=[500]))

        self.assertEqual(len(processor.tick(1.2).blink_events), 1)

    def test_disabled_by_default(self):
        processor = SignalProcessor()
        processor.ingest(sample(1.0, raw=[5000]))
        self.assertEqual(processor.tick(1.0).blink_events, [])


class TestFactory(unittest.TestCase):
    def test_built_from_config_defaults(self):
        import config as config_module

        processor = create_processor(config_module.load())

        self.assertEqual(processor.attention_window, 5)
        self.assertEqual(processor.blink_strength_threshold, 150)
        self.assertAlmostEqual(processor.blink_debounce_s, 0.3)
        self.assertEqual(processor.poor_signal_cutoff, 25)
        self.assertFalse(processor.classify_double)  # default is alternate mode

    def test_single_double_config_enables_classification(self):
        import config as config_module

        config = config_module.load()
        config.set("control.blink_mode", "single_double")

        self.assertTrue(create_processor(config).classify_double)


if __name__ == "__main__":
    unittest.main(verbosity=2)
