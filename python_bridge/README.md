# Python bridge

The laptop half of NeuroDrive: reads the EEG headset and the webcam, decides
what the vehicle should do, and sends commands to the ESP32.

Attention drives **FORWARD** and **STOP**. A raised hand turns **LEFT** and
**RIGHT**.

---

## Running it

```
python main.py                                  # uses config.json as-is
python main.py --source mock --esp32-ip 127.0.0.1   # no vehicle at all
python main.py --source serial --set eeg.serial.port=COM4
python main.py --source serial --vision-preview # show the camera window
python main.py --replay-file logs/demo_session.csv
python main.py --keyboard                       # start in override mode
python main.py --print-config                   # show the merged config, exit
```

| Flag | Effect |
|---|---|
| `--config PATH` | Use a different config file |
| `--source {serial,mock,replay}` | Where the EEG comes from |
| `--transport {udp,serial}` | How commands reach the vehicle |
| `--esp32-ip`, `--esp32-port` | Vehicle address |
| `--replay-file PATH` | Replay a recorded session |
| `--set KEY=VALUE` | Override any config key (repeatable) |
| `--vision-preview` | Show the camera window |
| `--skip-calibration` | Arm immediately |
| `--apply-calibration` | Use the thresholds calibration suggests |
| `--duration N` | Run N seconds then exit (for test runs) |
| `--keyboard` | Start in keyboard override |
| `--no-dashboard`, `--no-keyboard` | For scripted or piped runs |
| `--print-config` | Show the merged config and exit |

### Keys while running

| Key | Action |
|---|---|
| `k` | Toggle keyboard override |
| `↑` / `w` | Forward |
| `←` `→` / `a` `d` | Turn (a short pulse, then the previous state resumes) |
| `↓` / `s` / `space` | Stop. Under EEG control this is a software e-stop |
| `Enter` | Re-arm after a software e-stop |
| `c` | Restart calibration |
| `q` / `Esc` | Quit (transmits STOP first) |

### The dashboard

```
┌─ NeuroDrive Bridge ────────────────────────────────────────────
  EEG      CONNECTED    src=serial   samples=412    drops=0
  Signal   quality OK  (poor=  0)   attention  71.4 (raw  74)  med  38
  Attention [████████████████████████████·············]   71/100
  Command  FORWARD  attention 71 >= 60 -> forward
  Link     udp 192.168.4.1:4210      sent=1832  ack=1830  rtt=  8.4ms avg=  9.1ms
  Session  mode=EEG  up=03:12  loop=20.0Hz  turns=7  safe-stops=1
  Keys     up=fwd left/right=turn down/space=stop  k=override  c=recalibrate  q=quit
└────────────────────────────────────────────────────────────────
```

The attention bar crossing the threshold at the same moment the vehicle
starts moving is what makes the control decision visible while it is
happening.

---

## Module map

Data flows top to bottom; each module has one job.

| Module | Responsibility |
|---|---|
| `main.py` | Entry point, argument parsing, and the control loop |
| `thinkgear.py` | ThinkGear packet framing and payload decoding. Pure, no I/O |
| `eeg_sources.py` | `EEGSource` backends: serial, mock, replay |
| `eeg_reader.py` | Acquisition thread, reconnection, signal-loss detection |
| `signal_processor.py` | Rolling average and the signal-quality gate |
| `vision.py` | Webcam hand-raise detection, in its own thread |
| `command_mapper.py` | Thresholds → `FORWARD/LEFT/RIGHT/STOP` |
| `wifi_sender.py` | UDP/serial transport, send thread, keepalive, acks |
| `calibration.py` | Startup baseline and threshold suggestions |
| `console_ui.py` | Live dashboard |
| `keyboard_input.py` | Non-blocking key reader |
| `data_logger.py` | Log file and session CSV |
| `config.py` | Defaults, loading, merging, validation |
| `udp_test_sender.py` | Standalone: drive the vehicle with no headset |
| `vision_test.py` | Standalone: check the camera and pose model alone |

### Why the split between processor and mapper

The processor answers one question: what is the signal doing? Smoothing and
quality. The mapper answers the next one: so what should the vehicle do?
Thresholds, hysteresis, turn timing.

That boundary is the whole point. Retuning the vehicle for a different user
touches only the mapper's numbers in `config.json`, never the detection code
underneath.

### Threads

| Thread | Why |
|---|---|
| `eeg-reader` | A blocking serial read must never stall the control loop |
| `vision` | Camera capture and pose inference run at their own pace |
| main loop | Owns all policy state. Single-threaded, therefore testable |
| `cmd-sender` | A router glitch must never stall EEG processing |

They communicate through queues. No shared mutable state, no locks in the
control path.

---

## Output files

The bridge writes two files per run, into `logs/` or wherever `logging.dir`
points:

| File | Contents |
|---|---|
| `neurodrive_<stamp>.log` | Events, warnings, connection changes |
| `session_<stamp>.csv` | One row per control cycle: attention, quality, command, and the reason for it |

The session CSV is used for analysis and as the input to replay mode.
`logs/demo_session.csv` is committed, and is what `--source replay` plays
back by default.

Everything else in `logs/` is git-ignored, so ordinary runs never show up as
repository changes.

---

## Troubleshooting

### "cannot open EEG serial port"

* Is the headset on, and is its LED solid rather than blinking?
* Pairing can create two serial ports for the one headset. Only the
  **outgoing** one carries data; the other opens fine and then streams
  nothing. Pick the port whose hardware ID contains the headset's MAC
  address, not the one showing `000000000000`.
* Something else may hold the port. Close any other serial monitor.
* List what exists: `python -m serial.tools.list_ports -v`

### Connected, but attention stays `--`

The headset is streaming but has no good contact. The forehead sensor must
touch skin (not hair), and the ear clip must be on the earlobe. Watch
`poor=` on the dashboard: `0` is good, `200` means no contact at all. It
usually settles within 10-20 seconds of putting it on.

### The vehicle never moves

1. Is it armed? Calibration takes 15 seconds; the dashboard says
   `calibrating`.
2. Is `sent=` climbing? If not, the mapper is holding STOP. Read the reason
   text on the Command line.
3. Is `ack=` climbing? If `sent` climbs but `ack` does not, packets are not
   reaching the vehicle. Check the laptop joined the ESP32's network, and
   that `esp32_ip` matches what the ESP32 printed at boot.
4. Test the vehicle on its own: `python udp_test_sender.py --ping`

### It drives, but it will not turn

The fault is on the camera side. Check, in order:

1. Run `python vision_test.py`. If that sees nothing, the bridge will not
   either.
2. Are `mediapipe` and `opencv-python` installed? The camera thread logs
   `vision could not start` and exits if they are missing, and the rest of
   the bridge carries on without any way to turn.
3. Is another application holding the camera?
4. Is `vision.enabled` still `true`? Check with `--print-config`.

`vision.README.md` covers tuning in full.

### The vehicle moves but will not stop

The hardware kill switch cuts the motor supply immediately. The watchdog
should also stop it within 2 seconds of the bridge quitting; if it does not,
the firmware is not running the current build, or `WATCHDOG_TIMEOUT_MS` has
been changed.

### Turns are too long or too short

While a hand is held up and `control.hold_turn_while_raised` is `true`, the
turn lasts exactly as long as the hand stays raised and ends when it comes
down. That is the normal mode, and there is nothing to tune.

`control.turn_command_repeat_ms` (default 500 ms) is the grace window that
covers the gap between camera frames, not the turn length. The camera runs at
15 fps and the control loop at 20 Hz, so 500 ms has plenty of margin;
dropping it below roughly 200 ms will make a held turn stutter.

With `control.hold_turn_while_raised` set to `false`, one raise gives one
pulse of `turn_command_repeat_ms` instead, ending even if the arm is still
up.

The firmware's own `TURN_PULSE_MS` (300 ms in `config.h`) caps how long a
single pulse runs before the vehicle returns to its previous state. A longer
turn arrives as repeated pulses.

### The dashboard is garbled

The dashboard redraws with ANSI escape sequences, which some terminals do not
handle. `--no-dashboard` turns it off, and `ui.console_dashboard` does the
same permanently.

---

## Standalone tools

```
python udp_test_sender.py                 # forward/left/right/stop sequence
python udp_test_sender.py --ping          # does the vehicle answer?
python udp_test_sender.py --drive         # arrow-key driving, no EEG
python udp_test_sender.py --command F --hold 2
```

`udp_test_sender.py` needs neither a headset nor the bridge. It is the
fastest way to check wiring after a hardware change.

```
python vision_test.py                     # camera preview and live detection
```

See [`vision.README.md`](vision.README.md) for the webcam channel.
