"""
Tests for the console dashboard (UI-01, EEG-06).

The dashboard is presentation code, but it runs on every cycle of every run,
so a crash in it takes the vehicle's control loop down with it. These tests
cover exactly that: that it renders without raising, on any terminal, for
every state the bridge can be in -- including the states with missing data.
"""

import _bootstrap  # noqa: F401

import io
import unittest

from command_mapper import Command, CommandMapper, MapperState
from console_ui import (
    GLYPHS_ASCII,
    GLYPHS_UNICODE,
    Dashboard,
    print_banner,
    supports_unicode,
)
from eeg_reader import ReaderInfo, ReaderStatus
from signal_processor import BlinkEvent, ProcessedSignal
from wifi_sender import SenderStats

import config as config_module


def render_to_string(dashboard, processed, mapper_state, reader_info=None):
    """Render one frame into a string, whatever the terminal is."""
    lines = dashboard._build_lines(
        reader_info or ReaderInfo(status=ReaderStatus.CONNECTED, source_name="mock"),
        processed,
        mapper_state,
        SenderStats(),
        "udp 127.0.0.1:4210",
        elapsed_s=12.0,
        loop_hz=20.0,
        override_active=False,
        now=0.0,
    )
    return "\n".join(lines)


class TestGlyphFallback(unittest.TestCase):
    """Piping output on Windows drops stdout to cp1252, which cannot encode
    box-drawing characters. Without a fallback, redirecting the bridge to a
    file kills it mid-run."""

    def test_cp1252_stream_is_reported_as_ascii_only(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        self.assertFalse(supports_unicode(stream))

    def test_utf8_stream_supports_the_glyphs(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        self.assertTrue(supports_unicode(stream))

    def test_unknown_encoding_does_not_raise(self):
        class Weird:
            encoding = "not-a-real-codec"

        self.assertFalse(supports_unicode(Weird()))

    def test_ascii_glyphs_cover_every_unicode_glyph(self):
        self.assertEqual(set(GLYPHS_ASCII), set(GLYPHS_UNICODE))
        for glyph in GLYPHS_ASCII.values():
            glyph.encode("ascii")  # must not raise

    def test_ascii_rendering_encodes_in_cp1252(self):
        dashboard = Dashboard(colour=False)
        dashboard.glyphs = GLYPHS_ASCII

        text = render_to_string(
            dashboard,
            ProcessedSignal(
                timestamp=0.0,
                connected=True,
                raw_attention=70,
                attention=68.0,
                poor_signal=0,
                quality_ok=True,
            ),
            MapperState(command=Command.FORWARD, reason="attention 68 >= 60"),
        )

        text.encode("cp1252")  # the exact failure this guards against
        self.assertIn("#", text)


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.dashboard = Dashboard(colour=False)

    def render(self, processed, mapper_state=None, reader_info=None):
        return render_to_string(
            self.dashboard,
            processed,
            mapper_state or MapperState(),
            reader_info,
        )

    def test_shows_attention_and_command(self):
        text = self.render(
            ProcessedSignal(
                timestamp=0.0,
                connected=True,
                raw_attention=74,
                attention=71.4,
                meditation=38,
                poor_signal=0,
                quality_ok=True,
            ),
            MapperState(command=Command.FORWARD, reason="attention 71 >= 60"),
        )

        self.assertIn("FORWARD", text)
        self.assertIn("71.4", text)
        self.assertIn("74", text)
        self.assertIn("OK", text)

    def test_handles_missing_values_before_the_first_sample(self):
        """Everything is None at startup; this must not raise."""
        text = self.render(ProcessedSignal(timestamp=0.0))
        self.assertIn("--", text)
        self.assertIn("STOP", text)

    def test_shows_poor_quality(self):
        text = self.render(
            ProcessedSignal(
                timestamp=0.0,
                connected=True,
                raw_attention=70,
                attention=70.0,
                poor_signal=200,
                quality_ok=False,
            )
        )
        self.assertIn("POOR", text)
        self.assertIn("200", text)

    def test_renders_every_reader_status(self):
        for status in ReaderStatus:
            text = self.render(
                ProcessedSignal(timestamp=0.0),
                reader_info=ReaderInfo(status=status, source_name="serial"),
            )
            self.assertIn(status.value, text)

    def test_renders_every_command(self):
        for command in Command:
            text = self.render(
                ProcessedSignal(
                    timestamp=0.0, connected=True, attention=50.0, quality_ok=True
                ),
                MapperState(command=command, reason="test"),
            )
            self.assertIn(command.value, text)

    def test_attention_bar_tracks_the_value(self):
        def filled(value):
            text = self.render(
                ProcessedSignal(
                    timestamp=0.0,
                    connected=True,
                    attention=value,
                    quality_ok=True,
                )
            )
            return text.count(self.dashboard.glyphs["bar_full"])

        self.assertEqual(filled(0.0), 0)
        self.assertGreater(filled(50.0), 0)
        self.assertGreater(filled(100.0), filled(50.0))

    def test_out_of_range_attention_does_not_overflow_the_bar(self):
        for value in (-10.0, 150.0):
            self.render(
                ProcessedSignal(
                    timestamp=0.0, connected=True, attention=value, quality_ok=True
                )
            )

    def test_disabled_dashboard_renders_nothing(self):
        dashboard = Dashboard(enabled=False)
        dashboard.render(
            ReaderInfo(),
            ProcessedSignal(timestamp=0.0),
            MapperState(),
            SenderStats(),
            "udp",
            0.0,
            20.0,
            False,
        )  # must simply return


class TestBanner(unittest.TestCase):
    def test_banner_prints_the_key_settings(self):
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_banner(config_module.load(), "udp 192.168.4.1:4210", "logs/x.log")

        text = buffer.getvalue()
        self.assertIn("192.168.4.1", text)
        self.assertIn("60", text)   # forward threshold
        self.assertIn("150", text)  # blink threshold

    def test_banner_is_ascii_only(self):
        """It prints before the glyph fallback can help."""
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_banner(config_module.load(), "udp 192.168.4.1:4210", "logs/x.log")

        buffer.getvalue().encode("ascii")


class TestMapperStateIntegration(unittest.TestCase):
    """The dashboard reads MapperState; make sure the real thing fits."""

    def test_a_live_mapper_state_renders(self):
        mapper = CommandMapper()
        mapper.arm()
        processed = ProcessedSignal(
            timestamp=1.0,
            connected=True,
            raw_attention=80,
            attention=80.0,
            poor_signal=0,
            quality_ok=True,
            blink_events=[BlinkEvent.SINGLE],
        )
        mapper.update(processed, 1.0)

        text = render_to_string(Dashboard(colour=False), processed, mapper.state(1.0))
        self.assertIn("LEFT", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
