"""
Parser for the ThinkGear protocol the MindWave headset speaks.

Packet layout:

    [0xAA] [0xAA] [length] [payload bytes] [checksum]

    checksum = (~(sum(payload) & 0xFF)) & 0xFF

Each payload row is a code followed by its value. Codes under 0x80 carry one
value byte; codes 0x80 and up have a length byte first.

No I/O here. Feed it bytes, get decoded rows back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List

# --- Framing constants -----------------------------------------------------

SYNC = 0xAA
EXCODE = 0x55
MAX_PAYLOAD_LENGTH = 169

# --- Payload row codes (single-byte value) ---------------------------------

CODE_BATTERY = 0x01
CODE_POOR_SIGNAL = 0x02
CODE_HEART_RATE = 0x03
CODE_ATTENTION = 0x04
CODE_MEDITATION = 0x05
CODE_8BIT_RAW = 0x06
CODE_RAW_MARKER = 0x07
CODE_BLINK_STRENGTH = 0x16

# --- Payload row codes (multi-byte value) ----------------------------------

CODE_RAW_WAVE = 0x80
CODE_EEG_POWER = 0x81
CODE_ASIC_EEG_POWER = 0x83
CODE_RRINTERVAL = 0x86

CODE_NAMES = {
    CODE_BATTERY: "battery",
    CODE_POOR_SIGNAL: "poor_signal",
    CODE_HEART_RATE: "heart_rate",
    CODE_ATTENTION: "attention",
    CODE_MEDITATION: "meditation",
    CODE_8BIT_RAW: "raw_8bit",
    CODE_RAW_MARKER: "raw_marker",
    CODE_BLINK_STRENGTH: "blink_strength",
    CODE_RAW_WAVE: "raw_wave",
    CODE_EEG_POWER: "eeg_power",
    CODE_ASIC_EEG_POWER: "asic_eeg_power",
    CODE_RRINTERVAL: "rr_interval",
}

# The eight ASIC band powers, in the order the headset transmits them.
ASIC_BANDS = (
    "delta",
    "theta",
    "low_alpha",
    "high_alpha",
    "low_beta",
    "high_beta",
    "low_gamma",
    "mid_gamma",
)


@dataclass
class Row:
    """A single decoded payload row."""

    code: int
    name: str
    value: object
    extended: int = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Row({self.name}={self.value!r})"


@dataclass
class ParserStats:
    """Counters used by the console dashboard and by QA (M7)."""

    packets_ok: int = 0
    packets_bad_checksum: int = 0
    bytes_discarded: int = 0
    rows_decoded: int = 0
    unknown_codes: dict = field(default_factory=dict)


class ThinkGearParser:
    """Parses the ThinkGear byte stream, resyncing after bad data.

    Usage::

        parser = ThinkGearParser()
        for row in parser.feed(chunk_of_bytes):
            ...

    Chunks can break anywhere; a half-received packet is held until the rest
    arrives.
    """

    # Internal states of the framing machine.
    _S_SYNC1 = 0
    _S_SYNC2 = 1
    _S_PLENGTH = 2
    _S_PAYLOAD = 3
    _S_CHECKSUM = 4

    def __init__(self, max_buffer: int = 4096) -> None:
        self.stats = ParserStats()
        self._max_buffer = max_buffer
        self._reset_frame()

    def _reset_frame(self) -> None:
        self._state = self._S_SYNC1
        self._plength = 0
        self._payload = bytearray()

    def reset(self) -> None:
        """Drop any partially received packet (used after a reconnect)."""
        self._reset_frame()

    # -- framing ------------------------------------------------------------

    def feed(self, data: bytes) -> List[Row]:
        """Consume ``data`` and return every complete row decoded from it."""
        rows: List[Row] = []
        for byte in data:
            rows.extend(self._feed_byte(byte))
        return rows

    def _feed_byte(self, byte: int) -> Iterator[Row]:
        if self._state == self._S_SYNC1:
            if byte == SYNC:
                self._state = self._S_SYNC2
            else:
                self.stats.bytes_discarded += 1
            return ()

        if self._state == self._S_SYNC2:
            if byte == SYNC:
                self._state = self._S_PLENGTH
            else:
                # Not a real sync pair; treat this byte as noise and restart.
                self.stats.bytes_discarded += 2
                self._state = self._S_SYNC1
            return ()

        if self._state == self._S_PLENGTH:
            if byte == SYNC:
                # A third 0xAA simply means we were still inside the sync run.
                return ()
            if byte > MAX_PAYLOAD_LENGTH:
                self.stats.bytes_discarded += 1
                self._reset_frame()
                return ()
            self._plength = byte
            self._payload.clear()
            self._state = self._S_PAYLOAD if byte else self._S_CHECKSUM
            return ()

        if self._state == self._S_PAYLOAD:
            self._payload.append(byte)
            if len(self._payload) >= self._plength:
                self._state = self._S_CHECKSUM
            return ()

        # self._S_CHECKSUM
        expected = (~(sum(self._payload) & 0xFF)) & 0xFF
        payload = bytes(self._payload)
        self._reset_frame()
        if byte != expected:
            self.stats.packets_bad_checksum += 1
            return ()
        self.stats.packets_ok += 1
        return self._decode_payload(payload)

    # -- payload ------------------------------------------------------------

    def _decode_payload(self, payload: bytes) -> List[Row]:
        rows: List[Row] = []
        i = 0
        n = len(payload)
        while i < n:
            extended = 0
            while i < n and payload[i] == EXCODE:
                extended += 1
                i += 1
            if i >= n:
                break
            code = payload[i]
            i += 1

            if code < 0x80:
                if i >= n:
                    break
                raw_value: object = payload[i]
                i += 1
            else:
                if i >= n:
                    break
                vlength = payload[i]
                i += 1
                if i + vlength > n:
                    break
                raw_value = payload[i : i + vlength]
                i += vlength

            name = CODE_NAMES.get(code)
            if name is None:
                self.stats.unknown_codes[code] = (
                    self.stats.unknown_codes.get(code, 0) + 1
                )
                name = f"unknown_0x{code:02x}"
            value = self._interpret(code, raw_value)
            self.stats.rows_decoded += 1
            rows.append(Row(code=code, name=name, value=value, extended=extended))
        return rows

    @staticmethod
    def _interpret(code: int, raw_value: object) -> object:
        if code == CODE_RAW_WAVE and isinstance(raw_value, (bytes, bytearray)):
            if len(raw_value) != 2:
                return None
            return int.from_bytes(raw_value, "big", signed=True)
        if code == CODE_ASIC_EEG_POWER and isinstance(raw_value, (bytes, bytearray)):
            if len(raw_value) != 24:
                return None
            return {
                band: int.from_bytes(raw_value[j * 3 : j * 3 + 3], "big", signed=False)
                for j, band in enumerate(ASIC_BANDS)
            }
        if isinstance(raw_value, (bytes, bytearray)):
            return bytes(raw_value)
        return raw_value


# --- Packet construction ---------------------------------------------------


def build_packet(rows: List[tuple]) -> bytes:
    """Build a valid ThinkGear packet from ``(code, value)`` pairs.

    ``value`` is an ``int`` for single-byte codes and a ``bytes`` object for
    multi-byte codes. This is what lets the mock and replay sources feed the
    real parser instead of bypassing it.
    """
    payload = bytearray()
    for code, value in rows:
        payload.append(code)
        if code < 0x80:
            payload.append(int(value) & 0xFF)
        else:
            data = bytes(value)
            payload.append(len(data))
            payload.extend(data)
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise ValueError(f"payload too long: {len(payload)} > {MAX_PAYLOAD_LENGTH}")
    checksum = (~(sum(payload) & 0xFF)) & 0xFF
    return bytes([SYNC, SYNC, len(payload)]) + bytes(payload) + bytes([checksum])


def build_esense_packet(
    poor_signal: int, attention: int, meditation: int
) -> bytes:
    """Build the once-per-second eSense summary packet."""
    return build_packet(
        [
            (CODE_POOR_SIGNAL, poor_signal),
            (CODE_ATTENTION, attention),
            (CODE_MEDITATION, meditation),
        ]
    )


def build_blink_packet(strength: int) -> bytes:
    """Build a blink-strength packet (headset sends these asynchronously)."""
    return build_packet([(CODE_POOR_SIGNAL, 0), (CODE_BLINK_STRENGTH, strength)])


def build_raw_packet(sample: int) -> bytes:
    """Build a single 512 Hz raw-wave packet."""
    clamped = max(-32768, min(32767, int(sample)))
    return build_packet([(CODE_RAW_WAVE, clamped.to_bytes(2, "big", signed=True))])
