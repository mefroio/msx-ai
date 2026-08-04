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

from contextlib import contextmanager
import ipaddress
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
# A visible foreground DEBUG monitor may spend close to one second in host-side
# rendering around connection time. Physical targets can be slower still.
BOOTSTRAP_PROBE_TIMEOUT = 2.0
BOOTSTRAP_SCAN_LIMIT = 64
RECOVERY_SCAN_LIMIT = 1024
RECOVERY_DRAIN_LIMIT = 0x4000
RECOVERY_QUIET_SECONDS = 0.05
UART8251_BAUD = 19200
# One conservative bootstrap/reconnect delay is needed before framed HELLO
# advertises the explicit parser-ready ACK. Normal negotiated 8251 frames use
# that ACK and therefore do not depend on this scheduler timing heuristic.
UART8251_FRAME_WAKE_DELAY = 0.010
FRAME_WAKE_ACK = b"\x06"
FRAME_WAKE_BOOTSTRAP_TIMEOUT = 0.100

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
FEATURE_KEYBUF_INPUT = 0x01
FEATURE_DEBUG_PEER = 0x02
FEATURE_SNAPSHOT_LEASE = 0x04
FEATURE_FRAME_WAKE_ACK = 0x08
DEBUG_PEER_MAX = 63
SNAPSHOT_LEASE_TIMEOUTS = 8
SNAPSHOT_PAUSE_ATTEMPTS = 2
SNAPSHOT_REQUEST_TIMEOUT = 1.0
AGENT_FEATURE_NAMES = {
    FEATURE_KEYBUF_INPUT: "keybuf-input",
    FEATURE_DEBUG_PEER: "debug-peer-label",
    FEATURE_SNAPSHOT_LEASE: "snapshot-lease",
    FEATURE_FRAME_WAKE_ACK: "frame-wake-ack",
}
AGENT_TRANSPORT_NAMES = {
    0: "uart-8251",
    1: "uart-16c550",
}
AGENT_RUNTIME_MODES = {0: "resident", 1: "foreground-monitor"}

KEYBUF_START = 0xFBF0
KEYBUF_SIZE = 40
KEYBUF_END = KEYBUF_START + KEYBUF_SIZE
KEYBUF_CAPACITY = KEYBUF_SIZE - 1
PUTPNT = 0xF3F8
GETPNT = 0xF3FA
KEYBUF_INPUT_TIMEOUT = 10.0
KEYBUF_POLL_INTERVAL = 0.02
KEYBUF_LINE_SETTLE = 0.05


class RealMSXError(RuntimeError):
    pass


class RealMSXProtocolError(RealMSXError):
    pass


class RealMSXTimeoutError(RealMSXError):
    pass


class RealMSXRangeError(RealMSXError, ValueError):
    pass


class RealMSXKeyboardTimeoutError(RealMSXTimeoutError):
    pass


class RealMSX:
    """One byte-stream session with an MSX-AI physical-target agent.

    The convenience connection methods currently use TCP/IPv4 because that is
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
        self.feature_bits = 0
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
        self._debug_peer_sent = False
        self._snapshot_pause_owned = False
        self._v3 = None
        self._lock = threading.RLock()

    # ---- connection -------------------------------------------------
    def listen(self):
        """Open a TCP listener for adapters that initiate the connection."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
        try:
            self._configure_tcp_nodelay(conn)
        except Exception:
            conn.close()
            raise
        self.attach_stream(
            conn, peer=peer, network_transport="tcp", network_role="listen")
        if handshake:
            self.info()
        return self.peer

    def connect(self, host=None, port=None, timeout=60, handshake=True):
        """Connect to an adapter that exposes the agent as a TCP server."""
        target_host = self.host if host is None else host
        target_port = self.port if port is None else int(port)
        if ":" in str(target_host):
            raise RealMSXError("IPv6 is not supported; use an IPv4 endpoint")
        try:
            target_ipv4 = socket.gethostbyname(target_host)
            conn = socket.create_connection(
                (target_ipv4, target_port), timeout=float(timeout))
        except socket.gaierror as exc:
            raise RealMSXError(
                f"could not resolve an IPv4 address for MSX agent host "
                f"{target_host}: {exc}") from exc
        except socket.timeout as exc:
            raise RealMSXTimeoutError(
                f"timeout connecting to MSX agent at "
                f"{target_host}:{target_port}") from exc
        except OSError as exc:
            raise RealMSXError(
                f"could not connect to MSX agent at "
                f"{target_host}:{target_port}: {exc}") from exc
        self.host, self.port = target_host, target_port
        try:
            self._configure_tcp_nodelay(conn)
        except Exception:
            conn.close()
            raise
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
        self.feature_bits = 0
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
        self._debug_peer_sent = False
        self._snapshot_pause_owned = False
        self._v3 = None

    # ---- transport --------------------------------------------------
    def _require_connection(self):
        if self.conn is None:
            raise RealMSXError("MSX agent is not connected")

    @staticmethod
    def _configure_tcp_nodelay(stream):
        """Disable TCP coalescing on TCP-created agent streams when available."""
        if getattr(stream, "family", None) != socket.AF_INET:
            return
        setsockopt = getattr(stream, "setsockopt", None)
        if not callable(setsockopt):
            return
        try:
            setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            raise RealMSXError(
                f"could not enable TCP_NODELAY for MSX agent: {exc}") from exc

    def _send(self, data):
        self._require_connection()
        try:
            self.conn.sendall(data)
        except OSError as exc:
            raise RealMSXError(f"agent send failed: {exc}") from exc

    def _send_reconnect_escape(self):
        """Send the framed reset marker with an 8251-safe first-byte gap."""
        if self.agent_transport_id in (None, 0):
            self._send(RECONNECT_ESCAPE[:1])
            time.sleep(UART8251_FRAME_WAKE_DELAY)
            self._send(RECONNECT_ESCAPE[1:])
        else:
            self._send(RECONNECT_ESCAPE)

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

    def _request_v3(self, opcode, payload=b"", *, timeout=None, retries=None):
        if self._v3 is None:
            raise RealMSXProtocolError("framed v3 session is not active")
        if isinstance(opcode, str):
            if len(opcode) != 1:
                raise ValueError("opcode must be one character")
            opcode = ord(opcode)
        try:
            return self._v3.request(
                opcode, payload, timeout=timeout, retries=retries)
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

            self._send_reconnect_escape()
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
            return self._activate_bootstrap_hello(reply)

    def _activate_bootstrap_hello(self, reply, *, v3_timeout=None):
        """Validate a raw HELLO and upgrade the attached stream when possible."""
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
            self._send(b"F")
            upgrade = self._recv_exact(4)
            if upgrade[:2] != b"K\x03":
                raise RealMSXProtocolError(
                    f"agent rejected framed-v3 upgrade: {upgrade!r}")
            peer_max = int.from_bytes(upgrade[2:4], "little")
            if peer_max <= 0:
                raise RealMSXProtocolError(
                    f"agent advertised invalid v3 payload limit {peer_max}")
            session_timeout = (
                self.socket_timeout if v3_timeout is None
                else min(self.socket_timeout, float(v3_timeout)))
            known_8251_ack = (
                self.agent_transport_id == 0 and
                bool(self.feature_bits & FEATURE_FRAME_WAKE_ACK))
            known_16c550 = self.agent_transport_id == 1
            self._v3 = V3Session(
                self.conn, timeout=session_timeout, retries=2,
                max_payload=4096, peer_max_payload=peer_max,
                # The first framed HELLO carries the feature bit, so probe the
                # ACK with a bounded fallback when the transport is not known.
                # A recovered current 8251 session can require it immediately.
                frame_wake_ack=(None if known_16c550 else FRAME_WAKE_ACK),
                frame_wake_ack_optional=(
                    not known_16c550 and not known_8251_ack),
                frame_wake_ack_timeout=FRAME_WAKE_BOOTSTRAP_TIMEOUT)
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

    def _drain_recovery_noise(self):
        """Discard a bounded stale response up to a quiet stream boundary.

        A timed-out 320-byte VRAM response can still be crossing an 8251 when
        recovery starts. Draining before the raw escape marker prevents a
        payload byte sequence resembling ``M,2`` from being mistaken for the
        bootstrap HELLO that follows that marker.
        """
        self._require_connection()
        original_timeout = self.conn.gettimeout()
        drained = 0
        try:
            self.conn.settimeout(RECOVERY_QUIET_SECONDS)
            while drained < RECOVERY_DRAIN_LIMIT:
                try:
                    chunk = self.conn.recv(
                        min(4096, RECOVERY_DRAIN_LIMIT - drained))
                except (socket.timeout, TimeoutError):
                    break
                except OSError as exc:
                    raise RealMSXError(
                        f"agent receive failed during recovery: {exc}") from exc
                if not chunk:
                    raise RealMSXError("agent disconnected during recovery")
                drained += len(chunk)
        finally:
            self.conn.settimeout(original_timeout)
        return drained

    def _rebootstrap_v3(self):
        """Reset a damaged framed session and negotiate a fresh v3 session."""
        self._require_connection()
        with self._lock:
            original_timeout = self.conn.gettimeout()
            probe_timeout = min(self.socket_timeout, BOOTSTRAP_PROBE_TIMEOUT)
            try:
                self._drain_recovery_noise()
                # Once the marker is attempted, the old framed parser can no
                # longer be trusted even if the automatic raw HELLO is lost.
                self._v3 = None
                self._debug_peer_sent = False
                self._send_reconnect_escape()
                try:
                    reply = self._scan_bootstrap_hello(
                        probe_timeout, byte_limit=RECOVERY_SCAN_LIMIT)
                except RealMSXTimeoutError:
                    # The marker may already have switched the agent to raw
                    # mode while its unsolicited HELLO was truncated or lost.
                    # Drain that partial reply, then query raw mode explicitly.
                    self._drain_recovery_noise()
                    self._send(b"?")
                    reply = self._scan_bootstrap_hello(
                        probe_timeout, byte_limit=RECOVERY_SCAN_LIMIT)
                # The marker places the peer in raw bootstrap mode. Do not use
                # info() here: another '?' would queue a second raw HELLO ahead
                # of the framed-upgrade acknowledgement.
                self.bootstrap_recovered = True
                result = self._activate_bootstrap_hello(
                    reply, v3_timeout=SNAPSHOT_REQUEST_TIMEOUT)
                if self._v3 is None:
                    raise RealMSXProtocolError(
                        "agent did not renegotiate framed protocol v3")
                return result
            finally:
                self.conn.settimeout(original_timeout)

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
        self.feature_bits = reply[14] if len(reply) >= 15 else 0
        if (transport == 0 and
                self.feature_bits & FEATURE_FRAME_WAKE_ACK):
            self._v3.frame_wake_ack = FRAME_WAKE_ACK
            self._v3.frame_wake_ack_optional = False
            self._v3.frame_wake_delay = 0.0
        else:
            self._v3.frame_wake_ack = None
            self._v3.frame_wake_ack_optional = False
            self._v3.frame_wake_delay = (
                UART8251_FRAME_WAKE_DELAY if transport == 0 else 0.0)
        self._send_debug_peer_label()
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
            "features": [name for bit, name in AGENT_FEATURE_NAMES.items()
                         if self.feature_bits & bit],
            "feature_bits": self.feature_bits,
            "vdp_generation": self.vdp_generation,
            "vram_size": self.vram_size,
            "vram_banks": self.vram_banks,
            "bootstrap_recovered": self.bootstrap_recovered,
            "peer": self.peer,
        }

    def _send_debug_peer_label(self):
        """Announce host-known peer metadata to foreground DEBUG exactly once."""
        if (self._debug_peer_sent or self._v3 is None or not self.debug or
                not self.feature_bits & FEATURE_DEBUG_PEER or
                self.peer is None):
            return
        if isinstance(self.peer, (tuple, list)):
            peer_host = str(self.peer[0])
            if len(self.peer) >= 2:
                peer_label = f"{peer_host}:{self.peer[1]}"
            else:
                peer_label = peer_host
        else:
            peer_host = str(self.peer)
            peer_label = peer_host
        try:
            ipaddress.IPv4Address(peer_host)
        except ipaddress.AddressValueError:
            return
        try:
            payload = peer_label.encode("ascii")
        except UnicodeEncodeError:
            return
        if (not payload or len(payload) > DEBUG_PEER_MAX or
                any(byte < 0x20 or byte >= 0x7F for byte in payload)):
            return
        with self._lock:
            reply = self._request_v3("I", payload)
        if reply:
            raise RealMSXProtocolError(
                f"invalid debug peer-label response: {reply!r}")
        self._debug_peer_sent = True

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

    def _resume_snapshot_pause(self):
        """Release a host-owned snapshot pause without trusting prior state.

        The first command is deliberately a direct ``g``. A status query can
        itself be the frame whose reply is lost, so cleanup must not depend on
        one before attempting to release the target. If the framed session is
        damaged, reset it with the raw escape marker and verify the resulting
        state before giving up.
        """
        if not self._snapshot_pause_owned:
            return "running"
        direct_error = None
        try:
            if self._v3 is None:
                raise RealMSXProtocolError(
                    "framed v3 session is unavailable during snapshot cleanup")
            reply = self._request_v3("g")
            if reply:
                raise RealMSXProtocolError(
                    f"invalid snapshot-resume response: {reply!r}")
            self._snapshot_pause_owned = False
            return "running"
        except Exception as exc:
            direct_error = exc

        recovery_error = None
        for _attempt in range(2):
            try:
                self._rebootstrap_v3()
                state = self.status()["state"]
                if state == "running":
                    self._snapshot_pause_owned = False
                    return "running"
                if state != "paused":
                    raise RealMSXError(
                        "snapshot recovery expected a running or paused "
                        f"target, got {state!r}")
                reply = self._request_v3("g")
                if reply:
                    raise RealMSXProtocolError(
                        f"invalid recovered snapshot-resume response: {reply!r}")
                if self.status()["state"] != "running":
                    raise RealMSXError(
                        "agent acknowledged snapshot resume but did not report "
                        "the running state")
                self._snapshot_pause_owned = False
                return "running"
            except Exception as exc:
                recovery_error = exc

        raise RealMSXError(
            "could not guarantee that the MSX resumed after its snapshot "
            f"lease (direct resume failed: {direct_error}; recovery failed: "
            f"{recovery_error})") from recovery_error

    @contextmanager
    def snapshot_lease(self, *, atomic=True,
                       lease=SNAPSHOT_LEASE_TIMEOUTS):
        """Own a bounded agent pause for one atomic data acquisition.

        ``lease`` is measured in agent receive-timeout periods, not seconds.
        Valid traffic refreshes the agent-side timeout, while silence after a
        dead connection consumes the lease and eventually resumes the target.
        Normal completion always sends ``g`` immediately.

        A target that was already paused remains manually paused: this context
        resumes only a pause that it acquired from an initially running state.
        """
        if not isinstance(atomic, bool):
            raise TypeError("atomic must be a boolean")
        if (isinstance(lease, bool) or not isinstance(lease, int) or
                not 1 <= lease <= 0xFF):
            raise ValueError("snapshot lease must be in range 1..255")
        if not atomic:
            yield False
            return

        with self._lock:
            if self._snapshot_pause_owned:
                raise RealMSXError(
                    "a previous snapshot pause still requires cleanup")
            has_snapshot_lease = (
                self._v3 is not None and
                bool(self.feature_bits & FEATURE_SNAPSHOT_LEASE))
            # Feature-gate an old resident before even a status request: its
            # live hook may be vulnerable to reentry while servicing that
            # otherwise-small query. A foreground monitor has no resident hook
            # and is safe to query while idle or already manually paused.
            if (not has_snapshot_lease and
                    self.runtime_mode == "foreground-monitor"):
                state = self.status()["state"]
                if state in ("monitor", "paused"):
                    yield False
                    return
                raise RealMSXError(
                    "atomic capture of running foreground code requires the "
                    "snapshot-lease feature; stop or manually pause it, or "
                    "retry with atomic=false")
            if not has_snapshot_lease:
                raise RealMSXError(
                    "atomic capture requires an agent with the "
                    "snapshot-lease feature; update MSXAI.COM or retry with "
                    "atomic=false")
            state = self.status()["state"]
            if state == "paused":
                # Preserve a caller-owned/manual pause.
                yield False
                return
            if state == "monitor":
                # No asynchronous application is running in this state.
                yield False
                return
            if state != "running":
                raise RealMSXError(
                    f"cannot acquire an atomic snapshot while agent state is "
                    f"{state!r}")

            # Keep each retry comfortably inside the agent-side lease window.
            # A complete 333-byte v3 frame takes about 0.18 seconds at 19,200
            # baud; one second still leaves ample processing margin.
            original_v3_timeout = self._v3.timeout
            self._v3.timeout = min(
                original_v3_timeout, SNAPSHOT_REQUEST_TIMEOUT)
            try:
                # Set ownership before S is sent. Even if its acknowledgement
                # is lost, cleanup must assume that the target entered pause.
                self._snapshot_pause_owned = True
                try:
                    for attempt in range(SNAPSHOT_PAUSE_ATTEMPTS):
                        reply = self._request_v3("S", bytes([lease]))
                        if reply:
                            raise RealMSXProtocolError(
                                f"invalid snapshot-lease response: {reply!r}")
                        verified = self.status()["state"]
                        if verified == "paused":
                            break
                        # A delayed cached ACK can arrive after lease expiry. A
                        # fresh S sequence is then required to pause again.
                        if (verified == "running" and
                                attempt + 1 < SNAPSHOT_PAUSE_ATTEMPTS):
                            continue
                        if verified == "running":
                            self._snapshot_pause_owned = False
                        raise RealMSXError(
                            "agent acknowledged snapshot lease but reported "
                            f"state {verified!r}")
                    yield True
                    final_state = self.status()["state"]
                    if final_state == "running":
                        self._snapshot_pause_owned = False
                        raise RealMSXError(
                            "snapshot lease expired during acquisition; "
                            "captured data was discarded because it is not "
                            "atomic")
                    if final_state != "paused":
                        raise RealMSXError(
                            "snapshot lease ended in unexpected agent state "
                            f"{final_state!r}")
                except BaseException as operation_error:
                    if self._snapshot_pause_owned:
                        try:
                            self._resume_snapshot_pause()
                        except BaseException as cleanup_error:
                            # Keep acquisition failure as the primary error;
                            # cleanup diagnostics remain available as cause.
                            raise operation_error from cleanup_error
                    raise
                else:
                    if self._snapshot_pause_owned:
                        self._resume_snapshot_pause()
            finally:
                # Recovery may have replaced the V3Session. Restore the normal
                # request timeout on whichever framed session is now current.
                if self._v3 is not None:
                    self._v3.timeout = original_v3_timeout
                if self.conn is not None:
                    try:
                        self.conn.settimeout(original_v3_timeout)
                    except (OSError, ValueError):
                        pass

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

    # ---- BIOS keyboard ring ---------------------------------------
    @staticmethod
    def _keybuf_pointer(pointer, name):
        if not KEYBUF_START <= pointer < KEYBUF_END:
            raise RealMSXProtocolError(
                f"BIOS {name} pointer 0x{pointer:04X} is outside "
                f"KEYBUF 0x{KEYBUF_START:04X}-0x{KEYBUF_END - 1:04X}")
        return pointer

    def _keybuf_state_from_ram(self):
        raw = self.peek(PUTPNT, 4)
        put = self._keybuf_pointer(int.from_bytes(raw[:2], "little"), "PUTPNT")
        get = self._keybuf_pointer(int.from_bytes(raw[2:], "little"), "GETPNT")
        pending = ((put - KEYBUF_START) - (get - KEYBUF_START)) % KEYBUF_SIZE
        if pending > KEYBUF_CAPACITY:
            raise RealMSXProtocolError(
                f"invalid BIOS keyboard queue length {pending}")
        return put, get, pending

    def _keybuf_write_legacy(self, data, timeout=None):
        """Fallback for older agents without the atomic v3 keybuf opcode."""
        # Own the whole pause/read/write/publish/resume cycle.  Every protocol
        # primitive also takes this RLock, so concurrent screenshots or memory
        # operations cannot resume the target in the middle of the fallback.
        with self._lock:
            original_v3_timeout = self._v3.timeout if self._v3 is not None else None
            original_stream_timeout = self.conn.gettimeout()
            if timeout is not None:
                operation_count = 1 if not data else 9
                attempts = (self._v3.retries + 1) if self._v3 is not None else 1
                per_attempt = max(0.001, float(timeout) /
                                  (operation_count * attempts))
                if self._v3 is not None:
                    self._v3.timeout = min(self._v3.timeout, per_attempt)
                else:
                    self.conn.settimeout(min(original_stream_timeout, per_attempt))
            try:
                if not data:
                    return 0, self._keybuf_state_from_ram()[2]
                state = self.status()["state"]
                if state != "running":
                    raise RealMSXError(
                        "keyboard input requires a running resident target, "
                        f"got {state!r}")
                self.pause()
                transaction_error = None
                try:
                    put, _get, pending = self._keybuf_state_from_ram()
                    accepted = min(len(data), KEYBUF_CAPACITY - pending)
                    if accepted:
                        first = min(accepted, KEYBUF_END - put)
                        self.poke(put, data[:first])
                        if first < accepted:
                            self.poke(KEYBUF_START, data[first:accepted])
                        new_put = KEYBUF_START + (
                            (put - KEYBUF_START + accepted) % KEYBUF_SIZE)
                        # Publish last: CHGET never observes half a batch.
                        self.poke(PUTPNT, new_put.to_bytes(2, "little"))
                    return accepted, pending + accepted
                except BaseException as exc:
                    transaction_error = exc
                    raise
                finally:
                    try:
                        self.resume()
                    except Exception:
                        if transaction_error is None:
                            raise
            finally:
                if self._v3 is not None:
                    self._v3.timeout = original_v3_timeout
                self.conn.settimeout(original_stream_timeout)

    def keybuf_write(self, data, timeout=None):
        """Atomically enqueue up to 39 BIOS keyboard bytes.

        Return ``(accepted, pending)``.  New agents execute one idempotent v3
        operation inside the resident hook; older peers use a protected
        pause/write/publish/resume transaction for compatibility.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("keyboard data must be bytes-like")
        data = bytes(data)
        if len(data) > KEYBUF_CAPACITY:
            raise RealMSXRangeError(
                f"keyboard batch exceeds {KEYBUF_CAPACITY} bytes")
        if self._v3 is not None and self.feature_bits & FEATURE_KEYBUF_INPUT:
            request_timeout = None
            if timeout is not None:
                timeout = float(timeout)
                if timeout <= 0:
                    raise RealMSXKeyboardTimeoutError(
                        "keyboard input timeout expired before request")
                request_timeout = max(
                    0.001, timeout / (self._v3.retries + 1))
            with self._lock:
                reply = self._request_v3(
                    "t", data, timeout=request_timeout)
            if len(reply) != 2:
                raise RealMSXProtocolError(
                    f"invalid keybuf response: {reply!r}")
            accepted, pending = reply
            if accepted > len(data) or pending > KEYBUF_CAPACITY:
                raise RealMSXProtocolError(
                    f"invalid keybuf counts accepted={accepted}, pending={pending}")
            return accepted, pending
        return self._keybuf_write_legacy(data, timeout=timeout)

    def wait_keybuf_empty(self, timeout=KEYBUF_INPUT_TIMEOUT):
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RealMSXKeyboardTimeoutError(
                    "timeout waiting for software to consume BIOS keyboard input; "
                    "the target may read the key matrix directly")
            _accepted, pending = self.keybuf_write(b"", timeout=remaining)
            if pending == 0:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RealMSXKeyboardTimeoutError(
                    "timeout waiting for software to consume BIOS keyboard input; "
                    "the target may read the key matrix directly")
            time.sleep(min(KEYBUF_POLL_INTERVAL, remaining))

    @staticmethod
    def _encode_keyboard_text(text):
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        normalized = text.replace("\r\n", "\r").replace("\n", "\r")
        try:
            return normalized.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "real-agent keyboard input currently supports ASCII only") from exc

    def type(self, text, timeout=KEYBUF_INPUT_TIMEOUT):
        """Type text through the BIOS ring and wait until it is consumed."""
        payload = self._encode_keyboard_text(text)
        deadline = time.monotonic() + float(timeout)
        offset = 0
        while offset < len(payload):
            remaining = payload[offset:]
            batch_size = min(len(remaining), KEYBUF_CAPACITY)
            carriage_return = remaining.find(b"\r", 0, batch_size)
            if carriage_return >= 0:
                batch_size = carriage_return + 1
            batch = remaining[:batch_size]
            batch_offset = 0
            while batch_offset < len(batch):
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise RealMSXKeyboardTimeoutError(
                        "timeout while typing through the BIOS keyboard buffer")
                accepted, _pending = self.keybuf_write(
                    batch[batch_offset:], timeout=remaining_time)
                batch_offset += accepted
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise RealMSXKeyboardTimeoutError(
                        "timeout while typing through the BIOS keyboard buffer")
                self.wait_keybuf_empty(remaining_time)
                if accepted == 0 and batch_offset < len(batch):
                    continue
            offset += len(batch)
            if batch.endswith(b"\r"):
                time.sleep(KEYBUF_LINE_SETTLE)
        return len(payload)

    def type_line(self, text, timeout=KEYBUF_INPUT_TIMEOUT):
        """Type one text line followed by the MSX Return key."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return self.type(text + "\r", timeout=timeout)

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
    def read_screen(self, timeout=None):
        """Decode SCREEN 0/1 text through monitor RAM/VRAM reads.

        When ``timeout`` is supplied, divide it across every possible framed
        request and retry in this capture. A stalled link therefore cannot turn
        one prompt poll into several full default-timeout waits.
        """
        with self._lock:
            original_v3_timeout = (
                self._v3.timeout if self._v3 is not None else None)
            original_stream_timeout = self.conn.gettimeout()
            if timeout is not None:
                timeout = float(timeout)
                if timeout <= 0:
                    raise RealMSXKeyboardTimeoutError(
                        "screen capture timeout expired before request")
                chunk_size = (
                    self._v3.max_payload if self._v3 is not None else 255)
                operation_count = 1 + (
                    (40 * 24 + chunk_size - 1) // chunk_size)
                attempts = (
                    self._v3.retries + 1 if self._v3 is not None else 1)
                per_attempt = max(
                    0.001, timeout / (operation_count * attempts))
                if self._v3 is not None:
                    self._v3.timeout = min(
                        self._v3.timeout, per_attempt)
                else:
                    self.conn.settimeout(min(
                        original_stream_timeout, per_attempt))
            try:
                r1, r2 = self.peek(0xF3E0, 2)
                width = 40 if (r1 & 0x10) else 32
                base = (r2 & 0x0F) << 10
                data = self.vpeek(base, width * 24)
            finally:
                if self._v3 is not None:
                    self._v3.timeout = original_v3_timeout
                self.conn.settimeout(original_stream_timeout)
        rows = []
        for row in range(24):
            raw = data[row * width:(row + 1) * width]
            rows.append("".join(
                chr(b) if 32 <= b < 127 else " " for b in raw).rstrip())
        return rows

    def screen_text(self, timeout=None):
        return "\n".join(self.read_screen(timeout=timeout))

    def close(self):
        if self.conn is not None and self._snapshot_pause_owned:
            try:
                with self._lock:
                    self._resume_snapshot_pause()
            except Exception:
                # The bounded lease remains the final target-side safeguard
                # when the byte stream itself is no longer recoverable.
                pass
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
