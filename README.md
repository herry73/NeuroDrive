# NeuroDrive

**An EEG-controlled vehicle.** You concentrate, it drives. You blink, it
turns. You take the headset off, it stops.

A NeuroSky MindWave Mobile 2 streams brain-activity metrics to a laptop over
Bluetooth. A Python bridge turns those into four movement commands and sends
them over WiFi to an ESP32, which drives a 2WD chassis through an L298N motor
driver.

```
  ┌──────────┐  Bluetooth  ┌──────────┐  WiFi UDP  ┌────────┐  PWM  ┌───────┐
  │ MindWave │────────────►│  Python  │───────────►│ ESP32  │──────►│ L298N │──► motors
  │ Mobile 2 │  ThinkGear  │  bridge  │  "F\n"     │        │       │       │
  └──────────┘             └──────────┘            └────────┘       └───────┘
    attention               thresholds              state machine
    blink                   debounce                watchdog
```

---

## Quick start without hardware

The whole system runs on any laptop. The bridge ships with a synthetic EEG
source, and `tests/fake_esp32.py` reimplements the vehicle firmware's state
machine in Python.

```powershell
git clone <repo-url> && cd NeuroDrive
.\setup.ps1                            # macOS / Linux: ./setup.sh
```

Then, in two terminals:

```powershell
# terminal 1: the vehicle
python tests\fake_esp32.py

# terminal 2: the bridge
cd python_bridge
python main.py --esp32-ip 127.0.0.1 --skip-calibration
```

You will see the live dashboard, and the simulated vehicle printing its state
changes. Press `k` to drive it with the arrow keys, `q` to quit.

---

## Repository layout

```
NeuroDrive/
├── python_bridge/       the laptop application          (M2, M5)
│   ├── main.py                 entry point and control loop
│   ├── thinkgear.py            NeuroSky packet parser
│   ├── eeg_sources.py          serial / mock / replay backends
│   ├── eeg_reader.py           acquisition thread
│   ├── signal_processor.py     smoothing, blink detection
│   ├── command_mapper.py       thresholds -> FORWARD/LEFT/RIGHT/STOP
│   ├── wifi_sender.py          UDP + serial transport, send thread
│   ├── calibration.py          startup baseline
│   ├── console_ui.py           live dashboard
│   ├── keyboard_input.py       arrow-key override
│   ├── data_logger.py          log files and session CSVs
│   ├── config.json             every tunable parameter
│   └── config.README.md        what each parameter does
│
├── firmware/            the ESP32 vehicle firmware       (M3, M4)
│   └── neurodrive_firmware/
│       ├── neurodrive_firmware.ino   setup() and loop()
│       ├── motor_control.*           state machine and PWM
│       ├── comm.*                    UDP + serial receive
│       ├── safety.*                  watchdog and e-stop
│       ├── status_led.*              indicator LEDs
│       └── config.h                  pins, speeds, timings
│
├── tests/               test suite and simulators        (M7)
│   ├── fake_esp32.py           the firmware, in Python
│   ├── mock_eeg_generator.py   synthetic sessions
│   ├── latency_benchmark.py    COM-03 measurement
│   └── test_*.py               unit and integration tests
│
└── docs/
    └── INTERFACE_CONTRACT.md   the protocol. Read this first.
```

---

## Running it for real

### 1. The vehicle

Copy the WiFi credentials template, then flash:

```powershell
cd firmware\neurodrive_firmware
Copy-Item secrets.h.example secrets.h    # edit it
```

Open `neurodrive_firmware.ino` in the Arduino IDE (board: *ESP32 Dev
Module*) and upload, or from `firmware/`:

```powershell
pio run -t upload && pio device monitor
```

By default the ESP32 creates **its own WiFi network**, so the vehicle is
always at `192.168.4.1` and no venue network is involved. See
`firmware/README.md`.

### 2. The headset

Pair the MindWave Mobile 2 and find its **outgoing** COM port (Windows:
Bluetooth settings → *More Bluetooth options* → *COM Ports*). Then in
`python_bridge/config.json`:

```json
"eeg":       { "source": "serial", "serial": { "port": "COM5" } },
"transport": { "mode": "udp", "udp": { "esp32_ip": "192.168.4.1" } }
```

### 3. Drive

```powershell
cd python_bridge
python main.py
```

Fifteen seconds of calibration, during which the vehicle cannot move. Then
it arms.

| Key | Action |
|---|---|
| `k` | Toggle keyboard override |
| `↑ ← → ↓` | Drive, when the override is on |
| `space` | Software emergency stop (`Enter` re-arms) |
| `c` | Recalibrate |
| `q` | Quit. Always sends STOP first |

---

## How it decides what to do

| Input | Condition | Command |
|---|---|---|
| Attention (0-100, smoothed over 5 samples) | ≥ 60 | **FORWARD** |
| | < 40 for more than 1 s | **STOP** |
| | between 40 and 60 | hold. The dead band stops it stuttering |
| Blink strength | ≥ 150, debounced 300 ms | **LEFT**, then **RIGHT**, alternating |
| Signal quality | "poor signal" > 25 | commands paused |
| Link | no EEG data for 2 s | **STOP** |

Every number here is in `config.json`. See `config.README.md` for what to
change when it feels wrong.

---

## Safety

Four independent layers. Each works if the others fail.

1. **Hardware kill switch.** Physically breaks the motor supply. Not
   software. This is the one that always works.
2. **E-stop button (GPIO 4).** The firmware latches STOP and refuses
   movement commands until someone releases the button.
3. **Watchdog (2 s).** If commands stop arriving, the vehicle stops on its
   own. It does not care why: bridge crashed, WiFi dropped, laptop lid shut.
4. **Signal-loss stop.** The bridge sends STOP the moment the headset goes
   quiet or the signal quality collapses.

Quitting the bridge, in any way, transmits STOP before exiting.

> The vehicle can move on its own. Test it in a clear area, keep the kill
> switch in someone's hand, and never leave it powered and unattended.

---

## Testing

```powershell
python -m pytest tests -q                  # everything, ~1 minute
python -m pytest tests -q -k "not Full"    # fast subset, no full-app runs
python tests\latency_benchmark.py          # COM-03 measurement
```

No hardware required for any of it. The integration tests drive the real
bridge modules into `fake_esp32.py`, which implements the same state machine,
turn timing and watchdog as the firmware. The tests fail if the two halves
of the interface contract ever drift apart.

---

## Demo fallbacks

Ordered by preference, all tested and working:

1. **Keyboard override.** Press `k`. The dashboard still shows live EEG
   values, so the headset is visibly working even if it is not steering.
2. **Recorded session replay.** Every run writes `logs/session_*.csv`.
   Replay a good one with `python main.py --replay-file logs/session_X.csv`.
   It looks identical to a live drive. Say so if asked.
3. **Serial cable.** Set `transport.mode` to `serial` if WiFi misbehaves.
4. **Vehicle on blocks.** Wheels spin freely, and the system is still
   visibly working.

Generate a scripted session in advance:

```powershell
python tests\mock_eeg_generator.py --scenario demo --duration 90 `
    --out python_bridge\logs\demo_session.csv
```

---

## Documentation

| Document | What it covers |
|---|---|
| [`docs/INTERFACE_CONTRACT.md`](docs/INTERFACE_CONTRACT.md) | The protocol, the state machine, the module APIs, the pin map. **Read this before changing anything that crosses a module boundary.** |
| [`python_bridge/README.md`](python_bridge/README.md) | Running and troubleshooting the bridge |
| [`python_bridge/config.README.md`](python_bridge/config.README.md) | Every configuration key, and how to tune it |
| [`firmware/README.md`](firmware/README.md) | Flashing, wiring, and firmware troubleshooting |
| [`tests/README.md`](tests/README.md) | The test plan and how to run it |
 
