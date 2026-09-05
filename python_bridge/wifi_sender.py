"""
Sends commands from the laptop to the ESP32.

One ASCII character followed by a newline:

    F forward    L left    R right    S stop

The same command is re-sent periodically, which also feeds the firmware's
2-second watchdog. If this process dies, the vehicle stops on its own.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from command_mapper import COMMANDS_BY_WIRE, Command, mirror_turn

LOG = logging.getLogger("neurodrive.tx")

#: Legacy name -> wire string map, kept for scripts that predate the enum.
COMMANDS = {command.value: f"{command.wire}\n" for command in Command}

#: How often the worker checks for acknowledgements while otherwise idle.
#: Small enough that it does not distort the measured round-trip time.
ACK_POLL_INTERVAL_S = 0.01


class TransportError(RuntimeError):
    """Raised when the link to the ESP32 cannot be opened or used."""


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


class Transport(ABC):
    """A one-way-plus-acknowledgement byte channel to the vehicle."""

    name = "transport"
    description = "-"

    @abstractmethod
    def open(self) -> None:
        ...

    @abstractmethod
    def send(self, payload: bytes) -> None:
        ...

    def receive(self) -> bytes:
        """Return any bytes waiting from the vehicle (never blocks)."""
        return b""

    def close(self) -> None:
        ...


class UdpTransport(Transport):
    """Primary transport: UDP datagrams over the demo WiFi network.

    The same socket is bound locally and used to send, so the firmware's
    reply naturally comes back to ``listen_port`` without extra plumbing.
    """

    name = "udp"

    def __init__(
        self,
        host: str,
        port: int,
        listen_port: int = 0,
        expect_ack: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.listen_port = listen_port
        self.expect_ack = expect_ack
        self._socket: Optional[socket.socket] = None

    @property
    def description(self) -> str:
        return f"udp {self.host}:{self.port}"

    def open(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if self.expect_ack and self.listen_port:
                sock.bind(("", self.listen_port))
            sock.setblocking(False)
        except OSError as exc:
            raise TransportError(f"cannot open UDP socket: {exc}") from exc
        self._socket = sock

    def send(self, payload: bytes) -> None:
        if self._socket is None:
            raise TransportError("UDP socket is not open")
        try:
            self._socket.sendto(payload, (self.host, self.port))
        except OSError as exc:
            raise TransportError(f"UDP send failed: {exc}") from exc

    def receive(self) -> bytes:
        if self._socket is None or not self.expect_ack:
            return b""
        chunks = bytearray()
        while True:
            try:
                data, _addr = self._socket.recvfrom(256)
            except BlockingIOError:
                break
            except OSError:
                # On Windows an ICMP port-unreachable surfaces here; the
                # vehicle simply is not listening yet, so keep going.
                break
            if not data:
                break
            chunks.extend(data)
        return bytes(chunks)

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None


class SerialTransport(Transport):
    """Fallback transport: USB cable straight to the ESP32.

    Used when the venue's WiFi is unusable. The firmware
    accepts exactly the same ASCII commands on its USB serial port.
    """

    name = "serial"

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial = None

    @property
    def description(self) -> str:
        return f"serial {self.port}@{self.baudrate}"

    def open(self) -> None:
        try:
            import serial  # lazy: only needed for the cable fallback
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TransportError(
                "pyserial is not installed. Run: pip install -r requirements.txt"
            ) from exc
        try:
            self._serial = serial.Serial(
                port=self.port, baudrate=self.baudrate, timeout=0, write_timeout=0.2
            )
        except Exception as exc:
            raise TransportError(
                f"cannot open ESP32 serial port {self.port!r}: {exc}"
            ) from exc

    def send(self, payload: bytes) -> None:
        if self._serial is None:
            raise TransportError("serial port is not open")
        try:
            self._serial.write(payload)
        except Exception as exc:
            raise TransportError(f"serial write failed: {exc}") from exc

    def receive(self) -> bytes:
        if self._serial is None:
            return b""
        try:
            waiting = self._serial.in_waiting
            return self._serial.read(waiting) if waiting else b""
        except Exception:
            return b""

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None


def create_transport(config) -> Transport:
    """Build the transport named by ``transport.mode``."""
    mode = config.get("transport.mode", "udp")
    if mode == "udp":
        settings = config.section("transport.udp")
        return UdpTransport(
            host=settings.get("esp32_ip", "192.168.4.1"),
            port=settings.get("esp32_port", 4210),
            listen_port=settings.get("listen_port", 4211),
            expect_ack=settings.get("expect_ack", True),
        )
    if mode == "serial":
        settings = config.section("transport.serial")
        return SerialTransport(
            port=settings.get("port", "COM6"),
            baudrate=settings.get("baudrate", 115200),
        )
    raise ValueError(f"unknown transport.mode: {mode!r}")


# --------------------------------------------------------------------------
# Sender
# --------------------------------------------------------------------------


@dataclass
class SenderStats:
    """Transport health, surfaced on the dashboard and in the test report."""

    packets_sent: int = 0
    commands_changed: int = 0
    keepalives: int = 0
    acks_received: int = 0
    errors: int = 0
    queue_drops: int = 0
    last_error: str = ""
    last_rtt_ms: Optional[float] = None
    rtt_samples: Deque[float] = field(default_factory=lambda: deque(maxlen=100))

    @property
    def avg_rtt_ms(self) -> Optional[float]:
        if not self.rtt_samples:
            return None
        return sum(self.rtt_samples) / len(self.rtt_samples)

    @property
    def max_rtt_ms(self) -> Optional[float]:
        return max(self.rtt_samples) if self.rtt_samples else None


class CommandSender:
    """Owns the transport and a worker thread that keeps the vehicle fed.

    ``send()`` is called from the main loop every cycle and returns
    immediately. The worker transmits on change and re-transmits every
    ``resend_interval_ms`` so a single lost datagram cannot leave the vehicle
    running on a stale command.
    """

    def __init__(
        self,
        transport: Transport,
        resend_interval_ms: int = 250,
        queue_size: int = 32,
        turn_burst: int = 3,
        invert_turns: bool = False,
    ) -> None:
        self.transport = transport
        self.invert_turns = invert_turns
        self.resend_interval_s = max(0.02, resend_interval_ms / 1000.0)
        self.turn_burst = max(1, turn_burst)
        self.stats = SenderStats()

        self._queue: "queue.Queue[Command]" = queue.Queue(maxsize=max(1, queue_size))
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._requested: Optional[Command] = None
        self._current: Optional[Command] = None
        self._last_send_time = 0.0
        self._send_times: dict[str, float] = {}
        self._rx_buffer = bytearray()
        self._last_ack_time: Optional[float] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CommandSender already started")
        self.transport.open()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="cmd-sender", daemon=True
        )
        self._thread.start()
        LOG.info("command sender started on %s", self.transport.description)

    def stop(self, timeout: float = 2.0, final_command: Optional[Command] = None) -> None:
        """Stop the worker, optionally transmitting one last command.

        ``main.py`` passes ``Command.STOP`` so quitting the bridge always
        leaves the vehicle stationary rather than relying on the watchdog.
        """
        if final_command is not None:
            self._send_now(final_command, repeats=self.turn_burst)
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.transport.close()

    def __enter__(self) -> "CommandSender":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop(final_command=Command.STOP)

    # -- producer API -------------------------------------------------------

    def send(self, command: Command) -> None:
        """Request that ``command`` be the vehicle's current command.

        Repeats of the current request are dropped here rather than queued,
        which is what stops a 20 Hz main loop from flooding the network.
        """
        with self._lock:
            if command is self._requested:
                return
            self._requested = command
        try:
            self._queue.put_nowait(command)
            self.stats.commands_changed += 1
        except queue.Full:
            self.stats.queue_drops += 1
            LOG.warning("send queue full, dropped %s", command.value)

    @property
    def current_command(self) -> Optional[Command]:
        return self._current

    @property
    def ack_age_s(self) -> Optional[float]:
        if self._last_ack_time is None:
            return None
        return time.monotonic() - self._last_ack_time

    # -- worker -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()

            # Wait for the next command, but no longer than one ack
            # poll. Blocking for a whole resend interval would delay acks,
            # which shows up as fake round-trip time and slows shutdown.
            until_resend = self.resend_interval_s - (now - self._last_send_time)
            timeout = max(0.001, min(ACK_POLL_INTERVAL_S, until_resend))
            try:
                command = self._queue.get(timeout=timeout)
            except queue.Empty:
                command = None

            if command is not None:
                # Coalesce. If several changes queued up, only the newest
                # matters. The older ones are already obsolete.
                while True:
                    try:
                        command = self._queue.get_nowait()
                    except queue.Empty:
                        break
                repeats = self.turn_burst if command.is_turn else 1
                self._send_now(command, repeats=repeats)
            elif (
                self._current is not None
                and (time.monotonic() - self._last_send_time) >= self.resend_interval_s
            ):
                # keepalive re-send of the current command.
                self._send_now(self._current, repeats=1)
                self.stats.keepalives += 1

            self._drain_acks()

    def _send_now(self, command: Command, repeats: int = 1) -> None:
        # Every outgoing byte goes through here, so a mirrored vehicle is
        # corrected in one place for EEG, webcam and keyboard alike. The
        # command keeps its name elsewhere, so the dashboard and CSV still
        # show the direction the vehicle actually goes.
        on_wire = mirror_turn(command) if self.invert_turns else command
        payload = f"{on_wire.wire}\n".encode("ascii")
        for _ in range(repeats):
            try:
                self.transport.send(payload)
            except TransportError as exc:
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                LOG.warning("send failed: %s", exc)
                return
        self._current = command
        self._last_send_time = time.monotonic()
        self._send_times[command.wire] = self._last_send_time
        self.stats.packets_sent += repeats

    def _drain_acks(self) -> None:
        """Parse ``ACK:<char>`` lines and turn them into RTT samples."""
        try:
            data = self.transport.receive()
        except Exception:  # pragma: no cover - transport already logged
            return
        if not data:
            return

        self._rx_buffer.extend(data)
        # Guard against a chatty or malfunctioning device filling memory.
        if len(self._rx_buffer) > 4096:
            del self._rx_buffer[:-1024]

        while b"\n" in self._rx_buffer:
            line, _, rest = bytes(self._rx_buffer).partition(b"\n")
            self._rx_buffer = bytearray(rest)
            self._handle_ack_line(line.decode("ascii", "replace").strip())

    def _handle_ack_line(self, line: str) -> None:
        if not line:
            return
        LOG.debug("vehicle -> %s", line)
        if not line.startswith("ACK:"):
            return

        self.stats.acks_received += 1
        self._last_ack_time = time.monotonic()

        char = line[4:5].upper()
        sent_at = self._send_times.get(char)
        if sent_at is None or char not in COMMANDS_BY_WIRE:
            return
        rtt_ms = (self._last_ack_time - sent_at) * 1000.0
        self.stats.last_rtt_ms = rtt_ms
        self.stats.rtt_samples.append(rtt_ms)


# --------------------------------------------------------------------------
# One-shot helper
# --------------------------------------------------------------------------


def send_once(config, command) -> bool:
    """Fire a single command and close the transport again.

    Used by ``udp_test_sender.py`` and by anyone poking at the vehicle from a
    REPL. Returns True if the packet left the machine.
    """
    from command_mapper import map_to_command

    name = map_to_command(command)
    transport = create_transport(config)
    payload = COMMANDS[name].encode("ascii")
    try:
        transport.open()
        transport.send(payload)
        LOG.info("sent %s to %s", name, transport.description)
        return True
    except TransportError as exc:
        LOG.error("failed to send %s: %s", name, exc)
        return False
    finally:
        transport.close()
