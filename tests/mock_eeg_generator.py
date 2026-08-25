"""
Mock EEG data generator (M7, Week 1 deliverable).

Two jobs:

1. A library the tests use to produce deterministic EEG input, either as
   :class:`~eeg_sources.EEGSample` objects or as a genuine ThinkGear byte
   stream, so the tests run the real parser instead of bypassing it.

2. A command-line tool that writes a session CSV. That file feeds
   ``ReplaySource``, which is Fallback Level 2 in the demo strategy: a
   known-good drive that needs no headset.

Usage
-----
    python mock_eeg_generator.py --scenario demo --duration 60 \\
        --out ../python_bridge/logs/demo_session.csv

    python main.py --replay-file logs/demo_session.csv    # then drive it
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import argparse
import csv
import math
import os
import random
import sys
from typing import Iterable, List, Optional, Tuple

from data_logger import CSV_COLUMNS
from eeg_sources import EEGSample
from thinkgear import build_blink_packet, build_esense_packet, build_raw_packet

#: Sample rate of the generated eSense stream, matching the real headset.
ESENSE_HZ = 1.0


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def _clamp(value: float, low: float = 0, high: float = 100) -> int:
    return int(max(low, min(high, value)))


def attention_smooth(t: float, rng: random.Random, period: float = 20.0) -> int:
    """A clean sine sweep that crosses both thresholds every cycle."""
    return _clamp(55 + 35 * math.sin(2 * math.pi * t / period) + rng.gauss(0, 3))


def attention_flat(t: float, rng: random.Random) -> int:
    """A user who never concentrates. The vehicle should never move."""
    return _clamp(25 + rng.gauss(0, 4))


#: The scripted demo drive: (start_second, attention, description).
#: Chosen so an audience sees calibrate -> go -> turn -> go -> turn -> stop.
DEMO_SCRIPT: List[Tuple[float, int, str]] = [
    (0, 30, "settling"),
    (5, 45, "warming up"),
    (10, 72, "concentrating -> FORWARD"),
    (22, 78, "still driving"),
    (30, 35, "relaxing -> STOP"),
    (38, 70, "concentrating -> FORWARD"),
    (50, 30, "relaxing -> STOP"),
]


def attention_demo(t: float, rng: random.Random) -> int:
    """Piecewise-constant script with a little noise on top."""
    value = DEMO_SCRIPT[0][1]
    for start, level, _label in DEMO_SCRIPT:
        if t >= start:
            value = level
    return _clamp(value + rng.gauss(0, 3))


#: Blink times, in seconds, for the demo script.
DEMO_BLINKS = [16.0, 26.0, 44.0]

SCENARIOS = {
    "smooth": "clean sine sweep, blinks every 8 s, no signal problems",
    "noisy": "sine sweep with poor-signal bursts and weak sub-threshold blinks",
    "flat": "attention never reaches the forward threshold",
    "demo": "scripted drive suitable for the fallback demo",
}


# --------------------------------------------------------------------------
# Sample generation
# --------------------------------------------------------------------------


def generate_samples(
    duration_s: float = 60.0,
    scenario: str = "smooth",
    seed: int = 7,
    blink_interval_s: float = 8.0,
    blink_strength: int = 190,
    start_time: float = 0.0,
) -> List[EEGSample]:
    """Produce the sample stream for a scenario.

    Timestamps are absolute seconds from ``start_time``. eSense samples land
    once per second; blink samples are inserted at their own instants, just
    as the headset delivers them.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; pick one of {list(SCENARIOS)}")

    rng = random.Random(seed)
    samples: List[EEGSample] = []

    blink_times: List[float]
    if scenario == "demo":
        blink_times = [b for b in DEMO_BLINKS if b < duration_s]
    else:
        blink_times = []
        t = blink_interval_s
        while t < duration_s:
            blink_times.append(t)
            t += blink_interval_s

    step = 1.0 / ESENSE_HZ
    ticks = int(duration_s / step)
    blink_index = 0
    attention = 0
    meditation = 50

    for index in range(ticks):
        t = index * step

        # Insert any blinks that fall before this eSense tick.
        while blink_index < len(blink_times) and blink_times[blink_index] <= t:
            blink_at = blink_times[blink_index]
            strength = blink_strength
            if scenario == "noisy" and blink_index % 3 == 1:
                strength = rng.randint(60, 120)  # deliberately too weak (SP-04)
            samples.append(
                EEGSample(
                    timestamp=start_time + blink_at,
                    attention=attention,
                    meditation=meditation,
                    poor_signal=0,
                    blink_strength=strength,
                )
            )
            blink_index += 1

        if scenario == "flat":
            attention = attention_flat(t, rng)
        elif scenario == "demo":
            attention = attention_demo(t, rng)
        else:
            attention = attention_smooth(t, rng)

        meditation = _clamp(50 + 20 * math.cos(2 * math.pi * t / 25.0) + rng.gauss(0, 3))

        poor_signal = 0
        if scenario == "noisy":
            # A few seconds of bad contact roughly every 15 s (SF-03).
            if 12.0 <= (t % 15.0) < 15.0:
                poor_signal = rng.choice([26, 51, 200])

        samples.append(
            EEGSample(
                timestamp=start_time + t,
                attention=attention,
                meditation=meditation,
                poor_signal=poor_signal,
            )
        )

    return samples


def generate_thinkgear_stream(
    duration_s: float = 10.0,
    scenario: str = "smooth",
    seed: int = 7,
    include_raw: bool = False,
) -> bytes:
    """Render a scenario as raw ThinkGear bytes, as the headset would send.

    Used by ``test_thinkgear.py`` and by anyone who wants to feed a file
    into the parser with ``ThinkGearParser().feed(...)``.
    """
    chunks = bytearray()
    rng = random.Random(seed + 1)
    for sample in generate_samples(duration_s=duration_s, scenario=scenario, seed=seed):
        if sample.has_blink:
            chunks += build_blink_packet(sample.blink_strength)
            continue
        chunks += build_esense_packet(
            poor_signal=sample.poor_signal,
            attention=sample.attention or 0,
            meditation=sample.meditation or 0,
        )
        if include_raw:
            for _ in range(8):
                chunks += build_raw_packet(int(rng.gauss(0, 40)))
    return bytes(chunks)


# --------------------------------------------------------------------------
# CSV output
# --------------------------------------------------------------------------


def write_csv(path: str, samples: Iterable[EEGSample], start_offset: float = 0.0) -> int:
    """Write samples in the session-CSV format understood by ``ReplaySource``."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    rows = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for sample in samples:
            writer.writerow(
                {
                    "wall_clock": "",
                    "elapsed_s": f"{sample.timestamp - start_offset:.3f}",
                    "attention": "" if sample.attention is None else sample.attention,
                    "smoothed_attention": "",
                    "meditation": "" if sample.meditation is None else sample.meditation,
                    "poor_signal": sample.poor_signal,
                    "blink_strength": "" if sample.blink_strength is None else sample.blink_strength,
                    "blink_event": "",
                    "quality_ok": "",
                    "connected": 1,
                    "command": "",
                    "reason": "generated",
                }
            )
            rows += 1
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mock_eeg_generator",
        description="Generate synthetic EEG sessions for testing and demo fallback.",
    )
    parser.add_argument(
        "--scenario",
        default="smooth",
        choices=sorted(SCENARIOS),
        help="; ".join(f"{name}: {text}" for name, text in sorted(SCENARIOS.items())),
    )
    parser.add_argument("--duration", type=float, default=60.0, help="seconds")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--blink-interval", type=float, default=8.0)
    parser.add_argument("--out", help="write a session CSV to this path")
    parser.add_argument(
        "--thinkgear-out", help="write a raw ThinkGear byte stream to this path"
    )
    parser.add_argument(
        "--preview", action="store_true", help="print the first 20 samples"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    samples = generate_samples(
        duration_s=args.duration,
        scenario=args.scenario,
        seed=args.seed,
        blink_interval_s=args.blink_interval,
    )
    blinks = sum(1 for sample in samples if sample.has_blink)
    print(
        f"  scenario '{args.scenario}': {len(samples)} samples over "
        f"{args.duration:.0f}s ({blinks} blinks)"
    )

    if args.preview:
        for sample in samples[:20]:
            marker = f" BLINK {sample.blink_strength}" if sample.has_blink else ""
            print(
                f"    t={sample.timestamp:6.2f}  attention={sample.attention:>3}"
                f"  poor={sample.poor_signal:>3}{marker}"
            )

    if args.out:
        rows = write_csv(args.out, samples)
        print(f"  wrote {rows} rows to {args.out}")
        print(f"  replay it with:  python main.py --replay-file {args.out}")

    if args.thinkgear_out:
        data = generate_thinkgear_stream(
            duration_s=args.duration, scenario=args.scenario, seed=args.seed
        )
        with open(args.thinkgear_out, "wb") as handle:
            handle.write(data)
        print(f"  wrote {len(data)} bytes of ThinkGear stream to {args.thinkgear_out}")

    if not args.out and not args.thinkgear_out and not args.preview:
        print("  (nothing written. Pass --out, --thinkgear-out or --preview)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
