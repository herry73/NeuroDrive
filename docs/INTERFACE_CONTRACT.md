# NeuroDrive Interface Contract

**Owner:** M1 (Project Manager & System Integrator)
**Status:** v1.0, frozen
**Change policy:** nothing in this document changes without M1's approval and
agreement from M2, M3 and M5. Every change must come with updates to the
affected tests in `tests/`, in the same pull request.

This is the single source of truth for how the parts of NeuroDrive talk to
each other. If code and this document disagree, that is a bug in one of them.

---

## 1. The command protocol

### 1.1 Wire format

One ASCII character, terminated by a newline. Nothing else.

```
"F\n"   Forward
"L\n"   Turn left
"R\n"   Turn right
"S\n"   Stop
"P\n"   Ping / keepalive (feeds the watchdog, changes nothing)
```

Why so simple: it is readable in a serial monitor, visible in a Wireshark
capture, and typeable by hand. A student team debugging at 2am can send a
command with `nc`. A binary protocol would buy nothing here.

The firmware also accepts the long forms `FORWARD`, `LEFT`, `RIGHT`, `STOP`
and `PING`, case-insensitive, so the protocol has room to grow (NFR 3.6).
The bridge always transmits the single-character form. The long forms exist
so a human can drive the vehicle from a serial monitor.

A single datagram may contain several newline-separated commands. The
firmware processes them in order.

### 1.2 Transport

| | Primary | Fallback |
|---|---|---|
| Medium | WiFi UDP | USB serial |
| Address | ESP32 IP, port **4210** | COM port on the laptop |
| Baud | n/a | 115200 |
| Config key | `transport.mode = "udp"` | `transport.mode = "serial"` |

UDP, not TCP (NFR 3.2): no handshake to re-establish, no retransmission
stalling behind a lost packet. The bridge re-sends instead.

The ESP32 defaults to running its **own access point** (`WIFI_USE_AP 1`),
so the vehicle is always at `192.168.4.1` and no venue network is involved.
In station mode, read the IP the ESP32 prints at boot and put it in
`python_bridge/config.json` as `transport.udp.esp32_ip`.

### 1.3 Acknowledgement (COM-05)

For every command it accepts, the firmware replies:

```
"ACK:<command char>:<resulting state>\n"

e.g.  ACK:F:FORWARD
      ACK:L:TURN_LEFT
      ACK:S:STOP
```

Over UDP the reply goes to the sender's source address and port. Over serial
it goes to the serial port. Unparsable input produces `ERR:unknown command`
on serial. Over UDP the firmware counts it and says nothing.

The firmware sends the acknowledgement **after** it updates the motor state
machine, so the bridge's round-trip measurement includes the motor reaction,
not just the network hop.

Acknowledgements are advisory. The bridge does not wait for them and never
retransmits because one is missing. It uses them only to measure latency and
to show a link indicator.

### 1.4 Re-send and keepalive (COM-04, SF-02)

The bridge transmits:

* immediately whenever the command changes,
* three times in a row for `L` and `R`, because a turn is a one-shot event
  and UDP has no retries,
* every **250 ms** (`transport.resend_interval_ms`) otherwise, re-sending the
  current command.

That periodic re-send is also the watchdog keepalive. Two consequences that
matter:

* A lost datagram self-heals within 250 ms.
* If the bridge process dies, packets stop, and the vehicle stops by itself
  within 2 seconds. There is no "keep driving" failure mode.

---

## 2. Vehicle state machine

Implemented in `firmware/neurodrive_firmware/motor_control.cpp`, and mirrored
exactly in `tests/fake_esp32.py` so the tests run without hardware.

```
                     F
        ┌──────────────────────────┐
        │                          ▼
   ┌─────────┐   S            ┌─────────┐
   │  STOP   │◄───────────────│ FORWARD │
   └─────────┘                └─────────┘
        │  ▲                    │     ▲
      L │  │ turn expires       │ L/R │ turn expires
      R │  │ (base = STOP)      │     │ (base = FORWARD)
        ▼  │                    ▼     │
   ┌──────────────┐        ┌──────────────┐
   │ TURN_LEFT /  │        │ TURN_LEFT /  │
   │ TURN_RIGHT   │        │ TURN_RIGHT   │
   └──────────────┘        └──────────────┘
```

**Rules, in priority order:**

1. **Emergency stop latched** → STOP. Every command except `S` is refused
   until the button is released for 500 ms.
2. **Watchdog expired** (no valid command for 2000 ms) → STOP, reason
   `WATCHDOG`. Cleared by the next valid command.
3. `S` → STOP immediately, on the same tick. Also sets the *base state* to
   STOP, so a turn in progress returns to STOP rather than to FORWARD.
4. `F` → sets the base state to FORWARD. If a turn is in progress, the turn
   continues and FORWARD takes effect when it expires (MV-03).
5. `L` / `R` → enters that turn state for **300 ms** (`TURN_PULSE_MS`), then
   returns to the base state.
6. `P` → feeds the watchdog only.

**The re-entry rule.** Receiving the turn command that is *already in
progress* does **not** restart its timer. Without this, the bridge's 250 ms
keepalive would extend a 300 ms turn indefinitely. The bridge cooperates by
only repeating a turn command for 150 ms
(`control.turn_command_repeat_ms`), which is deliberately shorter than the
firmware's 300 ms pulse.

**Boot state.** STOP, with the watchdog already tripped. The vehicle never
has a grace period during which it might move before the bridge has said
anything.

---

## 3. Python module APIs

The bridge is five modules with one job each (NFR 3.5). These signatures are
the contract between M2 (signal work) and M5 (application and networking).

### `eeg_sources.EEGSource`

The acquisition interface. Swapping the headset means writing one new class
(NFR 3.6). Nothing downstream changes.

```python
class EEGSource:
    name: str
    def open(self) -> None: ...          # raises EEGConnectionError
    def poll(self) -> list[EEGSample]: ...  # non-blocking-ish, <200 ms
    def close(self) -> None: ...
```

```python
@dataclass
class EEGSample:
    timestamp: float          # time.monotonic()
    attention: int | None     # 0-100, None until the first value arrives
    meditation: int | None    # 0-100
    poor_signal: int          # 0 (perfect) .. 200 (no contact)
    blink_strength: int | None  # set ONLY on the sample carrying a blink
    raw: list[int]            # raw-wave samples since the last poll
    connected: bool           # False = link down, drive nothing
```

> `blink_strength is None` means *no blink*, not *a blink of strength zero*.
> Three shipped sources: `SerialThinkGearSource`, `MockSource`, `ReplaySource`.

### `eeg_reader.EEGReader`

```python
reader = EEGReader(source_factory, signal_timeout_ms, reconnect_attempts, ...)
reader.start()
reader.wait_for_connection(timeout=30.0) -> bool     # EEG-03
reader.read_all() -> list[EEGSample]                 # drains, never blocks
reader.info -> ReaderInfo                            # status, counters
reader.stop()
```

Publishes a sample with `connected=False` on signal loss (EEG-05).

### `signal_processor.SignalProcessor`

Conditioning only. No driving policy lives here.

```python
processor.ingest(sample: EEGSample) -> None      # 0..n per cycle
processor.tick(now: float) -> ProcessedSignal    # exactly once per cycle
```

```python
@dataclass
class ProcessedSignal:
    timestamp: float
    connected: bool
    raw_attention: int | None
    attention: float | None        # rolling mean, N=5 (SP-01)
    meditation: int | None
    poor_signal: int
    quality_ok: bool               # poor_signal <= cutoff (SF-03)
    window_filled: bool
    blink_events: list[BlinkEvent] # SINGLE / DOUBLE, debounced (SP-04..06)
```

### `command_mapper.CommandMapper`

Driving policy. All thresholds live here, so retuning never touches
detection code.

```python
mapper.arm() / mapper.disarm()                    # UI-02 calibration gate
mapper.update(processed: ProcessedSignal, now: float) -> Command
mapper.state(now: float) -> MapperState           # command + reason, for the UI
```

`Command` is `FORWARD | LEFT | RIGHT | STOP`; `Command.wire` gives the
protocol character.

### `wifi_sender.CommandSender`

```python
sender.start()
sender.send(command: Command) -> None    # never blocks (NFR 3.3)
sender.stats -> SenderStats              # packets, acks, rtt
sender.stop(final_command=Command.STOP)  # always leaves the vehicle stopped
```

Repeats of the current command are dropped rather than queued, so a 20 Hz
control loop does not flood the link.

---

## 4. Data flow and timing

```
[MindWave]  eSense at 1 Hz, blinks as they occur, raw at 512 Hz
     │  Bluetooth SPP, 57600 baud
     ▼
[eeg_reader thread]  ThinkGear framing -> EEGSample -> thread-safe queue
     │
     ▼
[control loop, 20 Hz]  processor.ingest / tick -> mapper.update -> sender.send
     │
     ▼
[sender thread]  UDP datagram "F\n"   (change: immediately; idle: every 250 ms)
     │
     ▼
[ESP32 loop, ~1 kHz]  safety -> receive -> motor state machine -> LEDs
     │
     ▼
[L298N]  IN1-IN4 direction + ENA/ENB PWM at 1 kHz, 50 % duty
```

Three threads in the bridge, and the reason for each (NFR 3.3):

| Thread | Why it is separate |
|---|---|
| `eeg-reader` | A blocking serial read must never stall the control loop |
| main / control loop | Owns all policy state; single-threaded, so it is testable |
| `cmd-sender` | A 200 ms router glitch must never stall EEG processing |

### Latency budget (COM-03, < 500 ms end to end)

| Leg | Typical | Measurable by us? |
|---|---|---|
| Brain event → headset reports it | ~500 ms (1 Hz eSense) | **No.** Property of the headset |
| Reader → processor → mapper | < 1 ms | Yes (`latency_benchmark.py`) |
| Control-loop quantisation | ≤ 50 ms (20 Hz) | Yes |
| Bridge → ESP32 → motors | ~5-20 ms on a quiet network | Yes (ACK round trip / 2) |

Report the headset leg separately. Claiming a single sub-500 ms figure that
silently excludes it would be dishonest, and an evaluator will ask.

---

## 5. ESP32 pin assignment

From Appendix C of the project plan. **M4 and M3 must both sign off before
anyone solders a wire.** The authoritative copy is
`firmware/neurodrive_firmware/config.h`.

| ESP32 pin | Connected to | Purpose |
|---|---|---|
| GPIO 25 | L298N IN1 | Left motor direction A |
| GPIO 26 | L298N IN2 | Left motor direction B |
| GPIO 27 | L298N IN3 | Right motor direction A |
| GPIO 14 | L298N IN4 | Right motor direction B |
| GPIO 32 | L298N ENA | Left motor PWM |
| GPIO 33 | L298N ENB | Right motor PWM |
| GPIO 4 | E-stop button | `INPUT_PULLUP`, LOW = pressed |
| GPIO 2 | Built-in LED | Link heartbeat |
| GPIO 19 | Green LED | Forward |
| GPIO 18 | Yellow LED | Turning |
| GPIO 5 | Red LED | Stopped |

**Power (NFR 3.4).** The battery powers the motors through the L298N. An
ESP32 pin never does, not once, not for a quick test. The ESP32 itself takes
5 V from a buck converter, and the grounds are common.

**Emergency stop (SF-01).** GPIO 4 is the *signalling* half. The button must
**also** physically break the motor supply. Firmware can hang; a switch in
the battery line cannot.

---

## 6. Configuration keys

`python_bridge/config.json` is the only place to set thresholds and
addresses (SP-07). Full documentation in `python_bridge/config.README.md`.
The keys other modules depend on:

| Key | Default | Consumed by |
|---|---|---|
| `eeg.source` | `mock` | `eeg_sources.create_source` |
| `transport.mode` | `udp` | `wifi_sender.create_transport` |
| `transport.udp.esp32_ip` | `192.168.4.1` | UDP transport |
| `transport.udp.esp32_port` | `4210` | must equal `UDP_COMMAND_PORT` in `config.h` |
| `transport.resend_interval_ms` | `250` | must be well under `WATCHDOG_TIMEOUT_MS` |
| `control.turn_command_repeat_ms` | `150` | must be **less than** `TURN_PULSE_MS` |
| `control.attention_forward_threshold` | `60` | `command_mapper` |
| `control.attention_stop_threshold` | `40` | must be below the forward threshold |
| `signal_processing.blink_strength_threshold` | `150` | `signal_processor` |
| `loop.rate_hz` | `20` | must be ≥ 10 (NFR 3.2) |

`config.Config.validate()` checks the three "must" relationships at startup
and refuses to run rather than produce strange vehicle behaviour.

---

## 7. Verification

A test covers every claim above:

| Contract clause | Test |
|---|---|
| Wire format | `test_wifi_sender.TestWireFormat` |
| Long forms accepted | `fake_esp32.FakeESP32._normalise` + `test_integration` |
| Acknowledgement + RTT | `test_wifi_sender.TestAcknowledgements` |
| Keepalive / dropped packets | `test_wifi_sender.test_keepalive_resends...` |
| State machine, turn expiry | `test_command_mapper.TestTurns`, `test_integration` |
| Watchdog (SF-02) | `test_integration.test_watchdog_stops_the_vehicle...` |
| E-stop refuses movement | `test_integration.test_emergency_stop_refuses...` |
| Calibration gate (UI-02) | `test_integration.test_calibration_phase_holds...` |
| Config relationships | `test_config.TestValidation` |
| Latency budget | `tests/latency_benchmark.py` |

If you change the protocol, change `fake_esp32.py` too. It is the executable
copy of this document, and the integration tests will catch the two halves
drifting apart.
