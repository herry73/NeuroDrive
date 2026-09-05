# Python bridge

The laptop half of NeuroDrive: reads the EEG headset, decides what the
vehicle should do, and sends commands to the ESP32.

---

## Running it

```
python main.py                                  # uses config.json as-is
python main.py --source mock --esp32-ip 127.0.0.1   # no hardware at all
python main.py --source serial --set eeg.serial.port=COM4
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
| `--vision` | Turn with raised hands (implies `--turn-source vision`) |
| `--vision-preview` | Show the camera window |
| `--turn-source {blink,vision,both}` | What produces LEFT/RIGHT |
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

Put this on the projector during the demo. The attention bar crossing the
threshold at the exact moment the vehicle starts moving is the one thing that
makes the system readable to an audience.

---

## Module map

Data flows top to bottom; each module has one job.

| Module | Responsibility |
|---|---|
| `main.py` | Entry point, argument parsing, and the control loop |
| `thinkgear.py` | ThinkGear packet framing and payload decoding. Pure, no I/O |
| `eeg_sources.py` | `EEGSource` backends: serial, mock, replay |
| `eeg_reader.py` | Acquisition thread, reconnection, signal-loss detection |
| `signal_processor.py` | Rolling average, blink detection, quality gate |
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

The processor answers one question: what is the signal doing? Smoothing,
blink detection, quality. The mapper answers the next one: so what should the
vehicle do? Thresholds, hysteresis, turn timing.

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
| `session_<stamp>.csv` | One row per control cycle: attention, quality, blinks, command, and the reason for it |

The session CSV is used for analysis and as the input to replay mode. A good
recorded run *is* the fallback demo. `logs/demo_session.csv` is committed for
exactly that purpose, and is what `--source replay` plays back by default.

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

### The vehicle moves but will not stop

Press the hardware kill switch. The watchdog should then stop it within 2
seconds of the bridge quitting. If it does not, the firmware is not running
the current build, or someone changed `WATCHDOG_TIMEOUT_MS`.

### Turns are too long or too short

`control.turn_command_repeat_ms` (default 500 ms) is how long the bridge
keeps transmitting a turn without a fresh trigger. Lower it for shorter
turns, raise it for longer ones.

While a hand is held up and `control.hold_turn_while_raised` is `true`, that
deadline is pushed forward on every control cycle, so the turn lasts exactly
as long as the hand stays raised and ends when it comes down. In that mode
the value is a grace window covering the gap between camera frames, not the
turn length, and lowering it below about 200 ms will make a held turn
stutter.

The firmware's own `TURN_PULSE_MS` (300 ms in `config.h`) caps how long a
single pulse runs before the vehicle returns to its previous state. A longer
bridge-side turn simply re-triggers it.

### The dashboard is garbled

The dashboard redraws with ANSI escape sequences, which some terminals do not
handle. Use a terminal with ANSI support, or run with `--no-dashboard`.

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
