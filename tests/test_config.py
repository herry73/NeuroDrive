"""
Unit tests for configuration loading and validation.

SP-07 / NFR 3.5: thresholds and addresses are changed in ``config.json``,
never in code. The validator exists so a typo surfaces as a clear message at
startup rather than as strange vehicle behaviour in front of an audience.
"""

import _bootstrap  # noqa: F401

import json
import os
import tempfile
import unittest

import config as config_module


class TestLoading(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        """The file in the repository must always start the application."""
        config = config_module.load()
        self.assertEqual(config.validate(), [])

    def test_shipped_config_matches_the_documented_defaults(self):
        config = config_module.load()
        self.assertEqual(config.get("transport.udp.esp32_port"), 4210)
        self.assertEqual(config.get("control.attention_forward_threshold"), 60)
        self.assertEqual(config.get("control.attention_stop_threshold"), 40)
        self.assertEqual(config.get("signal_processing.blink_strength_threshold"), 150)
        self.assertEqual(config.get("signal_processing.attention_window"), 5)
        self.assertEqual(config.get("signal_processing.poor_signal_cutoff"), 25)

    def test_missing_file_falls_back_to_defaults(self):
        config = config_module.load("/definitely/not/here/config.json")
        self.assertEqual(config.validate(), [])
        self.assertEqual(config.get("transport.udp.esp32_port"), 4210)

    def test_partial_file_is_merged_over_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"control": {"attention_forward_threshold": 72}}, handle)

            config = config_module.load(path)

            self.assertEqual(config.get("control.attention_forward_threshold"), 72)
            self.assertEqual(config.get("control.attention_stop_threshold"), 40)
            self.assertEqual(config.get("transport.udp.esp32_port"), 4210)

    def test_comment_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"_note": "hello", "loop": {"_why": "x", "rate_hz": 25}}, handle)

            config = config_module.load(path)

            self.assertIsNone(config.get("_note"))
            self.assertEqual(config.get("loop.rate_hz"), 25)


class TestAccess(unittest.TestCase):
    def test_dotted_get_with_a_default(self):
        config = config_module.load()
        self.assertEqual(config.get("nothing.here", "fallback"), "fallback")
        self.assertEqual(config.get("transport.mode"), "udp")

    def test_section_returns_a_dict(self):
        config = config_module.load()
        self.assertIn("esp32_ip", config.section("transport.udp"))
        self.assertEqual(config.section("does.not.exist"), {})

    def test_set_creates_intermediate_sections(self):
        config = config_module.load()
        config.set("brand.new.key", 5)
        self.assertEqual(config.get("brand.new.key"), 5)

    def test_set_through_a_scalar_is_an_error(self):
        config = config_module.load()
        with self.assertRaises(KeyError):
            config.set("transport.mode.nested", 1)


class TestCliOverrides(unittest.TestCase):
    def test_types_are_coerced(self):
        config = config_module.load(
            overrides=[
                "control.attention_forward_threshold=70",
                "transport.resend_interval_ms=125",
                "eeg.replay.speed=1.5",
                "control.require_good_signal=false",
                "eeg.source=mock",
            ]
        )

        self.assertEqual(config.get("control.attention_forward_threshold"), 70)
        self.assertEqual(config.get("transport.resend_interval_ms"), 125)
        self.assertAlmostEqual(config.get("eeg.replay.speed"), 1.5)
        self.assertIs(config.get("control.require_good_signal"), False)
        self.assertEqual(config.get("eeg.source"), "mock")

    def test_override_without_an_equals_sign_is_rejected(self):
        with self.assertRaises(ValueError):
            config_module.load(overrides=["nonsense"])


class TestValidation(unittest.TestCase):
    def check(self, key, value):
        config = config_module.load()
        config.set(key, value)
        return config.validate()

    def test_hysteresis_is_enforced(self):
        """The stop threshold must sit below the forward threshold."""
        problems = self.check("control.attention_stop_threshold", 60)
        self.assertTrue(any("hysteresis" in problem for problem in problems))

    def test_out_of_range_attention_threshold(self):
        self.assertTrue(self.check("control.attention_forward_threshold", 140))

    def test_blink_threshold_range(self):
        self.assertTrue(self.check("signal_processing.blink_strength_threshold", 300))
        self.assertTrue(self.check("signal_processing.blink_strength_threshold", 0))

    def test_quality_cutoff_range(self):
        self.assertTrue(self.check("signal_processing.poor_signal_cutoff", 500))

    def test_unknown_source_and_transport(self):
        self.assertTrue(self.check("eeg.source", "telepathy"))
        self.assertTrue(self.check("transport.mode", "carrier-pigeon"))

    def test_unknown_blink_mode_and_direction(self):
        self.assertTrue(self.check("control.blink_mode", "morse"))
        self.assertTrue(self.check("control.first_turn_direction", "SIDEWAYS"))

    def test_loop_rate_below_the_non_functional_requirement(self):
        """NFR 3.2 requires at least 10 Hz."""
        problems = self.check("loop.rate_hz", 5)
        self.assertTrue(any("rate_hz" in problem for problem in problems))
        self.assertEqual(self.check("loop.rate_hz", 10), [])

    def test_attention_window_must_be_positive(self):
        self.assertTrue(self.check("signal_processing.attention_window", 0))

    def test_problems_are_human_readable(self):
        problems = self.check("control.attention_stop_threshold", 99)
        self.assertTrue(problems)
        for problem in problems:
            self.assertIsInstance(problem, str)
            self.assertGreater(len(problem), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
