#!/usr/bin/env python3
"""Transport-neutral client for the MSX-AI physical-target agent.

The physical (or openMSX-simulated) MSX runs ``agent/msx_agent.asm``.  The
default MemMan TSR remains reachable through the BIOS hook chain while a
cooperative DOS program is running, allowing MCP to pause, inspect/patch RAM or
VRAM, and resume it. The optional foreground monitor also supports direct Z80
upload/call/run cycles.

Protocol v2 remains as a bootstrap/fallback for installed older agents.  A
capable peer upgrades in-place to framed v3 (sequence, CRC, resynchronization,
retry de-duplication and negotiated larger payloads). Protocol operations use
only a connected byte stream. TCP listen/connect helpers are provided here,
while the MSX-side UART/Wi-Fi implementation is negotiated independently.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time

from msx_v3 import V3Session, V3SessionError

PROJ = pathlib.Path(__file__).resolve().parent.parent
Z80ASM = (os.environ.get("Z80ASM") or shutil.which("z80asm") or
          "/opt/homebrew/bin/z80asm")
PROTOCOL_VERSION = 2
FRAMED_PROTOCOL_VERSION = 3
DEFAULT_PORT = 6603
RAM_SIZE = 0x10000
VRAM_SIZE = 0x20000
VRAM_BANK_SIZE = 0x4000
RECONNECT_ESCAPE = b"\x1b" * 8
BOOTSTRAP_PROBE_TIMEOUT = 0.5
BOOTSTRAP_SCAN_LIMIT = 64

STATE_NAMES = {0: "monitor", 1: "running", 2: "paused"}
CAPABILITY_RUN = 0x08
CAPABILITY_MAPPING = 0x80
CAPABILITY_NAMES = {
    0x01: "ram",
    0x02: "vram",
    0x04: "pause",
    CAPABILITY_RUN: "run",
    0x10: "raw-binary",
    0x20: "framed-v3",
    0x40: "hardware-io",
    CAPABILITY_MAPPING: "mapping",
}
AGENT_TRANSPORT_NAMES = {
    0: "uart-8251",
    1: "uart-16c550",
}
AGENT_RUNTIME_MODES = {0: "resident", 1: "foreground-monitor"}


class RealMSXError(RuntimeError):
    pass


class RealMSXProtocolError(RealMSXError):
    pass


class RealMSXTimeoutError(RealMSXError):
    pass


class RealMSXRangeError(RealMSXError, ValueError):
    pass


class RealMSX:
    """One byte-stream session with an MSX-AI physical-target agent.

    The convenience connection methods currently use TCP/IP because that is
    the common external contract. The framed protocol and all monitor
    operations remain independent from the UART or network adapter installed
    in the MSX.
    """

    def __init__(self, host="0.0.0.0", port=DEFAULT_PORT, socket_timeout=15):
        self.host, self.port = host, int(port)
        self.socket_timeout = float(socket_timeout)
        self.srv = None
        self.conn = None
        self.peer = None
        self.local_endpoint = None
        self.network_transport = None
        self.network_role = None
        self.protocol_version = None
        self.capabilities = 0
        self.resident_base = None
        self.vram_size = VRAM_SIZE
        self.vram_banks = VRAM_SIZE // VRAM_BANK_SIZE
        self.vdp_generation = None
        self.agent_transport_id = None
        self.agent_transport = None
        self.transport = None
        self.runtime_mode_id = None
        self.runtime_mode = None
        self.control_level = None
        self.debug = None
        self.simulation = None
        self.bootstrap_recovered = False
        self.bootstrap_protocol_version = None
        self._v3 = None
        self._lock = threading.RLock()

    # ---- connection -------------------------------------------------
    def listen(self):
        """Open a TCP listener for adapters that initiate the connection."""
        listener = socket.socket()
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(1)
        except Exception:
            listener.close()
            raise
        self.srv = listener
        # Port 0 is useful for isolated integration tests.
        self.port = self.srv.getsockname()[1]
        self.local_endpoint = self.srv.getsockname()
        self.network_transport = "tcp"
        self.network_role = "listen"
        return self

    def accept(self, timeout=60, handshake=True):
        if self.srv is None:
            raise RealMSXError("listener is not open")
        self.srv.settimeout(float(timeout))
        conn, peer = self.srv.accept()
        self.attach_stream(
            conn, peer=peer, network_transport="tcp", network_role="listen")
        if handshake:
            self.info()
        return self.peer

    def connect(self, host=None, port=None, timeout=60, handshake=True):
        """Connect to an adapter that exposes the agent as a TCP server."""
        target_host = self.host if host is None else host
        target_port = self.port if port is None else int(port)
        try:
            conn = socket.create_connection(
                (target_host, target_port), timeout=float(timeout))
        except socket.timeout as exc:
            raise RealMSXTimeoutError(
                f"timeout connecting to MSX agent at "
                f"{target_host}:{target_port}") from exc
        except OSError as exc:
            raise RealMSXError(
                f"could not connect to MSX agent at "
                f"{target_host}:{target_port}: {exc}") from exc
        self.host, self.port = target_host, target_port
        self.attach_stream(
            conn, peer=conn.getpeername(), network_transport="tcp",
            network_role="connect")
        if handshake:
            self.info()
        return self.peer

    def attach_stream(self, stream, *, peer=None,
                      network_transport="custom-stream",
                      network_role="attached"):
        """Attach any connected socket-like full-duplex byte stream.

        A stream must expose ``recv``, ``sendall``, ``settimeout`` and
        ``gettimeout``. This is the extension point for future TCP bridges or
        other host-side transports; monitor commands never inspect its type.
        """
        required = ("recv", "sendall", "settimeout", "gettimeout")
        missing = [name for name in required
                   if not callable(getattr(stream, name, None))]
        if missing:
            raise TypeError(
                "stream must provide "
                + ", ".join(f"{name}()" for name in missing))
        if self.conn is not None and self.conn is not stream:
            raise RealMSXError("an MSX agent stream is already attached")
        self.conn = stream
        self.peer = peer
        self.network_transport = str(network_transport)
        self.network_role = str(network_role)
        self.conn.settimeout(self.socket_timeout)
        try:
            self.local_endpoint = self.conn.getsockname()
        except (AttributeError, OSError):
            self.local_endpoint = None
        self._reset_protocol_state()
        return self

    def attach_socket(self, conn):
        """Backward-compatible alias for attaching a connected test socket."""
        return self.attach_stream(conn)

    def _reset_protocol_state(self):
        """Reset negotiated state when a new byte stream is attached."""
        self.protocol_version = None
        self.capabilities = 0
        self.resident_base = None
        self.vram_size = VRAM_SIZE
        self.vram_banks = VRAM_SIZE // VRAM_BANK_SIZE
        self.vdp_generation = None
        self.agent_transport_id = None
        self.agent_transport = None
        self.transport = None
        self.runtime_mode_id = None
        self.runtime_mode = None
        self.control_level = None
        self.debug = None
        self.simulation = None
        self.bootstrap_recovered = False
        self.bootstrap_protocol_version = None
        self._v3 = None

    # ---- transport --------------------------------------------------
    def _require_connection(self):
        if self.conn is None:
            raise RealMSXError("MSX agent is not connected")

    def _send(self, data):
        self._require_connection()
        try:
            self.conn.sendall(data)
        except OSError as exc:
            raise RealMSXError(f"agent send failed: {exc}") from exc

    def _recv_exact(self, n):
        self._require_connection()
        buf = bytearray()
        try:
            while len(buf) < n:
                data = self.conn.recv(n - len(buf))
                if not data:
                    raise RealMSXError("agent disconnected")
                buf += data
        except socket.timeout as exc:
            raise RealMSXTimeoutError(
                f"timeout waiting for {n - len(buf)} of {n} agent bytes") from exc
        except OSError as exc:
            raise RealMSXError(f"agent receive failed: {exc}") from exc
        return bytes(buf)

    @staticmethod
    def _address16(addr):
        return bytes([(addr >> 8) & 0xFF, addr & 0xFF])

    @staticmethod
    def _validate_range(addr, size, limit, space):
        if not isinstance(addr, int) or not isinstance(size, int):
            raise TypeError("address and size must be integers")
        if addr < 0 or size < 0 or addr > limit or addr + size > limit:
            raise RealMSXRangeError(
                f"{space} range 0x{addr:X}+{size} exceeds 0x{limit - 1:X}")

    @staticmethod
    def _validate_byte(value, name):
        if not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if not 0 <= value <= 0xFF:
            raise RealMSXRangeError(f"{name} must be in range 0..255")

    def _resident_runtime_active(self):
        """Return whether the negotiated peer is the MemMan resident build.

        Framed v3 names the runtime explicitly.  Raw v2 has no runtime byte, so
        the missing RUN capability is its backwards-compatible discriminator.
        Before a handshake there is deliberately no inferred runtime.
        """
        if self.runtime_mode_id is not None:
            return self.runtime_mode_id == 0
        return (self.protocol_version is not None and
                not bool(self.capabilities & CAPABILITY_RUN))

    @staticmethod
    def _ranges_overlap(addr, size, lower, upper):
        return size > 0 and addr < upper and addr + size > lower

    def _validate_mappable_page(self, page):
        if not isinstance(page, int):
            raise TypeError("page must be an integer")
        if not 0 <= page <= 1:
            raise RealMSXRangeError("page must be in range 0..1")
        if self._resident_runtime_active():
            raise RealMSXError(
                "slot/mapper mapping is unavailable in resident mode; "
                "use /MONITOR")
        if not self.capabilities & CAPABILITY_MAPPING:
            raise RealMSXError("agent does not advertise mapping capability")

    def _expect_ack(self):
        first = self._recv_exact(1)
        if first == b"K":
            return
        if first == b"E":
            code = self._recv_exact(1)[0]
            raise RealMSXProtocolError(f"agent rejected command (error {code})")
        raise RealMSXProtocolError(f"unexpected agent response: {first!r}")

    def _request_v3(self, opcode, payload=b""):
        if self._v3 is None:
            raise RealMSXProtocolError("framed v3 session is not active")
        if isinstance(opcode, str):
            if len(opcode) != 1:
                raise ValueError("opcode must be one character")
            opcode = ord(opcode)
        try:
            return self._v3.request(opcode, payload)
        except V3SessionError as exc:
            raise RealMSXProtocolError(f"framed agent request failed: {exc}") from exc

    @staticmethod
    def _le16(value):
        return int(value).to_bytes(2, "little")

    @staticmethod
    def _le24(value):
        return int(value).to_bytes(3, "little")

    # ---- monitor/session -------------------------------------------
    def _scan_bootstrap_hello(self, timeout, byte_limit=BOOTSTRAP_SCAN_LIMIT):
        """Return the next raw v2 HELLO while discarding bounded stream noise."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        window = bytearray()
        for _ in range(int(byte_limit)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.conn.settimeout(remaining)
            try:
                byte = self._recv_exact(1)
            except RealMSXTimeoutError:
                break
            window += byte
            del window[:-4]
            if len(window) == 4 and window[:2] == b"M\x02":
                return bytes(window)
        raise RealMSXTimeoutError(
            "timeout waiting for MSX-AI raw bootstrap HELLO")

    def _bootstrap_hello(self):
        """Read raw v2 HELLO, recovering a resident left in framed mode.

        The initial single-byte query preserves compatibility with older
        agents. Each phase scans a bounded amount of UART noise for ``M,2``.
        If the first query fails, the eight-ESC marker recovers framed v3; a
        final query then recovers raw agents which answered those ESC bytes
        with ordinary unknown-command errors.
        """
        self._require_connection()
        original_timeout = self.conn.gettimeout()
        probe_timeout = min(self.socket_timeout, BOOTSTRAP_PROBE_TIMEOUT)
        try:
            self._send(b"?")
            try:
                reply = self._scan_bootstrap_hello(probe_timeout)
                self.bootstrap_recovered = False
                return reply
            except RealMSXTimeoutError:
                pass

            self._send(RECONNECT_ESCAPE)
            try:
                reply = self._scan_bootstrap_hello(probe_timeout)
            except RealMSXTimeoutError:
                self._send(b"?")
                reply = self._scan_bootstrap_hello(probe_timeout)
            self.bootstrap_recovered = True
            return reply
        finally:
            self.conn.settimeout(original_timeout)

    def info(self):
        if self._v3 is not None:
            return self._info_v3()
        with self._lock:
            reply = self._bootstrap_hello()
        if reply[0:1] != b"M":
            raise RealMSXProtocolError(
                f"peer is not an MSX-AI physical-target agent: {reply!r}")
        version, capabilities, page = reply[1], reply[2], reply[3]
        if version != PROTOCOL_VERSION:
            raise RealMSXProtocolError(
                f"agent protocol {version}, host requires {PROTOCOL_VERSION}")
        self.bootstrap_protocol_version = version
        self.protocol_version = version
        self.capabilities = capabilities
        self.resident_base = page << 8
        if capabilities & 0x20:
            with self._lock:
                self._send(b"F")
                upgrade = self._recv_exact(4)
            if upgrade[:2] != b"K\x03":
                raise RealMSXProtocolError(
                    f"agent rejected framed-v3 upgrade: {upgrade!r}")
            peer_max = int.from_bytes(upgrade[2:4], "little")
            if peer_max <= 0:
                raise RealMSXProtocolError(
                    f"agent advertised invalid v3 payload limit {peer_max}")
            self._v3 = V3Session(
                self.conn, timeout=self.socket_timeout, retries=2,
                max_payload=4096, peer_max_payload=peer_max)
            return self._info_v3()
        return {
            "protocol": version,
            "capabilities": [name for bit, name in CAPABILITY_NAMES.items()
                             if capabilities & bit],
            "capability_bits": capabilities,
            "resident_base": self.resident_base,
            "network_transport": self.network_transport,
            "network_role": self.network_role,
            "local_endpoint": self.local_endpoint,
            "peer": self.peer,
        }

    def _info_v3(self):
        with self._lock:
            reply = self._request_v3("?")
        if len(reply) < 8 or reply[0] != FRAMED_PROTOCOL_VERSION:
            raise RealMSXProtocolError(f"invalid framed hello response: {reply!r}")
        version, capabilities, page, transport = reply[:4]
        peer_max = int.from_bytes(reply[4:6], "little")
        if peer_max <= 0:
            raise RealMSXProtocolError(
                f"agent advertised invalid v3 payload limit {peer_max}")
        self._v3.negotiate_max_payload(peer_max)
        self.protocol_version = version
        self.capabilities = capabilities
        self.resident_base = page << 8
        vdp_generation = reply[8] if len(reply) >= 9 else None
        if len(reply) >= 13:
            vram_banks = reply[9]
            vram_size = int.from_bytes(reply[10:13], "little")
            if (vram_banks not in (1, 4, 8) or
                    vram_size != vram_banks * VRAM_BANK_SIZE):
                raise RealMSXProtocolError(
                    "invalid framed VRAM capacity: "
                    f"{vram_banks} bank(s), {vram_size} byte(s)")
        else:
            # Compatibility with the original nine-byte v3 HELLO. It exposed
            # only machine generation and therefore could not distinguish a
            # 64-KiB MSX2 from a 128-KiB one.
            vram_size = VRAM_BANK_SIZE if vdp_generation == 0 else VRAM_SIZE
            vram_banks = vram_size // VRAM_BANK_SIZE
        self.vram_size = vram_size
        self.vram_banks = vram_banks
        self.agent_transport_id = transport
        self.agent_transport = AGENT_TRANSPORT_NAMES.get(
            transport, f"unknown-{transport}")
        # Compatibility for callers of the original API. New code should use
        # ``agent_transport`` so it cannot be confused with the TCP/IP link.
        self.transport = self.agent_transport
        self.control_level = reply[6]
        self.debug = bool(reply[7])
        self.vdp_generation = vdp_generation
        self.runtime_mode_id = reply[13] if len(reply) >= 14 else None
        self.runtime_mode = AGENT_RUNTIME_MODES.get(
            self.runtime_mode_id,
            (None if self.runtime_mode_id is None
             else f"unknown-{self.runtime_mode_id}"))
        return {
            "protocol": version,
            "bootstrap_protocol": self.bootstrap_protocol_version,
            "capabilities": [name for bit, name in CAPABILITY_NAMES.items()
                             if capabilities & bit],
            "capability_bits": capabilities,
            "resident_base": self.resident_base,
            "transport": self.agent_transport,
            "agent_transport": self.agent_transport,
            "agent_transport_id": self.agent_transport_id,
            "network_transport": self.network_transport,
            "network_role": self.network_role,
            "local_endpoint": self.local_endpoint,
            "max_payload": self._v3.max_payload,
            "control_level": self.control_level,
            "debug": self.debug,
            "runtime_mode": self.runtime_mode,
            "runtime_mode_id": self.runtime_mode_id,
            "vdp_generation": self.vdp_generation,
            "vram_size": self.vram_size,
            "vram_banks": self.vram_banks,
            "bootstrap_recovered": self.bootstrap_recovered,
            "peer": self.peer,
        }

    def status(self):
        if self._v3 is not None:
            with self._lock:
                reply = self._request_v3("q")
            if len(reply) != 2:
                raise RealMSXProtocolError(
                    f"invalid framed status response: {reply!r}")
            state, version = reply
            if version != FRAMED_PROTOCOL_VERSION:
                raise RealMSXProtocolError(
                    f"status protocol {version}, host requires "
                    f"{FRAMED_PROTOCOL_VERSION}")
            return {
                "state": STATE_NAMES.get(state, f"unknown-{state}"),
                "state_code": state,
                "protocol": version,
            }
        with self._lock:
            self._send(b"q")
            reply = self._recv_exact(3)
        if reply[0:1] != b"K":
            raise RealMSXProtocolError(f"invalid status response: {reply!r}")
        state, version = reply[1], reply[2]
        if version != PROTOCOL_VERSION:
            raise RealMSXProtocolError(
                f"status protocol {version}, host requires {PROTOCOL_VERSION}")
        return {
            "state": STATE_NAMES.get(state, f"unknown-{state}"),
            "state_code": state,
            "protocol": version,
        }

    def pause(self):
        with self._lock:
            state = self.status()["state"]
            if state == "paused":
                return state
            if state != "running":
                raise RealMSXError(
                    f"cannot pause while agent state is {state!r}")
            if self._v3 is not None:
                self._request_v3("s")
            else:
                self._send(b"s")
                self._expect_ack()
        return "paused"

    def resume(self):
        with self._lock:
            state = self.status()["state"]
            if state == "running":
                return state
            if state != "paused":
                raise RealMSXError(
                    f"cannot resume while agent state is {state!r}")
            if self._v3 is not None:
                self._request_v3("g")
            else:
                self._send(b"g")
                self._expect_ack()
        return "running"

    def stop(self):
        """Abandon foreground-launched code and return to the upload monitor."""
        if self.runtime_mode == "resident":
            raise RealMSXError(
                "stop is unsafe in resident mode; pause/inspect/resume the "
                "DOS-launched application instead")
        state = self.status()["state"]
        if state == "running":
            self.pause()
        with self._lock:
            if self._v3 is not None:
                self._request_v3("k")
            else:
                self._send(b"k")
                self._expect_ack()
        return "monitor"

    # ---- RAM --------------------------------------------------------
    def poke(self, addr, data):
        data = bytes(data)
        self._validate_range(addr, len(data), RAM_SIZE, "RAM")
        if data:
            if (self._resident_runtime_active() and
                    self._ranges_overlap(addr, len(data), 0x4000, 0x8000)):
                raise RealMSXRangeError(
                    "RAM write overlaps CPU page 1 (0x4000-0x7FFF), which "
                    "contains the mapped resident TSR")
            if (not self._resident_runtime_active() and
                    self.resident_base is not None and
                    addr + len(data) > self.resident_base):
                raise RealMSXRangeError(
                    f"RAM write overlaps protected foreground monitor area at "
                    f"0x{self.resident_base:04X}+")
        with self._lock:
            offset = 0
            while offset < len(data):
                target = addr + offset
                if self._v3 is not None:
                    chunk = data[offset:offset + self._v3.max_payload - 2]
                    self._request_v3("p", self._le16(target) + chunk)
                else:
                    chunk = data[offset:offset + 255]
                    self._send(b"p" + self._address16(target) +
                               bytes([len(chunk)]) + chunk)
                    self._expect_ack()
                offset += len(chunk)
        return len(data)

    def peek(self, addr, n):
        self._validate_range(addr, n, RAM_SIZE, "RAM")
        if (self._resident_runtime_active() and
                self._ranges_overlap(addr, n, 0x4000, 0x8000)):
            raise RealMSXRangeError(
                "RAM read overlaps CPU page 1 (0x4000-0x7FFF), which is "
                "temporarily occupied by the mapped resident TSR")
        out = bytearray()
        with self._lock:
            while len(out) < n:
                if self._v3 is not None:
                    size = min(n - len(out), self._v3.max_payload)
                    out += self._request_v3(
                        "r", self._le16(addr + len(out)) + self._le16(size))
                else:
                    size = min(n - len(out), 255)
                    self._send(b"r" + self._address16(addr + len(out)) +
                               bytes([size]))
                    out += self._recv_exact(size)
        return bytes(out)

    # ---- VRAM -------------------------------------------------------
    @staticmethod
    def _vram_header(command, addr, size):
        bank = (addr >> 14) & 0x07
        low14 = addr & 0x3FFF
        return command + bytes([bank]) + RealMSX._address16(low14) + bytes([size])

    def vpeek(self, addr, n):
        self._validate_range(addr, n, self.vram_size, "VRAM")
        out = bytearray()
        with self._lock:
            while len(out) < n:
                current = addr + len(out)
                boundary = 0x4000 - (current & 0x3FFF)
                if self._v3 is not None:
                    size = min(n - len(out), self._v3.max_payload, boundary)
                    out += self._request_v3(
                        "v", self._le24(current) + self._le16(size))
                else:
                    size = min(n - len(out), 255, boundary)
                    self._send(self._vram_header(b"v", current, size))
                    out += self._recv_exact(size)
        return bytes(out)

    def vpoke(self, addr, data):
        data = bytes(data)
        self._validate_range(addr, len(data), self.vram_size, "VRAM")
        with self._lock:
            offset = 0
            while offset < len(data):
                current = addr + offset
                boundary = 0x4000 - (current & 0x3FFF)
                if self._v3 is not None:
                    chunk = data[offset:offset + min(
                        self._v3.max_payload - 3, boundary)]
                    self._request_v3("w", self._le24(current) + chunk)
                else:
                    chunk = data[offset:offset + min(255, boundary)]
                    self._send(self._vram_header(b"w", current, len(chunk)) + chunk)
                    self._expect_ack()
                offset += len(chunk)
        return len(data)

    # ---- hardware ---------------------------------------------------
    def io_read(self, port):
        """Read one byte from an MSX I/O port through the resident agent."""
        self._validate_byte(port, "port")
        with self._lock:
            if self._v3 is not None:
                reply = self._request_v3("i", bytes([port]))
                if len(reply) != 1:
                    raise RealMSXProtocolError(
                        f"invalid I/O read response: {reply!r}")
                return reply[0]
            self._send(b"i" + bytes([port]))
            return self._recv_exact(1)[0]

    def io_read_many(self, ports):
        """Read several ports while keeping their requests contiguous."""
        ports = tuple(ports)
        for port in ports:
            self._validate_byte(port, "port")
        values = bytearray()
        with self._lock:
            for port in ports:
                if self._v3 is not None:
                    reply = self._request_v3("i", bytes([port]))
                    if len(reply) != 1:
                        raise RealMSXProtocolError(
                            f"invalid I/O read response: {reply!r}")
                    values += reply
                else:
                    self._send(b"i" + bytes([port]))
                    values += self._recv_exact(1)
        return bytes(values)

    def io_write(self, port, value, *, verify=False):
        """Write an MSX I/O port, optionally checking it with a read-back."""
        self._validate_byte(port, "port")
        self._validate_byte(value, "value")
        with self._lock:
            if self._v3 is not None:
                self._request_v3("o", bytes([port, value]))
            else:
                self._send(b"o" + bytes([port, value]))
                self._expect_ack()
            if verify:
                if self._v3 is not None:
                    actual = self._request_v3("i", bytes([port]))[0]
                else:
                    self._send(b"i" + bytes([port]))
                    actual = self._recv_exact(1)[0]
                if actual != value:
                    raise RealMSXProtocolError(
                        f"I/O verify failed at port 0x{port:02X}: "
                        f"wrote 0x{value:02X}, read 0x{actual:02X}")
        return b"K"

    def io_write_many(self, writes, *, verify=False):
        """Write ``(port, value)`` pairs under one transport lock."""
        writes = tuple(writes)
        normalized = []
        for item in writes:
            try:
                port, value = item
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "each I/O write must be a (port, value) pair") from exc
            self._validate_byte(port, "port")
            self._validate_byte(value, "value")
            normalized.append((port, value))

        with self._lock:
            for port, value in normalized:
                if self._v3 is not None:
                    self._request_v3("o", bytes([port, value]))
                else:
                    self._send(b"o" + bytes([port, value]))
                    self._expect_ack()
                if verify:
                    if self._v3 is not None:
                        actual = self._request_v3("i", bytes([port]))[0]
                    else:
                        self._send(b"i" + bytes([port]))
                        actual = self._recv_exact(1)[0]
                    if actual != value:
                        raise RealMSXProtocolError(
                            f"I/O verify failed at port 0x{port:02X}: "
                            f"wrote 0x{value:02X}, read 0x{actual:02X}")
        return len(normalized)

    def slot_select(self, page, slot_id):
        """Map a slot into page 0 or 1 in foreground-monitor mode."""
        self._validate_mappable_page(page)
        self._validate_byte(slot_id, "slot_id")
        with self._lock:
            if self._v3 is not None:
                self._request_v3("l", bytes([page, slot_id]))
            else:
                self._send(b"l" + bytes([page, slot_id]))
                self._expect_ack()
        return b"K"

    def mapper_select(self, page, segment):
        """Map a segment into page 0 or 1 in foreground-monitor mode."""
        self._validate_mappable_page(page)
        self._validate_byte(segment, "segment")
        with self._lock:
            if self._v3 is not None:
                self._request_v3("m", bytes([page, segment]))
            else:
                self._send(b"m" + bytes([page, segment]))
                self._expect_ack()
        return b"K"

    # ---- execution --------------------------------------------------
    def call(self, addr):
        self._validate_range(addr, 1, RAM_SIZE, "RAM")
        if not self.capabilities & CAPABILITY_RUN:
            raise RealMSXError(
                "call is unavailable in resident mode; use /MONITOR")
        with self._lock:
            if self._v3 is not None:
                self._request_v3("c", self._le16(addr))
            else:
                self._send(b"c" + self._address16(addr))
                self._expect_ack()
        return b"K"

    def run(self, addr):
        """Launch code and return after the monitor ACK, before the code exits."""
        self._validate_range(addr, 1, RAM_SIZE, "RAM")
        if not self.capabilities & CAPABILITY_RUN:
            raise RealMSXError(
                "run is unavailable in resident mode; use /MONITOR")
        with self._lock:
            if self._v3 is not None:
                self._request_v3("j", self._le16(addr))
            else:
                self._send(b"j" + self._address16(addr))
                self._expect_ack()
        return "accepted"

    def uninstall(self):
        if self._v3 is not None:
            raise RealMSXProtocolError(
                "uninstall is unavailable after framed-v3 upgrade; close or "
                "restart the monitor session")
        with self._lock:
            self._send(b"z")
            self._expect_ack()
        return "uninstalled"

    def asm_load(self, source, address=0x8000, execute="none"):
        if execute not in ("none", "call", "run"):
            raise ValueError("execute must be 'none', 'call' or 'run'")
        code = _assemble(source, address)
        self.poke(address, code)
        summary = f"[real asm] {len(code)} bytes loaded @0x{address:04X}"
        if execute == "call":
            self.call(address)
            summary += " (called)"
        elif execute == "run":
            self.run(address)
            summary += " (running asynchronously)"
        return summary

    # ---- screen -----------------------------------------------------
    def read_screen(self):
        """Decode SCREEN 0/1 text through monitor RAM/VRAM reads."""
        r1, r2 = self.peek(0xF3E0, 2)
        width = 40 if (r1 & 0x10) else 32
        base = (r2 & 0x0F) << 10
        data = self.vpeek(base, width * 24)
        rows = []
        for row in range(24):
            raw = data[row * width:(row + 1) * width]
            rows.append("".join(chr(b) if 32 <= b < 127 else " " for b in raw).rstrip())
        return rows

    def screen_text(self):
        return "\n".join(self.read_screen())

    def close(self):
        for sock in (self.conn, self.srv):
            if sock is not None:
                try:
                    close = getattr(sock, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass
        self.conn = self.srv = None
        self.peer = None
        self.local_endpoint = None
        self.network_transport = None
        self.network_role = None
        self._reset_protocol_state()


# Preferred semantic name for new integrations. Keep RealMSX as a public
# compatibility name because existing MCP clients and tests already import it.
MSXAgent = RealMSX


def _assemble(source, address):
    text = source if "org" in source.lower() else f"    org 0{address:04X}h\n" + source
    with tempfile.TemporaryDirectory() as directory:
        src = pathlib.Path(directory) / "program.asm"
        out = pathlib.Path(directory) / "program.bin"
        src.write_text(text)
        result = subprocess.run([Z80ASM, str(src), "-o", str(out)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RealMSXError("z80asm error:\n" + (result.stderr or result.stdout))
        return out.read_bytes()
