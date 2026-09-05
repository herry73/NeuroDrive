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

An optional webcam channel can steer as well: raise a hand and the vehicle
turns that way, for as long as you hold it up.

---

## Install

```
git clone https://github.com/herry73/NeuroDrive
cd NeuroDrive
```

Create a virtual environment and activate it using whatever your shell
expects, then install the dependency:

```
python -m venv .venv
pip install -r python_bridge/requirements.txt
```

That installs `pyserial`, which is needed only for real hardware. In mock and
replay mode the bridge runs on the standard library alone, so you can skip
the install entirely and still drive the whole pipeline.

The webcam channel needs two more packages, left out of `requirements.txt` on
purpose so the bridge runs on machines without them:

```
pip install mediapipe opencv-python
```

The pose model is already committed to the repository at
`python_bridge/models/pose_landmarker_lite.task`, so there is nothing to
download.

---

## Quick start without hardware

```
cd python_bridge
python main.py --source mock --skip-calibration
```

You get the live dashboard with a synthetic attention trace, commands being
decided, and a session CSV in `logs/`. Nothing is listening on the other end
of the UDP socket, which is fine — the bridge does not require an ack.

Press `k` to drive with the arrow keys, `q` to quit.

---

## Repository layout

```
NeuroDrive/
├── python_bridge/       the laptop application
│   ├── main.py                 entry point and control loop
│   ├── thinkgear.py            NeuroSky packet parser
│   ├── eeg_sources.py          serial / mock / replay backends
│   ├── eeg_reader.py           acquisition thread
│   ├── signal_processor.py     smoothing, blink detection
│   ├── command_mapper.py       thresholds -> FORWARD/LEFT/RIGHT/STOP
│   ├── wifi_sender.py          UDP + serial transport, send thread
│   ├── vision.py               webcam hand-raise input (optional)
│   ├── calibration.py          startup baseline
│   ├── console_ui.py           live dashboard
│   ├── keyboard_input.py       arrow-key override
│   ├── data_logger.py          log files and session CSVs
│   ├── config.py               defaults, merging, validation
│   ├── udp_test_sender.py      drive the vehicle without a headset
│   ├── vision_test.py          check the camera and pose model alone
│   ├── config.json             every tunable parameter
│   ├── config.README.md        what each parameter does
│   ├── vision.README.md        the webcam channel in detail
│   ├── requirements.txt        pyserial
│   ├── models/                 the committed pose model
│   └── logs/                   run logs and session CSVs, plus demo_session.csv
│
└── firmware/            the ESP32 vehicle firmware
    ├── platformio.ini              PlatformIO project
    └── neurodrive_firmware/
        ├── neurodrive_firmware.ino   setup() and loop()
        ├── motor_control.*           state machine and PWM
        ├── comm.*                    UDP + serial receive
        ├── safety.*                  watchdog and e-stop
        ├── status_led.*              indicator LEDs
        ├── config.h                  pins, speeds, timings
        └── secrets.h.example         WiFi credentials template
```

---

## Running it for real

### 1. The vehicle

Copy `firmware/neurodrive_firmware/secrets.h.example` to `secrets.h` in the
same folder. It ships with working credentials (`NeuroDrive` /
`neurodrive2024`), so it builds as-is; change them only if you want a
different network name or password. `secrets.h` is git-ignored, so the
password never lands in the repository.

Open `neurodrive_firmware.ino` in the Arduino IDE (board: *ESP32 Dev
Module*) and upload, or from `firmware/`:

```
pio run -t upload
pio device monitor
```

By default the ESP32 creates **its own WiFi network**, so the vehicle is
always at `192.168.4.1` and no venue network is involved. Join that network
from the laptop before starting the bridge. See `firmware/README.md`.

### 2. The headset

Pair the MindWave Mobile 2, then find its serial port. Pairing can create
**two** ports for the one device, and only one of them works:

```
python -c "import serial.tools.list_ports as p; [print(x.device, x.hwid) for x in p.comports()]"
```

Pick the port whose hardware ID contains the headset's **MAC address**. The
other one shows `000000000000` — that is the incoming port, and it opens
instantly and then streams nothing forever, which looks exactly like a
working connection that never produces data.

Put the port you picked into `python_bridge/config.json`:

```json
"eeg":       { "source": "serial", "serial": { "port": "COM4" } },
"transport": { "mode": "udp", "udp": { "esp32_ip": "192.168.4.1" } }
```

Power the headset on and wait for a **solid** blue LED before starting the
bridge. A blinking LED means it has not linked yet, and it goes back to sleep
if nothing connects.

### 3. Drive

```
cd python_bridge
python main.py --source serial
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

### With the webcam as well

```
python main.py --source serial --vision --turn-source both
```

`--vision` on its own switches steering to the camera. `--turn-source both`
keeps blinks working too. Add `--vision-preview` to watch what the pose model
is tracking. See `vision.README.md`.

---

## Every command

All of these run from `python_bridge/`.

### Running the bridge

```
python main.py                                  # whatever config.json says
python main.py --source serial                  # real headset
python main.py --source mock                    # synthetic signal, no hardware
python main.py --source replay                  # replays logs/demo_session.csv

python main.py --source serial --vision --turn-source both        # EEG + webcam
python main.py --source serial --vision --turn-source both --vision-preview

python main.py --replay-file logs/demo_session.csv  # replay one specific run
python main.py --keyboard                       # start in keyboard override
python main.py --skip-calibration               # arm immediately, no 15 s wait
python main.py --apply-calibration              # adopt the suggested thresholds
python main.py --duration 30                    # run 30 s then exit
python main.py --print-config                   # show merged config, then exit
python main.py --config config.json             # point at a different config file
python main.py --help
```

Quieter output, for scripting or logging to a file:

```
python main.py --source mock --no-dashboard --no-keyboard --duration 20
```

### Talking to the vehicle without a headset

```
python udp_test_sender.py                       # the standard F/L/R/S sequence
python udp_test_sender.py --ping                # does the vehicle reply?
python udp_test_sender.py --command F --hold 2  # hold FORWARD for 2 seconds
python udp_test_sender.py --command LEFT        # long forms work too
python udp_test_sender.py --drive               # arrow-key driving
python udp_test_sender.py --esp32-ip 192.168.1.50
python udp_test_sender.py --transport serial    # over the USB cable instead
```

### Checking the webcam on its own

```
python vision_test.py                           # preview window, live detection
python vision_test.py --camera 1                # a different camera
python vision_test.py --no-preview              # headless, prints events
python vision_test.py --duration 30
python vision_test.py --raise-margin 0.10       # require a higher raise
python vision_test.py --hold-frames 5           # require a steadier raise
python vision_test.py --refractory-ms 800       # shorter gap between turns
python vision_test.py --swap-sides              # if turns come out mirrored
```

### Finding the headset's serial port

```
python -c "import serial.tools.list_ports as p; [print(x.device, x.hwid) for x in p.comports()]"
```

---

## Setting parameters yourself

Three ways, in increasing order of permanence.

**1. Shortcut flags**, for the things you change most often:

| Flag | Overrides |
|---|---|
| `--source {serial,mock,replay}` | `eeg.source` |
| `--transport {udp,serial}` | `transport.mode` |
| `--esp32-ip` / `--esp32-port` | `transport.udp.esp32_ip` / `.esp32_port` |
| `--turn-source {blink,vision,both}` | `control.turn_source` |
| `--vision` | `vision.enabled` (and implies `--turn-source vision`) |
| `--vision-preview` | `vision.preview` |
| `--replay-file` | `eeg.replay.csv_path`, and implies `--source replay` |

**2. `--set key=value`** reaches *any* setting, for one run only:

```
python main.py --set control.attention_forward_threshold=65
python main.py --set eeg.serial.port=COM7 --set eeg.source=serial
python main.py --set transport.invert_turns=false
python main.py --set control.hold_turn_while_raised=false
python main.py --set signal_processing.blink_strength_threshold=120
python main.py --set control.calibration_seconds=0 --set loop.rate_hz=30
```

Repeat `--set` as many times as you like. Check the result before driving:

```
python main.py --set control.attention_forward_threshold=65 --print-config
```

**3. Edit `config.json`** to make it stick. Every key below is settable by
either route, and every default shown is what a fresh clone actually runs.

| Key | Default | What it does |
|---|---|---|
| `eeg.source` | `mock` | `serial`, `mock` or `replay` |
| `eeg.serial.port` | `COM4` | The headset's **outgoing** Bluetooth port |
| `eeg.serial.baudrate` | `57600` | Fixed by the MindWave |
| `eeg.serial.reconnect_attempts` | `3` | Raise it if the headset is slow to wake |
| `eeg.serial.reconnect_delay_s` | `2.0` | Wait between attempts |
| `eeg.replay.csv_path` | `logs/demo_session.csv` | Which run to replay |
| `eeg.replay.loop` / `.speed` | `true` / `1.0` | Loop forever, playback rate |
| `eeg.mock.seed` | `42` | Makes the synthetic signal reproducible |
| `eeg.mock.blink_interval_s` | `8.0` | How often the fake user blinks |
| `eeg.signal_timeout_ms` | `2000` | Silence after which the signal counts as lost |
| `signal_processing.attention_window` | `5` | Samples in the rolling average |
| `signal_processing.blink_strength_threshold` | `150` | Below this, a blink is ignored |
| `signal_processing.blink_debounce_ms` | `300` | Minimum gap between blinks |
| `signal_processing.poor_signal_cutoff` | `25` | Above this, commands pause |
| `vision.enabled` | `false` | Turn the webcam channel on |
| `vision.camera_index` | `0` | Which camera |
| `vision.raise_margin` | `0.05` | How far above the shoulder a wrist must be |
| `vision.hold_frames` | `3` | Frames a raise must persist |
| `vision.refractory_ms` | `1200` | Quiet period after a gesture |
| `vision.swap_sides` | `false` | Only if the *camera* turns are mirrored |
| `control.attention_forward_threshold` | `60` | Drive at or above this |
| `control.attention_stop_threshold` | `40` | Stop below this |
| `control.attention_stop_hold_ms` | `1000` | How long it must stay low |
| `control.turn_source` | `blink` | `blink`, `vision` or `both` |
| `control.hold_turn_while_raised` | `true` | Keep turning while the hand is up |
| `control.blink_mode` | `alternate` | Or `single_double` |
| `control.turn_command_repeat_ms` | `500` | How long a turn persists without a refresh |
| `control.calibration_seconds` | `15` | `0` skips calibration |
| `control.require_good_signal` | `true` | Refuse to drive on a poor signal |
| `transport.mode` | `udp` | Or `serial` over USB |
| `transport.udp.esp32_ip` | `192.168.4.1` | The vehicle's address |
| `transport.udp.esp32_port` | `4210` | Command port |
| `transport.invert_turns` | `true` | Fix a vehicle wired mirrored |
| `transport.resend_interval_ms` | `250` | Also the watchdog keepalive rate |
| `ui.console_dashboard` | `true` | The live display |
| `ui.colour` | `true` | Turn off for a dumb terminal |
| `logging.level` | `INFO` | `DEBUG` for a lot more detail |
| `loop.rate_hz` | `20` | Control-loop rate, minimum 10 |

`config.README.md` explains the reasoning behind each one.

---

## How it decides what to do

| Input | Condition | Command |
|---|---|---|
| Attention (0-100, smoothed over 5 samples) | ≥ 60 | **FORWARD** |
| | < 40 for more than 1 s | **STOP** |
| | between 40 and 60 | hold. The dead band stops it stuttering |
| Blink strength | ≥ 150, debounced 300 ms | **LEFT**, then **RIGHT**, alternating |
| Raised hand (webcam) | held up, when vision is on | **LEFT** / **RIGHT**, for as long as it stays up |
| Signal quality | "poor signal" > 25 | commands paused |
| Link | no EEG data for 2 s | **STOP** |

A raised hand goes through the same safety checks as a blink, so the camera
cannot move a vehicle whose operator the headset has lost track of.

Every number here lives in `config.json` — see [Setting parameters
yourself](#setting-parameters-yourself) above.

Two worth knowing about. `transport.invert_turns` fixes a vehicle that turns
the opposite way to the command; it is applied to the outgoing byte only, so
one setting corrects blink, webcam and keyboard turns together.
`control.hold_turn_while_raised` decides whether a raised hand turns for as
long as it is up, or gives one short pulse per raise.

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

## When it will not connect

Almost every failure is the Bluetooth link, not the software.

| What you see | What it means |
|---|---|
| `poor_signal` stuck at 200, "connected" | Wrong serial port — you have the incoming one. See step 2 above |
| *Semaphore timeout* | The headset is not answering: off, asleep, out of range, or a flat battery |
| *Pipe not connected* | The link came up and dropped. Usually a weak battery or a stale pairing; retry, then re-pair |
| *File not found*, or the port is missing | The port does not exist. The pairing was removed, or it was renumbered on re-pair |
| *Access denied* | Something else holds the port — another bridge, or the NeuroSky app |

A fresh AAA fixes more of these than anything else. The headset also holds
only one connection at a time, so turn Bluetooth off on any phone or tablet
it has been paired with.

---

## Demo fallbacks

Ordered by preference:

1. **Keyboard override.** Press `k`. The dashboard still shows live EEG
   values, so the headset is visibly working even if it is not steering.
2. **Recorded session replay.** Every run writes `logs/session_*.csv`, and
   `logs/demo_session.csv` is committed and replays out of the box:
   `python main.py --source replay`. It looks identical to a live drive. Say
   so if asked.
3. **Synthetic source.** `python main.py --source mock` needs no headset at
   all.
4. **Serial cable.** Set `transport.mode` to `serial` if WiFi misbehaves.
5. **Vehicle on blocks.** Wheels spin freely, and the system is still
   visibly working.

---

## Documentation

| Document | What it covers |
|---|---|
| [`python_bridge/README.md`](python_bridge/README.md) | Running and troubleshooting the bridge |
| [`python_bridge/config.README.md`](python_bridge/config.README.md) | Every configuration key, and how to tune it |
| [`python_bridge/vision.README.md`](python_bridge/vision.README.md) | The webcam channel: setup, tuning, and how it folds into the mapper |
| [`firmware/README.md`](firmware/README.md) | Flashing, wiring, and firmware troubleshooting |
