"""
Tests for the calibration phase (UI-02) and the keyboard override (UI-03).

Calibration exists so the vehicle cannot move while the operator is settling
the headset, and so each user gets threshold suggestions rather than
inheriting whoever tuned the system last (plan section 12.2).
"""

import _bootstrap  # noqa: F401

import unittest

import calibration as calibration_module
import config as config_module
from calibration import FORWARD_MAX, FORWARD_MIN, STOP_GAP, Calibrator
from command_mapper import Command
from main import ManualController
from signal_processor import BlinkEvent, ProcessedSignal


def signal(attention=None, quality_ok=True, blinks=()):
    return ProcessedSignal(
        timestamp=0.0,
        connected=True,
        raw_attention=attention,
        attention=None if attention is None else float(attention),
        poor_signal=0 if quality_ok else 100,
        quality_ok=quality_ok,
        blink_events=list(blinks),
    )


def calibrate(values, duration=0.0, **kwargs):
    calibrator = Calibrator(duration_s=duration)
    for value in values:
        calibrator.feed(signal(attention=value, **kwargs))
    return calibrator.finish()


class TestBaseline(unittest.TestCase):
    def test_statistics_are_computed(self):
        result = calibrate([40, 42, 44, 46, 48])

        self.assertTrue(result.completed)
        self.assertEqual(result.samples, 5)
        self.assertAlmostEqual(result.mean, 44.0)
        self.assertEqual(result.minimum, 40)
        self.assertEqual(result.maximum, 48)

    def test_poor_quality_samples_are_excluded_from_the_baseline(self):
        calibrator = Calibrator(duration_s=0.0)
        for value in (40, 42, 44):
            calibrator.feed(signal(attention=value))
        for value in (95, 98):
            calibrator.feed(signal(attention=value, quality_ok=False))

        result = calibrator.finish()

        self.assertEqual(len(result.attention_values), 3)
        self.assertAlmostEqual(result.mean, 42.0)
        self.assertEqual(result.samples, 5)
        self.assertAlmostEqual(result.quality_ratio, 0.6)

    def test_blinks_are_counted(self):
        calibrator = Calibrator(duration_s=0.0)
        calibrator.feed(signal(attention=50, blinks=[BlinkEvent.SINGLE]))
        calibrator.feed(signal(attention=50))
        calibrator.feed(signal(attention=50, blinks=[BlinkEvent.SINGLE]))

        self.assertEqual(calibrator.finish().blinks_seen, 2)


class TestSuggestions(unittest.TestCase):
    def test_thresholds_sit_above_the_resting_baseline(self):
        result = calibrate([38, 42, 40, 44, 39, 41])

        self.assertGreater(result.suggested_forward, result.mean)
        self.assertEqual(
            result.suggested_stop, result.suggested_forward - STOP_GAP
        )

    def test_suggestions_preserve_the_hysteresis_gap(self):
        """Whatever the user's baseline, the pair must remain valid config."""
        for baseline in (5, 20, 45, 70, 95):
            result = calibrate([baseline] * 10)
            self.assertLess(
                result.suggested_stop,
                result.suggested_forward,
                f"baseline {baseline} produced an invalid pair",
            )

    def test_suggestions_are_clamped_to_a_usable_range(self):
        low = calibrate([2, 3, 1, 2])
        high = calibrate([99, 98, 100, 99])

        self.assertGreaterEqual(low.suggested_forward, FORWARD_MIN)
        self.assertLessEqual(high.suggested_forward, FORWARD_MAX)

    def test_no_usable_values_produces_no_suggestion_but_a_warning(self):
        result = calibrate([], duration=0.0)

        self.assertIsNone(result.suggested_forward)
        self.assertTrue(result.warnings)
        self.assertIn("no usable attention values", " ".join(result.warnings))

    def test_flat_signal_is_flagged(self):
        """A constant reading usually means the ear clip is off."""
        result = calibrate([50] * 10)
        self.assertTrue(any("barely varied" in w for w in result.warnings))

    def test_poor_quality_session_is_flagged(self):
        calibrator = Calibrator(duration_s=0.0)
        for _ in range(10):
            calibrator.feed(signal(attention=50, quality_ok=False))

        result = calibrator.finish()
        self.assertTrue(any("poor" in w for w in result.warnings))

    def test_user_who_never_concentrates_is_flagged(self):
        result = calibrate([10, 12, 20, 8, 15, 30])
        self.assertTrue(any("never reached" in w for w in result.warnings))

    def test_summary_is_printable_in_both_cases(self):
        self.assertIn("Calibration", calibrate([40, 50, 60]).summary())
        self.assertIn("Calibration", calibrate([]).summary())


class TestApplyResult(unittest.TestCase):
    def test_applying_updates_the_config_and_stays_valid(self):
        config = config_module.load()
        result = calibrate([30, 35, 32, 38, 31])

        self.assertTrue(calibration_module.apply_result(config, result))

        self.assertEqual(
            config.get("control.attention_forward_threshold"),
            result.suggested_forward,
        )
        self.assertEqual(config.validate(), [])

    def test_applying_an_empty_result_changes_nothing(self):
        config = config_module.load()
        before = config.get("control.attention_forward_threshold")

        self.assertFalse(calibration_module.apply_result(config, calibrate([])))
        self.assertEqual(config.get("control.attention_forward_threshold"), before)

    def test_the_file_on_disk_is_not_rewritten(self):
        """Persisting a threshold is a deliberate act, not a side effect."""
        config = config_module.load()
        calibration_module.apply_result(config, calibrate([30, 35, 32, 38]))

        fresh = config_module.load()
        self.assertEqual(fresh.get("control.attention_forward_threshold"), 60)


class TestTiming(unittest.TestCase):
    def test_zero_duration_is_immediately_done(self):
        self.assertTrue(Calibrator(duration_s=0.0).is_done())

    def test_a_long_calibration_is_not_done_yet(self):
        calibrator = Calibrator(duration_s=30.0)
        self.assertFalse(calibrator.is_done())
        self.assertGreater(calibrator.remaining(), 29.0)

    def test_restart_clears_the_baseline(self):
        calibrator = Calibrator(duration_s=30.0)
        for value in (80, 85, 90):
            calibrator.feed(signal(attention=value))

        calibrator.restart()
        calibrator.feed(signal(attention=20))

        result = calibrator.finish()
        self.assertEqual(result.attention_values, [20])


class TestManualController(unittest.TestCase):
    """UI-03: the keyboard override behaves like the EEG path."""

    def test_starts_stopped(self):
        self.assertIs(ManualController().command(0.0), Command.STOP)

    def test_forward_is_held(self):
        manual = ManualController()
        manual.press(Command.FORWARD, 0.0)

        self.assertIs(manual.command(0.0), Command.FORWARD)
        self.assertIs(manual.command(10.0), Command.FORWARD)

    def test_turn_is_a_pulse_that_returns_to_the_previous_state(self):
        manual = ManualController(turn_repeat_s=0.15)
        manual.press(Command.FORWARD, 0.0)
        manual.press(Command.LEFT, 1.0)

        self.assertIs(manual.command(1.05), Command.LEFT)
        self.assertIs(manual.command(1.20), Command.FORWARD)

    def test_turn_from_stop_returns_to_stop(self):
        manual = ManualController(turn_repeat_s=0.15)
        manual.press(Command.RIGHT, 1.0)

        self.assertIs(manual.command(1.05), Command.RIGHT)
        self.assertIs(manual.command(1.20), Command.STOP)

    def test_stop_cancels_an_active_turn_immediately(self):
        manual = ManualController(turn_repeat_s=1.0)
        manual.press(Command.FORWARD, 0.0)
        manual.press(Command.LEFT, 1.0)
        manual.press(Command.STOP, 1.05)

        self.assertIs(manual.command(1.06), Command.STOP)

    def test_reset_returns_to_stop(self):
        manual = ManualController()
        manual.press(Command.FORWARD, 0.0)
        manual.reset()
        self.assertIs(manual.command(1.0), Command.STOP)


class TestKeyMapping(unittest.TestCase):
    def test_arrow_keys_and_wasd_cover_every_command(self):
        from keyboard_input import KEY_COMMANDS

        self.assertEqual(KEY_COMMANDS["UP"], "FORWARD")
        self.assertEqual(KEY_COMMANDS["LEFT"], "LEFT")
        self.assertEqual(KEY_COMMANDS["RIGHT"], "RIGHT")
        self.assertEqual(KEY_COMMANDS["DOWN"], "STOP")
        self.assertEqual(KEY_COMMANDS["SPACE"], "STOP")

        for name in set(KEY_COMMANDS.values()):
            Command(name)  # every mapped name must be a real command

    def test_reader_is_inert_when_disabled(self):
        from keyboard_input import KeyboardReader

        reader = KeyboardReader(enabled=False)
        self.assertFalse(reader.start())
        self.assertEqual(reader.poll(), [])
        reader.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
