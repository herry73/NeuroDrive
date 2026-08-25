"""
Webcam gesture input: which hand is the user holding up?

Requirement coverage:
    CV-01  The laptop acquires and processes a camera feed.
    MV-01  Produces the LEFT and RIGHT movement commands.
    SP-06  Debounces gestures, so one raise means one turn.
    NFR 3.3  All camera work happens off the control loop's thread.

Why pose landmarks rather than hand detection
---------------------------------------------
The question is not "where is a hand" but "which of *this person's* hands is
up", and that needs the shoulders as a reference. MediaPipe's pose model
labels landmarks anatomically: index 15 is the subject's own left wrist,
whichever side of the frame it appears on. That sidesteps the mirror trap. A webcam
pointed at a user shows a mirrored image, so a naive "hand on the left of
the frame means left" rule turns the vehicle the wrong way every single
time, and it does so consistently enough to look correct until someone
checks.

The decision itself is one comparison per arm. A wrist above its own
shoulder means that arm is raised. It is scale invariant, needs no
calibration, and works at any sensible distance from the camera.

Structure
---------
The classification and debounce logic are pure functions of their inputs and
take ``now`` as a parameter, so ``tests/test_vision.py`` exercises the whole
decision path with no camera and no model. Only :class:`VisionReader` touches
hardware.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional, Sequence

LOG = logging.getLogger("neurodrive.vision")

#: Pose landmark indices we care about. MediaPipe numbers these from the
#: subject's own anatomy, not from the image, which is the whole point.
LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
LEFT_WRIST = 15
RIGHT_WRIST = 16


class Gesture(str, Enum):
    """Which hand the user is holding up."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


@dataclass
class GestureEvent:
    """One accepted raise, ready to become a turn command."""

    timestamp: float
    gesture: Gesture


@dataclass
class VisionInfo:
    """Status snapshot for the dashboard and the session log."""

    enabled: bool = False
    running: bool = False
    camera_open: bool = False
    frames: int = 0
    pose_frames: int = 0
    gestures: int = 0
    fps: float = 0.0
    raised: Optional[str] = None       # what is up right now, for the UI
    last_error: str = ""


class VisionError(RuntimeError):
    """Camera or model could not be brought up."""


# ---------------------------------------------------------------------------
# Pure decision logic (no camera, no model, fully testable)
# ---------------------------------------------------------------------------


class _Landmark:
    """Minimal stand-in so tests can build landmarks without MediaPipe."""

    __slots__ = ("x", "y", "visibility")

    def __init__(self, x: float, y: float, visibility: float = 1.0) -> None:
        self.x = x
        self.y = y
        self.visibility = visibility


def classify_raise(
    landmarks: Sequence,
    raise_margin: float = 0.05,
    min_visibility: float = 0.6,
) -> Optional[Gesture]:
    """Return which hand is raised, or ``None``.

    Image coordinates run downward, so a raised wrist has a *smaller* y than
    its shoulder. ``raise_margin`` is expressed as a fraction of the frame
    height and stops a wrist hovering level with the shoulder from flickering
    between raised and not.

    Both arms up returns ``None`` on purpose. It is ambiguous, and a vehicle
    that guesses when the user is ambiguous is a vehicle nobody trusts.
    """
    if landmarks is None or len(landmarks) <= RIGHT_WRIST:
        return None

    def raised(wrist_idx: int, shoulder_idx: int) -> bool:
        wrist = landmarks[wrist_idx]
        shoulder = landmarks[shoulder_idx]
        # An occluded or guessed landmark is worse than no landmark: the model
        # still reports a position for a limb it cannot see.
        if getattr(wrist, "visibility", 1.0) < min_visibility:
            return False
        if getattr(shoulder, "visibility", 1.0) < min_visibility:
            return False
        return wrist.y < shoulder.y - raise_margin

    left_up = raised(LEFT_WRIST, LEFT_SHOULDER)
    right_up = raised(RIGHT_WRIST, RIGHT_SHOULDER)

    if left_up and right_up:
        return None
    if left_up:
        return Gesture.LEFT
    if right_up:
        return Gesture.RIGHT
    return None


class GestureDebouncer:
    """Turns a per-frame raise signal into discrete, deliberate events.

    Three rules, each earning its place:

    ``hold_frames``
        A raise must persist across this many consecutive frames before it
        counts. Rejects a hand passing through the frame on its way somewhere
        else, and rejects single-frame model glitches.

    Edge triggering
        One raise produces exactly one turn. The hand must come back down
        before the same gesture fires again. Without this, holding an arm up
        would stream turn commands at the frame rate and spin the vehicle.

    ``refractory_ms``
        A quiet period after each accepted event, mirroring the blink
        debounce in ``signal_processor`` (SP-06), so a wobbling arm near the
        threshold cannot double-fire.

    ``now`` is always passed in rather than read from the clock, so the tests
    can drive timing behaviour instantly and deterministically.
    """

    def __init__(
        self,
        hold_frames: int = 3,
        refractory_ms: int = 1200,
        repeat_while_held_ms: int = 0,
    ) -> None:
        if hold_frames < 1:
            raise ValueError("hold_frames must be at least 1")
        self.hold_frames = hold_frames
        self.refractory_s = refractory_ms / 1000.0
        # 0 disables repeats: one raise, one turn.
        self.repeat_s = repeat_while_held_ms / 1000.0

        self._candidate: Optional[Gesture] = None
        self._streak = 0
        self._armed = True          # False until the hand comes back down
        self._last_fired_at = -1e9
        self._last_fired: Optional[Gesture] = None

    @property
    def current(self) -> Optional[Gesture]:
        """What the debouncer believes is raised right now."""
        return self._candidate if self._streak >= self.hold_frames else None

    def feed(self, gesture: Optional[Gesture], now: float) -> Optional[GestureEvent]:
        """Fold in one frame's classification; return an event if it fires."""
        if gesture is None:
            # Hand down: forget the streak and re-arm for the next raise.
            self._candidate = None
            self._streak = 0
            self._armed = True
            return None

        if gesture is self._candidate:
            self._streak += 1
        else:
            # Switched hands mid-air. Treat it as a fresh raise, and re-arm so
            # the user can go left, right, left without lowering both arms.
            self._candidate = gesture
            self._streak = 1
            self._armed = True

        if self._streak < self.hold_frames:
            return None

        since_fire = now - self._last_fired_at
        if since_fire < self.refractory_s:
            return None

        if not self._armed:
            # Still holding the same arm up. Only repeat if asked to.
            if self.repeat_s <= 0 or since_fire < self.repeat_s:
                return None

        self._armed = False
        self._last_fired_at = now
        self._last_fired = gesture
        return GestureEvent(timestamp=now, gesture=gesture)

    def reset(self) -> None:
        self._candidate = None
        self._streak = 0
        self._armed = True
        self._last_fired_at = -1e9
        self._last_fired = None


# ---------------------------------------------------------------------------
# Camera + model thread
# ---------------------------------------------------------------------------


class VisionReader:
    """Runs the camera and the pose model on their own thread (NFR 3.3).

    The control loop never blocks on a frame grab or on inference. It calls
    :meth:`read_all`, which drains whatever events have accumulated and
    returns immediately, exactly like :class:`eeg_reader.EEGReader`.
    """

    def __init__(
        self,
        enabled: bool = False,
        camera_index: int = 0,
        width: int = 640,
        height: int = 480,
        fps_limit: float = 15.0,
        model_path: str = "models/pose_landmarker_lite.task",
        raise_margin: float = 0.05,
        min_visibility: float = 0.6,
        hold_frames: int = 3,
        refractory_ms: int = 1200,
        repeat_while_held_ms: int = 0,
        swap_sides: bool = False,
        preview: bool = False,
    ) -> None:
        self.enabled = enabled
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.frame_period = 1.0 / fps_limit if fps_limit > 0 else 0.0
        self.model_path = model_path
        self.raise_margin = raise_margin
        self.min_visibility = min_visibility
        self.swap_sides = swap_sides
        self.preview = preview

        self._debouncer = GestureDebouncer(
            hold_frames=hold_frames,
            refractory_ms=refractory_ms,
            repeat_while_held_ms=repeat_while_held_ms,
        )
        self._events: Deque[GestureEvent] = deque(maxlen=32)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._info = VisionInfo(enabled=enabled)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            LOG.info("vision disabled")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="vision", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            self._info.running = False

    def read_all(self) -> List[GestureEvent]:
        """Drain accepted gestures. Never blocks."""
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    @property
    def info(self) -> VisionInfo:
        with self._lock:
            return VisionInfo(**vars(self._info))

    # -- worker -------------------------------------------------------------

    def _run(self) -> None:
        try:
            cv2, landmarker, mp = self._open()
        except Exception as exc:  # camera missing, model missing, bad build
            LOG.error("vision could not start: %s", exc)
            with self._lock:
                self._info.last_error = str(exc)
                self._info.running = False
            return

        with self._lock:
            self._info.running = True
            self._info.camera_open = True

        cap = self._capture
        frame_index = 0
        fps_window: Deque[float] = deque(maxlen=30)
        last_frame_at = 0.0

        try:
            while not self._stop.is_set():
                if self.frame_period:
                    wait = last_frame_at + self.frame_period - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                last_frame_at = time.monotonic()

                ok, frame = cap.read()
                if not ok:
                    LOG.warning("camera returned no frame")
                    with self._lock:
                        self._info.last_error = "camera returned no frame"
                    time.sleep(0.25)
                    continue

                now = time.monotonic()
                fps_window.append(now)
                frame_index += 1

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                # VIDEO mode wants a monotonically rising millisecond stamp.
                result = landmarker.detect_for_video(image, int(now * 1000))

                landmarks = result.pose_landmarks[0] if result.pose_landmarks else None
                gesture = classify_raise(
                    landmarks, self.raise_margin, self.min_visibility
                )
                if gesture is not None and self.swap_sides:
                    gesture = (
                        Gesture.RIGHT if gesture is Gesture.LEFT else Gesture.LEFT
                    )

                event = self._debouncer.feed(gesture, now)

                with self._lock:
                    self._info.frames = frame_index
                    if landmarks is not None:
                        self._info.pose_frames += 1
                    held = self._debouncer.current
                    self._info.raised = held.value if held else None
                    if len(fps_window) > 1:
                        span = fps_window[-1] - fps_window[0]
                        self._info.fps = (len(fps_window) - 1) / span if span else 0.0
                    if event is not None:
                        self._events.append(event)
                        self._info.gestures += 1

                if event is not None:
                    LOG.info("gesture %s", event.gesture.value)

                if self.preview:
                    self._draw_preview(cv2, frame, landmarks, gesture)
        finally:
            try:
                cap.release()
            except Exception:
                pass
            try:
                landmarker.close()
            except Exception:
                pass
            if self.preview:
                try:
                    cv2.destroyWindow("NeuroDrive vision")
                except Exception:
                    pass
            with self._lock:
                self._info.running = False
                self._info.camera_open = False

    def _open(self):
        """Import the heavy dependencies and bring up camera plus model.

        MediaPipe drags TensorFlow in behind it, which costs a few seconds and
        prints a wall of log noise. Both happen here, on the vision thread, so
        the bridge starts driving on EEG without waiting for the camera.
        """
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
        os.environ.setdefault("GLOG_minloglevel", "2")

        import cv2  # noqa: WPS433 - deliberately lazy

        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision as mp_vision
        except ImportError as exc:
            raise VisionError(
                "mediapipe is not installed. Run: pip install mediapipe"
            ) from exc

        model = self.model_path
        if not os.path.isabs(model):
            model = os.path.join(os.path.dirname(os.path.abspath(__file__)), model)
        if not os.path.exists(model):
            raise VisionError(
                f"pose model not found at {model}. See vision.README.md for the "
                "one-line download."
            )

        # CAP_DSHOW avoids a multi-second open on Windows' default backend.
        backend = getattr(cv2, "CAP_DSHOW", 0) if os.name == "nt" else 0
        cap = cv2.VideoCapture(self.camera_index, backend)
        if not cap.isOpened():
            raise VisionError(f"cannot open camera {self.camera_index}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._capture = cap

        options = mp_vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_poses=1,
        )
        landmarker = mp_vision.PoseLandmarker.create_from_options(options)
        LOG.info("vision up: camera %d, model %s", self.camera_index, os.path.basename(model))
        return cv2, landmarker, mp

    def _draw_preview(self, cv2, frame, landmarks, gesture) -> None:
        """Operator view. Worth projecting during the demo (plan 12.3)."""
        height, width = frame.shape[:2]
        colour = (60, 220, 60) if landmarks is not None else (60, 60, 220)

        if landmarks is not None:
            for idx in (LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_WRIST, RIGHT_WRIST):
                point = landmarks[idx]
                cv2.circle(
                    frame,
                    (int(point.x * width), int(point.y * height)),
                    6,
                    colour,
                    -1,
                )
            for wrist, shoulder in ((LEFT_WRIST, LEFT_SHOULDER), (RIGHT_WRIST, RIGHT_SHOULDER)):
                a, b = landmarks[wrist], landmarks[shoulder]
                cv2.line(
                    frame,
                    (int(a.x * width), int(a.y * height)),
                    (int(b.x * width), int(b.y * height)),
                    colour,
                    2,
                )

        label = f"RAISED: {gesture.value}" if gesture else "no hand raised"
        cv2.putText(
            frame, label, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, colour, 2
        )
        cv2.putText(
            frame,
            f"gestures {self._info.gestures}   fps {self._info.fps:.0f}",
            (12, height - 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (200, 200, 200),
            1,
        )
        cv2.imshow("NeuroDrive vision", frame)
        cv2.waitKey(1)


def create_vision(config) -> VisionReader:
    """Build a :class:`VisionReader` from the configuration tree."""
    section = config.section("vision")
    return VisionReader(
        enabled=section.get("enabled", False),
        camera_index=section.get("camera_index", 0),
        width=section.get("width", 640),
        height=section.get("height", 480),
        fps_limit=section.get("fps_limit", 15),
        model_path=section.get("model_path", "models/pose_landmarker_lite.task"),
        raise_margin=section.get("raise_margin", 0.05),
        min_visibility=section.get("min_visibility", 0.6),
        hold_frames=section.get("hold_frames", 3),
        refractory_ms=section.get("refractory_ms", 1200),
        repeat_while_held_ms=section.get("repeat_while_held_ms", 0),
        swap_sides=section.get("swap_sides", False),
        preview=section.get("preview", False),
    )
