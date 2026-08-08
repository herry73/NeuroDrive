"""
Latency benchmark (M7, Weeks 3 and 5).

COM-03 requires end-to-end latency under 500 ms, from EEG event to motor
action. That total splits into three parts, and only two of them can be
measured in software:

    1. Headset -> laptop        ~1 s, and NOT measurable from here.
                                The MindWave reports eSense values once per
                                second, so a change in concentration is
                                visible to us on average half a sampling
                                period after it happens. This is a property
                                of the headset, not of our code, and must be
                                stated as such in the report.

    2. Bridge processing        sample in -> command decided.   MEASURED
    3. Transport + firmware     command sent -> vehicle acts.   MEASURED

This tool measures parts 2 and 3 and reports them separately, so the report
can make an honest claim: "the software adds N ms to the headset's own
sampling latency" rather than a single number that hides the difference.

Run against the simulator (any laptop):

    python latency_benchmark.py --samples 300

Run against real hardware (vehicle powered, on the same network):

    python latency_benchmark.py --target 192.168.4.1 --samples 300
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

import config as config_module
from command_mapper import Command, create_mapper
from eeg_sources import EEGSample
from fake_esp32 import FakeESP32
from signal_processor import create_processor
from wifi_sender import CommandSender, TransportError, UdpTransport

#: COM-03 budget for the parts we control.
SOFTWARE_BUDGET_MS = 500.0


@dataclass
class Measurements:
    processing_ms: List[float]
    round_trip_ms: List[float]

    def summary(self, values: List[float]) -> str:
        if not values:
            return "no samples"
        ordered = sorted(values)
        percentile95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        return (
            f"n={len(values):<5} "
            f"mean={statistics.fmean(values):7.3f}  "
            f"median={statistics.median(values):7.3f}  "
            f"p95={percentile95:7.3f}  "
            f"max={max(values):7.3f}  (ms)"
        )


def measure_processing(sample_count: int) -> List[float]:
    """Part 2: how long the bridge takes to turn a sample into a command."""
    config = config_module.load()
    processor = create_processor(config)
    mapper = create_mapper(config)
    mapper.arm()

    durations: List[float] = []
    for index in range(sample_count):
        # Alternate above and below the thresholds so both branches of the
        # policy, including the blink path, are exercised.
        attention = 75 if index % 20 < 10 else 25
        blink = 200 if index % 50 == 0 else None
        sample = EEGSample(
            timestamp=index * 0.05,
            attention=attention,
            poor_signal=0,
            blink_strength=blink,
        )

        started = time.perf_counter()
        processor.ingest(sample)
        processed = processor.tick(sample.timestamp)
        mapper.update(processed, sample.timestamp)
        durations.append((time.perf_counter() - started) * 1000.0)

    return durations


def measure_round_trip(
    host: str, port: int, sample_count: int, interval_s: float = 0.05
) -> List[float]:
    """Part 3: command sent -> acknowledgement back from the vehicle.

    The acknowledgement is emitted by the firmware *after* it has applied the
    command to the motor state machine, so this figure includes the motor
    reaction, not just the network hop.
    """
    sender = CommandSender(
        transport=UdpTransport(host, port, listen_port=0, expect_ack=True),
        resend_interval_ms=10_000,  # measure explicit sends, not keepalives
    )
    sender.start()
    try:
        # Alternate so every packet is a genuine state change.
        for index in range(sample_count):
            sender.send(Command.FORWARD if index % 2 == 0 else Command.STOP)
            time.sleep(interval_s)
        sender.send(Command.STOP)
        time.sleep(0.3)
        return list(sender.stats.rtt_samples)
    finally:
        sender.stop(final_command=Command.STOP)


def report(measurements: Measurements, target: str) -> bool:
    """Print the table and return True if the software budget is met."""
    processing = measurements.processing_ms
    round_trip = measurements.round_trip_ms

    print()
    print("  NeuroDrive latency benchmark")
    print("  " + "=" * 68)
    print(f"  target: {target}")
    print()
    print("  Part 2  bridge processing (sample in -> command decided)")
    print(f"          {measurements.summary(processing)}")
    print()
    print("  Part 3  transport + firmware (command sent -> vehicle acted)")
    print(f"          {measurements.summary(round_trip)}")
    if target.startswith("simulator"):
        print("          NOTE: against the simulator this figure is dominated by")
        print("          the host OS socket-timer granularity (~16 ms on Windows),")
        print("          not by the protocol. Use real hardware for report numbers.")
    print()

    if not processing or not round_trip:
        print("  INCOMPLETE: not enough samples to judge.")
        return False

    # The acknowledgement travels back as well, so the one-way command path
    # is about half the measured round trip.
    one_way = statistics.fmean(round_trip) / 2.0
    software_total = statistics.fmean(processing) + one_way

    print("  " + "-" * 68)
    print(f"  software path (part 2 + one-way part 3) : {software_total:7.1f} ms")
    print(f"  COM-03 budget                           : {SOFTWARE_BUDGET_MS:7.1f} ms")
    print()
    print("  Not included: the headset's own reporting latency. The MindWave")
    print("  emits eSense values at 1 Hz, so add roughly 500 ms on average")
    print("  for the brain-to-laptop leg. State this separately in the report.")
    print("  " + "=" * 68)

    within_budget = software_total < SOFTWARE_BUDGET_MS
    print(f"\n  RESULT: {'PASS' if within_budget else 'FAIL'} "
          f"(software path {'within' if within_budget else 'over'} budget)\n")
    return within_budget


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="latency_benchmark",
        description="Measure the latency the NeuroDrive software contributes.",
    )
    parser.add_argument(
        "--target",
        help="vehicle IP address. Omit to benchmark against the simulator.",
    )
    parser.add_argument("--port", type=int, default=4210)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument(
        "--interval",
        type=float,
        default=0.05,
        help="seconds between transmitted commands (default 0.05)",
    )
    args = parser.parse_args(argv)

    print(f"\n  measuring bridge processing ({args.samples} samples)...")
    processing = measure_processing(args.samples)

    simulator = None
    if args.target:
        host, port, target = args.target, args.port, f"{args.target}:{args.port}"
    else:
        simulator = FakeESP32(port=0, host="127.0.0.1")
        port = simulator.start()
        host, target = "127.0.0.1", f"simulator on 127.0.0.1:{port}"
        print("  no --target given: started the ESP32 simulator")

    try:
        print(f"  measuring round trip ({args.samples} commands)...")
        round_trip = measure_round_trip(host, port, args.samples, args.interval)
    except TransportError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if simulator is not None:
            simulator.stop()

    if not round_trip:
        print("\n  ERROR: no acknowledgements received.", file=sys.stderr)
        print("  Check that the vehicle is powered, on the same network,", file=sys.stderr)
        print("  and that SEND_ACK is enabled in firmware config.h.", file=sys.stderr)
        return 2

    ok = report(Measurements(processing, round_trip), target)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
