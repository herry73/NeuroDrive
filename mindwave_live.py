#!/usr/bin/env python3
"""
Live brainwave viewer for the NeuroSky MindWave Mobile 2.

Reads the ThinkGear serial protocol directly (no ThinkGear Connector needed)
and plots the raw EEG trace plus the on-chip band powers.

Usage:
    pip install pyserial matplotlib numpy
    python mindwave_live.py --port COM5              # Windows
    python mindwave_live.py --port /dev/rfcomm0      # Linux
    python mindwave_live.py --port /dev/tty.MindWaveMobile-SerialPort   # macOS

Pass --csv out.csv to also log raw samples to disk.
"""

import argparse
import csv
import threading
import time
from collections import deque

import matplotlib.pyplot as plt
import numpy as np
import serial

BAUD = 57600
FS = 512                    # raw sample rate, Hz
WINDOW_SECONDS = 4
BANDS = ["delta", "theta", "low-a", "high-a", "low-b", "high-b", "low-g", "mid-g"]

# --- shared state between the reader thread and the plot ---
raw = deque([0] * (FS * WINDOW_SECONDS), maxlen=FS * WINDOW_SECONDS)
state = {"poor": 200, "attention": 0, "meditation": 0, "bands": [0] * 8, "blink": 0}
lock = threading.Lock()
stop = threading.Event()


def read_packet(ser):
    """Block until a valid ThinkGear packet arrives, return its payload bytes."""
    while not stop.is_set():
        # sync on 0xAA 0xAA
        if ser.read(1) != b"\xaa":
            continue
        if ser.read(1) != b"\xaa":
            continue

        plength = ser.read(1)
        if not plength:
            continue
        plength = plength[0]
        if plength > 169:       # 170 and 171 are illegal, >171 means we desynced
            continue

        payload = ser.read(plength)
        if len(payload) != plength:
            continue

        chk = ser.read(1)
        if not chk:
            continue
        if (~(sum(payload) & 0xFF)) & 0xFF != chk[0]:
            continue            # bad checksum, drop it and resync

        return payload
    return None


def parse_payload(payload, writer=None):
    """Walk the data rows inside one payload and update shared state."""
    i = 0
    n = len(payload)
    while i < n:
        # skip any extended-code bytes
        while i < n and payload[i] == 0x55:
            i += 1
        if i >= n:
            break

        code = payload[i]
        i += 1

        if code < 0x80:                     # single-byte value
            if i >= n:
                break
            value = payload[i]
            i += 1
            with lock:
                if code == 0x02:
                    state["poor"] = value
                elif code == 0x04:
                    state["attention"] = value
                elif code == 0x05:
                    state["meditation"] = value
                elif code == 0x16:
                    state["blink"] = value
        else:                               # multi-byte value
            if i >= n:
                break
            vlength = payload[i]
            i += 1
            data = payload[i:i + vlength]
            i += vlength

            if code == 0x80 and len(data) == 2:          # raw EEG sample
                sample = int.from_bytes(data, "big", signed=True)
                with lock:
                    raw.append(sample)
                if writer:
                    writer.writerow([time.time(), sample])

            elif code == 0x83 and len(data) == 24:       # eight band powers
                powers = [int.from_bytes(data[j:j + 3], "big") for j in range(0, 24, 3)]
                with lock:
                    state["bands"] = powers


def reader_thread(port, csv_path):
    handle = open(csv_path, "w", newline="") if csv_path else None
    writer = csv.writer(handle) if handle else None
    if writer:
        writer.writerow(["timestamp", "raw"])

    try:
        with serial.Serial(port, BAUD, timeout=2) as ser:
            print(f"connected on {port}")
            while not stop.is_set():
                payload = read_packet(ser)
                if payload is None:
                    break
                parse_payload(payload, writer)
    except serial.SerialException as e:
        print(f"serial error: {e}")
        stop.set()
    finally:
        if handle:
            handle.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="serial port the headset is on")
    ap.add_argument("--csv", help="optional path to log raw samples")
    args = ap.parse_args()

    threading.Thread(target=reader_thread, args=(args.port, args.csv), daemon=True).start()

    fig, (ax_raw, ax_band) = plt.subplots(2, 1, figsize=(10, 6))
    fig.canvas.manager.set_window_title("MindWave Mobile 2")

    t = np.linspace(-WINDOW_SECONDS, 0, FS * WINDOW_SECONDS)
    (line,) = ax_raw.plot(t, np.zeros_like(t), lw=0.7)
    ax_raw.set_ylim(-2048, 2047)
    ax_raw.set_xlim(-WINDOW_SECONDS, 0)
    ax_raw.set_ylabel("raw EEG")
    ax_raw.set_xlabel("seconds ago")

    bars = ax_band.bar(BANDS, [0] * 8)
    ax_band.set_ylabel("band power (log)")
    ax_band.set_yscale("log")
    ax_band.set_ylim(1, 1e7)

    try:
        while not stop.is_set() and plt.fignum_exists(fig.number):
            with lock:
                trace = np.array(raw, dtype=float)
                s = dict(state)

            line.set_ydata(trace)
            for bar, value in zip(bars, s["bands"]):
                bar.set_height(max(value, 1))

            quality = "GOOD" if s["poor"] == 0 else f"poor signal {s['poor']}"
            ax_raw.set_title(
                f"{quality}   |   attention {s['attention']}   meditation {s['meditation']}"
            )
            plt.pause(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
