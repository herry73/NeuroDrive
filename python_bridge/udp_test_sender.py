"""
Standalone vehicle test tool. No EEG headset required.

This is the Week 1 integration tool (M5's deliverable): it proves the
laptop -> ESP32 -> L298N -> motors chain works before any brain signal is
involved, and it stays useful afterwards for wiring checks and demo warm-ups.

Usage
-----
    python udp_test_sender.py                  # run the standard sequence
    python udp_test_sender.py --command F      # send one command and exit
    python udp_test_sender.py --drive          # arrow-key driving
    python udp_test_sender.py --ping           # check the vehicle answers
    python udp_test_sender.py --transport serial --set transport.serial.port=COM6

All connection settings come from ``config.json``; the flags above are
shortcuts for overriding it.
"""

from __future__ import annotations

import argparse
import sys
import time

import config as config_module
from command_mapper import COMMANDS_BY_WIRE, Command, map_to_command
from keyboard_input import KEY_COMMANDS, KeyboardReader
from wifi_sender import CommandSender, TransportError, create_transport

#: The wiring-check sequence: every command, with pauses long enough to see
#: which way each wheel turns.
DEFAULT_SEQUENCE = [
    (Command.FORWARD, 2.0),
    (Command.STOP, 1.0),
    (Command.LEFT, 1.0),
    (Command.STOP, 1.0),
    (Command.RIGHT, 1.0),
    (Command.STOP, 1.0),
]


def _resolve(name: str) -> Command:
    """Accept 'F', 'f', 'FORWARD' or 'forward'."""
    upper = name.strip().upper()
    if upper in COMMANDS_BY_WIRE:
        return COMMANDS_BY_WIRE[upper]
    return Command(map_to_command(upper))


def run_sequence(sender: CommandSender, sequence=None) -> None:
    sequence = sequence or DEFAULT_SEQUENCE
    print("  Running vehicle test sequence. Ctrl-C to abort.\n")
    for command, hold in sequence:
        sender.send(command)
        print(f"    -> {command.value:<8} for {hold:.1f}s")
        time.sleep(hold)
    sender.send(Command.STOP)
    time.sleep(0.3)
    print("\n  Sequence complete.")
    _print_stats(sender)


def run_single(sender: CommandSender, command: Command, hold: float) -> None:
    print(f"  Sending {command.value} ('{command.wire}') for {hold:.1f}s")
    sender.send(command)
    time.sleep(hold)
    sender.send(Command.STOP)
    time.sleep(0.3)
    _print_stats(sender)


def run_ping(sender: CommandSender, attempts: int = 5) -> int:
    """Send STOP repeatedly and report whether the firmware acknowledges.

    Returns a process exit code: 0 if the vehicle answered.
    """
    print("  Pinging vehicle with STOP commands...")
    before = sender.stats.acks_received
    for index in range(attempts):
        sender.send(Command.STOP if index % 2 == 0 else Command.FORWARD)
        time.sleep(0.4)
    sender.send(Command.STOP)
    time.sleep(0.4)

    received = sender.stats.acks_received - before
    if received:
        print(f"  Vehicle answered ({received} acks, "
              f"avg rtt {sender.stats.avg_rtt_ms:.1f} ms).")
        return 0
    print("  No acknowledgement received.")
    print("  Check: same WiFi network? correct esp32_ip? firmware running?")
    print("         (ACKs also require transport.udp.expect_ack = true)")
    return 1


def run_drive(sender: CommandSender, turn_repeat_s: float = 0.3) -> None:
    """Arrow-key driving, the same control scheme as the bridge's override."""
    print("  Arrow keys / WASD to drive, space to stop, q to quit.\n")
    keyboard = KeyboardReader()
    if not keyboard.start():
        print("  Keyboard input is unavailable in this terminal.", file=sys.stderr)
        return

    base = Command.STOP
    turn = None
    turn_until = 0.0
    try:
        while True:
            now = time.monotonic()
            for key in keyboard.poll():
                if key in ("q", "ESC", "CTRL_C"):
                    return
                name = KEY_COMMANDS.get(key)
                if name is None:
                    continue
                command = Command(name)
                if command.is_turn:
                    turn, turn_until = command, now + turn_repeat_s
                else:
                    base, turn = command, None
                print(f"    -> {command.value}")

            if turn is not None and now >= turn_until:
                turn = None
            sender.send(turn if turn is not None else base)
            time.sleep(0.02)
    finally:
        keyboard.stop()
        sender.send(Command.STOP)
        time.sleep(0.2)
        _print_stats(sender)


def _print_stats(sender: CommandSender) -> None:
    stats = sender.stats
    print(f"    packets sent {stats.packets_sent}, acks {stats.acks_received}, "
          f"errors {stats.errors}")
    if stats.avg_rtt_ms is not None:
        print(f"    round trip   avg {stats.avg_rtt_ms:.1f} ms, "
              f"max {stats.max_rtt_ms:.1f} ms")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="udp_test_sender",
        description="Send commands to the NeuroDrive vehicle without a headset.",
    )
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--transport", choices=["udp", "serial"])
    parser.add_argument("--esp32-ip")
    parser.add_argument("--esp32-port", type=int)
    parser.add_argument(
        "--command", help="send a single command (F/L/R/S or FORWARD/LEFT/...)"
    )
    parser.add_argument(
        "--hold", type=float, default=1.0, help="seconds to hold --command (default 1)"
    )
    parser.add_argument("--drive", action="store_true", help="arrow-key driving mode")
    parser.add_argument("--ping", action="store_true", help="check the vehicle replies")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    config = config_module.load(args.config, overrides=args.set)
    if args.transport:
        config.set("transport.mode", args.transport)
    if args.esp32_ip:
        config.set("transport.udp.esp32_ip", args.esp32_ip)
    if args.esp32_port:
        config.set("transport.udp.esp32_port", args.esp32_port)

    transport = create_transport(config)
    sender = CommandSender(
        transport=transport,
        resend_interval_ms=config.get("transport.resend_interval_ms", 250),
        queue_size=config.get("transport.queue_size", 32),
    )

    print(f"\n  NeuroDrive test sender -> {transport.description}\n")
    try:
        sender.start()
    except TransportError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 2

    exit_code = 0
    try:
        if args.ping:
            exit_code = run_ping(sender)
        elif args.drive:
            run_drive(sender)
        elif args.command:
            run_single(sender, _resolve(args.command), args.hold)
        else:
            run_sequence(sender)
    except KeyboardInterrupt:
        print("\n  Aborted.")
    finally:
        sender.stop(final_command=Command.STOP)
    print()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
