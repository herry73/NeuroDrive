"""
Unit tests for the driving policy.

Covers MV-01 (four states), MV-03 (timed turn pulse), SP-02 (forward
threshold), SP-03 (stop after 1 s below threshold), SP-05 (blink to
direction), SF-03 and EEG-05 (safe stop), and UI-02 (no movement while
disarmed).
"""

import _bootstrap  # noqa: F401

import unittest

from command_mapper import (
    COMMANDS_BY_WIRE,
    Command,
    CommandMapper,
    create_mapper,
    map_to_command,
)
from signal_processor import BlinkEvent, ProcessedSignal


def signal(t, attention=None, quality_ok=True, connected=True, blinks=()):
    return ProcessedSignal(
        timestamp=t,
        connected=connected,
        raw_attention=None if attention is None else int(attention),
        attention=attention,
        poor_signal=0 if quality_ok else 51,
        quality_ok=quality_ok,
        blink_events=list(blinks),
    )


def armed_mapper(**kwargs):
    mapper = CommandMapper(**kwargs)
    mapper.arm()
    return mapper


class TestAttentionPolicy(unittest.TestCase):
    def test_above_forward_threshold_drives(self):
        mapper = armed_mapper()
        self.assertIs(mapper.update(signal(0, attention=75), 0), Command.FORWARD)

    def test_threshold_is_inclusive(self):
        """SP-02: 'at or above 60' -- exactly 60 must go."""
        mapper = armed_mapper(attention_forward_threshold=60)
        self.assertIs(mapper.update(signal(0, attention=60), 0), Command.FORWARD)

    def test_dead_band_holds_the_current_state(self):
        """Hysteresis: between 40 and 60 nothing changes, either way."""
        mapper = armed_mapper()
        mapper.update(signal(0, attention=75), 0)
        self.assertIs(mapper.update(signal(1, attention=50), 1), Command.FORWARD)

        mapper.update(signal(2, attention=20), 2)
        mapper.update(signal(4, attention=20), 4)  # held long enough to stop
        self.assertIs(mapper.update(signal(5, attention=50), 5), Command.STOP)

    def test_low_attention_must_persist_before_stopping(self):
        """SP-03: below 40 for more than 1 s, not instantly."""
        mapper = armed_mapper(attention_stop_hold_ms=1000)
        mapper.update(signal(0.0, attention=80), 0.0)

        self.assertIs(mapper.update(signal(1.0, attention=20), 1.0), Command.FORWARD)
        self.assertIs(mapper.update(signal(1.5, attention=20), 1.5), Command.FORWARD)
        self.assertIs(mapper.update(signal(2.0, attention=20), 2.0), Command.STOP)

    def test_recovering_attention_cancels_the_pending_stop(self):
        mapper = armed_mapper()
        mapper.update(signal(0.0, attention=80), 0.0)
        mapper.update(signal(0.5, attention=20), 0.5)
        mapper.update(signal(0.9, attention=80), 0.9)  # back up before 1 s

        self.assertIs(mapper.update(signal(1.4, attention=20), 1.4), Command.FORWARD)

    def test_stop_threshold_must_sit_below_the_forward_threshold(self):
        with self.assertRaises(ValueError):
            CommandMapper(attention_forward_threshold=50, attention_stop_threshold=60)


class TestSafety(unittest.TestCase):
    def test_disarmed_mapper_never_moves(self):
        """UI-02: nothing moves during the calibration phase."""
        mapper = CommandMapper()
        self.assertIs(mapper.update(signal(0, attention=95), 0), Command.STOP)
        self.assertFalse(mapper.armed)

    def test_signal_loss_stops_the_vehicle(self):
        """EEG-05."""
        mapper = armed_mapper()
        mapper.update(signal(0, attention=90), 0)

        self.assertIs(mapper.update(signal(1, connected=False), 1), Command.STOP)
        self.assertEqual(mapper.state(1).safe_stops, 1)

    def test_poor_quality_pauses_commands(self):
        """SF-03."""
        mapper = armed_mapper(require_good_signal=True)
        mapper.update(signal(0, attention=90), 0)

        command = mapper.update(signal(1, attention=90, quality_ok=False), 1)
        self.assertIs(command, Command.STOP)
        self.assertIn("poor signal", mapper.state(1).reason)

    def test_quality_gate_can_be_disabled(self):
        mapper = armed_mapper(require_good_signal=False)
        command = mapper.update(signal(0, attention=90, quality_ok=False), 0)
        self.assertIs(command, Command.FORWARD)

    def test_missing_attention_value_stops(self):
        mapper = armed_mapper()
        self.assertIs(mapper.update(signal(0, attention=None), 0), Command.STOP)

    def test_disarm_cancels_an_active_turn(self):
        mapper = armed_mapper()
        mapper.update(signal(0, attention=90, blinks=[BlinkEvent.SINGLE]), 0)
        mapper.disarm()
        self.assertIs(mapper.update(signal(0.05, attention=90), 0.05), Command.STOP)


class TestTurns(unittest.TestCase):
    """MV-03 and SP-05."""

    def test_blink_produces_a_turn(self):
        mapper = armed_mapper(first_turn_direction="LEFT")
        command = mapper.update(signal(0, attention=90, blinks=[BlinkEvent.SINGLE]), 0)
        self.assertIs(command, Command.LEFT)

    def test_turn_reverts_to_the_previous_state(self):
        mapper = armed_mapper(turn_command_repeat_ms=150)
        mapper.update(signal(0.0, attention=90), 0.0)
        mapper.update(signal(0.1, attention=90, blinks=[BlinkEvent.SINGLE]), 0.1)

        self.assertIs(mapper.update(signal(0.2, attention=90), 0.2), Command.LEFT)
        self.assertIs(mapper.update(signal(0.3, attention=90), 0.3), Command.FORWARD)

    def test_turn_from_stop_reverts_to_stop(self):
        mapper = armed_mapper(turn_command_repeat_ms=150)
        mapper.update(signal(0.0, attention=10), 0.0)
        mapper.update(signal(2.0, attention=10), 2.0)  # settled into STOP
        mapper.update(signal(2.1, attention=10, blinks=[BlinkEvent.SINGLE]), 2.1)

        self.assertIs(mapper.update(signal(2.3, attention=10), 2.3), Command.STOP)

    def test_alternate_mode_swings_left_then_right(self):
        """SP-05 as specified: successive blinks alternate direction."""
        mapper = armed_mapper(blink_mode="alternate", first_turn_direction="LEFT")

        directions = []
        for index in range(4):
            t = index * 1.0
            directions.append(
                mapper.update(signal(t, attention=90, blinks=[BlinkEvent.SINGLE]), t)
            )
            mapper.update(signal(t + 0.5, attention=90), t + 0.5)

        self.assertEqual(
            directions,
            [Command.LEFT, Command.RIGHT, Command.LEFT, Command.RIGHT],
        )

    def test_alternate_mode_can_start_on_the_right(self):
        mapper = armed_mapper(blink_mode="alternate", first_turn_direction="RIGHT")
        command = mapper.update(signal(0, attention=90, blinks=[BlinkEvent.SINGLE]), 0)
        self.assertIs(command, Command.RIGHT)

    def test_single_double_mode_maps_the_two_gestures(self):
        mapper = armed_mapper(blink_mode="single_double", first_turn_direction="LEFT")

        single = mapper.update(signal(0, attention=90, blinks=[BlinkEvent.SINGLE]), 0)
        mapper.update(signal(1, attention=90), 1)
        double = mapper.update(signal(2, attention=90, blinks=[BlinkEvent.DOUBLE]), 2)

        self.assertIs(single, Command.LEFT)
        self.assertIs(double, Command.RIGHT)

    def test_single_double_mode_does_not_alternate(self):
        mapper = armed_mapper(blink_mode="single_double", first_turn_direction="LEFT")
        first = mapper.update(signal(0, attention=90, blinks=[BlinkEvent.SINGLE]), 0)
        mapper.update(signal(1, attention=90), 1)
        second = mapper.update(signal(2, attention=90, blinks=[BlinkEvent.SINGLE]), 2)

        self.assertIs(first, Command.LEFT)
        self.assertIs(second, Command.LEFT)

    def test_attention_keeps_updating_during_a_turn(self):
        """The base state must be current when the pulse ends."""
        mapper = armed_mapper(turn_command_repeat_ms=200)
        mapper.update(signal(0.0, attention=90), 0.0)
        mapper.update(signal(0.1, attention=90, blinks=[BlinkEvent.SINGLE]), 0.1)

        # Attention collapses while the vehicle is mid-turn.
        for t in (0.15, 0.2, 0.25):
            self.assertIs(mapper.update(signal(t, attention=10), t), Command.LEFT)

        self.assertIs(mapper.update(signal(1.3, attention=10), 1.3), Command.STOP)

    def test_rejects_an_unknown_blink_mode(self):
        with self.assertRaises(ValueError):
            CommandMapper(blink_mode="telepathy")


class TestWireProtocol(unittest.TestCase):
    """Appendix A: the four commands and their characters."""

    def test_wire_characters(self):
        self.assertEqual(Command.FORWARD.wire, "F")
        self.assertEqual(Command.LEFT.wire, "L")
        self.assertEqual(Command.RIGHT.wire, "R")
        self.assertEqual(Command.STOP.wire, "S")

    def test_reverse_lookup_is_complete(self):
        self.assertEqual(set(COMMANDS_BY_WIRE), {"F", "L", "R", "S"})
        for char, command in COMMANDS_BY_WIRE.items():
            self.assertEqual(command.wire, char)

    def test_is_turn(self):
        self.assertTrue(Command.LEFT.is_turn)
        self.assertTrue(Command.RIGHT.is_turn)
        self.assertFalse(Command.FORWARD.is_turn)
        self.assertFalse(Command.STOP.is_turn)


class TestMapToCommandHelper(unittest.TestCase):
    def test_accepts_names_characters_and_enums(self):
        self.assertEqual(map_to_command("FORWARD"), "FORWARD")
        self.assertEqual(map_to_command("forward"), "FORWARD")
        self.assertEqual(map_to_command("L"), "LEFT")
        self.assertEqual(map_to_command(Command.RIGHT), "RIGHT")

    def test_anything_unrecognised_fails_safe_to_stop(self):
        for value in ("BACKWARD", "", None, 42, object()):
            self.assertEqual(map_to_command(value), "STOP")


class TestFactory(unittest.TestCase):
    def test_built_from_config_defaults(self):
        import config as config_module

        mapper = create_mapper(config_module.load())

        self.assertEqual(mapper.forward_threshold, 60)
        self.assertEqual(mapper.stop_threshold, 40)
        self.assertAlmostEqual(mapper.stop_hold_s, 1.0)
        self.assertEqual(mapper.blink_mode, "alternate")
        self.assertFalse(mapper.armed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
