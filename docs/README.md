# Documentation

| Document | Owner | Status |
|---|---|---|
| [`INTERFACE_CONTRACT.md`](INTERFACE_CONTRACT.md) | M1 | **Frozen.** The protocol, state machine, module APIs and pin map |
| `architecture/` | M8 | Architecture diagrams (draw.io `.xml` plus exported `.png`) |
| `diagrams/` | M8 | Wiring diagram, communication sequence, signal flow |
| `report/` | M8 | Technical report drafts and the final PDF |

The three empty directories are placeholders for M8's Week 2-5 deliverables.
Diagrams belong in version control as both the editable `.xml` and the
exported `.png`, so the source stays editable and the image stays viewable in
the GitHub README.

## Where the technical content already lives

Much of what the report needs is already written down next to the code, and
should be cited rather than paraphrased:

| Report section | Source |
|---|---|
| Architecture and interfaces | `INTERFACE_CONTRACT.md` |
| Signal processing decisions | `python_bridge/config.README.md`, docstrings in `signal_processor.py` |
| Threshold rationale | `config.README.md` § threshold tuning guide |
| Firmware design | `firmware/README.md`, header comments in `motor_control.h` |
| Safety architecture | `README.md` § safety, `firmware/neurodrive_firmware/safety.cpp` |
| Test methodology and results | `tests/README.md`, output of `tests/latency_benchmark.py` |
| Latency figures | `latency_benchmark.py`. Report the three legs separately |

## A note on the latency claim

COM-03 asks for under 500 ms end to end. The software achieves that
comfortably, but the MindWave reports its eSense values only once per second,
so roughly 500 ms of the brain-to-wheels figure belongs to the headset and
not to anything the team wrote.

Report the legs separately. A single number that quietly excludes the headset
is the kind of claim an evaluator will probe. The honest version is better
anyway. It shows the team measured the whole system, not just the part that
flattered it.
