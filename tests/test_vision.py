"""
Webcam gesture logic, tested without a webcam.

The classification and debounce rules are pure functions of landmark
positions and an explicit ``now``, so the whole decision path runs here in
milliseconds with no camera, no model and no MediaPipe import. That is the
same split the rest of the suite uses: ``fake_esp32.py`` stands in for the
firmware, and here a handful of coordinates stands in for a person.

Requirement coverage:
    MV-01  The reader produces LEFT and RIGHT.
    SP-06  One raise produces exactly one turn.
"""

import unittest

import _bootstrap  # noqa: F401 - puts python_bridge on sys.path

from command_mapper import Command, CommandMapper
from signal_processor import ProcessedSignal
from vision import (
    LEFT_SHOULDER,
    LEFT_WRIST,
    RIGHT_SHOULDER,
    RIGHT_WRIST,
    Gesture,
    GestureDebouncer,
    GestureEvent,
    _Landmark,
    classify_raise,
)


def pose(left_wrist_y=0.9, right_wrist_y=0.9, shoulder_y=0.5, visibility=1.0):
    """Build a 33-landmark pose with the four points that matter set.

    Image coordinates run downward, so a wrist *above* a shoulder has the
    smaller y. Defaults put both hands down by the user's sides.
    """
    marks = [_Landmark(0.5, 0.5, visibility) for _ in range(33)]
    marks[LEFT_SHOULDER] = _Landmark(0.4, shoulder_y, visibility)
    marks[RIGHT_SHOULDER] = _Landmark(0.6, shoulder_y, visibility)
    marks[LEFT_WRIST] = _Landmark(0.35, left_wrist_y, visibility)
    marks[RIGHT_WRIST] = _Landmark(0.65, right_wrist_y, visibility)
    return marks


class TestClassifyRaise(unittest.TestCase):
    def test_no_hands_up_is_no_gesture(self):
        self.assertIsNone(classify_raise(pose()))

    def test_left_hand_above_shoulder(self):
        self.assertIs(classify_raise(pose(left_wrist_y=0.2)), Gesture.LEFT)

    def test_right_hand_above_shoulder(self):
        self.assertIs(classify_raise(pose(right_wrist_y=0.2)), Gesture.RIGHT)

    def test_both_hands_up_is_ambiguous(self):
        """Guessing between two raised arms is worse than doing nothing."""
        self.assertIsNone(classify_raise(pose(left_wrist_y=0.2, right_wrist_y=0.2)))

    def test_margin_rejects_a_wrist_level_with_the_shoulder(self):
        """Without the dead band this flickers once per frame."""
        marks = pose(left_wrist_y=0.49, shoulder_y=0.50)
        self.assertIsNone(classify_raise(marks, raise_margin=0.05))
        self.assertIs(classify_raise(marks, raise_margin=0.0), Gesture.LEFT)

    def test_low_visibility_landmarks_are_ignored(self):
        """The model reports a position for a limb it cannot actually see."""
        marks = pose(left_wrist_y=0.2, visibility=0.2)
        self.assertIsNone(classify_raise(marks, min_visibility=0.6))

    def test_missing_or_short_landmark_list(self):
        self.assertIsNone(classify_raise(None))
        self.assertIsNone(classify_raise([]))
        self.assertIsNone(classify_raise([_Landmark(0.5, 0.5)] * 4))


class TestDebouncer(unittest.TestCase):
    def test_a_raise_must_persist(self):
        d = GestureDebouncer(hold_frames=3, refractory_ms=0)
        self.assertIsNone(d.feed(Gesture.LEFT, 0.00))
        self.assertIsNone(d.feed(Gesture.LEFT, 0.05))
        event = d.feed(Gesture.LEFT, 0.10)
        self.assertIsNotNone(event)
        self.assertIs(event.gesture, Gesture.LEFT)

    def test_a_hand_passing_through_fires_nothing(self):
        d = GestureDebouncer(hold_frames=3, refractory_ms=0)
        d.feed(Gesture.LEFT, 0.00)
        d.feed(Gesture.LEFT, 0.05)
        d.feed(None, 0.10)
        self.assertIsNone(d.feed(Gesture.LEFT, 0.15))

    def test_holding_the_arm_up_produces_exactly_one_turn(self):
        """Otherwise a held arm streams turns at the frame rate."""
        d = GestureDebouncer(hold_frames=1, refractory_ms=0)
        self.assertIsNotNone(d.feed(Gesture.RIGHT, 0.0))
        for i in range(1, 40):
            self.assertIsNone(d.feed(Gesture.RIGHT, i * 0.05))

    def test_lowering_and_raising_again_fires_again(self):
        d = GestureDebouncer(hold_frames=1, refractory_ms=0)
        self.assertIsNotNone(d.feed(Gesture.RIGHT, 0.0))
        d.feed(None, 0.5)
        self.assertIsNotNone(d.feed(Gesture.RIGHT, 1.0))

    def test_switching_hands_without_lowering(self):
        """Left, then right, then left should all register."""
        d = GestureDebouncer(hold_frames=1, refractory_ms=0)
        self.assertIs(d.feed(Gesture.LEFT, 0.0).gesture, Gesture.LEFT)
        self.assertIs(d.feed(Gesture.RIGHT, 1.0).gesture, Gesture.RIGHT)
        self.assertIs(d.feed(Gesture.LEFT, 2.0).gesture, Gesture.LEFT)

    def test_refractory_period_blocks_a_rapid_second_event(self):
        d = GestureDebouncer(hold_frames=1, refractory_ms=1000)
        self.assertIsNotNone(d.feed(Gesture.LEFT, 0.0))
        d.feed(None, 0.1)
        self.assertIsNone(d.feed(Gesture.LEFT, 0.5))     # inside the window
        d.feed(None, 0.9)
        self.assertIsNotNone(d.feed(Gesture.LEFT, 1.1))  # outside it

    def test_repeat_while_held_when_asked_for(self):
        d = GestureDebouncer(hold_frames=1, refractory_ms=0, repeat_while_held_ms=500)
        self.assertIsNotNone(d.feed(Gesture.LEFT, 0.0))
        self.assertIsNone(d.feed(Gesture.LEFT, 0.3))
        self.assertIsNotNone(d.feed(Gesture.LEFT, 0.6))

    def test_reset_clears_everything(self):
        d = GestureDebouncer(hold_frames=1, refractory_ms=5000)
        d.feed(Gesture.LEFT, 0.0)
        d.reset()
        self.assertIsNotNone(d.feed(Gesture.LEFT, 0.1))

    def test_hold_frames_must_be_positive(self):
        with self.assertRaises(ValueError):
            GestureDebouncer(hold_frames=0)


def drivable(attention=70.0, now=0.0):
    """A conditioned signal good enough for the mapper to act on."""
    return ProcessedSignal(
        timestamp=now,
        connected=True,
        raw_attention=int(attention),
        attention=attention,
        meditation=50,
        poor_signal=0,
        quality_ok=True,
        window_filled=True,
        blink_events=[],
    )


class TestMapperTakesGestures(unittest.TestCase):
    def make(self, turn_source):
        mapper = CommandMapper(turn_source=turn_source)
        mapper.arm()
        return mapper

    def test_raised_right_hand_turns_right(self):
        mapper = self.make("vision")
        event = GestureEvent(timestamp=1.0, gesture=Gesture.RIGHT)
        self.assertIs(mapper.update(drivable(), 1.0, [event]), Command.RIGHT)

    def test_raised_left_hand_turns_left(self):
        mapper = self.make("vision")
        event = GestureEvent(timestamp=1.0, gesture=Gesture.LEFT)
        self.assertIs(mapper.update(drivable(), 1.0, [event]), Command.LEFT)

    def test_turn_is_a_pulse_and_then_forward_resumes(self):
        """MV-03: same behaviour as a blink turn."""
        mapper = self.make("vision")
        event = GestureEvent(timestamp=1.0, gesture=Gesture.LEFT)
        self.assertIs(mapper.update(drivable(), 1.0, [event]), Command.LEFT)
        self.assertIs(mapper.update(drivable(), 5.0), Command.FORWARD)

    def test_gestures_ignored_when_turn_source_is_blink(self):
        mapper = self.make("blink")
        event = GestureEvent(timestamp=1.0, gesture=Gesture.LEFT)
        self.assertIs(mapper.update(drivable(), 1.0, [event]), Command.FORWARD)

    def test_gestures_cannot_move_a_disarmed_vehicle(self):
        """UI-02: nothing moves during calibration, cameras included."""
        mapper = CommandMapper(turn_source="vision")
        event = GestureEvent(timestamp=1.0, gesture=Gesture.LEFT)
        self.assertIs(mapper.update(drivable(), 1.0, [event]), Command.STOP)

    def test_gestures_cannot_move_a_vehicle_with_no_eeg(self):
        """EEG-05: a hand raise must not drive a headset that fell off."""
        mapper = self.make("vision")
        dead = ProcessedSignal(
            timestamp=1.0,
            connected=False,
            raw_attention=None,
            attention=None,
            meditation=None,
            poor_signal=200,
            quality_ok=False,
            window_filled=False,
            blink_events=[],
        )
        event = GestureEvent(timestamp=1.0, gesture=Gesture.LEFT)
        self.assertIs(mapper.update(dead, 1.0, [event]), Command.STOP)

    def test_unknown_turn_source_is_rejected(self):
        with self.assertRaises(ValueError):
            CommandMapper(turn_source="telepathy")

    def test_existing_two_argument_calls_still_work(self):
        """The signature change must not break any existing caller."""
        mapper = self.make("blink")
        self.assertIs(mapper.update(drivable(), 1.0), Command.FORWARD)


if __name__ == "__main__":
    unittest.main()
