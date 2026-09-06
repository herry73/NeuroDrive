# ESP32 firmware

Receives movement commands and drives the motors.

---

## Building and flashing

The sources live in `neurodrive_firmware/`, which is both a valid Arduino
sketch folder and the PlatformIO source directory. There is no duplicated
copy of the code.

The build needs `neurodrive_firmware/secrets.h`, which is a copy of
`neurodrive_firmware/secrets.h.example` in the same folder.

`secrets.h` holds the WiFi credentials and is git-ignored. The build fails
without it, on purpose, so nobody commits a password by accident. The
template ships with working credentials (`NeuroDrive` / `neurodrive2024`), so
the copy is enough to build and flash; change them only if you want a
different network name or password. In access-point mode the password must be
at least 8 characters, or the ESP32 silently refuses to start the network.

### Arduino IDE

1. *Boards Manager* → install **esp32 by Espressif Systems** (2.x or 3.x
   both work).
2. Open `neurodrive_firmware/neurodrive_firmware.ino`.
3. Board: **ESP32 Dev Module**. Select the port. Upload.
4. Serial monitor at **115200**.

### PlatformIO

From the `firmware/` directory:

```
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
| GPIO 32 | L298N ENA | Left PWM. **Remove the jumper** |
| GPIO 33 | L298N ENB | Right PWM. **Remove the jumper** |
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

Three rules, most expensive to break first:

1. **Motors are never powered from an ESP32 pin.** A stalled motor draws far
   more than a GPIO can supply, and the ESP32 will not survive it.
2. **The grounds must be common** or the L298N reads the ESP32's logic
   levels as noise, and the motors twitch at random.
3. **Remove the ENA/ENB jumpers.** With them fitted the enable pins sit
   high, PWM does nothing, and the motors only ever run at full speed.

The L298N's onboard 5 V regulator can power the ESP32 at 7.4 V input, but it
gets hot and sags under motor load, which is why the 5 V rail comes from a
separate buck converter.

### Emergency stop

GPIO 4 is the **signalling** half. The firmware latches STOP and refuses
movement commands while someone holds the button down.

That is not enough on its own. The other half is a switch in the battery
line that **physically interrupts the motor supply**. Firmware can hang; a
switch cannot, so both halves exist.

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
keeps the packet-to-motor time under 10 ms.

The order matters. Safety runs first, so a tripped watchdog or a pressed
button stops the vehicle before the firmware acts on a newly arrived command.

### The state machine

`STOP`, `FORWARD`, `TURN_LEFT`, `TURN_RIGHT`. A turn is a 300 ms pulse
(`TURN_PULSE_MS`) that returns to whatever the vehicle was doing before.

Three rules that are easy to get wrong:

* **Re-sending an in-progress turn does not restart its timer.** The bridge
  re-sends every 250 ms as a keepalive; without this rule a 300 ms turn
  would never end.
* **A turn that arrives after the previous pulse has expired starts a new
  one.** This is how a held hand keeps the vehicle turning: the bridge sends
  `L` for as long as the arm is up, and the firmware turns that into a train
  of 300 ms pulses. See `control.hold_turn_while_raised` in the bridge's
  `config.json`.
* **`FORWARD` received mid-turn does not cancel the turn.** It updates the
  state the turn returns to. `STOP`, by contrast, applies immediately.
  Stopping is always allowed to interrupt.

### LEDs

| LED | Meaning |
|---|---|
| Green | Driving forward |
| Yellow | Turning |
| Red, solid | Stopped on command |
| Red, slow blink | Stopped by the watchdog. No commands arriving |
| Red, fast blink | Emergency stop latched |
| Built-in, solid | Commands are arriving |
| Built-in, slow blink | Waiting for the first command |

The two red-blink patterns distinguish a watchdog stop from a latched
emergency stop without anyone reading a screen.

---

## Configuration (`config.h`)

| Setting | Default | Notes |
|---|---|---|
| `SPEED_FORWARD_PCT` | 50 | Forward duty cycle, as a percentage |
| `SPEED_TURN_PCT` | 55 | Turns need more torque to break static friction |
| `TRIM_LEFT_PCT` / `TRIM_RIGHT_PCT` | 100 / 100 | Straight-line trim |
| `MIN_MOVE_DUTY` | 60 | Below this, motors buzz instead of turning |
| `TURN_STYLE_PIVOT` | 1 | 1 = counter-rotate, 0 = inside wheel stopped |
| `TURN_PULSE_MS` | 300 | Length of one turn pulse |
| `WATCHDOG_TIMEOUT_MS` | 2000 | Stop if no command arrives |
| `ESTOP_DEBOUNCE_MS` | 30 | E-stop button debounce |
| `ESTOP_RELEASE_MS` | 500 | Button must stay released this long to re-arm |
| `SERIAL_BAUD` | 115200 | Must match `transport.serial.baudrate` in the bridge |
| `TELEMETRY_INTERVAL_MS` | 1000 | How often the `[state]` line prints |
| `WIFI_USE_AP` | 1 | 1 = the ESP32 makes its own network |
| `WIFI_AP_CHANNEL` | 6 | Access-point channel |
| `WIFI_CONNECT_TIMEOUT_S` | 20 | Station-mode join timeout |
| `UDP_COMMAND_PORT` | 4210 | Must match `transport.udp.esp32_port` |
| `SEND_ACK` | 1 | Reply `ACK:` so the bridge can measure round-trip time |

### Driving straight

Two "identical" motors never match, so the vehicle drifts to one side under
equal duty. `TRIM_LEFT_PCT` and `TRIM_RIGHT_PCT` scale each side
independently, and lowering the faster side's trim straightens the run:

```c
#define TRIM_LEFT_PCT 100
#define TRIM_RIGHT_PCT 92   // right motor was faster; slow it down
```

Both default to 100, meaning no correction. The values live in `config.h`,
so they take effect at the next flash.

### AP mode versus station mode

**AP mode (default).** The ESP32 creates its own network. The laptop joins
it, the vehicle is always `192.168.4.1`, and no existing network is involved,
so the address never changes and nothing else can interfere with it.

**Station mode** (`WIFI_USE_AP 0`). The ESP32 joins an existing network. It
prints its IP at boot; put that in `python_bridge/config.json` as
`transport.udp.esp32_ip`. Needed only if the laptop must stay on the internet
at the same time.

---

## Troubleshooting

### It will not compile

* `secrets.h: No such file`. Copy `secrets.h.example` first.
* `ledcSetup was not declared`. You are on core 3.x and the compatibility
  shim did not engage. Check `ESP_ARDUINO_VERSION_MAJOR` is defined; update
  the ESP32 core to at least 2.0.3.

### It will not upload

Hold **BOOT** while upload starts, release when "Connecting..." appears. If
that fails, lower `upload_speed` to `115200` in `platformio.ini` — there is a
commented-out line at the bottom of that file for exactly this.

### Motors do not turn

1. Are the ENA/ENB jumpers removed?
2. Is the motor supply connected to the L298N `+12V`, and are the grounds
   common?
3. Does the serial monitor show `[state] FORWARD`? If yes, the fault is
   electrical, not in software.
4. Raise `MIN_MOVE_DUTY`. The motors may be stalling at low duty.

### One motor runs backwards

That motor's two wires are reversed at the L298N screw terminals, and
swapping them there is the fix. Correcting it in software instead would leave
the wiring disagreeing with the pin map above.

### It moves, then stops after ~2 seconds

That is the watchdog working correctly. Commands stopped arriving. Either the
bridge is not running, or the packets are not reaching the vehicle. Check
the network and `esp32_ip`.

### Nothing arrives over WiFi

```
python ../python_bridge/udp_test_sender.py --ping --esp32-ip 192.168.4.1
```

If that fails: is the laptop on the ESP32's network? Is a firewall blocking
outbound UDP 4210? Setting `transport.mode` to `serial` sends the same
commands over the USB cable instead, which bypasses the network entirely.

### Debugging without the bridge

The serial monitor is a full control interface. Type a command and press
Enter:

```
F        drive forward
L        turn left
R        turn right
S        stop
P        keepalive ping; does not change the state
```

The firmware answers `ACK:F:FORWARD` and prints a `[state]` line every
second.
