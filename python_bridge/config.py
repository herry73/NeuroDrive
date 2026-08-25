"""
Configuration loading for the NeuroDrive bridge.

Requirement coverage: SP-07 / NFR 3.5. Every tunable parameter lives in a
single ``config.json``, changeable without touching code.

The loader merges the on-disk file over a full set of built-in defaults, so a
partial (or missing) ``config.json`` still yields a complete, valid
configuration. Values may also be overridden from the command line via
``--set control.attention_forward_threshold=65``.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

#: Built-in defaults. Keep in sync with ``config.README.md``.
DEFAULTS: Dict[str, Any] = {
    "eeg": {
        "source": "mock",
        "serial": {
            "port": "COM5",
            "baudrate": 57600,
            "read_timeout_s": 0.2,
            "reconnect_attempts": 3,
            "reconnect_delay_s": 2.0,
        },
        "replay": {
            "csv_path": "logs/recorded_session.csv",
            "loop": True,
            "speed": 1.0,
        },
        "mock": {
            "seed": 42,
            "blink_interval_s": 8.0,
            "attention_period_s": 20.0,
            "emit_raw": False,
        },
        "signal_timeout_ms": 2000,
    },
    "signal_processing": {
        "attention_window": 5,
        "blink_strength_threshold": 150,
        "blink_debounce_ms": 300,
        "double_blink_window_ms": 500,
        "poor_signal_cutoff": 25,
        "blink_from_raw": {
            "enabled": False,
            "amplitude_threshold": 300,
            "refractory_ms": 400,
        },
    },
    "vision": {
        "enabled": False,
        "camera_index": 0,
        "width": 640,
        "height": 480,
        "fps_limit": 15,
        "model_path": "models/pose_landmarker_lite.task",
        "raise_margin": 0.05,
        "min_visibility": 0.6,
        "hold_frames": 3,
        "refractory_ms": 1200,
        "repeat_while_held_ms": 0,
        "swap_sides": False,
        "preview": False,
    },
    "control": {
        "attention_forward_threshold": 60,
        "attention_stop_threshold": 40,
        "attention_stop_hold_ms": 1000,
        "turn_source": "blink",
        "blink_mode": "alternate",
        "first_turn_direction": "LEFT",
        "turn_command_repeat_ms": 150,
        "calibration_seconds": 15,
        "require_good_signal": True,
    },
    "transport": {
        "mode": "udp",
        "udp": {
            "esp32_ip": "192.168.4.1",
            "esp32_port": 4210,
            "listen_port": 4211,
            "expect_ack": True,
        },
        "serial": {
            "port": "COM6",
            "baudrate": 115200,
        },
        "resend_interval_ms": 250,
        "queue_size": 32,
    },
    "ui": {
        "console_dashboard": True,
        "refresh_hz": 10,
        "keyboard_override": True,
        "colour": True,
    },
    "logging": {
        "dir": "logs",
        "level": "INFO",
        "csv_data_log": True,
        "console_log": False,
    },
    "loop": {
        "rate_hz": 20,
    },
}

# Keys that exist purely as documentation inside config.json.
_COMMENT_PREFIX = "_"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith(_COMMENT_PREFIX):
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _coerce(text: str) -> Any:
    """Turn a CLI override string into the most plausible Python value."""
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


class Config:
    """Read-only view over the merged configuration tree."""

    def __init__(self, data: Dict[str, Any], path: str | None = None) -> None:
        self._data = data
        self.path = path

    # -- access -------------------------------------------------------------

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Fetch a value by dotted path, e.g. ``"transport.udp.esp32_port"``."""
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted_key: str) -> Dict[str, Any]:
        """Fetch a subtree as a dict (empty dict if absent)."""
        value = self.get(dotted_key, {})
        return value if isinstance(value, dict) else {}

    def set(self, dotted_key: str, value: Any) -> None:
        """Override a value in memory (used by CLI flags and by tests)."""
        parts = dotted_key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise KeyError(f"{dotted_key}: '{part}' is not a section")
        node[parts[-1]] = value

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config(path={self.path!r})"

    # -- validation ---------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration problems.

        Called at startup by ``main.py``; a non-empty result aborts the run
        rather than letting a typo surface as strange vehicle behaviour.
        """
        problems: list[str] = []

        source = self.get("eeg.source")
        if source not in ("serial", "mock", "replay"):
            problems.append(
                f"eeg.source must be one of serial/mock/replay (got {source!r})"
            )

        mode = self.get("transport.mode")
        if mode not in ("udp", "serial"):
            problems.append(f"transport.mode must be udp or serial (got {mode!r})")

        turn_source = self.get("control.turn_source")
        if turn_source not in ("blink", "vision", "both"):
            problems.append(
                f"control.turn_source must be blink/vision/both (got {turn_source!r})"
            )
        if turn_source in ("vision", "both") and not self.get("vision.enabled"):
            problems.append(
                "control.turn_source wants the camera, so vision.enabled must be true"
            )

        hold_frames = self.get("vision.hold_frames")
        if not isinstance(hold_frames, int) or hold_frames < 1:
            problems.append("vision.hold_frames must be an integer of at least 1")

        forward = self.get("control.attention_forward_threshold")
        stop = self.get("control.attention_stop_threshold")
        if not isinstance(forward, (int, float)) or not 0 <= forward <= 100:
            problems.append("control.attention_forward_threshold must be 0..100")
        if not isinstance(stop, (int, float)) or not 0 <= stop <= 100:
            problems.append("control.attention_stop_threshold must be 0..100")
        if isinstance(forward, (int, float)) and isinstance(stop, (int, float)):
            if stop >= forward:
                problems.append(
                    "control.attention_stop_threshold must be below "
                    "control.attention_forward_threshold (hysteresis; see "
                    "Appendix B of the project plan)"
                )

        window = self.get("signal_processing.attention_window")
        if not isinstance(window, int) or window < 1:
            problems.append("signal_processing.attention_window must be >= 1")

        blink = self.get("signal_processing.blink_strength_threshold")
        if not isinstance(blink, int) or not 1 <= blink <= 255:
            problems.append("signal_processing.blink_strength_threshold must be 1..255")

        quality = self.get("signal_processing.poor_signal_cutoff")
        if not isinstance(quality, int) or not 0 <= quality <= 200:
            problems.append("signal_processing.poor_signal_cutoff must be 0..200")

        blink_mode = self.get("control.blink_mode")
        if blink_mode not in ("alternate", "single_double"):
            problems.append(
                f"control.blink_mode must be alternate or single_double "
                f"(got {blink_mode!r})"
            )

        if self.get("control.first_turn_direction") not in ("LEFT", "RIGHT"):
            problems.append("control.first_turn_direction must be LEFT or RIGHT")

        rate = self.get("loop.rate_hz")
        if not isinstance(rate, (int, float)) or rate < 10:
            # NFR 3.2: the parsing loop must run at at least 10 Hz.
            problems.append("loop.rate_hz must be >= 10 (non-functional requirement)")

        return problems


def load(path: str | None = None, overrides: list[str] | None = None) -> Config:
    """Load ``config.json``, merged over :data:`DEFAULTS`.

    ``overrides`` is a list of ``"dotted.key=value"`` strings from the CLI.
    A missing file is not an error. The loader uses the defaults.
    """
    path = path or CONFIG_PATH
    data = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            data = _deep_merge(data, json.load(handle))
    config = Config(data, path=path)

    for override in overrides or []:
        if "=" not in override:
            raise ValueError(f"--set expects key=value, got {override!r}")
        key, _, raw = override.partition("=")
        config.set(key.strip(), _coerce(raw))

    return config
