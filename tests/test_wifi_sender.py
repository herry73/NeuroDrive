"""
Unit tests for the command transport.

Covers COM-02 (UDP delivery), COM-04 (keepalive re-send so a dropped packet
self-heals), COM-05 (acknowledgement handling and round-trip measurement),
and the requirement that ``send()`` never blocks the control loop (NFR 3.3).
"""

import _bootstrap  # noqa: F401

import socket
import threading
import time
import unittest

import config as config_module
from command_mapper import Command
from wifi_sender import (
    COMMANDS,
    CommandSender,
    SerialTransport,
    Transport,
    TransportError,
    UdpTransport,
    create_transport,
    send_once,
)


class RecordingTransport(Transport):
    """In-memory transport that records payloads and can be made to fail."""

    name = "recording"
    description = "recording"

    def __init__(self):
        self.payloads = []
        self.opened = False
        self.closed = False
        self.fail_on_send = False
        self.inbox = bytearray()
        self._lock = threading.Lock()

    def open(self):
        self.opened = True

    def send(self, payload):
        if self.fail_on_send:
            raise TransportError("simulated link failure")
        with self._lock:
            self.payloads.append(payload)

    def receive(self):
        with self._lock:
            data, self.inbox = bytes(self.inbox), bytearray()
        return data

    def close(self):
        self.closed = True

    # -- helpers for the tests ---------------------------------------------

    def deliver(self, data: bytes):
        with self._lock:
            self.inbox.extend(data)

    def chars(self):
        with self._lock:
            return [payload.decode().strip() for payload in self.payloads]

    def wait_for(self, count, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.payloads) >= count:
                    return True
            time.sleep(0.005)
        return False


class TestWireFormat(unittest.TestCase):
    def test_payloads_match_appendix_a(self):
        self.assertEqual(COMMANDS["FORWARD"], "F\n")
        self.assertEqual(COMMANDS["LEFT"], "L\n")
        self.assertEqual(COMMANDS["RIGHT"], "R\n")
        self.assertEqual(COMMANDS["STOP"], "S\n")

    def test_sender_transmits_the_documented_bytes(self):
        transport = RecordingTransport()
        sender = CommandSender(transport, resend_interval_ms=10_000)
        sender.start()
        try:
            sender.send(Command.FORWARD)
            self.assertTrue(transport.wait_for(1))
            self.assertEqual(transport.payloads[0], b"F\n")
        finally:
            sender.stop()


class TestSendBehaviour(unittest.TestCase):
    def setUp(self):
        self.transport = RecordingTransport()

    def test_repeated_identical_requests_are_not_queued(self):
        """A 20 Hz loop must not flood the link with duplicates."""
        sender = CommandSender(self.transport, resend_interval_ms=10_000)
        sender.start()
        try:
            for _ in range(50):
                sender.send(Command.FORWARD)
            self.assertTrue(self.transport.wait_for(1))
            time.sleep(0.1)
            self.assertEqual(self.transport.chars(), ["F"])
            self.assertEqual(sender.stats.commands_changed, 1)
        finally:
            sender.stop()

    def test_changes_are_transmitted(self):
        sender = CommandSender(self.transport, resend_interval_ms=10_000)
        sender.start()
        try:
            for command in (Command.FORWARD, Command.STOP, Command.FORWARD):
                sender.send(command)
                time.sleep(0.05)
            self.assertEqual(self.transport.chars()[:3], ["F", "S", "F"])
        finally:
            sender.stop()

    def test_turn_commands_are_burst_for_packet_loss(self):
        """UDP has no retries, so a one-shot turn gets sent more than once."""
        sender = CommandSender(self.transport, resend_interval_ms=10_000, turn_burst=3)
        sender.start()
        try:
            sender.send(Command.LEFT)
            self.assertTrue(self.transport.wait_for(3))
            self.assertEqual(self.transport.chars()[:3], ["L", "L", "L"])
        finally:
            sender.stop()

    def test_keepalive_resends_the_current_command(self):
        """COM-04 / SF-02: the vehicle keeps hearing from us."""
        sender = CommandSender(self.transport, resend_interval_ms=40)
        sender.start()
        try:
            sender.send(Command.FORWARD)
            time.sleep(0.3)
            self.assertGreaterEqual(sender.stats.keepalives, 3)
            self.assertTrue(all(char == "F" for char in self.transport.chars()))
        finally:
            sender.stop()

    def test_nothing_is_sent_before_the_first_command(self):
        sender = CommandSender(self.transport, resend_interval_ms=20)
        sender.start()
        try:
            time.sleep(0.15)
            self.assertEqual(self.transport.payloads, [])
        finally:
            sender.stop()

    def test_send_does_not_block_when_the_link_fails(self):
        sender = CommandSender(self.transport, resend_interval_ms=20)
        sender.start()
        try:
            self.transport.fail_on_send = True
            started = time.monotonic()
            for command in (Command.FORWARD, Command.STOP, Command.LEFT):
                sender.send(command)
            self.assertLess(time.monotonic() - started, 0.05)
            time.sleep(0.15)
            self.assertGreater(sender.stats.errors, 0)
            self.assertIn("simulated link failure", sender.stats.last_error)
        finally:
            sender.stop()

    def test_stop_transmits_a_final_command(self):
        """Quitting the bridge must not leave the vehicle driving."""
        sender = CommandSender(self.transport, resend_interval_ms=10_000)
        sender.start()
        sender.send(Command.FORWARD)
        self.assertTrue(self.transport.wait_for(1))

        sender.stop(final_command=Command.STOP)

        self.assertEqual(self.transport.chars()[-1], "S")
        self.assertTrue(self.transport.closed)


class TestAcknowledgements(unittest.TestCase):
    """COM-05."""

    def test_ack_is_counted_and_timed(self):
        transport = RecordingTransport()
        sender = CommandSender(transport, resend_interval_ms=10_000)
        sender.start()
        try:
            sender.send(Command.FORWARD)
            self.assertTrue(transport.wait_for(1))
            transport.deliver(b"ACK:F:FORWARD\n")

            deadline = time.monotonic() + 1.0
            while sender.stats.acks_received == 0 and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertEqual(sender.stats.acks_received, 1)
            self.assertIsNotNone(sender.stats.last_rtt_ms)
            self.assertGreaterEqual(sender.stats.last_rtt_ms, 0.0)
            self.assertLess(sender.stats.last_rtt_ms, 1000.0)
        finally:
            sender.stop()

    def test_partial_and_multiple_lines_are_handled(self):
        transport = RecordingTransport()
        sender = CommandSender(transport, resend_interval_ms=10_000)
        sender.start()
        try:
            sender.send(Command.FORWARD)
            self.assertTrue(transport.wait_for(1))
            transport.deliver(b"ACK:F:FOR")
            time.sleep(0.05)
            transport.deliver(b"WARD\nACK:F:FORWARD\nAC")
            time.sleep(0.1)
            self.assertEqual(sender.stats.acks_received, 2)
        finally:
            sender.stop()

    def test_non_ack_chatter_is_ignored(self):
        transport = RecordingTransport()
        sender = CommandSender(transport, resend_interval_ms=10_000)
        sender.start()
        try:
            transport.deliver(b"[state] STOP reason=WATCHDOG\nERR:unknown command\n")
            time.sleep(0.1)
            self.assertEqual(sender.stats.acks_received, 0)
        finally:
            sender.stop()


class TestUdpTransport(unittest.TestCase):
    """Real sockets on loopback. Proves the datagrams actually leave."""

    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.settimeout(2.0)
        self.port = self.server.getsockname()[1]

    def tearDown(self):
        self.server.close()

    def test_datagram_arrives(self):
        transport = UdpTransport("127.0.0.1", self.port, listen_port=0, expect_ack=False)
        transport.open()
        try:
            transport.send(b"F\n")
            data, _peer = self.server.recvfrom(64)
            self.assertEqual(data, b"F\n")
        finally:
            transport.close()

    def test_ack_comes_back_on_the_bound_port(self):
        transport = UdpTransport("127.0.0.1", self.port, listen_port=0, expect_ack=True)
        transport.open()
        try:
            transport.send(b"S\n")
            _data, peer = self.server.recvfrom(64)
            self.server.sendto(b"ACK:S:STOP\n", peer)

            deadline = time.monotonic() + 1.0
            received = b""
            while not received and time.monotonic() < deadline:
                received = transport.receive()
                time.sleep(0.005)
            self.assertEqual(received, b"ACK:S:STOP\n")
        finally:
            transport.close()

    def test_send_before_open_is_an_error_not_a_crash(self):
        transport = UdpTransport("127.0.0.1", self.port)
        with self.assertRaises(TransportError):
            transport.send(b"F\n")

    def test_send_once_helper(self):
        config = config_module.load()
        config.set("transport.mode", "udp")
        config.set("transport.udp.esp32_ip", "127.0.0.1")
        config.set("transport.udp.esp32_port", self.port)
        config.set("transport.udp.listen_port", 0)

        self.assertTrue(send_once(config, "FORWARD"))
        data, _peer = self.server.recvfrom(64)
        self.assertEqual(data, b"F\n")

    def test_send_once_rejects_an_unknown_command_safely(self):
        config = config_module.load()
        config.set("transport.udp.esp32_ip", "127.0.0.1")
        config.set("transport.udp.esp32_port", self.port)
        config.set("transport.udp.listen_port", 0)

        self.assertTrue(send_once(config, "TELEPORT"))
        data, _peer = self.server.recvfrom(64)
        self.assertEqual(data, b"S\n")  # fails safe to STOP


class TestTransportFactory(unittest.TestCase):
    def test_builds_udp_from_config(self):
        config = config_module.load()
        config.set("transport.mode", "udp")
        config.set("transport.udp.esp32_ip", "10.0.0.5")
        config.set("transport.udp.esp32_port", 9999)

        transport = create_transport(config)
        self.assertIsInstance(transport, UdpTransport)
        self.assertEqual(transport.host, "10.0.0.5")
        self.assertEqual(transport.port, 9999)

    def test_builds_serial_from_config(self):
        config = config_module.load()
        config.set("transport.mode", "serial")
        config.set("transport.serial.port", "COM9")

        transport = create_transport(config)
        self.assertIsInstance(transport, SerialTransport)
        self.assertEqual(transport.port, "COM9")

    def test_rejects_an_unknown_mode(self):
        config = config_module.load()
        config.set("transport.mode", "carrier-pigeon")
        with self.assertRaises(ValueError):
            create_transport(config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
