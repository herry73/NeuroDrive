# Webcam gesture control

Raise your right hand, the vehicle turns right. Raise your left, it turns
left. Attention still drives forward and stop, so this replaces the blink
gesture rather than the EEG.

Owner: **M8** (CV research), with **M5** for the bridge integration.

---

## Setup

Two packages and one model file, on the demo laptop only:

```powershell
pip install mediapipe opencv-python
```

```powershell
cd python_bridge
mkdir models
curl -o models/pose_landmarker_lite.task `
  https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task
```

The model is 5.8 MB and is the only thing here that needs the internet.
Download it before demo day, because the venue laptop will be joined to the
ESP32's own network with no route out.

Nothing else in the bridge depends on any of this. `vision.py` imports
mediapipe lazily inside its own thread, so a machine without it runs the
whole test suite and drives on blinks exactly as before.

---

## Running it

```powershell
python vision_test.py                    # camera only, no headset, no vehicle
python main.py --vision                  # hands turn, attention drives
python main.py --vision-preview          # same, plus the camera window
python main.py --turn-source both        # blinks and hands both turn
```

Tune it with `vision_test.py` first. It shows a preview window with the
tracked shoulders and wrists drawn on, prints one line per accepted gesture,
and needs neither the headset nor the vehicle:

```powershell
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
calibration. Both arms up returns nothing, on purpose. That pose is ambiguous, and a
vehicle that guesses when the user is ambiguous is one nobody trusts.

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
gesture, mirroring the blink debounce in `signal_processor` (SP-06).

Set `repeat_while_held_ms` above 0 if you would rather a held arm keep
turning. It is off by default.

---

## Configuration

Full list in `config.json` under `vision`. The ones worth touching:

| Key | Default | Change it when |
|---|---|---|
| `camera_index` | `0` | A second webcam, or it grabbed the laptop lid camera |
| `fps_limit` | `15` | Lower it on a slow laptop. It never touches the control loop |
| `raise_margin` | `0.05` | A resting hand triggers turns: raise it |
| `hold_frames` | `3` | A passing hand triggers turns: raise it |
| `refractory_ms` | `1200` | One raise gives two turns: raise it |
| `min_visibility` | `0.6` | The user is partly out of frame and it is guessing |
| `preview` | `false` | You want the camera window during the demo |

And in `control`:

| Key | Default | Meaning |
|---|---|---|
| `turn_source` | `blink` | `blink`, `vision`, or `both` |

`config.Config.validate()` refuses to start if `turn_source` wants the camera
while `vision.enabled` is false, rather than leaving you wondering why
raising a hand does nothing.

---

## Safety

Gestures go through the same gates as blinks, which means a raised hand
**cannot** move the vehicle when:

* the calibration phase is still running (UI-02),
* the EEG link is down or the signal quality is poor (EEG-05, SF-03),
* the software e-stop has disarmed the mapper.

That is deliberate. This is a brain-controlled vehicle with a camera on the
side, not a camera-controlled vehicle. If the headset falls off mid-demo, the
car stops, and waving at it will not restart it.

Turns are the same 300 ms pulse as ever (MV-03), so the firmware's state
machine and watchdog behave identically no matter which input produced the
command. Nothing in `docs/INTERFACE_CONTRACT.md` changes.

---

## Testing

```powershell
python -m pytest tests/test_vision.py -q
```

24 tests, no camera, no model, no mediapipe import, about 40 ms. The
classification and debounce logic are pure functions taking `now` as a
parameter, so a handful of coordinates stands in for a person the same way
`fake_esp32.py` stands in for the firmware.

What the tests do not cover is whether the camera can see a real human in
your actual room. Nothing but `vision_test.py` and a person will tell you
that, so run it in the demo room before demo day.

---

## Demo notes

Project the preview window. Watching the tracked skeleton follow you and the
label flip to `RAISED: RIGHT` at the moment the vehicle turns does the same
job for the camera that the attention bar does for the EEG. It makes an
invisible decision visible to the audience (plan section 12.3).

Test the framing in the actual room. Backlighting from a window behind the
user is the usual failure, and `vision_test.py` reports what percentage of
frames actually found a person. Anything under about 90% means move the
camera or fix the lighting before you rely on it.
