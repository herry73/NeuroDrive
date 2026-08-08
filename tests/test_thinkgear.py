"""Unit tests for the ThinkGear protocol parser (EEG-02)."""

import _bootstrap  # noqa: F401

import unittest

from mock_eeg_generator import generate_thinkgear_stream
from thinkgear import (
    CODE_ATTENTION,
    CODE_BLINK_STRENGTH,
    CODE_POOR_SIGNAL,
    CODE_RAW_WAVE,
    ThinkGearParser,
    build_blink_packet,
    build_esense_packet,
    build_packet,
    build_raw_packet,
)


def rows_by_name(rows):
    return {row.name: row.value for row in rows}


class TestPacketFraming(unittest.TestCase):
    def test_decodes_a_well_formed_esense_packet(self):
        parser = ThinkGearParser()
        rows = parser.feed(build_esense_packet(poor_signal=0, attention=57, meditation=42))

        values = rows_by_name(rows)
        self.assertEqual(values["poor_signal"], 0)
        self.assertEqual(values["attention"], 57)
        self.assertEqual(values["meditation"], 42)
        self.assertEqual(parser.stats.packets_ok, 1)
        self.assertEqual(parser.stats.packets_bad_checksum, 0)

    def test_packet_split_across_reads_is_reassembled(self):
        """Serial reads land on arbitrary boundaries, including mid-packet."""
        packet = build_esense_packet(poor_signal=0, attention=88, meditation=10)
        for split in range(1, len(packet)):
            parser = ThinkGearParser()
            rows = parser.feed(packet[:split])
            rows += parser.feed(packet[split:])
            self.assertEqual(
                rows_by_name(rows).get("attention"), 88, f"split at byte {split}"
            )

    def test_one_byte_at_a_time(self):
        packet = build_esense_packet(poor_signal=3, attention=61, meditation=20)
        parser = ThinkGearParser()
        rows = []
        for byte in packet:
            rows.extend(parser.feed(bytes([byte])))
        self.assertEqual(rows_by_name(rows)["attention"], 61)

    def test_bad_checksum_is_rejected_without_emitting_rows(self):
        packet = bytearray(build_esense_packet(0, 50, 50))
        packet[-1] ^= 0xFF  # corrupt the checksum

        parser = ThinkGearParser()
        rows = parser.feed(bytes(packet))

        self.assertEqual(rows, [])
        self.assertEqual(parser.stats.packets_bad_checksum, 1)
        self.assertEqual(parser.stats.packets_ok, 0)

    def test_recovers_after_a_corrupt_packet(self):
        corrupt = bytearray(build_esense_packet(0, 50, 50))
        corrupt[-1] ^= 0xFF
        good = build_esense_packet(0, 77, 33)

        parser = ThinkGearParser()
        rows = parser.feed(bytes(corrupt) + good)

        self.assertEqual(rows_by_name(rows)["attention"], 77)
        self.assertEqual(parser.stats.packets_ok, 1)

    def test_leading_noise_is_discarded(self):
        noise = bytes([0x12, 0x34, 0xAA, 0x56, 0x00, 0xFF])
        parser = ThinkGearParser()
        rows = parser.feed(noise + build_esense_packet(0, 65, 40))
        self.assertEqual(rows_by_name(rows)["attention"], 65)

    def test_extra_sync_bytes_before_the_length(self):
        """A run of 0xAA is legal; only the last pair starts the packet."""
        packet = build_esense_packet(0, 44, 44)
        padded = b"\xaa\xaa\xaa" + packet[2:]
        parser = ThinkGearParser()
        self.assertEqual(rows_by_name(parser.feed(padded))["attention"], 44)

    def test_oversized_length_byte_is_rejected(self):
        parser = ThinkGearParser()
        rows = parser.feed(b"\xaa\xaa\xff" + b"\x00" * 8)
        self.assertEqual(rows, [])
        self.assertEqual(parser.stats.packets_ok, 0)


class TestPayloadDecoding(unittest.TestCase):
    def test_blink_strength_row(self):
        parser = ThinkGearParser()
        values = rows_by_name(parser.feed(build_blink_packet(178)))
        self.assertEqual(values["blink_strength"], 178)

    def test_raw_wave_is_signed_big_endian(self):
        parser = ThinkGearParser()
        for sample in (0, 1, -1, 32767, -32768, 1234, -1234):
            rows = parser.feed(build_raw_packet(sample))
            self.assertEqual(rows_by_name(rows)["raw_wave"], sample)

    def test_asic_eeg_power_expands_to_eight_bands(self):
        payload = bytes(range(1, 25))  # 24 bytes = 8 x 3-byte values
        parser = ThinkGearParser()
        rows = parser.feed(build_packet([(0x83, payload)]))

        bands = rows_by_name(rows)["asic_eeg_power"]
        self.assertEqual(len(bands), 8)
        self.assertEqual(bands["delta"], 0x010203)
        self.assertEqual(bands["mid_gamma"], 0x161718)

    def test_excode_prefix_is_skipped(self):
        """0x55 EXCODE bytes precede the real code and carry no value.

        Built by hand because an EXCODE is a bare byte with no value, which
        ``build_packet`` (which pairs every code with a value) cannot express.
        """
        payload = bytes([0x55, 0x55, CODE_ATTENTION, 73])
        checksum = (~(sum(payload) & 0xFF)) & 0xFF
        packet = bytes([0xAA, 0xAA, len(payload)]) + payload + bytes([checksum])

        parser = ThinkGearParser()
        rows = parser.feed(packet)

        self.assertEqual(rows_by_name(rows).get("attention"), 73)
        self.assertEqual(rows[0].extended, 2)

    def test_unknown_code_is_counted_not_dropped_silently(self):
        parser = ThinkGearParser()
        rows = parser.feed(build_packet([(0x42, 9), (CODE_ATTENTION, 50)]))

        self.assertEqual(rows_by_name(rows)["attention"], 50)
        self.assertEqual(parser.stats.unknown_codes.get(0x42), 1)

    def test_truncated_multibyte_row_does_not_raise(self):
        """A payload that claims more bytes than it carries must not crash."""
        payload = bytes([CODE_RAW_WAVE, 8, 0x01, 0x02])  # says 8, provides 2
        checksum = (~(sum(payload) & 0xFF)) & 0xFF
        packet = bytes([0xAA, 0xAA, len(payload)]) + payload + bytes([checksum])

        parser = ThinkGearParser()
        self.assertEqual(parser.feed(packet), [])


class TestRealisticStream(unittest.TestCase):
    def test_generated_session_parses_cleanly(self):
        stream = generate_thinkgear_stream(duration_s=30.0, scenario="smooth")
        parser = ThinkGearParser()

        rows = []
        # Feed in awkward 7-byte chunks to mimic a real serial port.
        for offset in range(0, len(stream), 7):
            rows.extend(parser.feed(stream[offset : offset + 7]))

        self.assertEqual(parser.stats.packets_bad_checksum, 0)
        self.assertEqual(parser.stats.bytes_discarded, 0)

        attentions = [row.value for row in rows if row.code == CODE_ATTENTION]
        blinks = [row.value for row in rows if row.code == CODE_BLINK_STRENGTH]
        signals = [row.value for row in rows if row.code == CODE_POOR_SIGNAL]

        self.assertEqual(len(attentions), 30)
        self.assertGreaterEqual(len(blinks), 3)
        self.assertTrue(all(0 <= value <= 100 for value in attentions))
        self.assertTrue(all(0 <= value <= 200 for value in signals))

    def test_parser_reset_drops_a_partial_packet(self):
        parser = ThinkGearParser()
        parser.feed(build_esense_packet(0, 50, 50)[:4])  # mid-packet
        parser.reset()

        rows = parser.feed(build_esense_packet(0, 91, 12))
        self.assertEqual(rows_by_name(rows)["attention"], 91)


if __name__ == "__main__":
    unittest.main(verbosity=2)
