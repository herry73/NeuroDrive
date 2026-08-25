"""
NeuroDrive bridge -- application entry point.

Wires the five modules together and runs the control loop:

    EEGReader -> SignalProcessor -> CommandMapper -> CommandSender -> ESP32

Requirement coverage:
    EEG-03  Waits (up to 30 s) for a stable headset connection at startup.
    UI-01   Live console dashboard.
    UI-02   Calibration phase during which the vehicle stays stopped.
    UI-03   Keyboard override for testing and for the demo-day fallback.
    NFR 3.2 Control loop runs at loop.rate_hz (>= 10 Hz, default 20 Hz).
    NFR 3.3 Acquisition and transmission each run on their own thread, so
            neither can stall the control loop.

Run ``python main.py --help`` for the command line, or see README.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional

import calibration as calibration_module
import config as config_module
from calibration import Calibrator
from command_mapper import Command, create_mapper
from console_ui import Dashboard, print_banner
from data_logger import SessionRecorder, make_run_id, setup_logging
from eeg_reader import EEGReader
from eeg_sources import create_source
from keyboard_input import KEY_COMMANDS, KeyboardReader
from signal_processor import ProcessedSignal, create_processor
from vision import create_vision
from wifi_sender import CommandSender, TransportError, create_transport

LOG = logging.getLogger("neurodrive.main")

CONNECT_TIMEOUT_S = 30.0  # EEG-03


class ManualController:
    """Keyboard driving model (UI-03).

    Mirrors the mapper's behaviour so the vehicle feels the same in either
    mode: a turn key produces a short pulse, after which the previous
    forward/stop state resumes.
    """

    def __init__(self, turn_repeat_s: float = 0.15) -> None:
        self.turn_repeat_s = turn_repeat_s
        self._base = Command.STOP
        self._turn: Optional[Command] = None
        self._turn_until = 0.0
        self.reason = "keyboard override"

    def press(self, command: Command, now: float) -> None:
        if command.is_turn:
            self._turn = command
            self._turn_until = now + self.turn_repeat_s
            self.reason = f"key -> {command.value}"
        else:
            self._base = command
            self._turn = None
            self.reason = f"key -> {command.value}"

    def command(self, now: float) -> Command:
        if self._turn is not None:
            if now < self._turn_until:
                return self._turn
            self._turn = None
        return self._base

    def reset(self) -> None:
        self._base = Command.STOP
        self._turn = None
        self.reason = "keyboard override"


class Bridge:
    """Owns every component for one run of the bridge."""

    def __init__(self, config, args) -> None:
        self.config = config
        self.args = args
        self.run_id = make_run_id()
        self.log_path = setup_logging(config, self.run_id)

        self.processor = create_processor(config)
        self.mapper = create_mapper(config)
        self.reader = EEGReader(
            source_factory=lambda: create_source(config),
            signal_timeout_ms=config.get("eeg.signal_timeout_ms", 2000),
            reconnect_attempts=config.get("eeg.serial.reconnect_attempts", 3),
            reconnect_delay_s=config.get("eeg.serial.reconnect_delay_s", 2.0),
        )
        self.transport = create_transport(config)
        self.sender = CommandSender(
            transport=self.transport,
            resend_interval_ms=config.get("transport.resend_interval_ms", 250),
            queue_size=config.get("transport.queue_size", 32),
            turn_burst=config.get("transport.turn_burst", 3),
        )
        self.dashboard = Dashboard(
            enabled=config.get("ui.console_dashboard", True) and not args.no_dashboard,
            colour=config.get("ui.colour", True),
            refresh_hz=config.get("ui.refresh_hz", 10),
        )
        self.keyboard = KeyboardReader(
            enabled=config.get("ui.keyboard_override", True) and not args.no_keyboard
        )
        self.vision = create_vision(config)
        self.recorder = SessionRecorder(config, self.run_id)
        self.manual = ManualController(
            turn_repeat_s=config.get("control.turn_command_repeat_ms", 150) / 1000.0
        )

        self.override_active = bool(args.keyboard)
        self.calibrator: Optional[Calibrator] = None
        self.running = False
        self._loop_hz = 0.0
        self._started_at = time.monotonic()

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> int:
        print_banner(self.config, self.transport.description, self.log_path)

        try:
            self.sender.start()
        except TransportError as exc:
            print(f"  ERROR: cannot open the vehicle link: {exc}", file=sys.stderr)
            print("  Check transport.mode / IP / COM port in config.json.", file=sys.stderr)
            return 2

        self.reader.start()
        self.vision.start()
        self.keyboard.start()
        self.running = True
        self._started_at = time.monotonic()

        try:
            self._await_connection()
            self._begin_calibration()
            self._loop()
        except KeyboardInterrupt:
            LOG.info("interrupted by operator")
        finally:
            self._shutdown()
        return 0

    def _await_connection(self) -> None:
        print(f"  Connecting to EEG source (timeout {CONNECT_TIMEOUT_S:.0f}s)...")
        if self.reader.wait_for_connection(CONNECT_TIMEOUT_S):
            info = self.reader.info
            print(f"  Connected via {info.source_name} in {info.connect_seconds:.1f}s.")
        else:
            # Not fatal: the reader keeps retrying, and the vehicle simply
            # stays stopped until real samples arrive (EEG-05).
            print("  WARNING: no EEG connection yet -- the vehicle will not move.")
            print(f"           {self.reader.info.last_error}")
            print("           Press 'k' for keyboard override.")

    def _begin_calibration(self) -> None:
        seconds = float(self.config.get("control.calibration_seconds", 15))
        if self.args.skip_calibration or seconds <= 0:
            self.mapper.arm()
            print("  Calibration skipped -- vehicle is ARMED.\n")
            return
        self.calibrator = Calibrator(seconds)
        self.mapper.disarm()
        print(f"  Calibrating for {seconds:.0f}s -- the vehicle will not move.\n")

    def _shutdown(self) -> None:
        self.running = False
        self.dashboard.close()
        print("\n  Stopping vehicle and closing down...")
        self.mapper.disarm()
        try:
            self.sender.stop(final_command=Command.STOP)
        except Exception:  # pragma: no cover - best effort on the way out
            LOG.exception("error stopping sender")
        self.reader.stop()
        self.vision.stop()
        self.keyboard.stop()
        self.recorder.close()
        self._print_summary()

    def _print_summary(self) -> None:
        stats = self.sender.stats
        info = self.reader.info
        elapsed = time.monotonic() - self._started_at
        print("  " + "-" * 52)
        print(f"  Session {self.run_id}  ({elapsed:.0f}s)")
        print(f"    EEG samples      : {info.samples_received} "
              f"(dropped {info.samples_dropped}, reconnects {info.reconnect_count})")
        print(f"    Blinks qualified : {self.processor.stats.blinks_qualified} "
              f"(rejected {self.processor.stats.blinks_rejected_threshold} weak / "
              f"{self.processor.stats.blinks_rejected_debounce} bounced)")
        print(f"    Packets sent     : {stats.packets_sent} "
              f"({stats.commands_changed} changes, {stats.keepalives} keepalives)")
        print(f"    Acks received    : {stats.acks_received}")
        vinfo = self.vision.info
        if vinfo.enabled:
            seen = (100.0 * vinfo.pose_frames / vinfo.frames) if vinfo.frames else 0.0
            print(f"    Hand gestures    : {vinfo.gestures} "
                  f"({vinfo.frames} frames, user visible in {seen:.0f}%)")
            if vinfo.last_error:
                print(f"    Vision warning   : {vinfo.last_error}")
        if stats.avg_rtt_ms is not None:
            print(f"    Round trip       : avg {stats.avg_rtt_ms:.1f} ms, "
                  f"max {stats.max_rtt_ms:.1f} ms")
        if self.recorder.path:
            print(f"    Session CSV      : {self.recorder.path} "
                  f"({self.recorder.rows_written} rows)")
        print(f"    Log file         : {self.log_path}")
        print("  " + "-" * 52 + "\n")

    # -- control loop -------------------------------------------------------

    def _loop(self) -> None:
        period = 1.0 / float(self.config.get("loop.rate_hz", 20))
        deadline = self.args.duration and (time.monotonic() + self.args.duration)
        next_tick = time.monotonic()

        while self.running:
            now = time.monotonic()

            samples = self.reader.read_all()
            for sample in samples:
                self.processor.ingest(sample)
            processed = self.processor.tick(now)

            gestures = self.vision.read_all()

            self._handle_keys(now)
            if not self.running:
                break

            command, reason = self._decide(processed, now, gestures)
            self.sender.send(command)
            self.recorder.log_cycle(processed, command, reason, samples)
            self._render(processed, now)

            if deadline and now >= deadline:
                LOG.info("duration limit reached")
                break

            next_tick += period
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # Fell behind (a slow terminal, a GC pause): resynchronise
                # instead of spiralling into a catch-up burst.
                next_tick = time.monotonic()
            self._update_loop_rate(now)

    def _decide(self, processed: ProcessedSignal, now: float, gestures=None):
        """Return the command to transmit this cycle and why."""
        if self.calibrator is not None:
            self.calibrator.feed(processed)
            if self.calibrator.is_done():
                self._finish_calibration()
            else:
                remaining = self.calibrator.remaining(now)
                return Command.STOP, f"calibrating ({remaining:.0f}s left)"

        if self.override_active:
            command = self.manual.command(now)
            # Keep the mapper's state fresh so switching back is seamless,
            # but ignore what it wants while the operator is driving.
            self.mapper.update(processed, now, gestures)
            return command, self.manual.reason

        command = self.mapper.update(processed, now, gestures)
        return command, self.mapper.state(now).reason

    def _finish_calibration(self) -> None:
        result = self.calibrator.finish()
        self.calibrator = None
        self.dashboard.message(result.summary())

        if self.args.apply_calibration and calibration_module.apply_result(
            self.config, result
        ):
            self.mapper = create_mapper(self.config)
            self.dashboard.message(
                "  Applied calibrated thresholds to this session."
            )

        self.mapper.arm()
        self.dashboard.message("  Vehicle ARMED.\n")

    def _handle_keys(self, now: float) -> None:
        for key in self.keyboard.poll():
            if key in ("q", "CTRL_C", "ESC"):
                self.running = False
                return

            if key == "k":
                self.override_active = not self.override_active
                self.manual.reset()
                self.dashboard.notify(
                    "keyboard override ON" if self.override_active else "EEG control ON"
                )
                LOG.info("keyboard override -> %s", self.override_active)
                continue

            if key == "c":
                seconds = float(self.config.get("control.calibration_seconds", 15))
                self.calibrator = Calibrator(seconds)
                self.mapper.disarm()
                self.dashboard.notify(f"recalibrating for {seconds:.0f}s")
                LOG.info("recalibration requested")
                continue

            if key == "ENTER":
                if not self.mapper.armed and self.calibrator is None:
                    self.mapper.arm()
                    self.dashboard.notify("re-armed")
                continue

            command_name = KEY_COMMANDS.get(key)
            if command_name is None:
                continue

            if self.override_active:
                self.manual.press(Command(command_name), now)
            elif command_name == "STOP":
                # Software emergency stop while under EEG control. The
                # hardware button (SF-01) remains the authoritative one.
                self.mapper.disarm()
                self.dashboard.notify("SOFT E-STOP -- press ENTER to re-arm", 6.0)
                LOG.warning("soft e-stop triggered from keyboard")

    def _render(self, processed: ProcessedSignal, now: float) -> None:
        self.dashboard.render(
            reader_info=self.reader.info,
            processed=processed,
            mapper_state=self.mapper.state(now),
            sender_stats=self.sender.stats,
            transport_description=self.transport.description,
            elapsed_s=now - self._started_at,
            loop_hz=self._loop_hz,
            override_active=self.override_active,
            vision_info=self.vision.info,
        )

    def _update_loop_rate(self, cycle_started: float) -> None:
        delta = time.monotonic() - cycle_started
        if delta <= 0:
            return
        instantaneous = 1.0 / delta
        # Exponential moving average keeps the displayed rate readable.
        self._loop_hz = (
            instantaneous if self._loop_hz == 0 else 0.9 * self._loop_hz + 0.1 * instantaneous
        )


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neurodrive",
        description="NeuroDrive EEG-to-vehicle bridge.",
        epilog="Every setting can also be overridden with --set key=value.",
    )
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a config value, e.g. --set control.attention_forward_threshold=65",
    )
    parser.add_argument(
        "--source",
        choices=["serial", "mock", "replay"],
        help="EEG source (shortcut for --set eeg.source=...)",
    )
    parser.add_argument(
        "--transport",
        choices=["udp", "serial"],
        help="vehicle link (shortcut for --set transport.mode=...)",
    )
    parser.add_argument("--esp32-ip", help="override transport.udp.esp32_ip")
    parser.add_argument("--esp32-port", type=int, help="override transport.udp.esp32_port")
    parser.add_argument("--replay-file", help="CSV to replay (implies --source replay)")
    parser.add_argument(
        "--keyboard", action="store_true", help="start in keyboard override mode"
    )
    parser.add_argument(
        "--vision",
        action="store_true",
        help="turn with raised hands from the webcam (implies --turn-source vision)",
    )
    parser.add_argument(
        "--vision-preview",
        action="store_true",
        help="show the camera window with the tracked shoulders and wrists",
    )
    parser.add_argument(
        "--turn-source",
        choices=["blink", "vision", "both"],
        help="what produces LEFT/RIGHT (shortcut for --set control.turn_source=...)",
    )
    parser.add_argument(
        "--skip-calibration", action="store_true", help="arm the vehicle immediately"
    )
    parser.add_argument(
        "--apply-calibration",
        action="store_true",
        help="use the thresholds suggested by the calibration phase",
    )
    parser.add_argument(
        "--duration", type=float, help="run for N seconds then exit (for test runs)"
    )
    parser.add_argument("--no-dashboard", action="store_true", help="disable the live display")
    parser.add_argument("--no-keyboard", action="store_true", help="disable keyboard input")
    parser.add_argument(
        "--print-config", action="store_true", help="show the merged config and exit"
    )
    return parser


def apply_cli_overrides(config, args) -> None:
    """Fold the convenience flags into the configuration tree."""
    if args.source:
        config.set("eeg.source", args.source)
    if args.replay_file:
        config.set("eeg.source", "replay")
        config.set("eeg.replay.csv_path", args.replay_file)
    if args.transport:
        config.set("transport.mode", args.transport)
    if args.esp32_ip:
        config.set("transport.udp.esp32_ip", args.esp32_ip)
    if args.esp32_port:
        config.set("transport.udp.esp32_port", args.esp32_port)
    if args.turn_source:
        config.set("control.turn_source", args.turn_source)
    if args.vision_preview:
        config.set("vision.preview", True)
    if args.vision or args.vision_preview:
        config.set("vision.enabled", True)
        if not args.turn_source:
            # --vision on its own means "turn with my hands". Asking for both
            # is still possible, it just has to be said explicitly.
            config.set("control.turn_source", "vision")
    if args.turn_source in ("vision", "both"):
        # Only the flag auto-enables. A config.json that asks for vision turns
        # while leaving the camera off is a mistake worth reporting, not one
        # worth silently repairing.
        config.set("vision.enabled", True)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = config_module.load(args.config, overrides=args.set)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot load configuration: {exc}", file=sys.stderr)
        return 2

    apply_cli_overrides(config, args)

    problems = config.validate()
    if problems:
        print("ERROR: configuration is invalid:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    if args.print_config:
        import json

        print(json.dumps(config.as_dict(), indent=2))
        return 0

    return Bridge(config, args).run()


if __name__ == "__main__":
    sys.exit(main())
