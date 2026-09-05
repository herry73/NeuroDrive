# Webcam gesture control

Raise your right hand, the vehicle turns right. Raise your left, it turns
left. Attention still drives forward and stop, so this replaces the blink
gesture rather than the EEG.

---

## Setup

Two packages, on the laptop that will run the camera:

```
pip install mediapipe opencv-python
```

That is the whole setup. The pose model is committed to the repository at
`python_bridge/models/pose_landmarker_lite.task` (5.8 MB), so a fresh clone
already has it and nothing needs downloading — which matters on demo day,
when the laptop is joined to the ESP32's own network with no route out.

Nothing else in the bridge depends on any of this. `vision.py` imports
mediapipe lazily inside its own thread, so a machine without it still runs
the bridge and drives on blinks exactly as before.

---

## Running it

```
python vision_test.py                    # camera only, no headset, no vehicle
python main.py --vision                  # hands turn, attention drives
python main.py --vision-preview          # same, plus the camera window
python main.py --turn-source both        # blinks and hands both turn
```

Tune it with `vision_test.py` first. It shows a preview window with the
tracked shoulders and wrists drawn on, prints one line per accepted gesture,
and needs neither the headset nor the vehicle:

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

It also avoids the mirror trap. A webcam pointed at a user shows a mirrored
image, so a hand-position rule like "hand on the left of the frame means
left" turns the vehicle the wrong way every time, and does it consistently
enough to look correct until somebody actually checks. MediaPipe labels
landmarks anatomically, so index 15 is the subject's own left wrist wherever
it appears in the frame, and the mapping stays honest.

If the turns still come out inverted on your setup, `vision.swap_sides`
flips them. You should not need it.

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
gesture, mirroring the blink debounce in `signal_processor`.

---

## How long the vehicle keeps turning

That is `control.hold_turn_while_raised`, which is `true` by default.

**`true`** — the turn persists. The mapper refreshes the turn deadline on
every control cycle the hand is still up, and clears it the moment the hand
comes down, so the vehicle turns for exactly as long as you hold the raise
and straightens when you lower it.

**`false`** — one raise gives one pulse of `control.turn_command_repeat_ms`
(500 ms), and the vehicle straightens even if the arm is still up.

`control.turn_command_repeat_ms` still matters in the `true` case, but as a
grace window rather than a turn length: it has to outlast the gap between
camera frames. The camera runs at 15 fps and the control loop at 20 Hz, so
the 500 ms default has plenty of margin, and dropping it below roughly
200 ms will make a held turn stutter.

`vision.repeat_while_held_ms` is a separate, lower-level knob on the detector
itself: at `0` (the default) a held arm emits one gesture event, and above
`0` it re-emits one that often. Leave it at `0` and use
`hold_turn_while_raised` for held turns.

---

## Configuration

Full list in [`config.README.md`](config.README.md). The ones worth
touching:

| Key | Default | Change it when |
|---|---|---|
| `vision.camera_index` | `0` | A second webcam, or it grabbed the wrong one |
| `vision.fps_limit` | `15` | Lower it on a slow laptop. It never touches the control loop |
| `vision.raise_margin` | `0.05` | A resting hand triggers turns: raise it |
| `vision.hold_frames` | `3` | A passing hand triggers turns: raise it |
| `vision.refractory_ms` | `1200` | One raise gives two turns: raise it |
| `vision.min_visibility` | `0.6` | The user is partly out of frame and it is guessing |
| `vision.preview` | `false` | You want the camera window during the demo |
| `control.turn_source` | `blink` | `blink`, `vision`, or `both` |
| `control.hold_turn_while_raised` | `true` | `false` for one pulse per raise |

`config.Config.validate()` refuses to start if `turn_source` wants the camera
while `vision.enabled` is false, rather than leaving you wondering why
raising a hand does nothing.

---

## Safety

Gestures go through the same gates as blinks, which means a raised hand
**cannot** move the vehicle when:

* the calibration phase is still running,
* the EEG link is down or the signal quality is poor,
* the software e-stop has disarmed the mapper.

That is deliberate. This is a brain-controlled vehicle with a camera on the
side, not a camera-controlled vehicle. If the headset falls off mid-demo, the
car stops, and waving at it will not restart it.

The command that reaches the firmware is the same `L` or `R` a blink
produces, so the state machine and the watchdog behave identically no matter
which input generated it.

---

## Checking it works

```
python vision_test.py
```

The classification and debounce logic are pure functions that take `now` as
a parameter, so they behave the same every run. What no amount of that will
tell you is whether the camera can see a real human in your actual room.
Only `vision_test.py` and a person standing in front of it will, so run it
where you plan to demo.

---

## Demo notes

Project the preview window. Watching the tracked skeleton follow you and the
label flip to `RAISED: RIGHT` at the moment the vehicle turns does the same
job for the camera that the attention bar does for the EEG. It makes an
invisible decision visible to the audience.

Test the framing in the actual room. Backlighting from a window behind the
user is the usual failure, and `vision_test.py` reports what percentage of
frames actually found a person. Anything under about 90% means move the
camera or fix the lighting before you rely on it.
