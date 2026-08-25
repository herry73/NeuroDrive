"""
Camera and gesture tuning, with no headset and no vehicle.

The counterpart to ``udp_test_sender.py``. That one exercises the link
without the EEG, this one exercises the camera without either. Use it to
frame the shot, check the lighting, and confirm that raising a hand fires
exactly one gesture before you put any of it in front of an audience.

    python vision_test.py                    # preview window, prints gestures
    python vision_test.py --no-preview       # headless, for a remote session
    python vision_test.py --camera 1         # a second webcam
    python vision_test.py --hold-frames 5    # demand a steadier raise
    python vision_test.py --swap-sides       # if the turns feel inverted

Press q in the preview window, or Ctrl+C, to stop.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import config as config_module
from vision import VisionReader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vision_test.py",
        description="Watch the webcam gesture detector on its own.",
    )
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--camera", type=int, help="camera index (default from config)")
    parser.add_argument("--no-preview", action="store_true", help="no camera window")
    parser.add_argument("--hold-frames", type=int, help="frames a raise must persist")
    parser.add_argument("--refractory-ms", type=int, help="quiet period after a gesture")
    parser.add_argument("--raise-margin", type=float, help="fraction of frame height")
    parser.add_argument("--swap-sides", action="store_true", help="invert left/right")
    parser.add_argument("--duration", type=float, help="stop after N seconds")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    config = config_module.load(args.config)
    config.set("vision.enabled", True)
    config.set("vision.preview", not args.no_preview)
    if args.camera is not None:
        config.set("vision.camera_index", args.camera)
    if args.hold_frames is not None:
        config.set("vision.hold_frames", args.hold_frames)
    if args.refractory_ms is not None:
        config.set("vision.refractory_ms", args.refractory_ms)
    if args.raise_margin is not None:
        config.set("vision.raise_margin", args.raise_margin)
    if args.swap_sides:
        config.set("vision.swap_sides", True)

    from vision import create_vision

    reader: VisionReader = create_vision(config)

    print()
    print("  NeuroDrive vision test")
    print("  " + "-" * 52)
    print(f"  Camera        : {config.get('vision.camera_index')}")
    print(f"  Hold frames   : {config.get('vision.hold_frames')}")
    print(f"  Refractory    : {config.get('vision.refractory_ms')} ms")
    print(f"  Raise margin  : {config.get('vision.raise_margin')}")
    print("  " + "-" * 52)
    print("  Loading the pose model, this takes a few seconds...")
    print("  Raise one hand above your shoulder. Ctrl+C to stop.\n")

    reader.start()
    started = time.monotonic()
    announced = False
    last_state = None

    try:
        while True:
            time.sleep(0.05)
            info = reader.info

            if not announced and info.running:
                print(f"  Camera open. Watching at {info.fps:.0f} fps.\n")
                announced = True
            if not info.running and info.last_error and not announced:
                print(f"  ERROR: {info.last_error}", file=sys.stderr)
                return 2

            for event in reader.read_all():
                elapsed = event.timestamp - started
                arrow = "<--" if event.gesture.value == "LEFT" else "-->"
                print(f"  [{elapsed:7.2f}s]  {arrow}  {event.gesture.value}")

            state = info.raised
            if state != last_state:
                if state:
                    print(f"             ..  holding {state}")
                last_state = state

            if args.duration and time.monotonic() - started >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        info = reader.info
        seen = (100.0 * info.pose_frames / info.frames) if info.frames else 0.0
        print()
        print("  " + "-" * 52)
        print(f"  Frames        : {info.frames}")
        print(f"  User visible  : {seen:.0f}% of frames")
        print(f"  Gestures      : {info.gestures}")
        if info.last_error:
            print(f"  Last warning  : {info.last_error}")
        print("  " + "-" * 52 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
