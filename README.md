# NeuroDrive

**An EEG-controlled vehicle.** You concentrate, it drives. You raise a hand,
it turns. You take the headset off, it stops.

A NeuroSky MindWave Mobile 2 streams brain-activity metrics to a laptop over
Bluetooth, and a webcam watches the driver. A Python bridge turns attention
into forward and stop, turns a raised hand into left and right, and sends the
result over WiFi to an ESP32, which drives a 2WD chassis through an L298N
motor driver.

```
  ┌──────────┐  Bluetooth  ┌──────────┐  WiFi UDP  ┌────────┐  PWM  ┌───────┐
  │ MindWave │────────────►│          │───────────►│ ESP32  │──────►│ L298N │──► motors
  │ Mobile 2 │  ThinkGear  │  Python  │  "F\n"     │        │       │       │
  └──────────┘  attention  │  bridge  │            └────────┘       └───────┘
  ┌──────────┐             │          │             state machine
  │  Webcam  │────────────►│          │             watchdog
  └──────────┘  hand raise └──────────┘
                            thresholds
```

Two inputs, four commands:

| Input | Produces |
|---|---|
| Attention level from the headset | **FORWARD** and **STOP** |
| A raised hand at the webcam | **LEFT** and **RIGHT** |

---

## Install

```
git clone https://github.com/herry73/NeuroDrive
cd NeuroDrive
```

Create a virtual environment and activate it using whatever your shell
expects, then install the dependencies:

```
python -m venv .venv
pip install -r python_bridge/requirements.txt
```

That installs `pyserial` for the headset and the ESP32's USB fallback, plus
`mediapipe` and `opencv-python` for the webcam.

The pose model is already committed to the repository at
`python_bridge/models/pose_landmarker_lite.task`, so there is nothing to
download and the bridge needs no internet access to run.

---

## Quick start without hardware

```
cd python_bridge
python main.py --source mock --skip-calibration
```

You get the live dashboard with a synthetic attention trace, commands being
decided, and a session CSV in `logs/`. Raise a hand at the webcam and the
turn appears on the Command line. Nothing is listening on the other end of
the UDP socket, which is fine — the bridge does not require an ack.

Press `k` to drive with the arrow keys, `q` to quit.

To run with no camera either, turn it off for that run:

```
python main.py --source mock --skip-calibration --set vision.enabled=false
```

Attention still drives forward and stop; there is simply no way to turn.

---

## Repository layout

```
NeuroDrive/
├── python_bridge/       the laptop application
│   ├── main.py                 entry point and control loop
│   ├── thinkgear.py            NeuroSky packet parser
│   ├── eeg_sources.py          serial / mock / replay backends
│   ├── eeg_reader.py           acquisition thread
│   ├── signal_processor.py     smoothing and the signal-quality gate
│   ├── vision.py               webcam hand-raise detection
│   ├── command_mapper.py       thresholds -> FORWARD/LEFT/RIGHT/STOP
│   ├── wifi_sender.py          UDP + serial transport, send thread
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
│   ├── requirements.txt        pyserial, mediapipe, opencv-python
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

That port is what `python_bridge/config.json` needs:

```json
"eeg":       { "source": "serial", "serial": { "port": "COM4" } },
"transport": { "mode": "udp", "udp": { "esp32_ip": "192.168.4.1" } }
```

Power the headset on and wait for a **solid** blue LED before starting the
bridge. A blinking LED means it has not linked yet, and it goes back to sleep
if nothing connects.

### 3. The camera

`vision_test.py` exercises the camera and the pose model on their own, with
no headset and no vehicle:

```
cd python_bridge
python vision_test.py
```

A preview window opens with the driver's shoulders and wrists drawn on, and
every accepted raise prints a line. It also reports the percentage of frames
in which it found a person; backlighting from a window behind the driver is
the usual cause of a low figure. See `vision.README.md`.

### 4. Drive

```
cd python_bridge
python main.py --source serial
```

Fifteen seconds of calibration, during which the vehicle cannot move. Then
it arms. Concentrate to go, relax to stop, raise a hand to turn.

Add `--vision-preview` to put the tracked skeleton on screen while driving.

| Key | Action |
|---|---|
| `k` | Toggle keyboard override |
| `↑ ← → ↓` | Drive, when the override is on |
| `space` | Software emergency stop (`Enter` re-arms) |
| `c` | Recalibrate |
| `q` | Quit. Always sends STOP first |

---

## Every command

All of these run from `python_bridge/`.

### Running the bridge

```
python main.py                                  # whatever config.json says
python main.py --source serial                  # real headset
python main.py --source mock                    # synthetic signal, no headset
python main.py --source replay                  # replays logs/demo_session.csv

python main.py --source serial --vision-preview # show the camera window too

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
| `--vision-preview` | `vision.preview` |
| `--replay-file` | `eeg.replay.csv_path`, and implies `--source replay` |

**2. `--set key=value`** reaches *any* setting, for one run only:

```
python main.py --set control.attention_forward_threshold=65
python main.py --set eeg.serial.port=COM7 --set eeg.source=serial
python main.py --set vision.hold_frames=5
python main.py --set control.hold_turn_while_raised=false
python main.py --set vision.raise_margin=0.10
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
| `eeg.signal_timeout_ms` | `2000` | Silence after which the signal counts as lost |
| `signal_processing.attention_window` | `5` | Samples in the rolling average |
| `signal_processing.poor_signal_cutoff` | `25` | Above this, commands pause |
| `vision.enabled` | `true` | The webcam channel |
| `vision.camera_index` | `0` | Which camera |
| `vision.raise_margin` | `0.05` | How far above the shoulder a wrist must be |
| `vision.hold_frames` | `3` | Frames a raise must persist |
| `vision.refractory_ms` | `1200` | Quiet period after a gesture |
| `vision.preview` | `false` | Show the camera window |
| `control.attention_forward_threshold` | `60` | Drive at or above this |
| `control.attention_stop_threshold` | `40` | Stop below this |
| `control.attention_stop_hold_ms` | `1000` | How long it must stay low |
| `control.turn_source` | `vision` | Raised hands produce the turns |
| `control.hold_turn_while_raised` | `true` | Keep turning while the hand is up |
| `control.turn_command_repeat_ms` | `500` | How long a turn persists without a refresh |
| `control.calibration_seconds` | `15` | `0` skips calibration |
| `control.require_good_signal` | `true` | Refuse to drive on a poor signal |
| `transport.mode` | `udp` | Or `serial` over USB |
| `transport.udp.esp32_ip` | `192.168.4.1` | The vehicle's address |
| `transport.udp.esp32_port` | `4210` | Command port |
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
| Raised hand (webcam) | held up | **LEFT** / **RIGHT**, for as long as it stays up |
| Signal quality | "poor signal" > 25 | commands paused |
| Link | no EEG data for 2 s | **STOP** |

A raised hand goes through the same safety gates as everything else, so the
camera cannot move a vehicle whose operator the headset has lost track of.

Every number here lives in `config.json` — see [Setting parameters
yourself](#setting-parameters-yourself) above.

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

> The vehicle moves under its own power, and the software layers above can
> all fail at once. The hardware kill switch is the only one that cannot,
> which is why it stays in an operator's hand whenever the motors are live.

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

If the vehicle drives but will not turn, the fault is the camera rather than
the headset. Run `python vision_test.py` and see `vision.README.md`.

---

## Documentation

| Document | What it covers |
|---|---|
| [`python_bridge/README.md`](python_bridge/README.md) | Running and troubleshooting the bridge |
| [`python_bridge/config.README.md`](python_bridge/config.README.md) | Every configuration key, and how to tune it |
| [`python_bridge/vision.README.md`](python_bridge/vision.README.md) | The webcam channel: setup, tuning, and how it folds into the mapper |
| [`firmware/README.md`](firmware/README.md) | Flashing, wiring, and firmware troubleshooting |
