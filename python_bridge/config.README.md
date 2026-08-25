# `config.json` reference

Every tunable parameter of the bridge lives in `config.json` (SP-07 / NFR
3.5). Nothing in this list requires editing code.

JSON has no comments, so the loader treats any key beginning with `_` as
documentation and skips it. That is why `config.json` contains
`_source`, `_mode` and similar keys.

Three ways to change a value, least permanent first:

```powershell
python main.py --set control.attention_forward_threshold=68   # this run only
python main.py --source mock --transport serial               # shortcuts
# edit config.json                                            # persistent
```

Missing keys fall back to the built-in defaults in `config.py`, so a partial
file is fine. `config.Config.validate()` runs at startup and refuses an
invalid combination outright, rather than letting it turn into odd vehicle
behaviour an hour later.

---

## `eeg`: where the signal comes from

| Key | Default | Meaning |
|---|---|---|
| `source` | `mock` | `serial` (real headset), `mock` (synthetic), `replay` (recorded CSV) |
| `signal_timeout_ms` | `2000` | No samples for this long ⇒ report signal loss and safe-stop (EEG-05) |

**Set `source` to `serial` for real use.** The default is `mock` so a fresh
clone runs on any laptop with no hardware.

### `eeg.serial`: the MindWave over Bluetooth

| Key | Default | Meaning |
|---|---|---|
| `port` | `COM5` | Windows: the *outgoing* COM port created when pairing. Linux: `/dev/rfcomm0`. macOS: `/dev/tty.MindWaveMobile-SerialPo` |
| `baudrate` | `57600` | Fixed by the MindWave Mobile 2. Do not change. |
| `read_timeout_s` | `0.2` | Blocking read timeout; also paces the reader thread |
| `reconnect_attempts` | `3` | NFR 3.1. After this many failures the status becomes `FAILED`, then retries continue slowly so a demo can recover without a restart |
| `reconnect_delay_s` | `2.0` | Wait between attempts |

> **Finding the port on Windows:** pair the headset, then Bluetooth settings
> → *More Bluetooth options* → *COM Ports*. Use the **Outgoing** one. Two
> ports appear; the incoming one will not work.

### `eeg.replay`: recorded sessions (demo Fallback Level 2)

| Key | Default | Meaning |
|---|---|---|
| `csv_path` | `logs/recorded_session.csv` | Relative paths resolve from `python_bridge/` |
| `loop` | `true` | Restart at the end instead of going silent |
| `speed` | `1.0` | Playback rate. `2.0` runs twice as fast |

Replay any `session_*.csv` a previous run wrote. That is how a good session
becomes the fallback demo.

### `eeg.mock`: synthetic signal, no hardware

| Key | Default | Meaning |
|---|---|---|
| `seed` | `42` | Fixed seed ⇒ reproducible runs, which is what makes it useful in tests |
| `blink_interval_s` | `8.0` | Seconds between synthetic blinks |
| `attention_period_s` | `20.0` | Period of the attention sweep. Lower ⇒ the vehicle starts and stops more often |
| `emit_raw` | `false` | Also generate the 512 Hz raw wave (only needed to test `blink_from_raw`) |

---

## `signal_processing`: conditioning

| Key | Default | Req. | Meaning |
|---|---|---|---|
| `attention_window` | `5` | SP-01 | Rolling-average length. Higher = steadier but slower to react |
| `blink_strength_threshold` | `150` | SP-04 | Minimum blink strength that counts. Raise if ordinary blinking triggers turns |
| `blink_debounce_ms` | `300` | SP-06 | Minimum gap between accepted blinks. Raise if one blink registers twice |
| `double_blink_window_ms` | `500` | SP-05 | Two blinks closer than this are one *double* gesture (only used in `single_double` mode) |
| `poor_signal_cutoff` | `25` | SF-03 | Above this, the headset's own quality metric says the signal is unusable, and the bridge pauses commands. `0` = perfect contact, `200` = not on a head |

### `signal_processing.blink_from_raw`: fallback detector

Off by default. Enable only if your headset does not emit blink rows (0x16).
It detects blinks from large excursions in the raw wave instead. Requires
`eeg.mock.emit_raw` or a real headset streaming raw data.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the fallback on |
| `amplitude_threshold` | `300` | Raw-wave magnitude that counts as a blink artefact |
| `refractory_ms` | `400` | Ignore further peaks for this long (one blink is many samples) |

---

## `control`: the driving policy

| Key | Default | Req. | Meaning |
|---|---|---|---|
| `attention_forward_threshold` | `60` | SP-02 | At or above this smoothed attention, drive forward |
| `attention_stop_threshold` | `40` | SP-03 | Below this, start the stop timer. **Must be below the forward threshold.** The validator enforces it |
| `attention_stop_hold_ms` | `1000` | SP-03 | How long attention must stay low before stopping. Prevents one dip from halting the vehicle |
| `blink_mode` | `alternate` | SP-05 | `alternate`: each blink turns the other way (fastest). `single_double`: one blink one way, two blinks the other (adds ~500 ms while classifying) |
| `first_turn_direction` | `LEFT` | n/a | Which way the first blink turns. In `single_double` mode, the direction a *single* blink means |
| `turn_command_repeat_ms` | `150` | MV-03 | How long the bridge keeps transmitting a turn. **Must be less than `TURN_PULSE_MS` (300 ms) in the firmware**, or re-sends extend the turn |
| `calibration_seconds` | `15` | UI-02 | Startup phase during which the vehicle cannot move |
| `require_good_signal` | `true` | SF-03 | Whether poor signal quality pauses commands. Leave `true` |

### The dead band

Between `attention_stop_threshold` and `attention_forward_threshold` the
vehicle **holds** whatever it is doing. Without that gap, attention hovering
near one threshold would make the vehicle stutter several times a second.
Appendix B of the project plan recommends keeping the two 15-20 apart.

---

## `transport`: talking to the vehicle

| Key | Default | Meaning |
|---|---|---|
| `mode` | `udp` | `udp` (wireless, primary) or `serial` (USB cable, fallback) |
| `resend_interval_ms` | `250` | Re-send the current command this often. Doubles as the watchdog keepalive, so it **must stay well below the firmware's 2000 ms `WATCHDOG_TIMEOUT_MS`** |
| `queue_size` | `32` | How many command changes can wait before the sender drops the oldest |
| `turn_burst` | `3` | How many times the bridge transmits a turn, since UDP has no retries |

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
| `ui.keyboard_override` | `true` | Enable the arrow-key override (UI-03) |
| `ui.colour` | `true` | ANSI colour. Skipped when the output is not a terminal |
| `logging.dir` | `logs` | Where log and session files go. Relative to `python_bridge/` |
| `logging.level` | `INFO` | `DEBUG` also records every acknowledgement line |
| `logging.csv_data_log` | `true` | Write `session_*.csv` (EEG-04). Also what replay reads back |
| `logging.console_log` | `false` | Echo log records to the terminal. Off, because the dashboard owns it |
| `loop.rate_hz` | `20` | Control-loop rate. **Must be ≥ 10** (NFR 3.2). 20 Hz gives ~50 ms of quantisation |

---

## Threshold tuning guide

From Appendix B of the project plan, plus what the calibration phase reports.

| Symptom | Change |
|---|---|
| Vehicle drives off too easily | Raise `attention_forward_threshold` |
| Cannot get it to move | Lower `attention_forward_threshold`; check signal quality first |
| Stutters between forward and stop | Widen the gap between the two thresholds |
| Stops on a momentary lapse | Raise `attention_stop_hold_ms` |
| Ordinary blinking causes turns | Raise `blink_strength_threshold` |
| One blink causes two turns | Raise `blink_debounce_ms` |
| Sluggish response to concentration | Lower `attention_window` |
| Jittery, noisy attention | Raise `attention_window` |
| Commands keep pausing | Check headset fit; as a last resort raise `poor_signal_cutoff` |

Run `python main.py` and let the calibration phase finish. It reports the
user's mean, spread and range, then suggests thresholds. `--apply-calibration`
uses them for that session without writing to the file.

**Tune with at least three different people** (plan section 12.2). Thresholds
fitted to whoever wore the headset most will not fit the person who wears it
on demo day.
