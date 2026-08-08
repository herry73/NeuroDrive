"""
ESP32 firmware simulator.

Speaks exactly the protocol in ``docs/INTERFACE_CONTRACT.md``, and mirrors
the real firmware's behaviour: the same four-state machine, the same 300 ms
turn pulse, the same 2 s watchdog, the same ``ACK:`` reply.

Why it exists:

* M5 and M7 can test the bridge end to end before the vehicle is wired, and
  on any laptop, in CI, or on the train.
* It is the executable check that the two halves of the interface contract
  actually agree -- ``test_integration.py`` drives the real bridge modules
  into this simulator and asserts on the states it reaches.
* On demo day it is a fast way to prove "the laptop side is fine" when
  something goes wrong with the hardware.

Usage
-----
    python fake_esp32.py                 # listen on 0.0.0.0:4210
    python fake_esp32.py --port 4210 --verbose
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import argparse
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Tuple

#: Must match firmware/neurodrive_firmware/config.h.
TURN_PULSE_S = 0.300
WATCHDOG_TIMEOUT_S = 2.000
DEFAULT_PORT = 4210


class MotorState(str, Enum):
    STOP = "STOP"
    FORWARD = "FORWARD"
    TURN_LEFT = "TURN_LEFT"
    TURN_RIGHT = "TURN_RIGHT"


class StopReason(str, Enum):
    NONE = "NONE"
    COMMAND = "COMMAND"
    WATCHDOG = "WATCHDOG"
    ESTOP = "ESTOP"


#: Wire character -> the state it requests. 'P' is the keepalive: it feeds
#: the watchdog without changing the state.
COMMAND_STATES = {
    "F": MotorState.FORWARD,
    "L": MotorState.TURN_LEFT,
    "R": MotorState.TURN_RIGHT,
    "S": MotorState.STOP,
}


@dataclass
class Transition:
    """One recorded state change, for assertions and for the console log."""

    at: float
    state: MotorState
    reason: StopReason
    trigger: str


@dataclass
class VehicleStats:
    packets: int = 0
    rejected: int = 0
    acks_sent: int = 0
    watchdog_trips: int = 0
    turns: int = 0
    transitions: List[Transition] = field(default_factory=list)


class VehicleModel:
    """The firmware's state machine, in Python.

    Deliberately mirrors ``motor_control.cpp`` line for line in behaviour --
    including the rule that re-sending the turn already in progress does not
    restart its timer.
    """

    def __init__(
        self,
        turn_pulse_s: float = TURN_PULSE_S,
        watchdog_timeout_s: float = WATCHDOG_TIMEOUT_S,
        on_change: Optional[Callable[[Transition], None]] = None,
    ) -> None:
        self.turn_pulse_s = turn_pulse_s
        self.watchdog_timeout_s = watchdog_timeout_s
        self.on_change = on_change

        self.state = MotorState.STOP
        self.base_state = MotorState.STOP
        self.stop_reason = StopReason.NONE
        self.estop_latched = False
        self.stats = VehicleStats()

        self._turn_started = 0.0
        # Boot state: already tripped, exactly as safetySetup() arranges.
        self._last_command_at = -watchdog_timeout_s
        self._watchdog_tripped = True

    # -- state machine ------------------------------------------------------

    def _enter(self, state: MotorState, trigger: str, now: float) -> None:
        if state is self.state:
            return
        self.state = state
        transition = Transition(
            at=now, state=state, reason=self.stop_reason, trigger=trigger
        )
        self.stats.transitions.append(transition)
        if self.on_change is not None:
            self.on_change(transition)

    def command(self, char: str, now: float) -> bool:
        """Apply one wire command. Returns True if it was understood."""
        char = char.upper()
        if char == "P":
            self._feed_watchdog(now)
            return True
        if char not in COMMAND_STATES:
            return False

        requested = COMMAND_STATES[char]
        self._feed_watchdog(now)

        if self.estop_latched and requested is not MotorState.STOP:
            return True  # accepted but refused, as the firmware does

        if requested is MotorState.STOP:
            self.base_state = MotorState.STOP
            # An explicit STOP clears a WATCHDOG reason, but never a latched
            # e-stop -- the button decides when that clears.
            if not self.estop_latched:
                self.stop_reason = StopReason.COMMAND
            self._enter(MotorState.STOP, char, now)
        elif requested is MotorState.FORWARD:
            self.base_state = MotorState.FORWARD
            self.stop_reason = StopReason.NONE
            if self.state not in (MotorState.TURN_LEFT, MotorState.TURN_RIGHT):
                self._enter(MotorState.FORWARD, char, now)
        else:  # a turn
            self.stop_reason = StopReason.NONE
            if self.state is not requested:
                self._turn_started = now
                self.stats.turns += 1
                self._enter(requested, char, now)
        return True

    def tick(self, now: float) -> None:
        """Expire turn pulses and enforce the watchdog."""
        if self.state in (MotorState.TURN_LEFT, MotorState.TURN_RIGHT):
            if now - self._turn_started >= self.turn_pulse_s:
                self._enter(
                    MotorState.STOP if self.estop_latched else self.base_state,
                    "turn-expired",
                    now,
                )

        if self.estop_latched:
            return

        if now - self._last_command_at >= self.watchdog_timeout_s:
            if not self._watchdog_tripped:
                self._watchdog_tripped = True
                self.stats.watchdog_trips += 1
            if self.state is not MotorState.STOP or self.stop_reason is not StopReason.WATCHDOG:
                self.base_state = MotorState.STOP
                self.stop_reason = StopReason.WATCHDOG
                self._enter(MotorState.STOP, "watchdog", now)

    def press_estop(self, now: float) -> None:
        self.estop_latched = True
        self.base_state = MotorState.STOP
        self.stop_reason = StopReason.ESTOP
        self._enter(MotorState.STOP, "estop", now)

    def release_estop(self) -> None:
        self.estop_latched = False
        if self.stop_reason is StopReason.ESTOP:
            self.stop_reason = StopReason.COMMAND

    def _feed_watchdog(self, now: float) -> None:
        self._last_command_at = now
        self._watchdog_tripped = False

    @property
    def watchdog_tripped(self) -> bool:
        return self._watchdog_tripped

    def ack(self, char: str) -> bytes:
        return f"ACK:{char.upper()}:{self.state.value}\n".encode("ascii")


class FakeESP32:
    """UDP server wrapping a :class:`VehicleModel`.

    Runs its own thread and ticks the model continuously, so watchdog and
    turn-pulse timing behave the same as on real hardware even when no
    packets are arriving.
    """

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        host: str = "127.0.0.1",
        send_acks: bool = True,
        verbose: bool = False,
        tick_interval_s: float = 0.005,
    ) -> None:
        self.host = host
        self.port = port
        self.send_acks = send_acks
        self.verbose = verbose
        self.tick_interval_s = tick_interval_s

        self.model = VehicleModel(on_change=self._on_change)
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._start_time = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> int:
        """Bind and start serving. Returns the bound port."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, self.port))
        self._socket.settimeout(self.tick_interval_s)
        self.port = self._socket.getsockname()[1]

        self._start_time = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fake-esp32", daemon=True)
        self._thread.start()
        return self.port

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> "FakeESP32":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- server loop --------------------------------------------------------

    def _run(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                data, peer = self._socket.recvfrom(256)
            except socket.timeout:
                self.model.tick(time.monotonic())
                continue
            except OSError:
                break

            now = time.monotonic()
            self.model.stats.packets += 1

            for line in data.replace(b"\r", b"\n").split(b"\n"):
                text = line.strip().decode("ascii", "replace")
                if not text:
                    continue
                char = self._normalise(text)
                if char is None or not self.model.command(char, now):
                    self.model.stats.rejected += 1
                    if self.verbose:
                        print(f"  [{self._elapsed():7.3f}] rejected {text!r}")
                    continue
                if self.send_acks:
                    try:
                        self._socket.sendto(self.model.ack(char), peer)
                        self.model.stats.acks_sent += 1
                    except OSError:
                        pass

            self.model.tick(now)

    @staticmethod
    def _normalise(text: str) -> Optional[str]:
        """Accept 'F' or 'FORWARD', as the firmware's decodeCommand() does."""
        upper = text.upper()
        if len(upper) == 1:
            return upper if upper in ("F", "L", "R", "S", "P") else None
        long_forms = {
            "FORWARD": "F",
            "LEFT": "L",
            "RIGHT": "R",
            "STOP": "S",
            "PING": "P",
        }
        return long_forms.get(upper)

    # -- reporting ----------------------------------------------------------

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def _on_change(self, transition: Transition) -> None:
        if not self.verbose:
            return
        print(
            f"  [{transition.at - self._start_time:7.3f}] "
            f"{transition.state.value:<10} reason={transition.reason.value:<8} "
            f"trigger={transition.trigger}"
        )

    @property
    def state(self) -> MotorState:
        return self.model.state

    @property
    def stats(self) -> VehicleStats:
        return self.model.stats

    def states_seen(self) -> List[MotorState]:
        return [transition.state for transition in self.model.stats.transitions]

    def wait_for_state(self, state: MotorState, timeout: float = 3.0) -> bool:
        """Block until the model reaches ``state``. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.model.state is state:
                return True
            time.sleep(0.005)
        return False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fake_esp32",
        description="Simulate the NeuroDrive vehicle firmware over UDP.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address (default 0.0.0.0 so another machine can reach it)",
    )
    parser.add_argument("--no-acks", action="store_true", help="do not reply (COM-05 off)")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args(argv)

    vehicle = FakeESP32(
        port=args.port,
        host=args.host,
        send_acks=not args.no_acks,
        verbose=not args.quiet,
    )
    port = vehicle.start()
    print(f"\n  Fake ESP32 listening on {args.host}:{port}")
    print(f"  turn pulse {TURN_PULSE_S * 1000:.0f} ms, "
          f"watchdog {WATCHDOG_TIMEOUT_S * 1000:.0f} ms, "
          f"acks {'off' if args.no_acks else 'on'}")
    print("  Point config.json at this address, then run main.py. Ctrl-C to stop.\n")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        vehicle.stop()
        stats = vehicle.stats
        print("\n  ----------------------------------------")
        print(f"  packets     : {stats.packets} ({stats.rejected} rejected)")
        print(f"  acks sent   : {stats.acks_sent}")
        print(f"  transitions : {len(stats.transitions)}")
        print(f"  turns       : {stats.turns}")
        print(f"  watchdog    : {stats.watchdog_trips} trip(s)")
        print("  ----------------------------------------\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
