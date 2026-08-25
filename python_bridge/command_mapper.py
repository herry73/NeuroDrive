"""
Driving policy: conditioned signal -> discrete vehicle command.

Requirement coverage:
    MV-01  Four movement states: FORWARD, LEFT, RIGHT, STOP.
    MV-03  Turns are a timed pulse, after which the previous state resumes.
    SP-02  Attention at or above the forward threshold (default 60) drives.
    SP-03  Attention below the stop threshold (default 40) held for longer
           than 1 s stops the vehicle.
    SP-05  Blink gestures produce the direction commands.
    SF-03  Commands are withheld while the EEG signal quality is poor.
    UI-02  Nothing is emitted while the calibration phase is running.

All thresholds live in ``config.json`` (SP-07), so tuning the vehicle's
behaviour never requires editing this file.

Hysteresis
----------
Between the stop and forward thresholds the current state is *held*. Without
that dead band an attention value hovering around a single threshold makes
the vehicle stutter between FORWARD and STOP several times a second.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, List, Optional

from signal_processor import BlinkEvent, ProcessedSignal

if TYPE_CHECKING:  # imported for typing only, so vision stays an optional extra
    from vision import GestureEvent


class Command(str, Enum):
    """The four commands understood by the firmware."""

    FORWARD = "FORWARD"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    STOP = "STOP"

    @property
    def wire(self) -> str:
        """The single ASCII character sent on the wire (Appendix A)."""
        return WIRE_CHARS[self]

    @property
    def is_turn(self) -> bool:
        return self in (Command.LEFT, Command.RIGHT)


#: Command -> on-the-wire character. Kept next to the enum so the protocol
#: definition lives in exactly one place (see docs/INTERFACE_CONTRACT.md).
WIRE_CHARS = {
    Command.FORWARD: "F",
    Command.LEFT: "L",
    Command.RIGHT: "R",
    Command.STOP: "S",
}

#: Reverse lookup, used by the firmware simulator and the tests.
COMMANDS_BY_WIRE = {char: command for command, char in WIRE_CHARS.items()}

VALID_COMMANDS = [command.value for command in Command]


@dataclass
class MapperState:
    """Everything the dashboard needs to explain the current command."""

    command: Command = Command.STOP
    base_command: Command = Command.STOP
    reason: str = "not armed"
    armed: bool = False
    turn_remaining_s: float = 0.0
    low_attention_held_s: float = 0.0
    next_turn: Command = Command.LEFT
    turns_issued: int = 0
    safe_stops: int = 0


class CommandMapper:
    """Stateful map from :class:`ProcessedSignal` to :class:`Command`.

    One instance drives one vehicle. ``update`` must be called once per main
    loop cycle with a monotonic ``now``; it is the only method that mutates
    state, which keeps the behaviour reproducible in tests.
    """

    def __init__(
        self,
        attention_forward_threshold: float = 60,
        attention_stop_threshold: float = 40,
        attention_stop_hold_ms: int = 1000,
        turn_command_repeat_ms: int = 150,
        blink_mode: str = "alternate",
        first_turn_direction: str = "LEFT",
        require_good_signal: bool = True,
        turn_source: str = "blink",
    ) -> None:
        if attention_stop_threshold >= attention_forward_threshold:
            raise ValueError(
                "attention_stop_threshold must be below "
                "attention_forward_threshold (hysteresis)"
            )
        if blink_mode not in ("alternate", "single_double"):
            raise ValueError(f"unknown blink_mode: {blink_mode!r}")
        if turn_source not in ("blink", "vision", "both"):
            raise ValueError(f"unknown turn_source: {turn_source!r}")

        self.forward_threshold = attention_forward_threshold
        self.stop_threshold = attention_stop_threshold
        self.stop_hold_s = attention_stop_hold_ms / 1000.0
        self.turn_repeat_s = turn_command_repeat_ms / 1000.0
        self.blink_mode = blink_mode
        self.require_good_signal = require_good_signal
        self.turn_source = turn_source

        self._first_turn = Command[first_turn_direction]
        self._next_turn = self._first_turn
        self._base = Command.STOP
        self._low_since: Optional[float] = None
        self._turn_command: Optional[Command] = None
        self._turn_until = 0.0
        self._reason = "not armed"
        self._armed = False
        self._turns_issued = 0
        self._safe_stops = 0

    # -- lifecycle ----------------------------------------------------------

    def arm(self) -> None:
        """Allow movement. Called once the calibration phase finishes."""
        self._armed = True
        self._reason = "armed"

    def disarm(self) -> None:
        """Force STOP regardless of signal (calibration, e-stop, shutdown)."""
        self._armed = False
        self._base = Command.STOP
        self._clear_turn()
        self._reason = "not armed"

    @property
    def armed(self) -> bool:
        return self._armed

    # -- main entry point ---------------------------------------------------

    def update(
        self,
        processed: ProcessedSignal,
        now: float,
        gestures: Optional[List["GestureEvent"]] = None,
    ) -> Command:
        """Fold one conditioned signal into the policy and return a command.

        ``gestures`` carries accepted webcam raises for this cycle. It stays
        optional so every existing caller and test keeps working unchanged,
        and it is ignored unless ``turn_source`` admits vision.

        Gestures are deliberately gated behind the same safety checks as
        blinks. No arm, no trustworthy EEG, no turning: a hand raise must not
        be able to move a vehicle whose operator the headset has lost track
        of (EEG-05, SF-03).
        """
        if not self._armed:
            self._base = Command.STOP
            self._clear_turn()
            self._reason = "calibrating / not armed"
            return Command.STOP

        if not self._signal_is_drivable(processed):
            # EEG-05 / SF-03: no trustworthy signal means no movement.
            if self._base is not Command.STOP or self._turn_command is not None:
                self._safe_stops += 1
            self._base = Command.STOP
            self._clear_turn()
            self._low_since = None
            return Command.STOP

        self._apply_attention_policy(processed.attention, now)
        if self.turn_source in ("blink", "both"):
            self._apply_blink_events(processed.blink_events, now)
        if gestures and self.turn_source in ("vision", "both"):
            # Applied second, so a hand raise wins a tie against a blink that
            # landed in the same 50 ms cycle. Raising an arm is deliberate;
            # a blink can be involuntary.
            self._apply_gesture_events(gestures, now)

        if self._turn_command is not None:
            if now < self._turn_until:
                return self._turn_command
            self._clear_turn()

        return self._base

    # -- policy pieces ------------------------------------------------------

    def _signal_is_drivable(self, processed: ProcessedSignal) -> bool:
        if not processed.connected:
            self._reason = "no usable EEG signal -> safe stop"
            return False
        if processed.attention is None:
            self._reason = "waiting for first attention value"
            return False
        if self.require_good_signal and not processed.quality_ok:
            self._reason = (
                f"poor signal quality ({processed.poor_signal}) -> commands paused"
            )
            return False
        return True

    def _apply_attention_policy(self, attention: float, now: float) -> None:
        if attention >= self.forward_threshold:
            # SP-02
            self._low_since = None
            self._base = Command.FORWARD
            self._reason = (
                f"attention {attention:.0f} >= {self.forward_threshold:.0f} -> forward"
            )
        elif attention < self.stop_threshold:
            # SP-03: only stop once the low reading has persisted.
            if self._low_since is None:
                self._low_since = now
            held = now - self._low_since
            if held >= self.stop_hold_s:
                self._base = Command.STOP
                self._reason = (
                    f"attention {attention:.0f} < {self.stop_threshold:.0f} "
                    f"for {held:.1f}s -> stop"
                )
            else:
                self._reason = (
                    f"attention {attention:.0f} low for {held:.1f}s "
                    f"(stop at {self.stop_hold_s:.1f}s)"
                )
        else:
            # Dead band: hold whatever we were doing.
            self._low_since = None
            self._reason = (
                f"attention {attention:.0f} in dead band "
                f"[{self.stop_threshold:.0f}, {self.forward_threshold:.0f}) -> hold"
            )

    def _apply_blink_events(self, events: List[BlinkEvent], now: float) -> None:
        for event in events:
            direction = self._direction_for(event)
            self._turn_command = direction
            self._turn_until = now + self.turn_repeat_s
            self._turns_issued += 1
            self._reason = f"{event.value.lower()} blink -> {direction.value}"

    def _apply_gesture_events(self, events, now: float) -> None:
        """Turn accepted hand raises into turn pulses.

        Unlike blinks, there is nothing to decide here. The user raised a
        specific hand and the mapping is theirs: right hand, right turn.
        """
        for event in events:
            direction = Command[event.gesture.value]
            self._turn_command = direction
            self._turn_until = now + self.turn_repeat_s
            self._turns_issued += 1
            self._reason = f"{direction.value.lower()} hand raised -> {direction.value}"

    def _direction_for(self, event: BlinkEvent) -> Command:
        if self.blink_mode == "single_double":
            # SP-05 variant: one blink turns one way, two blinks the other.
            other = Command.RIGHT if self._first_turn is Command.LEFT else Command.LEFT
            return self._first_turn if event is BlinkEvent.SINGLE else other

        # "alternate": successive blinks swing the vehicle left, right, left...
        direction = self._next_turn
        self._next_turn = (
            Command.RIGHT if direction is Command.LEFT else Command.LEFT
        )
        return direction

    def _clear_turn(self) -> None:
        self._turn_command = None
        self._turn_until = 0.0

    # -- introspection ------------------------------------------------------

    def state(self, now: float) -> MapperState:
        """Snapshot for the console dashboard and the log."""
        turn_remaining = max(0.0, self._turn_until - now) if self._turn_command else 0.0
        held = 0.0 if self._low_since is None else max(0.0, now - self._low_since)
        command = (
            self._turn_command
            if self._turn_command is not None and turn_remaining > 0
            else self._base
        )
        if not self._armed:
            command = Command.STOP
        return MapperState(
            command=command,
            base_command=self._base,
            reason=self._reason,
            armed=self._armed,
            turn_remaining_s=turn_remaining,
            low_attention_held_s=held,
            next_turn=self._next_turn,
            turns_issued=self._turns_issued,
            safe_stops=self._safe_stops,
        )

    def reset(self) -> None:
        """Return to the power-on policy state (keeps thresholds)."""
        self._next_turn = self._first_turn
        self._base = Command.STOP
        self._low_since = None
        self._clear_turn()
        self._armed = False
        self._reason = "not armed"


def map_to_command(intent) -> str:
    """Coerce an arbitrary intent value to a valid command name.

    Kept as a defensive helper for callers outside the mapper (the keyboard
    override, replay tooling, tests): anything unrecognised becomes STOP.
    """
    if isinstance(intent, Command):
        return intent.value
    if isinstance(intent, str):
        upper = intent.strip().upper()
        if upper in VALID_COMMANDS:
            return upper
        if upper in COMMANDS_BY_WIRE:
            return COMMANDS_BY_WIRE[upper].value
    return Command.STOP.value


def create_mapper(config) -> CommandMapper:
    """Build a :class:`CommandMapper` from the configuration tree."""
    section = config.section("control")
    return CommandMapper(
        attention_forward_threshold=section.get("attention_forward_threshold", 60),
        attention_stop_threshold=section.get("attention_stop_threshold", 40),
        attention_stop_hold_ms=section.get("attention_stop_hold_ms", 1000),
        turn_command_repeat_ms=section.get("turn_command_repeat_ms", 150),
        blink_mode=section.get("blink_mode", "alternate"),
        first_turn_direction=section.get("first_turn_direction", "LEFT"),
        require_good_signal=section.get("require_good_signal", True),
        turn_source=section.get("turn_source", "blink"),
    )
