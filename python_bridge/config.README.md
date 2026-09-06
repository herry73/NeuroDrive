# `config.json` reference

Every tunable parameter of the bridge lives in `config.json`. Nothing in
this list requires editing code.

JSON has no comments, so the loader treats any key beginning with `_` as
documentation and skips it. That is why `config.json` contains
`_source`, `_mode` and similar keys.

Three ways to change a value, least permanent first:

```
python main.py --set control.attention_forward_threshold=68   # this run only
python main.py --source mock --transport serial               # shortcuts
```

To make a change permanent, edit `config.json`.

Missing keys fall back to the built-in defaults in `config.py`, so a partial
file is fine. `config.Config.validate()` runs at startup and refuses an
invalid combination outright, rather than letting it turn into odd vehicle
behaviour an hour later.

Every default in this document is the value the shipped `config.json`
actually uses, which is what a fresh clone runs.

---

## `eeg`: where the signal comes from

| Key | Default | Meaning |
|---|---|---|
| `source` | `mock` | `serial` (real headset), `mock` (synthetic), `replay` (recorded CSV) |
| `signal_timeout_ms` | `2000` | No samples for this long ⇒ report signal loss and safe-stop |

`serial` is the setting for a real headset. The default is `mock` so a fresh
clone runs on any laptop with no hardware.

### `eeg.serial`: the MindWave over Bluetooth

| Key | Default | Meaning |
|---|---|---|
| `port` | `COM4` | The serial port the paired headset streams on |
| `baudrate` | `57600` | Fixed by the MindWave Mobile 2 hardware |
| `read_timeout_s` | `0.2` | Blocking read timeout; also paces the reader thread |
| `reconnect_attempts` | `3` | After this many failures the status becomes `FAILED`, then retries continue slowly, so a headset that comes back is picked up without restarting the bridge |
| `reconnect_delay_s` | `2.0` | Wait between attempts |

> **Finding the port.** Pair the headset, then list what appeared:
>
> ```
> python -c "import serial.tools.list_ports as p; [print(x.device, x.hwid) for x in p.comports()]"
> ```
>
> Pairing can produce two ports for the one headset. Use the one whose
> hardware ID contains the headset's MAC address. The other shows
> `000000000000`, opens instantly, and then never delivers a byte.

### `eeg.replay`: recorded sessions

| Key | Default | Meaning |
|---|---|---|
| `csv_path` | `logs/demo_session.csv` | Relative paths resolve from `python_bridge/` |
| `loop` | `true` | Restart at the end instead of going silent |
| `speed` | `1.0` | Playback rate. `2.0` runs twice as fast |

`logs/demo_session.csv` is committed, so `python main.py --source replay`
works from a fresh clone with no hardware and no setup. Point `csv_path` at
any `session_*.csv` a previous run wrote to replay that run instead.

### `eeg.mock`: synthetic signal, no hardware

| Key | Default | Meaning |
|---|---|---|
| `seed` | `42` | Fixed seed ⇒ reproducible runs, which is what makes it useful in tests |
| `attention_period_s` | `20.0` | Period of the attention sweep. Lower ⇒ the vehicle starts and stops more often |

---

## `signal_processing`: conditioning

| Key | Default | Meaning |
|---|---|---|
| `attention_window` | `5` | Rolling-average length. Higher = steadier but slower to react |
| `poor_signal_cutoff` | `25` | Above this, the headset's own quality metric says the signal is unusable, and the bridge pauses commands. `0` = perfect contact, `200` = not on a head |

---

## `vision`: how the vehicle turns

The webcam is the steering input: a raised hand turns the vehicle that way.
[`vision.README.md`](vision.README.md) covers it in full.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | The camera thread. Set `false` and the vehicle has no way to turn |
| `camera_index` | `0` | Which camera |
| `width` / `height` | `640` / `480` | Capture resolution |
| `fps_limit` | `15` | Camera frame rate. Never touches the control loop |
| `model_path` | `models/pose_landmarker_lite.task` | The committed pose model |
| `raise_margin` | `0.05` | How far above its shoulder a wrist must be, as a fraction of frame height |
| `min_visibility` | `0.6` | Below this landmark confidence, the frame is ignored |
| `hold_frames` | `3` | Consecutive frames a raise must persist |
| `refractory_ms` | `1200` | Quiet period after an accepted gesture |
| `repeat_while_held_ms` | `0` | `0` = one raise, one gesture. Above `0`, a held arm re-fires this often |
| `preview` | `false` | Show the camera window |

---

## `control`: the driving policy

| Key | Default | Meaning |
|---|---|---|
| `attention_forward_threshold` | `60` | At or above this smoothed attention, drive forward |
| `attention_stop_threshold` | `40` | Below this, start the stop timer. **Must be below the forward threshold.** The validator enforces it |
| `attention_stop_hold_ms` | `1000` | How long attention must stay low before stopping. Prevents one dip from halting the vehicle |
| `turn_source` | `vision` | Raised hands produce LEFT and RIGHT |
| `hold_turn_while_raised` | `true` | `true` = the vehicle keeps turning while the hand is up, and straightens when it comes down. `false` = one raise gives one pulse |
| `turn_command_repeat_ms` | `500` | How long a turn persists without a fresh trigger. See below |
| `calibration_seconds` | `15` | Startup phase during which the vehicle cannot move |
| `require_good_signal` | `true` | Whether poor signal quality pauses commands |

### How long a turn lasts

`turn_command_repeat_ms` sets a deadline. The bridge keeps transmitting the
turn until that deadline passes, then returns to whatever it was doing.

With `hold_turn_while_raised` set to `true`, the deadline is pushed forward
on every control cycle for as long as the hand stays up, and cleared the
moment it comes down. The turn therefore lasts exactly as long as the raise.
Here the value is a grace window that covers the gap between camera frames,
not a turn length — the camera runs at 15 fps and the control loop at 20 Hz,
so anything below roughly 200 ms will make a held turn stutter.

With it set to `false`, a raise gives one pulse of `turn_command_repeat_ms`
and the vehicle straightens even if the arm is still up.

The firmware caps a single pulse at `TURN_PULSE_MS` (300 ms in the firmware's
`config.h`) and will not restart that timer for a re-sent command. A turn
longer than 300 ms therefore arrives as repeated pulses rather than one
continuous one, which is what keeps the vehicle turning while the hand is up.

### The dead band

Between `attention_stop_threshold` and `attention_forward_threshold` the
vehicle **holds** whatever it is doing. Without that gap, attention hovering
near one threshold would make the vehicle stutter several times a second. A
gap of 15-20 is wide enough to prevent that.

---

## `transport`: talking to the vehicle

| Key | Default | Meaning |
|---|---|---|
| `mode` | `udp` | `udp` (wireless, primary) or `serial` (USB cable, fallback) |
| `resend_interval_ms` | `250` | Re-send the current command this often. Doubles as the watchdog keepalive, so it **must stay well below the firmware's 2000 ms `WATCHDOG_TIMEOUT_MS`** |
| `queue_size` | `32` | How many command changes can wait before the sender drops the oldest |

### `transport.udp`

| Key | Default | Meaning |
|---|---|---|
| `esp32_ip` | `192.168.4.1` | Correct when the ESP32 runs its own access point (the firmware default). In station mode, use the IP the ESP32 prints at boot |
| `esp32_port` | `4210` | Must match `UDP_COMMAND_PORT` in the firmware's `config.h` |
| `listen_port` | `4211` | Local port acknowledgements come back on. `0` = let the OS choose |
| `expect_ack` | `true` | Consume `ACK:` replies and measure round-trip time |

### `transport.serial`

| Key | Default | Meaning |
|---|---|---|
| `port` | `COM6` | The ESP32's USB port, **not** the headset's port |
| `baudrate` | `115200` | Must match `SERIAL_BAUD` in the firmware |

---

## `ui`, `logging`, `loop`

| Key | Default | Meaning |
|---|---|---|
| `ui.console_dashboard` | `true` | Live display. Turn off when piping output to a file |
| `ui.refresh_hz` | `10` | Dashboard redraw rate. Lower it if the terminal flickers |
| `ui.keyboard_override` | `true` | Enable the arrow-key override |
| `ui.colour` | `true` | ANSI colour. Skipped when the output is not a terminal |
| `logging.dir` | `logs` | Where log and session files go. Relative to `python_bridge/` |
| `logging.level` | `INFO` | `DEBUG` also records every acknowledgement line |
| `logging.csv_data_log` | `true` | Write `session_*.csv`. Also what replay reads back |
| `logging.console_log` | `false` | Echo log records to the terminal. Off, because the dashboard owns it |
| `loop.rate_hz` | `20` | Control-loop rate. **Must be ≥ 10**. 20 Hz gives ~50 ms of quantisation |

---

## Tuning guide

For the attention thresholds, based on what the calibration phase reports:

| Symptom | Change |
|---|---|
| Vehicle drives off too easily | Raise `attention_forward_threshold` |
| Cannot get it to move | Lower `attention_forward_threshold`; check signal quality first |
| Stutters between forward and stop | Widen the gap between the two thresholds |
| Stops on a momentary lapse | Raise `attention_stop_hold_ms` |
| Sluggish response to concentration | Lower `attention_window` |
| Jittery, noisy attention | Raise `attention_window` |
| Commands keep pausing | Check headset fit; as a last resort raise `poor_signal_cutoff` |

For the turns:

| Symptom | Change |
|---|---|
| A resting hand triggers turns | Raise `vision.raise_margin` |
| A hand passing through frame triggers turns | Raise `vision.hold_frames` |
| One raise gives two turns | Raise `vision.refractory_ms` |
| A held turn stutters | Raise `control.turn_command_repeat_ms` |

The calibration phase reports the user's mean, spread and range at startup
and suggests thresholds from them. `--apply-calibration` uses those
suggestions for that session without writing to the file.

Attention baselines differ from person to person, so thresholds fitted to one
wearer will not necessarily suit another.
