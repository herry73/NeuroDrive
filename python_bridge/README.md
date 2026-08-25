# Python bridge

The laptop half of NeuroDrive: reads the EEG headset, decides what the
vehicle should do, and sends commands to the ESP32.

Owners: **M2** (EEG and signal processing), **M5** (application and
networking).

---

## Running it

```powershell
python main.py                                  # uses config.json as-is
python main.py --source mock --esp32-ip 127.0.0.1   # no hardware at all
python main.py --source serial --set eeg.serial.port=COM5
python main.py --replay-file logs/session_20250101_120000.csv
python main.py --keyboard                       # start in override mode
python main.py --print-config                   # show the merged config, exit
```

| Flag | Effect |
|---|---|
| `--source {serial,mock,replay}` | Where the EEG comes from |
| `--transport {udp,serial}` | How commands reach the vehicle |
| `--esp32-ip`, `--esp32-port` | Vehicle address |
| `--replay-file PATH` | Replay a recorded session |
| `--set KEY=VALUE` | Override any config key (repeatable) |
| `--skip-calibration` | Arm immediately |
| `--apply-calibration` | Use the thresholds calibration suggests |
| `--duration N` | Run N seconds then exit (for test runs) |
| `--keyboard` | Start in keyboard override |
| `--no-dashboard`, `--no-keyboard` | For scripted or piped runs |

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
makes the system readable to an audience (plan section 12.3).

---

## Module map

Data flows left to right; each module has one job (NFR 3.5).

| Module | Responsibility | Requirements |
|---|---|---|
| `thinkgear.py` | ThinkGear packet framing and payload decoding. Pure, no I/O | EEG-02 |
| `eeg_sources.py` | `EEGSource` backends: serial, mock, replay | EEG-01, NFR 3.6 |
| `eeg_reader.py` | Acquisition thread, reconnection, signal-loss detection | EEG-03/04/05, NFR 3.1 |
| `signal_processor.py` | Rolling average, blink detection, quality gate | SP-01, SP-04/05/06, SF-03 |
| `command_mapper.py` | Thresholds → `FORWARD/LEFT/RIGHT/STOP` | MV-01, MV-03, SP-02/03 |
| `wifi_sender.py` | UDP/serial transport, send thread, keepalive, acks | COM-02/03/04/05 |
| `calibration.py` | Startup baseline and threshold suggestions | UI-02 |
| `console_ui.py` | Live dashboard | UI-01, EEG-06 |
| `keyboard_input.py` | Cross-platform non-blocking key reader | UI-03 |
| `data_logger.py` | Log file and session CSV | EEG-04 |
| `config.py` | Loading, merging, validation | SP-07, NFR 3.5 |

Full API signatures: [`../docs/INTERFACE_CONTRACT.md`](../docs/INTERFACE_CONTRACT.md).

### Why the split between processor and mapper

The processor answers one question: what is the signal doing? Smoothing,
blink detection, quality. The mapper answers the next one: so what should the
vehicle do? Thresholds, hysteresis, turn timing.

That boundary is the whole point. Retuning the vehicle for a different user
touches only the mapper's numbers in `config.json`, never the detection code
that took M2 weeks to get right.

### Threads

| Thread | Why |
|---|---|
| `eeg-reader` | A blocking serial read must never stall the control loop |
| main loop | Owns all policy state. Single-threaded, therefore testable |
| `cmd-sender` | A router glitch must never stall EEG processing (NFR 3.3) |

They communicate through two queues. No shared mutable state, no locks in the
control path.

---

## Output files

The bridge writes two files per run, into `logs/` or wherever `logging.dir`
points:

| File | Contents |
|---|---|
| `neurodrive_<stamp>.log` | Events, warnings, connection changes |
| `session_<stamp>.csv` | One row per control cycle: attention, quality, blinks, command, and the reason for it |

The session CSV is both the analysis input for M7's report and the input to
replay mode. A good recorded run *is* the fallback demo.

---

## Troubleshooting

### "cannot open EEG serial port"

* Is the headset on, and is its LED solid rather than blinking?
* On Windows, use the **Outgoing** COM port. Pairing creates two; the
  incoming one will never work.
* Something else may hold the port. Close any other serial monitor.
* Check it exists: `python -m serial.tools.list_ports -v`

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

`TURN_PULSE_MS` in the firmware's `config.h` sets the actual turn duration.
`control.turn_command_repeat_ms` here **must stay below it** (default 150 vs
300 ms), or the bridge's re-sends will keep extending the turn.

### The dashboard is garbled

Some terminals do not handle ANSI redraws. Use Windows Terminal or
PowerShell 7, or run with `--no-dashboard`.

---

## Standalone tools

```powershell
python udp_test_sender.py                 # forward/left/right/stop sequence
python udp_test_sender.py --ping          # does the vehicle answer?
python udp_test_sender.py --drive         # arrow-key driving, no EEG
python udp_test_sender.py --command F --hold 2
```

`udp_test_sender.py` needs neither a headset nor the bridge. It is the fastest
way to check wiring after M4 changes something.
