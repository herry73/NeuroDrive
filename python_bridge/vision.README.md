# Webcam gesture control

Raise your right hand, the vehicle turns right. Raise your left, it turns
left. Attention from the headset drives forward and stop, so the camera is
the steering input and the headset is the throttle.

---

## Setup

Two packages, both in `requirements.txt`:

```
pip install -r requirements.txt
```

That is the whole setup. The pose model is committed to the repository at
`python_bridge/models/pose_landmarker_lite.task` (5.8 MB), so a fresh clone
already has it and the camera needs no internet access.

`vision.py` imports mediapipe lazily inside its own thread. If the packages
or the camera are missing, the thread logs `vision could not start` and
exits, and the rest of the bridge keeps running — attention still drives
forward and stop, but there is no way to turn.

---

## Running it

```
python vision_test.py                    # camera only, no headset, no vehicle
python main.py                           # attention drives, hands turn
python main.py --vision-preview          # same, plus the camera window
```

`vision_test.py` shows a preview window with the tracked shoulders and wrists
drawn on, prints one line per accepted gesture, and needs neither the headset
nor the vehicle:

```
python vision_test.py --hold-frames 5        # demand a steadier raise
python vision_test.py --refractory-ms 2000   # slower repeat
python vision_test.py --raise-margin 0.10    # hand must go higher
```

---

## How it decides

One comparison per arm. A wrist above its own shoulder means that arm is
raised:

| Landmark | Index | Used for |
|---|---|---|
| Left shoulder | 11 | Reference height, left arm |
| Right shoulder | 12 | Reference height, right arm |
| Left wrist | 15 | Is the left arm up? |
| Right wrist | 16 | Is the right arm up? |

Scale invariant, so it works at any sensible distance and needs no
calibration. Both arms up returns nothing, on purpose. That pose is
ambiguous, and a vehicle that guesses when the user is ambiguous is one
nobody trusts.

### Why pose landmarks and not hand detection

The question is not "where is a hand" but "which of *this person's* hands is
up", and answering that needs the shoulders as a reference.

MediaPipe labels landmarks anatomically, so index 15 is the subject's own
left wrist wherever it appears in the frame. A rule based on position within
the frame instead would be wrong for a camera facing the user, and wrong
consistently enough to look correct until somebody checked.

---

## Making one raise mean one turn

Three rules, in `GestureDebouncer`:

**`hold_frames`** (default 3). A raise must persist across this many
consecutive frames. Rejects a hand passing through the frame on its way
somewhere else, and rejects single-frame model glitches.

**Edge triggering.** The hand must come back down before the same gesture
fires again. Without this, holding an arm up streams turn commands at the
frame rate and spins the vehicle on the spot. Switching directly from one
hand to the other still counts, so left, right, left works without lowering
both arms in between.

**`refractory_ms`** (default 1200). A quiet period after each accepted
gesture.

---

## How long the vehicle keeps turning

That is `control.hold_turn_while_raised`, which is `true` by default.

**`true`** — the turn persists. The mapper refreshes the turn deadline on
every control cycle the hand is still up, and clears it the moment the hand
comes down, so the vehicle turns for exactly as long as the raise is held and
straightens when it ends.

**`false`** — one raise gives one pulse of `control.turn_command_repeat_ms`
(500 ms), and the vehicle straightens even if the arm is still up.

`control.turn_command_repeat_ms` still matters in the `true` case, but as a
grace window rather than a turn length: it has to outlast the gap between
camera frames. The camera runs at 15 fps and the control loop at 20 Hz, so
the 500 ms default has plenty of margin, and dropping it below roughly
200 ms will make a held turn stutter.

`vision.repeat_while_held_ms` is a separate, lower-level knob on the detector
itself: at `0` (the default) a held arm emits one gesture event, and above
`0` it re-emits one that often. It stays at `0`, and held turns come from
`hold_turn_while_raised`.

---

## Configuration

Full list in [`config.README.md`](config.README.md). The ones that change
behaviour most:

| Key | Default | Effect |
|---|---|---|
| `vision.enabled` | `true` | The camera thread. `false` leaves no way to turn |
| `vision.camera_index` | `0` | Which camera, when there is more than one |
| `vision.fps_limit` | `15` | Camera frame rate. It never touches the control loop |
| `vision.raise_margin` | `0.05` | How high a wrist must go above its shoulder. Higher rejects a resting hand |
| `vision.hold_frames` | `3` | Frames a raise must persist. Higher rejects a passing hand |
| `vision.refractory_ms` | `1200` | Quiet period after a gesture. Higher stops one raise reading as two |
| `vision.min_visibility` | `0.6` | Landmark confidence below which a frame is ignored |
| `vision.preview` | `false` | Show the camera window |
| `control.turn_source` | `vision` | Raised hands produce LEFT and RIGHT |
| `control.hold_turn_while_raised` | `true` | `false` gives one pulse per raise |

`config.Config.validate()` refuses to start if `turn_source` wants the camera
while `vision.enabled` is false, rather than leaving the vehicle silently
unable to turn.

---

## Safety

Gestures go through the same gates as every other input, which means a raised
hand **cannot** move the vehicle when:

* the calibration phase is still running,
* the EEG link is down or the signal quality is poor,
* the software e-stop has disarmed the mapper.

That is deliberate. This is a brain-controlled vehicle with a camera on the
side, not a camera-controlled vehicle. If the headset comes off mid-run, the
vehicle stops, and waving at it will not restart it.

The command that reaches the firmware is an ordinary `L` or `R`, so the state
machine and the watchdog behave the same as for any other source.

---

## Checking it works

```
python vision_test.py
```

The classification and debounce logic are pure functions that take `now` as
a parameter, so they behave identically every run. What that cannot show is
whether the camera can see a real person in a real room, which is what
`vision_test.py` is for: it draws the tracked skeleton, labels the detected
raise, and reports the percentage of frames in which it found a person.

A figure below about 90% means the camera cannot see the driver reliably.
Backlighting from a window behind them is the usual cause.
