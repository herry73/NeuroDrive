"""
Integration tests: the whole chain, without any hardware.

    mock EEG -> SignalProcessor -> CommandMapper -> CommandSender -> UDP
             -> FakeESP32 (the firmware's state machine in Python)

These are the tests that check the *seams*, which is where the plan (section
12.1) expects most bugs to live. They also serve as the executable version
of docs/INTERFACE_CONTRACT.md: if the bridge and the firmware model ever
disagree about the protocol, these fail.
"""

import _bootstrap  # noqa: F401

import os
import shutil
import tempfile
import time
import unittest

import config as config_module
import main as main_module
from command_mapper import Command, create_mapper
from fake_esp32 import FakeESP32, MotorState, StopReason
from mock_eeg_generator import generate_samples, write_csv
from signal_processor import create_processor
from wifi_sender import CommandSender, UdpTransport


def wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestPipelineIntoVehicle(unittest.TestCase):
    """Processor + mapper + sender against the firmware model."""

    def setUp(self):
        self.vehicle = FakeESP32(port=0, host="127.0.0.1")
        self.port = self.vehicle.start()

        self.sender = CommandSender(
            transport=UdpTransport("127.0.0.1", self.port, listen_port=0),
            resend_interval_ms=100,
        )
        self.sender.start()

    def tearDown(self):
        self.sender.stop(final_command=Command.STOP)
        self.vehicle.stop()

    def drive(self, samples, mapper, processor, clock_start=0.0):
        """Push samples through the chain using their own timestamps."""
        for sample in samples:
            processor.ingest(sample)
            processed = processor.tick(sample.timestamp)
            command = mapper.update(processed, sample.timestamp)
            self.sender.send(command)
            time.sleep(0.002)  # let the sender thread actually transmit

    def test_concentration_drives_the_vehicle_forward(self):
        config = config_module.load()
        processor = create_processor(config)
        mapper = create_mapper(config)
        mapper.arm()

        samples = generate_samples(duration_s=20, scenario="smooth", seed=3)
        self.drive(samples, mapper, processor)

        self.assertTrue(
            wait_until(lambda: MotorState.FORWARD in self.vehicle.states_seen()),
            f"vehicle never drove; states seen: {self.vehicle.states_seen()}",
        )

    def test_blink_produces_a_turn_that_expires_by_itself(self):
        """MV-03 across the whole chain, including the firmware's timer."""
        config = config_module.load()
        processor = create_processor(config)
        mapper = create_mapper(config)
        mapper.arm()

        samples = generate_samples(duration_s=20, scenario="smooth", seed=3)
        self.drive(samples, mapper, processor)

        states = self.vehicle.states_seen()
        self.assertTrue(
            MotorState.TURN_LEFT in states or MotorState.TURN_RIGHT in states,
            f"no turn reached the vehicle; states seen: {states}",
        )
        # The turn must not stick: the vehicle has to leave the turn state.
        self.assertTrue(
            wait_until(
                lambda: self.vehicle.state
                not in (MotorState.TURN_LEFT, MotorState.TURN_RIGHT),
                timeout=2.0,
            )
        )

    def test_flat_attention_never_moves_the_vehicle(self):
        """A user who cannot concentrate must not get a runaway vehicle."""
        config = config_module.load()
        processor = create_processor(config)
        mapper = create_mapper(config)
        mapper.arm()

        samples = generate_samples(duration_s=15, scenario="flat", seed=5)
        self.drive(samples, mapper, processor)
        time.sleep(0.2)

        self.assertEqual(self.vehicle.state, MotorState.STOP)
        self.assertNotIn(MotorState.FORWARD, self.vehicle.states_seen())

    def test_poor_signal_stops_the_vehicle(self):
        """SF-03 end to end."""
        config = config_module.load()
        processor = create_processor(config)
        mapper = create_mapper(config)
        mapper.arm()

        samples = generate_samples(duration_s=40, scenario="noisy", seed=11)
        self.drive(samples, mapper, processor)
        time.sleep(0.2)

        self.assertGreater(processor.stats.poor_quality_samples, 0)
        self.assertGreater(mapper.state(time.monotonic()).safe_stops, 0)

    def test_watchdog_stops_the_vehicle_when_the_bridge_goes_quiet(self):
        """SF-02: the single most important safety behaviour."""
        self.sender.send(Command.FORWARD)
        self.assertTrue(
            wait_until(lambda: self.vehicle.state is MotorState.FORWARD),
            "vehicle did not start",
        )

        # Simulate the laptop dying: stop transmitting entirely.
        self.sender.stop()

        self.assertTrue(
            wait_until(lambda: self.vehicle.state is MotorState.STOP, timeout=4.0),
            "watchdog did not stop the vehicle",
        )
        self.assertIs(self.vehicle.model.stop_reason, StopReason.WATCHDOG)
        self.assertGreaterEqual(self.vehicle.stats.watchdog_trips, 1)

        # Re-create the sender so tearDown has something valid to close.
        self.sender = CommandSender(
            transport=UdpTransport("127.0.0.1", self.port, listen_port=0)
        )
        self.sender.start()

    def test_keepalive_prevents_a_spurious_watchdog_trip(self):
        """The vehicle must keep driving while the operator holds attention."""
        self.sender.send(Command.FORWARD)
        self.assertTrue(wait_until(lambda: self.vehicle.state is MotorState.FORWARD))

        time.sleep(3.0)  # longer than the 2 s watchdog

        self.assertIs(self.vehicle.state, MotorState.FORWARD)
        self.assertEqual(self.vehicle.stats.watchdog_trips, 0)
        self.assertGreater(self.sender.stats.keepalives, 10)

    def test_emergency_stop_refuses_movement_commands(self):
        """SF-01: while the button is held, FORWARD is ignored."""
        self.vehicle.model.press_estop(time.monotonic())

        self.sender.send(Command.FORWARD)
        time.sleep(0.4)
        self.assertIs(self.vehicle.state, MotorState.STOP)
        self.assertIs(self.vehicle.model.stop_reason, StopReason.ESTOP)

        # A STOP arriving while the button is held must not overwrite the
        # reason -- the indicator has to keep showing ESTOP, not COMMAND.
        self.sender.send(Command.STOP)
        time.sleep(0.2)
        self.assertIs(self.vehicle.model.stop_reason, StopReason.ESTOP)

        self.vehicle.model.release_estop()
        self.sender.send(Command.STOP)
        self.sender.send(Command.FORWARD)

        self.assertTrue(
            wait_until(lambda: self.vehicle.state is MotorState.FORWARD, timeout=2.0),
            "vehicle did not recover after the e-stop was released",
        )

    def test_acknowledgements_come_back(self):
        """COM-05 across a real socket."""
        self.sender.send(Command.FORWARD)
        self.assertTrue(wait_until(lambda: self.sender.stats.acks_received > 0))
        self.assertIsNotNone(self.sender.stats.last_rtt_ms)


class TestFullApplication(unittest.TestCase):
    """Runs main.py itself against the simulated vehicle."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="neurodrive-test-")
        self.vehicle = FakeESP32(port=0, host="127.0.0.1")
        self.port = self.vehicle.start()

    def tearDown(self):
        self.vehicle.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_bridge(self, *extra):
        argv = [
            "--source", "mock",
            "--transport", "udp",
            "--esp32-ip", "127.0.0.1",
            "--esp32-port", str(self.port),
            "--no-dashboard",
            "--no-keyboard",
            "--duration", "8",
            "--set", "transport.udp.listen_port=0",
            "--set", f"logging.dir={self.tmpdir}",
            "--set", "eeg.mock.attention_period_s=6",
            "--set", "eeg.mock.blink_interval_s=2.5",
            *extra,
        ]
        return main_module.main(argv)

    def test_end_to_end_run_drives_and_turns(self):
        exit_code = self.run_bridge("--skip-calibration")

        self.assertEqual(exit_code, 0)
        states = self.vehicle.states_seen()
        self.assertIn(MotorState.FORWARD, states)
        self.assertTrue(
            MotorState.TURN_LEFT in states or MotorState.TURN_RIGHT in states,
            f"expected a turn; states seen: {states}",
        )
        self.assertGreater(self.vehicle.stats.acks_sent, 0)
        self.assertEqual(self.vehicle.stats.rejected, 0)

    def test_the_run_leaves_the_vehicle_stopped(self):
        self.run_bridge("--skip-calibration")
        self.assertIs(self.vehicle.state, MotorState.STOP)

    def test_calibration_phase_holds_the_vehicle_still(self):
        """UI-02: nothing moves for the whole calibration window.

        The calibration window is set well beyond the run duration so the
        run ends while still calibrating -- otherwise the vehicle legitimately
        arms in the final milliseconds and the assertion becomes a race.
        """
        self.run_bridge("--set", "control.calibration_seconds=30")

        self.assertEqual(
            self.vehicle.states_seen(),
            [],
            "the vehicle moved during calibration",
        )

    def test_a_session_csv_is_written(self):
        """EEG-04."""
        self.run_bridge("--skip-calibration")

        sessions = [n for n in os.listdir(self.tmpdir) if n.startswith("session_")]
        logs = [n for n in os.listdir(self.tmpdir) if n.startswith("neurodrive_")]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(logs), 1)
        with open(os.path.join(self.tmpdir, sessions[0]), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertGreater(len(lines), 50)
        self.assertIn("attention", lines[0])

    def test_recorded_session_can_be_replayed(self):
        """Demo Fallback Level 2, verified rather than assumed."""
        csv_path = os.path.join(self.tmpdir, "replay.csv")
        write_csv(csv_path, generate_samples(duration_s=60, scenario="demo", seed=2))

        # Play at 4x so the scripted drive (which first crosses the forward
        # threshold at t=10 s) fits inside the 8 s test run.
        exit_code = self.run_bridge(
            "--skip-calibration",
            "--replay-file", csv_path,
            "--source", "replay",
            "--set", "eeg.replay.speed=4",
        )

        self.assertEqual(exit_code, 0)
        self.assertIn(MotorState.FORWARD, self.vehicle.states_seen())

    def test_invalid_configuration_is_rejected_before_anything_starts(self):
        exit_code = main_module.main(
            [
                "--no-dashboard",
                "--no-keyboard",
                "--set", "control.attention_stop_threshold=90",
            ]
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(self.vehicle.stats.packets, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
