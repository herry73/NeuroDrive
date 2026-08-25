# Test suite

Owner: **M7** (QA / Testing & Integration Lead).

Everything here runs on a bare laptop. No headset, no ESP32, no motors. That
is deliberate: the plan's critical path runs through hardware, so the test
suite must not.

---

## Running

```powershell
python -m pytest tests -q                # everything, ~1 minute
python -m pytest tests -q -k "not Full"  # fast subset (~15 s)
python -m pytest tests/test_signal_processor.py -v
python -m unittest discover -s tests     # if pytest is unavailable
```

Run from the repository root. `_bootstrap.py` puts `python_bridge/` on the
path, so no installation step is needed.

---

## What is here

| File | Purpose |
|---|---|
| `test_thinkgear.py` | Packet framing, checksums, split reads, corrupt data |
| `test_signal_processor.py` | Smoothing, blink threshold, debounce, double blink, quality gate |
| `test_command_mapper.py` | Thresholds, hysteresis, stop delay, turn pulses, safe stops |
| `test_wifi_sender.py` | Wire format, keepalive, coalescing, acks, real UDP sockets |
| `test_eeg_reader.py` | Reader lifecycle, reconnection policy, signal-loss detection |
| `test_config.py` | Loading, merging, CLI overrides, validation rules |
| `test_integration.py` | The whole chain, and `main.py` itself, against the vehicle simulator |
| `fake_esp32.py` | The firmware's state machine, in Python |
| `mock_eeg_generator.py` | Synthetic EEG sessions and ThinkGear byte streams |
| `latency_benchmark.py` | COM-03 latency measurement |

---

## The two simulators

These are what make hardware-free testing honest rather than decorative.

### `fake_esp32.py`

A UDP server implementing the same four-state machine, the same 300 ms turn
pulse, the same 2 s watchdog and the same `ACK:` replies as the real
firmware.

```powershell
python tests\fake_esp32.py --verbose
```

```
  [  1.204] FORWARD    reason=NONE     trigger=F
  [  4.881] TURN_LEFT  reason=NONE     trigger=L
  [  5.181] FORWARD    reason=NONE     trigger=turn-expired
  [ 12.402] STOP       reason=WATCHDOG trigger=watchdog
```

It is also the executable copy of `docs/INTERFACE_CONTRACT.md`. If the bridge
and the firmware ever disagree about the protocol, `test_integration.py`
fails. **When you change the protocol, change this file in the same pull
request.**

Its limits, stated plainly. It does not model radio loss, motor inertia, or
battery sag. It proves the *logic* is right, not that the vehicle works.

### `mock_eeg_generator.py`

Four scenarios:

| Scenario | What it exercises |
|---|---|
| `smooth` | Clean sweep across both thresholds, regular blinks |
| `noisy` | Poor-signal bursts (SF-03) and sub-threshold blinks (SP-04) |
| `flat` | Attention never reaches the threshold, so the vehicle must never move |
| `demo` | A scripted drive suitable for the fallback demo |

```powershell
# a replayable session for demo day
python tests\mock_eeg_generator.py --scenario demo --duration 90 `
    --out ..\python_bridge\logs\demo_session.csv

# a raw ThinkGear byte stream, for parser work
python tests\mock_eeg_generator.py --thinkgear-out stream.bin
```

It emits genuine ThinkGear packets, so mock mode exercises the real parser
rather than bypassing it.

---

## Latency benchmark (COM-03)

```powershell
python tests\latency_benchmark.py                          # simulator
python tests\latency_benchmark.py --target 192.168.4.1     # real vehicle
```

It reports two figures separately, and refuses to blur them:

* **Bridge processing.** Sample in to command decided. Well under 1 ms.
* **Transport and firmware.** Command sent to acknowledgement back, which
  includes the motor state machine acting on it.

It does **not** measure the headset's own latency, and says so. The MindWave
reports eSense values once per second, so roughly 500 ms of the end-to-end
figure belongs to the hardware and not to our code. Report that leg
separately; a single number that quietly excludes it will not survive a
question from an evaluator.

Against the simulator, the round-trip figure is dominated by the host OS
socket-timer granularity (~16 ms on Windows). Use real hardware for any
number that goes in the report.

---

## MVP acceptance checklist

Section 10.1 of the project plan, and where each item is verified.

| # | Criterion | Verified by |
|---|---|---|
| 1 | Headset connects and streams attention + blink | `test_eeg_reader`, manual with hardware |
| 2 | Bridge emits all four commands | `test_command_mapper`, `test_integration` |
| 3 | ESP32 receives over UDP (serial fallback) | `test_integration`, `test_wifi_sender` |
| 4 | Motors respond to all four commands | **Manual**, `udp_test_sender.py` |
| 5 | Forward on concentration, stop on relaxing | `test_integration`, then a real user |
| 6 | Blink produces a direction change | `test_command_mapper.TestTurns` |
| 7 | Hardware emergency stop works | **Manual**, logic in `test_integration` |
| 8 | Watchdog stops on communication loss | `test_integration.test_watchdog_...` |
| 9 | Stable for 5 minutes | `python main.py --duration 300` + `--target` benchmark |
| 10 | Thresholds tunable without code changes | `test_config` |

Items 4 and 7 cannot be automated. They are physical. Everything else is
covered, and rerunning the suite after any change is the regression test.

### The 5-minute stability run (item 9)

```powershell
# terminal 1
python tests\fake_esp32.py --quiet
# terminal 2
cd python_bridge
python main.py --source mock --esp32-ip 127.0.0.1 --skip-calibration --duration 300
```

Then check the printed summary: `dropped` and `errors` should be zero,
`reconnects` zero, and `acks` should track `sent`. Repeat with the real
vehicle before signing off.

---

## Writing new tests

* Put the requirement ID in the docstring (`SP-03`, `SF-02`). The traceability
  is what makes the test report credible.
* Pass time in explicitly rather than sleeping. `SignalProcessor` and
  `CommandMapper` both take `now` as a parameter precisely so timing
  behaviour can be tested instantly and deterministically.
* Use `wait_until(...)` from `test_integration.py` for anything genuinely
  concurrent. Never a bare `sleep` followed by an assertion.
* Test the failure path too. Half of these tests check that the vehicle
  *stops*, which is the behaviour that actually matters.
