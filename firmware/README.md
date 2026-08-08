# ESP32 firmware

Receives movement commands and drives the motors. Owners: **M3** (firmware),
**M4** (wiring and power).

---

## Building and flashing

The sources live in `neurodrive_firmware/`, which is both a valid Arduino
sketch folder and the PlatformIO source directory. There is no duplicated
copy of the code.

**First, always:**

```powershell
cd neurodrive_firmware
Copy-Item secrets.h.example secrets.h     # bash: cp secrets.h.example secrets.h
```

`secrets.h` holds the WiFi credentials and is git-ignored. The build fails
without it — deliberately, so nobody accidentally commits a password.

### Arduino IDE

1. *Boards Manager* → install **esp32 by Espressif Systems** (2.x or 3.x
   both work).
2. Open `neurodrive_firmware/neurodrive_firmware.ino`.
3. Board: **ESP32 Dev Module**. Select the port. Upload.
4. Serial monitor at **115200**.

### PlatformIO

```powershell
cd firmware
pio run                # build
pio run -t upload      # flash
pio device monitor     # serial monitor
```

---

## Wiring

| ESP32 | Connects to | Notes |
|---|---|---|
| GPIO 25 | L298N IN1 | Left direction A |
| GPIO 26 | L298N IN2 | Left direction B |
| GPIO 27 | L298N IN3 | Right direction A |
| GPIO 14 | L298N IN4 | Right direction B |
| GPIO 32 | L298N ENA | Left PWM — **remove the jumper** |
| GPIO 33 | L298N ENB | Right PWM — **remove the jumper** |
| GPIO 4 | E-stop button → GND | `INPUT_PULLUP`; no resistor needed |
| GPIO 2 | Built-in LED | Link heartbeat |
| GPIO 19 / 18 / 5 | Green / yellow / red LED | Through 220 Ω to GND |
| GND | L298N GND, battery −, buck converter − | **All grounds must be common** |

### Power

```
  Battery 7.4 V ──┬── L298N +12V ─── motors
                  │
                  └── Buck converter ── 5 V ── ESP32 VIN
```

Three rules, in order of how expensive it is to break them:

1. **Motors are never powered from an ESP32 pin.** A stalled motor draws far
   more than a GPIO can supply, and the ESP32 will not survive it (NFR 3.4).
2. **The grounds must be common** or the L298N reads the ESP32's logic
   levels as noise, and the motors twitch at random.
3. **Remove the ENA/ENB jumpers.** With them fitted the enable pins are tied
   high, PWM does nothing, and the motors only ever run at full speed.

The L298N's onboard 5 V regulator can power the ESP32 at 7.4 V input, but it
gets hot and sags under motor load. Use a separate buck converter.

### Emergency stop (SF-01)

GPIO 4 is the **signalling** half: the firmware latches STOP and refuses
movement commands while the button is held.

That is not sufficient on its own. The plan requires the emergency stop to
**physically interrupt the motor supply** as well — a switch in the battery
line. Firmware can hang; a switch cannot. Wire both.

---

## What the firmware does

```
loop():
    safetyTick()   e-stop and watchdog. First, unconditionally.
    commTick()     poll UDP and serial, dispatch commands, send acks
    motorTick()    expire turn pulses, write the PWM outputs
    ledTick()      indicators
```

Nothing blocks. There is no `delay()` in the command path, which is what
keeps the packet-to-motor time under 10 ms (NFR 3.2).

The order matters: safety runs first, so a tripped watchdog or a pressed
button stops the vehicle before any newly arrived command can be acted on.

### The state machine

`STOP`, `FORWARD`, `TURN_LEFT`, `TURN_RIGHT`. A turn is a 300 ms pulse that
returns to whatever the vehicle was doing before (MV-03).

Two rules that are easy to get wrong and are covered by the tests:

* **Re-sending an in-progress turn does not restart its timer.** The bridge
  re-sends every 250 ms as a keepalive; without this rule a 300 ms turn
  would never end.
* **`FORWARD` received mid-turn does not cancel the turn.** It updates the
  state the turn returns to. `STOP`, by contrast, applies immediately —
  stopping is always allowed to interrupt.

Full specification: [`../docs/INTERFACE_CONTRACT.md`](../docs/INTERFACE_CONTRACT.md).

### LEDs

| LED | Meaning |
|---|---|
| Green | Driving forward |
| Yellow | Turning |
| Red, solid | Stopped on command |
| Red, slow blink | Stopped by the watchdog — no commands arriving |
| Red, fast blink | Emergency stop latched |
| Built-in, solid | Commands are arriving |
| Built-in, slow blink | Waiting for the first command |

The red-blink patterns are worth pointing out during the demo: they make the
watchdog visible without anyone reading a screen.

---

## Configuration (`config.h`)

| Setting | Default | Notes |
|---|---|---|
| `SPEED_FORWARD_PCT` | 50 | Demo speed (MV-05). Raise carefully |
| `SPEED_TURN_PCT` | 55 | Turns need more torque to break static friction |
| `TRIM_LEFT_PCT` / `TRIM_RIGHT_PCT` | 100 | M6's straight-line trim |
| `MIN_MOVE_DUTY` | 60 | Below this, motors buzz instead of turning |
| `TURN_STYLE_PIVOT` | 1 | 1 = counter-rotate, 0 = inside wheel stopped |
| `TURN_PULSE_MS` | 300 | Turn duration (MV-03) |
| `WATCHDOG_TIMEOUT_MS` | 2000 | Stop if no command arrives (SF-02) |
| `WIFI_MODE_AP` | 1 | 1 = the ESP32 makes its own network |
| `UDP_COMMAND_PORT` | 4210 | Must match `transport.udp.esp32_port` |

### Driving straight

Two "identical" motors never match. On a straight-line run, note which way
the vehicle drifts and reduce that side's trim:

```c
#define TRIM_LEFT_PCT 100
#define TRIM_RIGHT_PCT 92   // right motor was faster; slow it down
```

Adjust in steps of 5, re-flash, re-test. Record the values you settle on —
that measurement is M6's Week 2 deliverable.

### AP mode versus station mode

**AP mode (default).** The ESP32 creates its own network. The laptop joins
it, the vehicle is always `192.168.4.1`, and no venue WiFi is involved. Use
this for the demo — it removes an entire category of failure.

**Station mode** (`WIFI_MODE_AP 0`). The ESP32 joins an existing network. It
prints its IP at boot; put that in `python_bridge/config.json`. Needed only
if the laptop must stay on the internet at the same time.

---

## Troubleshooting

### It will not compile

* `secrets.h: No such file` — copy `secrets.h.example` first.
* `ledcSetup was not declared` — you are on core 3.x and the compatibility
  shim did not engage. Check `ESP_ARDUINO_VERSION_MAJOR` is defined; update
  the ESP32 core to at least 2.0.3.

### It will not upload

Hold **BOOT** while upload starts, release when "Connecting..." appears. If
that fails, lower `upload_speed` to `115200` in `platformio.ini`.

### Motors do not turn

1. Are the ENA/ENB jumpers removed?
2. Is the motor supply connected to the L298N `+12V`, and are the grounds
   common?
3. Does the serial monitor show `[state] FORWARD`? If yes, the fault is
   electrical, not in software.
4. Raise `MIN_MOVE_DUTY` — the motors may be stalling at low duty.

### One motor runs backwards

Swap that motor's two wires at the L298N screw terminals. Do not fix it in
software; the wiring should match the pin map.

### It moves, then stops after ~2 seconds

That is the watchdog working correctly. Commands stopped arriving. Either the
bridge is not running, or the packets are not reaching the vehicle — check
the network and `esp32_ip`.

### Nothing arrives over WiFi

```powershell
python ..\python_bridge\udp_test_sender.py --ping --esp32-ip 192.168.4.1
```

If that fails: is the laptop on the ESP32's network? Is a firewall blocking
outbound UDP 4210? As a fallback, use the USB cable — `transport.mode` to
`serial` — and carry on working.

### Debugging without the bridge

The serial monitor is a full control interface. Type a command and press
Enter:

```
F        drive forward
L        turn left
S        stop
```

The firmware answers `ACK:F:FORWARD` and prints a `[state]` line every
second.
