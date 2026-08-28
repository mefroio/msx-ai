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
import errno
import ipaddress
import math
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time

if __package__:
    from .msx_v3 import V3Session, V3SessionError
    from .msx_cpu import (
        CPU_CONTEXT_VERSION,
        CPUSnapshotError,
        parse_agent_cpu_context,
    )
    from .msx_transfer import (
        FEATURE_FILE_TRANSFER_V2,
        TRANSFER_OPCODE,
        TransferBindingError,
        TransferCapability,
        TransferCancelledError,
        TransferDescriptor,
        TransferDirection,
        TransferEncoding,
        TransferError,
        TransferFastCapability,
        TransferJournal,
        TransferRemoteError,
        TransferReplyFlag,
        TransferState,
        crc32_file_prefix,
        crc32_update,
        encode_cancel,
        encode_capabilities_request,
        encode_close,
        encode_get_ack,
        encode_get_read,
        encode_fast_capabilities_request,
        encode_fast_begin,
        encode_open,
        encode_put_data,
        encode_status,
        new_transfer_id,
        parse_cancel_reply,
        parse_capabilities_reply,
        parse_close_reply,
        parse_get_ack_reply,
        parse_get_read_reply,
        parse_fast_capabilities_reply,
        parse_open_reply,
        parse_put_data_reply,
        parse_status_reply,
        prepare_msx_basic_source,
        prepare_put_payload,
    )
    from .paths import source_root, transfer_state_directory, user_root
else:  # pragma: no cover - repository-style top-level import
    from msx_v3 import V3Session, V3SessionError
    from msx_cpu import (
        CPU_CONTEXT_VERSION,
        CPUSnapshotError,
        parse_agent_cpu_context,
    )
    from msx_transfer import (
        FEATURE_FILE_TRANSFER_V2,
        TRANSFER_OPCODE,
        TransferBindingError,
        TransferCapability,
        TransferCancelledError,
        TransferDescriptor,
        TransferDirection,
        TransferEncoding,
        TransferError,
        TransferFastCapability,
        TransferJournal,
        TransferRemoteError,
        TransferReplyFlag,
        TransferState,
        crc32_file_prefix,
        crc32_update,
        encode_cancel,
        encode_capabilities_request,
        encode_close,
        encode_get_ack,
        encode_get_read,
        encode_fast_capabilities_request,
        encode_fast_begin,
        encode_open,
        encode_put_data,
        encode_status,
        new_transfer_id,
        parse_cancel_reply,
        parse_capabilities_reply,
        parse_close_reply,
        parse_get_ack_reply,
        parse_get_read_reply,
        parse_fast_capabilities_reply,
        parse_open_reply,
        parse_put_data_reply,
        parse_status_reply,
        prepare_msx_basic_source,
        prepare_put_payload,
    )
    from paths import source_root, transfer_state_directory, user_root

PROJ = source_root() or user_root()
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
UART8251_FRAME_WAKE_DELAY = 0.050
FRAME_WAKE_ACK = b"\x06"
FRAME_WAKE_BOOTSTRAP_TIMEOUT = 0.250

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
FEATURE_TIMI_POLL_SAFE = 0x10
FEATURE_KEYBUF_SPOOL = 0x20
FEATURE_CPU_SNAPSHOT = 0x40
FEATURE_FILE_TRANSFER = FEATURE_FILE_TRANSFER_V2
DEBUG_PEER_MAX = 63
SNAPSHOT_LEASE_TIMEOUTS = 8
SNAPSHOT_PAUSE_ATTEMPTS = 2
SNAPSHOT_REQUEST_TIMEOUT = 1.0
AGENT_FEATURE_NAMES = {
    FEATURE_KEYBUF_INPUT: "keybuf-input",
    FEATURE_DEBUG_PEER: "debug-peer-label",
    FEATURE_SNAPSHOT_LEASE: "snapshot-lease",
    FEATURE_FRAME_WAKE_ACK: "frame-wake-ack",
    FEATURE_TIMI_POLL_SAFE: "timi-poll-safe",
    FEATURE_KEYBUF_SPOOL: "keybuf-spool",
    FEATURE_CPU_SNAPSHOT: "cpu-snapshot-v1",
    FEATURE_FILE_TRANSFER: "file-transfer-v2",
}
AGENT_TRANSPORT_NAMES = {
    0: "uart-8251",
    1: "uart-16c550",
    2: "tcpip-unapi",
}
AGENT_RUNTIME_MODES = {0: "resident", 1: "foreground-monitor"}

KEYBUF_START = 0xFBF0
KEYBUF_SIZE = 40
KEYBUF_END = KEYBUF_START + KEYBUF_SIZE
KEYBUF_CAPACITY = KEYBUF_SIZE - 1
PUTPNT = 0xF3F8
GETPNT = 0xF3FA
INTFLG = 0xFC9B
KEYBUF_INPUT_TIMEOUT = 10.0
KEYBUF_POLL_INTERVAL = 0.02
KEYBUF_LINE_SETTLE = 0.05
KEYBUF_SPOOL_CAPACITY = 255
KEYBUF_SPOOL_MAX_PENDING = KEYBUF_SPOOL_CAPACITY + KEYBUF_CAPACITY
KEYBUF_SPOOL_FLAG_BARRIER = 0x01
KEYBUF_SPOOL_FLAG_ACTIVE = 0x02
KEYBUF_SPOOL_FLAG_AUTHORIZED = 0x04
KEYBUF_SPOOL_REQUEST_PUMP = 0x01
KEYBUF_SPOOL_REQUEST_CANCEL = 0x02
KEYBUF_SPOOL_REFILL_TARGET = 128
FILE_TRANSFER_STATE_DIR = transfer_state_directory()
FILE_TRANSFER_POLL_INTERVAL = 0.05
FILE_TRANSFER_PROGRESS_TIMEOUT = 600.0
FILE_TRANSFER_JOURNAL_INTERVAL = 64 * 1024
FILE_TRANSFER_MAX_UNCOMMITTED = 16 * 1024
# A full 2048-byte frame takes about 1.07 seconds at 19,200 baud and about
# 4.3 seconds at 4,800 baud.  This is a deadline, not a pacing delay.
FILE_TRANSFER_FAST_FRAME_TIMEOUT = 15.0
SPECIAL_KEY_BYTES = {
    "ESC": 0x1B,
    "RET": 0x0D,
    "SPACE": 0x20,
    "SELECT": 0x18,
    "TAB": 0x09,
}
SPECIAL_KEY_INTFLG = {
    "CTRL+C": 0x03,
    "CTRL+STOP": 0x03,
    "STOP": 0x04,
}


class RealMSXError(RuntimeError):
    pass


class RealMSXProtocolError(RealMSXError):
    pass


class RealMSXTimeoutError(RealMSXError):
    pass


class RealMSXCancelledError(RealMSXError):
    """A resumable transfer was cancelled by its caller."""


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

    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, socket_timeout=15,
                 file_transfer_state_directory=FILE_TRANSFER_STATE_DIR):
        self.host, self.port = host, int(port)
        self.socket_timeout = float(socket_timeout)
        self.file_transfer_state_directory = pathlib.Path(
            file_transfer_state_directory).expanduser()
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
        self.resident_entry = None
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
        self.bootstrap_feature_bits = 0
        self.bootstrap_features_known = False
        self.transfer_capabilities = None
        self.transfer_fast_capabilities = None
        self._fast_transfer_id = None
        self._debug_peer_sent = False
        self._snapshot_pause_owned = False
        self._v3 = None
        self._attachment_quarantine_reason = None
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

    def accept(self, timeout=60, handshake=True, cancelled=None):
        if self.srv is None:
            raise RealMSXError("listener is not open")
        if isinstance(timeout, bool):
            raise TypeError("accept timeout must be a number")
        timeout = float(timeout)
        if not math.isfinite(timeout) or not 0 < timeout <= 86400:
            raise ValueError("accept timeout must be finite and at most 86400 seconds")
        deadline = time.monotonic() + timeout
        while True:
            if cancelled is not None and cancelled():
                raise RealMSXCancelledError(
                    "MSX agent listener cancelled before a connection arrived")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RealMSXTimeoutError("timeout waiting for an MSX agent connection")
            self.srv.settimeout(min(0.1, remaining))
            try:
                conn, peer = self.srv.accept()
                break
            except socket.timeout:
                continue
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
        if not 1 <= target_port <= 65535:
            raise ValueError("port must be in range 1..65535")
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
        same_stream = self.conn is stream
        if self.conn is not None and not same_stream:
            raise RealMSXError("an MSX agent stream is already attached")
        if same_stream:
            # Materialize a session-local quarantine before protocol reset
            # discards the V3Session that owns its original reason.
            self.write_quarantined
        self.conn = stream
        if not same_stream:
            # Object identity is the only transport-neutral proof available
            # here that this attachment cannot be the quarantined byte stream.
            self._attachment_quarantine_reason = None
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
        self.resident_entry = None
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
        self.bootstrap_feature_bits = 0
        self.bootstrap_features_known = False
        self.transfer_capabilities = None
        self.transfer_fast_capabilities = None
        self._fast_transfer_id = None
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
        if self.write_quarantined:
            raise RealMSXProtocolError(
                "cannot write to a write-quarantined attachment; close it "
                "and attach a fresh stream")
        try:
            self.conn.sendall(data)
        except OSError as exc:
            raise RealMSXError(f"agent send failed: {exc}") from exc

    def _send_reconnect_escape(self):
        """Send one framed reset marker using negotiated byte credits."""
        if self.write_quarantined:
            raise RealMSXProtocolError(
                "cannot reconnect a write-quarantined attachment")
        credited = bool(self.feature_bits & FEATURE_FRAME_WAKE_ACK)
        if credited:
            original_timeout = self.conn.gettimeout()
            marker_started = False
            try:
                try:
                    self.conn.settimeout(min(
                        self.socket_timeout, FRAME_WAKE_BOOTSTRAP_TIMEOUT))
                    for marker_byte in RECONNECT_ESCAPE:
                        # sendall() may transfer an unknown prefix before it
                        # reports failure, so mark the stream indeterminate
                        # before the first write is attempted.
                        marker_started = True
                        self._send(bytes([marker_byte]))
                        ack = self._recv_exact(1)
                        if ack != FRAME_WAKE_ACK:
                            raise RealMSXProtocolError(
                                "agent did not credit a reconnect byte: "
                                f"{ack!r}")
                finally:
                    self.conn.settimeout(original_timeout)
            except BaseException as exc:
                if marker_started:
                    self._quarantine_attachment_writes(exc)
                raise
        elif self.agent_transport_id in (None, 0):
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
        if self.write_quarantined:
            raise RealMSXProtocolError(
                "cannot write to a write-quarantined attachment; close it "
                "and attach a fresh stream")
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

    @property
    def write_quarantined(self):
        """Whether writes are suppressed after an indeterminate link failure."""

        with self._lock:
            if self._attachment_quarantine_reason is not None:
                return True
            session = self._v3
            if (session is not None and
                    getattr(session, "write_quarantined", False)):
                reason = getattr(session, "quarantine_reason", None)
                self._attachment_quarantine_reason = (
                    reason if reason is not None else
                    "indeterminate framed transport failure")
                return True
            return False

    @property
    def quarantine_reason(self):
        """Return the attachment-level write-quarantine reason, if present."""

        self.write_quarantined
        with self._lock:
            return self._attachment_quarantine_reason

    def _quarantine_attachment_writes(self, reason):
        """Permanently suppress writes on the current attached byte stream."""

        with self._lock:
            if self._attachment_quarantine_reason is None:
                self._attachment_quarantine_reason = reason

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
        """Discover raw/framed state without releasing an uncredited burst.

        Raw agents reject the first ESC with ``E,1`` and are then queried with
        ``?``. A current framed agent credits each of all eight ESC bytes and
        emits its raw HELLO. Silence stops the probe after that single byte;
        legacy framed recovery is intentionally not attempted automatically.
        """
        self._require_connection()
        original_timeout = self.conn.gettimeout()
        probe_timeout = min(self.socket_timeout, BOOTSTRAP_PROBE_TIMEOUT)
        probe_started = False
        try:
            try:
                self.conn.settimeout(probe_timeout)
                # A send failure can still mean that the peer consumed this
                # byte, so the attachment becomes indeterminate from here on.
                probe_started = True
                self._send(RECONNECT_ESCAPE[:1])
                first = self._recv_exact(1)
                if first == b"E":
                    error_code = self._recv_exact(1)
                    if error_code != b"\x01":
                        raise RealMSXProtocolError(
                            "unexpected raw bootstrap probe rejection: "
                            f"E{error_code.hex()}")
                    self._send(b"?")
                    self.bootstrap_recovered = False
                    return self._scan_bootstrap_hello(probe_timeout)
                if first != FRAME_WAKE_ACK:
                    raise RealMSXProtocolError(
                        "unexpected safe bootstrap probe response: "
                        f"{first!r}")
                for marker_byte in RECONNECT_ESCAPE[1:]:
                    self._send(bytes([marker_byte]))
                    ack = self._recv_exact(1)
                    if ack != FRAME_WAKE_ACK:
                        raise RealMSXProtocolError(
                            "agent did not credit a bootstrap reconnect byte: "
                            f"{ack!r}")
                self.bootstrap_recovered = True
                return self._scan_bootstrap_hello(probe_timeout)
            except RealMSXTimeoutError as exc:
                raise RealMSXTimeoutError(
                    "safe bootstrap probe timed out; no additional uncredited "
                    "bytes were sent. Restart or update a legacy framed agent") \
                    from exc
            finally:
                self.conn.settimeout(original_timeout)
        except BaseException as exc:
            if probe_started:
                self._quarantine_attachment_writes(exc)
            raise

    def _query_bootstrap_features(self):
        """Negotiate safety flags in raw mode before the first v3 frame."""

        self._send(b"N")
        reply = self._recv_exact(2)
        if reply[:1] == b"K":
            self.bootstrap_features_known = True
            self.bootstrap_feature_bits = reply[1]
        elif reply == b"E\x01":
            self.bootstrap_features_known = False
            self.bootstrap_feature_bits = 0
        else:
            raise RealMSXProtocolError(
                f"invalid bootstrap feature response: {reply!r}")
        self.feature_bits = self.bootstrap_feature_bits
        return self.bootstrap_feature_bits

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
        self.resident_entry = None
        if capabilities & 0x20:
            bootstrap_features = self._query_bootstrap_features()
            bootstrap_safe = bool(
                bootstrap_features & FEATURE_TIMI_POLL_SAFE)
            bootstrap_ack = bool(
                bootstrap_features & FEATURE_FRAME_WAKE_ACK)
            bootstrap_resident = not bool(capabilities & CAPABILITY_RUN)
            if bootstrap_safe and not bootstrap_resident:
                raise RealMSXProtocolError(
                    "agent advertised timi-poll-safe outside resident mode")
            if bootstrap_safe and not bootstrap_ack:
                raise RealMSXProtocolError(
                    "timi-poll-safe requires frame-wake-ack during bootstrap")
            if bootstrap_resident and not bootstrap_safe:
                raise RealMSXProtocolError(
                    "resident agent lacks safe pre-v3 negotiation; update "
                    "MSXAI.COM, uninstall the old TSR and install it again")
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
            self._v3 = V3Session(
                self.conn, timeout=session_timeout,
                retries=(0 if bootstrap_safe else 2),
                max_payload=4096, peer_max_payload=peer_max,
                # Current residents negotiate this ACK before F, so their
                # first framed HELLO is strict and never retransmitted.
                frame_wake_ack=FRAME_WAKE_ACK,
                frame_wake_ack_optional=not bootstrap_ack,
                frame_wake_ack_timeout=FRAME_WAKE_BOOTSTRAP_TIMEOUT,
                quarantine_on_transport_failure=bootstrap_safe)
            return self._info_v3()
        return {
            "protocol": version,
            "capabilities": [name for bit, name in CAPABILITY_NAMES.items()
                             if capabilities & bit],
            "capability_bits": capabilities,
            "resident_base": self.resident_base,
            "resident_entry": self.resident_entry,
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
            if self.write_quarantined:
                raise RealMSXProtocolError(
                    "cannot rebootstrap a write-quarantined attachment; "
                    "attach a fresh stream")
            original_timeout = self.conn.gettimeout()
            probe_timeout = min(self.socket_timeout, BOOTSTRAP_PROBE_TIMEOUT)
            try:
                self._drain_recovery_noise()
                # Once the marker is attempted, the old framed parser can no
                # longer be trusted even if the automatic raw HELLO is lost.
                self._fast_transfer_id = None
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
        self.resident_entry = (
            self.resident_base | reply[15] if len(reply) >= 16 else None)
        if self.bootstrap_features_known:
            safety_mask = FEATURE_FRAME_WAKE_ACK | FEATURE_TIMI_POLL_SAFE
            bootstrap_safety = self.bootstrap_feature_bits & safety_mask
            framed_safety = self.feature_bits & safety_mask
            if framed_safety != bootstrap_safety:
                error = RealMSXProtocolError(
                    "agent changed its safety features during the v3 "
                    "upgrade: bootstrap=0x"
                    f"{bootstrap_safety:02X}, framed=0x{framed_safety:02X}")
                self._quarantine_attachment_writes(error)
                raise error
        timi_poll_safe = bool(
            self.feature_bits & FEATURE_TIMI_POLL_SAFE)
        if timi_poll_safe and self.runtime_mode != "resident":
            error = RealMSXProtocolError(
                "agent advertised timi-poll-safe outside resident mode")
            self._quarantine_attachment_writes(error)
            raise error
        if (timi_poll_safe and not
                self.feature_bits & FEATURE_FRAME_WAKE_ACK):
            error = RealMSXProtocolError(
                "timi-poll-safe requires the frame-wake-ack feature")
            self._quarantine_attachment_writes(error)
            raise error
        if self.feature_bits & FEATURE_FRAME_WAKE_ACK:
            self._v3.frame_wake_ack = FRAME_WAKE_ACK
            self._v3.frame_wake_ack_optional = False
            self._v3.frame_wake_delay = 0.0
        else:
            self._v3.frame_wake_ack = None
            self._v3.frame_wake_ack_optional = False
            self._v3.frame_wake_delay = (
                UART8251_FRAME_WAKE_DELAY if transport == 0 else 0.0)
        # A safe resident request is attempted exactly once. If that attempt
        # expires, the stream is quarantined so a retry cannot re-enter a
        # target whose mapping or interrupt state is no longer known.
        self._v3.retries = 0 if timi_poll_safe else 2
        self._v3.quarantine_on_transport_failure = timi_poll_safe
        self._send_debug_peer_label()
        return {
            "protocol": version,
            "bootstrap_protocol": self.bootstrap_protocol_version,
            "capabilities": [name for bit, name in CAPABILITY_NAMES.items()
                             if capabilities & bit],
            "capability_bits": capabilities,
            "resident_base": self.resident_base,
            "resident_entry": self.resident_entry,
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

    def cpu_snapshot(self):
        """Capture the negotiated Z80 context without a raw-v2 fallback.

        The physical-agent contract is the register frame saved at BIOS
        H.TIMI callback entry. It deliberately does not claim to be an
        arbitrary application instruction boundary; the decoder preserves
        service-only stack/return values under debug metadata.
        """
        if self._v3 is None:
            raise RealMSXError(
                "CPU snapshots require the framed-v3 agent protocol")
        if not self.feature_bits & FEATURE_CPU_SNAPSHOT:
            raise RealMSXError(
                "the connected agent does not advertise cpu-snapshot-v1; "
                "install the current MSXAI.COM suite")
        with self._lock:
            reply = self._request_v3(
                "D", bytes([CPU_CONTEXT_VERSION]))
        try:
            return parse_agent_cpu_context(reply)
        except CPUSnapshotError as exc:
            raise RealMSXProtocolError(
                f"invalid CPU snapshot response: {exc}") from exc

    def pause(self):
        with self._lock:
            if (self.runtime_mode == "resident" and
                    self.feature_bits & FEATURE_TIMI_POLL_SAFE):
                raise RealMSXError(
                    "persistent manual pause is disabled for the safe "
                    "resident; use an atomic snapshot operation with its "
                    "bounded lease")
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
        if self.write_quarantined:
            # Sending g/status/reconnect bytes after an indeterminate timeout
            # is precisely the unsafe retry pattern this profile forbids. The
            # target-side lease is the authoritative recovery mechanism.
            self._snapshot_pause_owned = False
            reason = getattr(self._v3, "quarantine_reason", "timeout")
            raise RealMSXError(
                "snapshot session timed out and was write-quarantined; no "
                "cleanup bytes were sent, and the bounded agent lease will "
                f"resume the MSX automatically ({reason})")
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

        if self.write_quarantined:
            # The direct g may itself have timed out and activated quarantine.
            # Do not follow it with the old reconnect/status recovery sequence.
            self._snapshot_pause_owned = False
            reason = getattr(self._v3, "quarantine_reason", direct_error)
            raise RealMSXError(
                "snapshot resume timed out and was write-quarantined; no "
                "recovery bytes were sent, and the bounded agent lease will "
                f"resume the MSX automatically ({reason})") from direct_error

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
                if self.write_quarantined:
                    self._snapshot_pause_owned = False
                    reason = getattr(
                        self._v3, "quarantine_reason", recovery_error)
                    raise RealMSXError(
                        "snapshot recovery became write-quarantined; no "
                        "further recovery bytes were sent, and the bounded "
                        "agent lease will resume the MSX automatically "
                        f"({reason})") from recovery_error

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
            if (self.runtime_mode == "resident" and not
                    self.feature_bits & FEATURE_TIMI_POLL_SAFE):
                raise RealMSXError(
                    "atomic capture of running resident software requires the "
                    "timi-poll-safe feature; update MSXAI.COM before taking "
                    "an in-game screenshot")

            # Bound even the initial status probe. This happens before the
            # target is paused, so an absent peer must return control promptly.
            original_status_timeout = self._v3.timeout
            self._v3.timeout = min(
                original_status_timeout, SNAPSHOT_REQUEST_TIMEOUT)
            try:
                state = self.status()["state"]
            finally:
                self._v3.timeout = original_status_timeout
                if self.conn is not None:
                    try:
                        self.conn.settimeout(original_status_timeout)
                    except (OSError, ValueError):
                        pass
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

    def keybuf_spool_write(self, data, timeout=None, *, pump=False,
                           cancel=False):
        """Query or update the resident keyboard spool.

        ``pump`` authorizes one logical line. ``cancel`` atomically discards
        the MCP spool and BIOS queue and cannot carry data. Return
        ``(accepted, pending, credits, barrier, active, authorized)``.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("keyboard data must be bytes-like")
        data = bytes(data)
        if not isinstance(pump, bool) or not isinstance(cancel, bool):
            raise TypeError("pump and cancel must be booleans")
        if cancel and (pump or data):
            raise ValueError("keyboard spool cancellation must be sent alone")
        if len(data) > KEYBUF_SPOOL_CAPACITY:
            raise RealMSXRangeError(
                f"keyboard spool batch exceeds {KEYBUF_SPOOL_CAPACITY} bytes")
        if self._v3 is None or not self.feature_bits & FEATURE_KEYBUF_SPOOL:
            raise RealMSXError(
                "resident keyboard spool was not negotiated")
        control = ((KEYBUF_SPOOL_REQUEST_PUMP if pump else 0) |
                   (KEYBUF_SPOOL_REQUEST_CANCEL if cancel else 0))
        payload = b"" if not control and not data else bytes([control]) + data
        if len(payload) > self._v3.max_payload:
            raise RealMSXRangeError(
                "keyboard spool batch exceeds the negotiated frame payload")
        request_timeout = None
        if timeout is not None:
            timeout = float(timeout)
            if timeout <= 0:
                raise RealMSXKeyboardTimeoutError(
                    "keyboard input timeout expired before request")
            request_timeout = max(
                0.001, timeout / (self._v3.retries + 1))
        with self._lock:
            reply = self._request_v3("T", payload, timeout=request_timeout)
        if len(reply) != 7:
            raise RealMSXProtocolError(
                f"invalid keybuf spool response: {reply!r}")
        accepted = int.from_bytes(reply[0:2], "little")
        pending = int.from_bytes(reply[2:4], "little")
        credits = int.from_bytes(reply[4:6], "little")
        flags = reply[6]
        if (accepted > len(data) or pending > KEYBUF_SPOOL_MAX_PENDING or
                credits > KEYBUF_SPOOL_CAPACITY or
                flags & ~(KEYBUF_SPOOL_FLAG_BARRIER |
                          KEYBUF_SPOOL_FLAG_ACTIVE |
                          KEYBUF_SPOOL_FLAG_AUTHORIZED)):
            raise RealMSXProtocolError(
                "invalid keybuf spool counts "
                f"accepted={accepted}, pending={pending}, credits={credits}, "
                f"flags=0x{flags:02X}")
        return (accepted, pending, credits,
                bool(flags & KEYBUF_SPOOL_FLAG_BARRIER),
                bool(flags & KEYBUF_SPOOL_FLAG_ACTIVE),
                bool(flags & KEYBUF_SPOOL_FLAG_AUTHORIZED))

    def cancel_keybuf_spool(self, timeout=None):
        """Discard pending MCP keyboard input after an interrupted operation."""
        return self.keybuf_spool_write(
            b"", timeout=timeout, cancel=True)

    def wait_keybuf_empty(self, timeout=KEYBUF_INPUT_TIMEOUT):
        deadline = time.monotonic() + float(timeout)
        previous = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RealMSXKeyboardTimeoutError(
                    "timeout waiting for software to consume BIOS keyboard input; "
                    "the target may read the key matrix directly")
            if (self._v3 is not None and
                    self.feature_bits & FEATURE_KEYBUF_SPOOL):
                (_accepted, pending, credits, barrier, active,
                 authorized) = self.keybuf_spool_write(
                    b"", timeout=remaining)
                state = (pending, credits, barrier, active, authorized)
                if (pending == 0 and not barrier and not active and
                        not authorized):
                    return
            else:
                _accepted, pending = self.keybuf_write(
                    b"", timeout=remaining)
                state = (pending,)
                if pending == 0:
                    return
            if previous is not None:
                progressed = (state[0] < previous[0])
                if len(state) > 1:
                    progressed = (progressed or state[1] > previous[1] or
                                  (previous[2] and not state[2]) or
                                  (previous[3] and not state[3]) or
                                  (previous[4] and not state[4]))
                if progressed:
                    deadline = time.monotonic() + float(timeout)
            previous = state
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
        """Type text and wait until it is consumed.

        ``timeout`` is a no-progress timeout, not a total-program deadline.
        New residents accept 255-byte frames into a private credit-controlled
        spool. Older residents are streamed directly into free BIOS-ring slots
        while retaining a hard drain barrier at every Return.
        """
        payload = self._encode_keyboard_text(text)
        timeout = float(timeout)
        if timeout <= 0:
            raise RealMSXKeyboardTimeoutError(
                "keyboard input timeout must be positive")
        if not payload:
            return 0
        if (self._v3 is not None and
                self.feature_bits & FEATURE_KEYBUF_SPOOL):
            return self._type_spooled(payload, timeout)

        offset = 0
        while offset < len(payload):
            remaining = payload[offset:]
            carriage_return = remaining.find(b"\r")
            if carriage_return >= 0:
                batch = remaining[:carriage_return + 1]
            else:
                batch = remaining
            batch_offset = 0
            credits = KEYBUF_CAPACITY
            deadline = time.monotonic() + timeout
            previous_pending = None
            while batch_offset < len(batch):
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise RealMSXKeyboardTimeoutError(
                        "timeout while typing through the BIOS keyboard buffer")
                if credits:
                    request = batch[
                        batch_offset:batch_offset + min(
                            credits, KEYBUF_CAPACITY)]
                else:
                    request = b""
                accepted, pending = self.keybuf_write(
                    request, timeout=remaining_time)
                batch_offset += accepted
                credits = KEYBUF_CAPACITY - pending
                if (accepted or previous_pending is not None and
                        pending < previous_pending):
                    deadline = time.monotonic() + timeout
                previous_pending = pending
                if batch_offset < len(batch) and not credits:
                    time.sleep(KEYBUF_POLL_INTERVAL)
            self.wait_keybuf_empty(timeout)
            offset += len(batch)
            if batch.endswith(b"\r"):
                time.sleep(KEYBUF_LINE_SETTLE)
        return len(payload)

    def _type_spooled(self, payload, timeout):
        deadline = time.monotonic() + timeout
        offset = 0
        credits = KEYBUF_SPOOL_CAPACITY
        pending = 0
        barrier = False
        active = False
        authorized = False
        previous = None
        max_batch = min(KEYBUF_SPOOL_CAPACITY, self._v3.max_payload - 1)
        if max_batch <= 0:
            raise RealMSXProtocolError(
                "negotiated frame payload cannot carry keyboard spool data")
        try:
            while True:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise RealMSXKeyboardTimeoutError(
                        "timeout while waiting for resident keyboard-spool "
                        "progress")
                bytes_left = len(payload) - offset
                refill_goal = min(bytes_left, KEYBUF_SPOOL_REFILL_TARGET)
                if (bytes_left and credits and
                        (previous is None or credits >= refill_goal)):
                    request = payload[
                        offset:offset + min(credits, max_batch)]
                else:
                    request = b""
                pump = (not barrier and not active and not authorized and
                        bool(pending or request))
                (accepted, pending, credits, barrier, active,
                 authorized) = self.keybuf_spool_write(
                    request, timeout=remaining_time, pump=pump)
                offset += accepted
                state = (pending, credits, barrier, active, authorized)
                progressed = bool(accepted)
                if previous is not None:
                    progressed = (
                        progressed or pending < previous[0] or
                        credits > previous[1] or
                        (previous[2] and not barrier) or
                        (not previous[3] and active) or
                        (previous[3] and not active) or
                        (not previous[4] and authorized) or
                        (previous[4] and not authorized))
                if progressed:
                    deadline = time.monotonic() + timeout
                previous = state
                if (offset == len(payload) and pending == 0 and not barrier and
                        not active and not authorized):
                    return len(payload)
                if not accepted:
                    time.sleep(KEYBUF_POLL_INTERVAL)
        except BaseException:
            # A timed-out caller must not leave complete future commands queued
            # for execution when BASIC/DOS later resumes consuming KEYBUF.
            try:
                self.cancel_keybuf_spool(timeout=min(timeout, 0.5))
            except Exception:
                pass
            raise

    def type_lines(self, lines, timeout=KEYBUF_INPUT_TIMEOUT):
        """Type several logical lines in one operation, each with Return."""
        if isinstance(lines, (str, bytes, bytearray)):
            raise TypeError("lines must be an iterable of strings")
        try:
            lines = tuple(lines)
        except TypeError as exc:
            raise TypeError("lines must be an iterable of strings") from exc
        for line in lines:
            if not isinstance(line, str):
                raise TypeError("each line must be a string")
            if "\r" in line or "\n" in line:
                raise ValueError("each item must contain exactly one logical line")
        if not lines:
            return 0
        return self.type("\r".join(lines) + "\r", timeout=timeout)

    # ---- resumable DOS file transfer ---------------------------------
    def _require_file_transfer_v2(self):
        if self.runtime_mode != "resident":
            raise RealMSXError(
                "DOS file transfer requires the resident agent")
        if self._v3 is None or not self.feature_bits & FEATURE_FILE_TRANSFER:
            raise RealMSXError(
                "the resident does not advertise file-transfer-v2")

    @staticmethod
    def _parse_transfer_reply(parser, payload, *args, **kwargs):
        try:
            return parser(payload, *args, **kwargs)
        except TransferError as exc:
            raise RealMSXProtocolError(
                f"invalid file-transfer-v2 response: {exc}") from exc

    def file_transfer_capabilities(self, *, refresh=False):
        """Return the separately negotiated opcode-X transfer limits."""
        self._require_file_transfer_v2()
        if self.transfer_capabilities is not None and not refresh:
            return self.transfer_capabilities
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE, encode_capabilities_request())
        capabilities = self._parse_transfer_reply(
            parse_capabilities_reply, payload)
        self.transfer_capabilities = capabilities
        return capabilities

    def file_transfer_fast_capabilities(self, *, refresh=False):
        """Return the required fast-v1 stream-pump limits."""
        self._require_file_transfer_v2()
        if self.transfer_fast_capabilities is not None and not refresh:
            return self.transfer_fast_capabilities
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE, encode_fast_capabilities_request())
        capabilities = self._parse_transfer_reply(
            parse_fast_capabilities_reply, payload)
        self.transfer_fast_capabilities = capabilities
        return capabilities

    def _require_file_transfer_fast_pump(self):
        capabilities = self.file_transfer_fast_capabilities()
        required_capabilities = (
            TransferFastCapability.PUMP | TransferFastCapability.STREAM)
        if capabilities.capabilities & required_capabilities != required_capabilities:
            raise RealMSXError(
                "the resident does not advertise the redesigned fast-v1 "
                "stream pump")
        local_max = getattr(self._v3, "local_max_payload", None)
        required = max(
            21 + capabilities.max_put_chunk,
            8 + capabilities.max_get_chunk)
        if local_max is None or required > local_max:
            raise RealMSXRangeError(
                f"fast-v1 requires a {required}-byte framed payload; host "
                f"maximum is {local_max if local_max is not None else 'unknown'}")
        return capabilities

    def _file_transfer_request_timeout(self, transfer_id=None):
        """Use a wire-sized deadline only while one fast transfer is armed."""
        if transfer_id is None or self._fast_transfer_id != bytes(transfer_id):
            return None
        session_timeout = getattr(self._v3, "timeout", 0.0)
        return max(FILE_TRANSFER_FAST_FRAME_TIMEOUT, float(session_timeout))

    @contextmanager
    def _file_transfer_fast_payload_scope(self, required):
        """Temporarily admit one large fast-v1 request/response frame."""
        session = self._v3
        if session is None or not all(hasattr(session, name) for name in (
                "local_max_payload", "peer_max_payload",
                "negotiate_max_payload")):
            raise RealMSXProtocolError(
                "fast-v1 requires a framed-v3 session with payload negotiation")
        required = int(required)
        if required <= 0 or required > session.local_max_payload:
            raise RealMSXRangeError(
                f"fast-v1 frame requires {required} payload bytes; host "
                f"maximum is {session.local_max_payload}")
        previous = session.peer_max_payload
        effective = session.negotiate_max_payload(required)
        if effective < required:
            session.negotiate_max_payload(previous)
            raise RealMSXRangeError(
                f"fast-v1 frame requires {required} payload bytes; "
                f"negotiated maximum is {effective}")
        try:
            yield
        finally:
            session.negotiate_max_payload(previous)

    def file_transfer_fast_begin(self, transfer_id):
        """Arm synchronous pumping after MSXAIXF has published READY."""
        self._require_file_transfer_fast_pump()
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE, encode_fast_begin(transfer_id),
                timeout=FILE_TRANSFER_FAST_FRAME_TIMEOUT)
            reply = self._parse_transfer_reply(parse_open_reply, payload)
            if (reply.error is TransferRemoteError.NONE and
                    reply.state in (TransferState.READY,
                                    TransferState.TRANSFERRING)):
                self._fast_transfer_id = bytes(transfer_id)
            return reply

    def file_transfer_open(self, descriptor):
        """Stage an immutable descriptor before launching MSXAIXF.COM."""
        if not isinstance(descriptor, TransferDescriptor):
            raise TypeError("descriptor must be a TransferDescriptor")
        capabilities = self.file_transfer_capabilities()
        required = (TransferCapability.RAW | TransferCapability.CRC32 |
                    (TransferCapability.PUT
                     if descriptor.direction is TransferDirection.PUT
                     else TransferCapability.GET))
        if capabilities.capabilities & required != required:
            raise RealMSXError(
                "the resident lacks required raw/CRC32 file-transfer "
                "capabilities")
        if (descriptor.resume and not
                capabilities.capabilities & TransferCapability.RESUME):
            raise RealMSXError("the resident does not support transfer resume")
        if (descriptor.encoding is TransferEncoding.PACKBITS and not
                capabilities.capabilities & TransferCapability.PACKBITS_DECODE):
            raise RealMSXError(
                "the resident does not advertise PackBits decompression")
        self._require_file_transfer_fast_pump()
        path_length = len(descriptor.path.encode("ascii"))
        if path_length > capabilities.max_path:
            raise RealMSXRangeError(
                f"MSX path is {path_length} bytes; target maximum is "
                f"{capabilities.max_path}")
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE, encode_open(descriptor))
        return self._parse_transfer_reply(parse_open_reply, payload)

    def file_transfer_status(self, transfer_id):
        self._require_file_transfer_v2()
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE, encode_status(transfer_id),
                timeout=self._file_transfer_request_timeout(transfer_id))
        reply = self._parse_transfer_reply(
            parse_status_reply, payload,
            expected_transfer_id=bytes(transfer_id))
        if reply.state in (TransferState.COMPLETE, TransferState.FAILED,
                           TransferState.CANCELLED):
            if self._fast_transfer_id == bytes(transfer_id):
                self._fast_transfer_id = None
        return reply

    def file_transfer_put_data(self, transfer_id, offset, data):
        self._require_file_transfer_v2()
        capabilities = self._require_file_transfer_fast_pump()
        if len(data) > capabilities.max_put_chunk:
            raise RealMSXRangeError(
                f"PUT block is {len(data)} bytes; target maximum is "
                f"{capabilities.max_put_chunk}")
        request = encode_put_data(transfer_id, offset, data)
        with self._lock:
            if self._fast_transfer_id != bytes(transfer_id):
                raise RealMSXError("fast-v1 PUT pump is not armed")
            with self._file_transfer_fast_payload_scope(len(request)):
                payload = self._request_v3(
                    TRANSFER_OPCODE, request,
                    timeout=self._file_transfer_request_timeout(transfer_id))
        return self._parse_transfer_reply(parse_put_data_reply, payload)

    def file_transfer_get_read(self, transfer_id, offset, maximum):
        self._require_file_transfer_v2()
        capabilities = self._require_file_transfer_fast_pump()
        maximum = min(int(maximum), capabilities.max_get_chunk)
        request = encode_get_read(transfer_id, offset, maximum)
        with self._lock:
            if self._fast_transfer_id != bytes(transfer_id):
                raise RealMSXError("fast-v1 GET pump is not armed")
            with self._file_transfer_fast_payload_scope(8 + maximum):
                payload = self._request_v3(
                    TRANSFER_OPCODE, request,
                    timeout=self._file_transfer_request_timeout(transfer_id))
        return self._parse_transfer_reply(parse_get_read_reply, payload)

    def file_transfer_get_ack(self, transfer_id, next_offset, prefix_crc32):
        self._require_file_transfer_v2()
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE,
                encode_get_ack(transfer_id, next_offset, prefix_crc32),
                timeout=self._file_transfer_request_timeout(transfer_id))
        return self._parse_transfer_reply(parse_get_ack_reply, payload)

    def file_transfer_close(self, transfer_id, *, rate_bps):
        self._require_file_transfer_v2()
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE,
                encode_close(transfer_id, rate_bps=rate_bps),
                timeout=self._file_transfer_request_timeout(transfer_id))
        return self._parse_transfer_reply(parse_close_reply, payload)

    def file_transfer_cancel(self, transfer_id):
        self._require_file_transfer_v2()
        with self._lock:
            payload = self._request_v3(
                TRANSFER_OPCODE, encode_cancel(transfer_id),
                timeout=self._file_transfer_request_timeout(transfer_id))
        reply = self._parse_transfer_reply(parse_cancel_reply, payload)
        if self._fast_transfer_id == bytes(transfer_id):
            self._fast_transfer_id = None
        return reply

    @staticmethod
    def _raise_transfer_remote(reply, operation):
        error = getattr(reply, "error", TransferRemoteError.NONE)
        state = getattr(reply, "state", None)
        if error is not TransferRemoteError.NONE:
            raise RealMSXError(
                f"MSX {operation} failed: {error.name.lower()} "
                f"(state {state.name.lower() if state is not None else 'unknown'})")
        if state in (TransferState.FAILED, TransferState.CANCELLED):
            raise RealMSXError(
                f"MSX {operation} entered terminal state {state.name.lower()}")

    @staticmethod
    def _validate_progress_timeout(timeout):
        if isinstance(timeout, bool):
            raise TypeError("transfer timeout must be a number")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise TypeError("transfer timeout must be a number") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("transfer timeout must be a positive finite number")
        return timeout

    @staticmethod
    def _report_file_transfer(progress, completed, total, message):
        if progress is not None:
            try:
                progress(
                    float(completed),
                    None if total is None else float(total),
                    message)
            except Exception:
                # Progress is observation only. A broken UI/client callback
                # cannot be allowed to strand a foreground DOS helper.
                pass

    @classmethod
    def _host_transfer_progress(cls, progress, *, completed, total, label):
        """Adapt local work to the monotonic byte-transfer progress stream.

        Preparation and prefix hashing can traverse the source more than once.
        Their own byte counters therefore live in the descriptive message,
        while the public completed value stays at the next wire boundary and
        never moves backwards when the actual transfer begins.
        """
        if progress is None:
            return None

        def report(local_completed, local_total, message):
            if message is None:
                denominator = ("?" if local_total is None else
                               str(int(local_total)))
                message = f"{int(local_completed)}/{denominator} bytes"
            cls._report_file_transfer(
                progress, completed, total, f"{label}: {message}")

        return report

    @staticmethod
    def _raise_pre_open_transfer_cancelled(exc):
        raise RealMSXCancelledError(
            "MSX file transfer cancelled during local preparation before "
            "the remote worker opened") from exc

    def _raise_active_local_transfer_cancelled(self, transfer_id, exc):
        try:
            self._cancel_file_transfer_if_requested(
                transfer_id, lambda: True)
        except RealMSXCancelledError as cancelled_exc:
            raise cancelled_exc from exc
        raise AssertionError("forced transfer cancellation did not raise")

    def _cancel_file_transfer_if_requested(self, transfer_id, cancelled):
        if cancelled is None or not cancelled():
            return
        detail = ""
        try:
            self.file_transfer_cancel(transfer_id)
        except Exception as exc:
            # Preserve cancellation as the primary outcome. The durable host
            # journal still makes a later reconnect/resume fail closed even if
            # the best-effort remote CANCEL could not cross a broken link.
            detail = f"; remote CANCEL could not be confirmed: {exc}"
        raise RealMSXCancelledError(
            "MSX file transfer cancelled; durable resume state was preserved"
            + detail)

    @staticmethod
    def _cancel_before_open_if_requested(cancelled):
        if cancelled is not None and cancelled():
            raise RealMSXCancelledError(
                "MSX file transfer cancelled before the remote worker opened")

    def _wait_file_transfer(self, transfer_id, states, *, timeout,
                            previous=None, progress=None,
                            progress_total=None, direction="transfer",
                            cancelled=None):
        """Poll STATUS until one of ``states`` with a no-progress deadline."""
        states = frozenset(states)
        deadline = time.monotonic() + timeout
        marker = previous
        while True:
            self._cancel_file_transfer_if_requested(transfer_id, cancelled)
            status = self.file_transfer_status(transfer_id)
            self._raise_transfer_remote(status, "file transfer")
            current = (
                status.state, status.durable_offset, status.accepted_offset,
                status.prefix_crc32, status.credit, status.flags)
            if current != marker:
                marker = current
                deadline = time.monotonic() + timeout
                if progress_total is not None:
                    self._report_file_transfer(
                        progress, status.durable_offset, progress_total,
                        f"{direction}: {status.state.name.lower()} "
                        f"({status.durable_offset}/{progress_total} durable bytes)")
            if status.state in states:
                return status
            if status.state is TransferState.SUSPENDED:
                raise RealMSXTimeoutError(
                    "MSX file transfer was suspended; invoke it again with "
                    "resume enabled")
            if time.monotonic() >= deadline:
                raise RealMSXTimeoutError(
                    "timeout waiting for MSX file-transfer progress")
            time.sleep(FILE_TRANSFER_POLL_INTERVAL)

    @staticmethod
    def _validate_transfer_binding(status, descriptor):
        if status.transfer_id != descriptor.transfer_id:
            raise RealMSXProtocolError("MSX transfer ID changed after OPEN")
        if (status.direction is not descriptor.direction or
                status.encoding is not descriptor.encoding):
            raise RealMSXProtocolError(
                "MSX transfer direction or encoding changed after OPEN")
        unknown_get = (
            descriptor.direction is TransferDirection.GET and
            descriptor.wire_size == descriptor.wire_crc32 ==
            descriptor.final_size == descriptor.final_crc32 == 0)
        if not unknown_get and (
                status.wire_size != descriptor.wire_size or
                status.wire_crc32 != descriptor.wire_crc32 or
                status.final_size != descriptor.final_size or
                status.final_crc32 != descriptor.final_crc32):
            raise RealMSXProtocolError(
                "MSX transfer metadata differs from the staged descriptor")

    def _recover_active_transfer(self, descriptor):
        """Return a still-active journalled transfer, or ``None`` if absent.

        A restarted IDE may reconnect while the DOS foreground worker is still
        alive.  Reissuing OPEN in that state is intentionally rejected by the
        agent, so probe the unguessable journalled ID first and attach only
        after the complete wire binding has been validated.
        """
        try:
            status = self.file_transfer_status(descriptor.transfer_id)
        except RealMSXProtocolError:
            if self.write_quarantined:
                raise
            return None
        if status.state in (
                TransferState.IDLE, TransferState.FAILED,
                TransferState.CANCELLED):
            return None
        self._raise_transfer_remote(status, "recovered file transfer")
        self._validate_transfer_binding(status, descriptor)
        return status

    @staticmethod
    def _exact_local_write(stream, data):
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = stream.write(view[written:])
            if count is None:
                count = len(view) - written
            if not isinstance(count, int) or count <= 0:
                raise OSError("short local file write during MSX GET")
            written += count

    def put_file(self, source, target, *, compression="auto", resume=True,
                 existing_only=False,
                 state_directory=None,
                 timeout=FILE_TRANSFER_PROGRESS_TIMEOUT,
                 progress=None, cancelled=None):
        """Stream a local file to MSX-DOS with CRC-32 and durable resume.

        Compression is negotiated independently. ``auto`` falls back to raw
        when the connected target has no PackBits decoder; explicit ``packbits``
        fails instead of silently changing the requested representation.
        Existing archive/media formats selected by the planner remain raw.
        """
        timeout = self._validate_progress_timeout(timeout)
        self._cancel_before_open_if_requested(cancelled)
        if not isinstance(resume, bool):
            raise TypeError("resume must be a boolean")
        if not isinstance(existing_only, bool):
            raise TypeError("existing_only must be a boolean")
        source_path = pathlib.Path(source).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise ValueError(f"local PUT source is not a regular file: {source_path}")
        source_size = source_path.stat().st_size
        state_directory = pathlib.Path(
            self.file_transfer_state_directory
            if state_directory is None else state_directory).expanduser()
        capabilities = self.file_transfer_capabilities()
        transfer_limits = self._require_file_transfer_fast_pump()
        target_path = str(target)
        packbits_decode = bool(
            capabilities.capabilities & TransferCapability.PACKBITS_DECODE)
        if compression == "packbits":
            if not packbits_decode:
                raise RealMSXError(
                    "PackBits was requested but the MSX has no negotiated decoder")
        # Do not spend host CPU and temporary disk space producing a PackBits
        # stream that the target cannot consume. Explicit PackBits fails above;
        # automatic mode simply keeps the original byte stream as RAW.
        raw_fallback_reason = None
        if compression == "auto" and not packbits_decode:
            raw_fallback_reason = "target has no PackBits decoder"
        planning_mode = (
            "raw" if raw_fallback_reason is not None
            else compression)
        preparation_progress = self._host_transfer_progress(
            progress, completed=0, total=source_size,
            label="PUT preparation")
        try:
            basic_source = prepare_msx_basic_source(
                source_path, target_path, state_directory=state_directory,
                progress=preparation_progress, cancelled=cancelled)
        except TransferCancelledError as exc:
            self._raise_pre_open_transfer_cancelled(exc)
        prepared = None
        try:
            try:
                prepared = prepare_put_payload(
                    basic_source.transfer_path,
                    state_directory=state_directory, mode=planning_mode,
                    progress=preparation_progress, cancelled=cancelled)
            except TransferCancelledError as exc:
                self._raise_pre_open_transfer_cancelled(exc)
            self._cancel_before_open_if_requested(cancelled)
            caller_binding = str(source_path)
            candidate = TransferDescriptor(
                direction=TransferDirection.PUT,
                encoding=prepared.encoding,
                transfer_id=new_transfer_id(),
                wire_size=prepared.wire_digest.size,
                wire_crc32=prepared.wire_digest.crc32,
                final_size=prepared.final_digest.size,
                final_crc32=prepared.final_digest.crc32,
                path=target_path,
            )
            journal = TransferJournal(state_directory)
            record = (journal.find_matching(
                candidate, caller_binding=caller_binding) if resume else None)
            if existing_only and record is None:
                raise RealMSXError(
                    "no matching PUT journal exists for active-only recovery")
            if record is None:
                descriptor = candidate
                confirmed_offset = 0
                confirmed_crc = 0
                close_intent = False
            else:
                descriptor = record.resumed_descriptor()
                confirmed_offset = record.confirmed_offset
                confirmed_crc = record.prefix_crc32
                close_intent = record.close_intent

            try:
                local_prefix = crc32_file_prefix(
                    prepared.wire_path, confirmed_offset,
                    progress=self._host_transfer_progress(
                        progress, completed=0,
                        total=prepared.wire_digest.size,
                        label="PUT resume validation"),
                    cancelled=cancelled,
                    progress_phase="local PUT resume prefix CRC-32").crc32
            except TransferCancelledError as exc:
                self._raise_pre_open_transfer_cancelled(exc)
            if local_prefix != confirmed_crc:
                raise TransferBindingError(
                    "local PUT prefix no longer matches its resume journal")
            journal.save(
                descriptor, confirmed_offset=confirmed_offset,
                prefix_crc32=confirmed_crc,
                caller_binding=caller_binding,
                close_intent=close_intent)

            self._cancel_before_open_if_requested(cancelled)
            status = (self._recover_active_transfer(descriptor)
                      if record is not None else None)
            if existing_only and status is None:
                raise RealMSXError(
                    "the journalled PUT is no longer active on the MSX")
            if status is None:
                self._cancel_before_open_if_requested(cancelled)
                opened = self.file_transfer_open(descriptor)
                self._raise_transfer_remote(opened, "PUT open")
                if opened.state not in (TransferState.STAGED,
                                        TransferState.OPENING,
                                        TransferState.READY):
                    raise RealMSXProtocolError(
                        f"unexpected PUT OPEN state {opened.state.name.lower()}")
                status_state = opened.state
            else:
                status_state = status.state

            self._cancel_file_transfer_if_requested(
                descriptor.transfer_id, cancelled)
            if status_state is TransferState.STAGED:
                if existing_only:
                    raise RealMSXError(
                        "the recovered PUT is staged but no DOS prompt is "
                        "visible to launch it safely")
                self.type_line(
                    f"MSXAIXF /PUT {descriptor.transfer_id.hex().upper()}",
                    timeout=timeout)
                status = None
            if status is None or status.state is TransferState.OPENING:
                status = self._wait_file_transfer(
                    descriptor.transfer_id,
                    (TransferState.READY, TransferState.TRANSFERRING,
                     TransferState.VERIFYING, TransferState.POSTPROCESS,
                     TransferState.COMPLETE), timeout=timeout,
                    progress=progress, progress_total=descriptor.wire_size,
                    direction="PUT", cancelled=cancelled)
            self._validate_transfer_binding(status, descriptor)
            if status.state in (
                    TransferState.READY, TransferState.TRANSFERRING):
                armed = self.file_transfer_fast_begin(descriptor.transfer_id)
                self._raise_transfer_remote(armed, "fast PUT begin")

            with prepared.wire_path.open("rb") as stream:
                durable_offset = status.durable_offset
                try:
                    durable_crc = crc32_file_prefix(
                        prepared.wire_path, durable_offset,
                        progress=self._host_transfer_progress(
                            progress, completed=durable_offset,
                            total=descriptor.wire_size,
                            label="PUT recovery validation"),
                        cancelled=cancelled,
                        progress_phase="active PUT prefix CRC-32").crc32
                except TransferCancelledError as exc:
                    self._raise_active_local_transfer_cancelled(
                        descriptor.transfer_id, exc)
                if durable_crc != status.prefix_crc32:
                    raise TransferBindingError(
                        "MSX PUT partial prefix differs from the local source")
                accepted_offset = status.accepted_offset
                if not durable_offset <= accepted_offset <= descriptor.wire_size:
                    raise RealMSXProtocolError(
                        "MSX PUT reported invalid durable/accepted progress")
                if (accepted_offset - durable_offset >
                        FILE_TRANSFER_MAX_UNCOMMITTED):
                    raise RealMSXProtocolError(
                        "MSX PUT exceeded the bounded uncommitted window")
                # The helper releases each physical frame after retaining it
                # in the transient accumulator, and advances durability only
                # after the complete window is written and ENSUREd. Rebuild
                # that bounded gap once after reconnect; subsequent CRC
                # progress remains incremental.
                stream.seek(durable_offset)
                inflight = bytearray(
                    stream.read(accepted_offset - durable_offset))
                if len(inflight) != accepted_offset - durable_offset:
                    raise RealMSXProtocolError(
                        "local PUT source ended inside accepted progress")
                if durable_offset != confirmed_offset:
                    # The descriptor was fsync-journalled before OPEN. Avoid a
                    # byte-identical second journal at the common zero-offset
                    # READY boundary; only recovered remote progress needs a
                    # new durable host record here.
                    journal.save(
                        descriptor, confirmed_offset=durable_offset,
                        prefix_crc32=durable_crc,
                        caller_binding=caller_binding,
                        close_intent=close_intent)
                journal_checkpoint = durable_offset
                stream_start_offset = accepted_offset
                stream_started_at = time.monotonic()
                stream_completed_at = (
                    stream_started_at
                    if durable_offset == descriptor.wire_size else None)
                self._report_file_transfer(
                    progress, accepted_offset, descriptor.wire_size,
                    f"PUT: {accepted_offset}/{descriptor.wire_size} accepted bytes; "
                    f"{durable_offset} durable")

                def reconcile_durable(remote_offset, remote_crc=None):
                    nonlocal durable_offset, durable_crc, journal_checkpoint
                    nonlocal stream_completed_at
                    if not durable_offset <= remote_offset <= accepted_offset:
                        raise RealMSXProtocolError(
                            "MSX PUT durable offset diverged from host progress")
                    advance = remote_offset - durable_offset
                    if advance > len(inflight):
                        raise RealMSXProtocolError(
                            "MSX PUT committed bytes outside its accepted block")
                    if advance:
                        durable_crc = crc32_update(
                            memoryview(inflight)[:advance], durable_crc)
                        del inflight[:advance]
                        durable_offset = remote_offset
                        if (durable_offset == descriptor.wire_size or
                                durable_offset - journal_checkpoint >=
                                FILE_TRANSFER_JOURNAL_INTERVAL):
                            journal.save(
                                descriptor, confirmed_offset=durable_offset,
                                prefix_crc32=durable_crc,
                                caller_binding=caller_binding,
                                close_intent=close_intent)
                            journal_checkpoint = durable_offset
                        if (durable_offset == descriptor.wire_size and
                                stream_completed_at is None):
                            stream_completed_at = time.monotonic()
                        self._report_file_transfer(
                            progress, accepted_offset, descriptor.wire_size,
                            f"PUT: {accepted_offset}/{descriptor.wire_size} "
                            f"accepted bytes; {durable_offset} durable")
                    if remote_crc is not None and remote_crc != durable_crc:
                        raise TransferBindingError(
                            "MSX PUT rolling CRC differs from local source")

                def persist_close_intent():
                    """Authorize only a proven full-boundary terminal replay."""

                    nonlocal close_intent
                    if close_intent:
                        return
                    if (durable_offset != descriptor.wire_size or
                            accepted_offset != descriptor.wire_size or
                            durable_crc != descriptor.wire_crc32):
                        raise RealMSXProtocolError(
                            "cannot close PUT before its durable CRC boundary")
                    # This fsync-backed bit distinguishes a helper that reached
                    # READY and the complete durable boundary from a journal
                    # written before OPEN.  A later OPEN may use it to recover
                    # a successful publication whose sidecar and terminal
                    # response are both already gone.
                    journal.save(
                        descriptor, confirmed_offset=durable_offset,
                        prefix_crc32=durable_crc,
                        caller_binding=caller_binding,
                        close_intent=True)
                    close_intent = True

                def stream_rate_hint():
                    if stream_completed_at is None:
                        return 0
                    elapsed = stream_completed_at - stream_started_at
                    count = descriptor.wire_size - stream_start_offset
                    if elapsed <= 0 or count <= 0:
                        return 0
                    return min(0xFFFF, int(count / elapsed + 0.5))

                if (durable_offset == descriptor.wire_size and
                        accepted_offset == descriptor.wire_size):
                    persist_close_intent()

                close_sent = False
                progress_marker = (
                    status.state, status.durable_offset,
                    status.accepted_offset, status.credit)
                progress_deadline = time.monotonic() + timeout

                def observe_put_progress(latest_status):
                    """Advance the no-progress deadline only on wire progress."""

                    nonlocal progress_marker, progress_deadline
                    current_marker = (
                        latest_status.state, latest_status.durable_offset,
                        latest_status.accepted_offset, latest_status.credit)
                    now = time.monotonic()
                    if current_marker != progress_marker:
                        progress_marker = current_marker
                        progress_deadline = now + timeout
                    elif now >= progress_deadline:
                        raise RealMSXTimeoutError(
                            "timeout waiting for durable MSX PUT progress")
                    if latest_status.state is TransferState.SUSPENDED:
                        raise RealMSXTimeoutError(
                            "MSX PUT was suspended; retry with resume enabled")

                while status.state is not TransferState.COMPLETE:
                    self._cancel_file_transfer_if_requested(
                        descriptor.transfer_id, cancelled)
                    if accepted_offset < descriptor.wire_size:
                        # The foreground pump consumes exactly one large frame,
                        # returns its response, writes that RAM block through
                        # DOS, and only then wakes the next frame. TCP ordering
                        # plus the one-byte frame-wake credit provides all
                        # pacing, so a STATUS transaction between blocks is
                        # redundant. Keep a bounded uncommitted window only for
                        # durable resume accounting.
                        available_window = (
                            FILE_TRANSFER_MAX_UNCOMMITTED -
                            (accepted_offset - durable_offset))
                        if available_window <= 0:
                            time.sleep(FILE_TRANSFER_POLL_INTERVAL)
                            status = self.file_transfer_status(
                                descriptor.transfer_id)
                            self._raise_transfer_remote(status, "fast PUT")
                            self._validate_transfer_binding(status, descriptor)
                            observe_put_progress(status)
                            if status.accepted_offset != accepted_offset:
                                raise RealMSXProtocolError(
                                    "MSX fast PUT accepted offset diverged "
                                    "from the host")
                            reconcile_durable(
                                status.durable_offset, status.prefix_crc32)
                            continue
                        stream.seek(accepted_offset)
                        block = stream.read(min(
                            transfer_limits.max_put_chunk,
                            available_window,
                            descriptor.wire_size - accepted_offset))
                        if not block:
                            raise RealMSXProtocolError(
                                "local fast PUT source ended before its "
                                "declared size")
                        put_reply = self.file_transfer_put_data(
                            descriptor.transfer_id, accepted_offset, block)
                        self._raise_transfer_remote(put_reply, "fast PUT data")
                        if (put_reply.accepted != len(block) or
                                put_reply.accepted_end !=
                                accepted_offset + len(block) or
                                put_reply.durable_end >
                                put_reply.accepted_end):
                            raise RealMSXProtocolError(
                                "MSX fast PUT did not accept the complete "
                                "sequential RAM block")
                        inflight.extend(block)
                        accepted_offset = put_reply.accepted_end
                        reconcile_durable(put_reply.durable_end)
                        self._report_file_transfer(
                            progress, accepted_offset,
                            descriptor.wire_size,
                            f"PUT: {accepted_offset}/{descriptor.wire_size} "
                            f"accepted bytes; {durable_offset} durable")
                        progress_marker = (
                            put_reply.state, put_reply.durable_end,
                            put_reply.accepted_end, put_reply.credit)
                        progress_deadline = time.monotonic() + timeout
                        continue

                    if (durable_offset == descriptor.wire_size and
                            accepted_offset == descriptor.wire_size):
                        # CLOSE is the explicit end-of-stream signal.  The
                        # foreground DOS worker cannot verify, decompress, or
                        # publish the file until it sees this flag.
                        if not close_sent:
                            persist_close_intent()
                            closing = self.file_transfer_close(
                                descriptor.transfer_id,
                                rate_bps=stream_rate_hint())
                            self._raise_transfer_remote(
                                closing, "PUT end-of-stream")
                            close_sent = True
                            progress_deadline = time.monotonic() + timeout
                        else:
                            time.sleep(FILE_TRANSFER_POLL_INTERVAL)
                    else:
                        time.sleep(FILE_TRANSFER_POLL_INTERVAL)

                    status = self.file_transfer_status(
                        descriptor.transfer_id)
                    self._raise_transfer_remote(status, "PUT")
                    self._validate_transfer_binding(status, descriptor)
                    observe_put_progress(status)
                    if status.accepted_offset != accepted_offset:
                        raise RealMSXProtocolError(
                            "MSX PUT accepted offset diverged from the host")
                    reconcile_durable(
                        status.durable_offset, status.prefix_crc32)

            if not close_sent:
                closing = self.file_transfer_close(
                    descriptor.transfer_id,
                    rate_bps=stream_rate_hint())
                self._raise_transfer_remote(closing, "PUT end-of-stream")
            required_flags = (
                TransferReplyFlag.WIRE_VERIFIED |
                TransferReplyFlag.FINAL_VERIFIED |
                TransferReplyFlag.PUBLISHED)
            if (durable_offset != descriptor.wire_size or
                    durable_crc != descriptor.wire_crc32 or
                    status.durable_offset != durable_offset or
                    status.prefix_crc32 != durable_crc or
                    status.flags & required_flags != required_flags):
                raise RealMSXProtocolError(
                    "MSX PUT completed without durable CRC-verified publication")
            journal.remove(descriptor, caller_binding=caller_binding)
            if stream_completed_at is None:
                raise RealMSXProtocolError(
                    "PUT completed without a measured durable stream boundary")
            self._report_file_transfer(
                progress, descriptor.wire_size, descriptor.wire_size,
                f"PUT complete: {descriptor.wire_size} durable bytes")
            stream_bytes = descriptor.wire_size - stream_start_offset
            stream_seconds = max(
                0.0, stream_completed_at - stream_started_at)
            result = {
                "direction": "put",
                "transfer_id": descriptor.transfer_id.hex(),
                "source": str(source_path),
                "target": descriptor.path,
                "encoding": descriptor.encoding.name.lower(),
                "compression_reason": raw_fallback_reason or prepared.reason,
                "wire_bytes": descriptor.wire_size,
                "final_bytes": descriptor.final_size,
                "wire_crc32": f"{descriptor.wire_crc32:08x}",
                "final_crc32": f"{descriptor.final_crc32:08x}",
                "resumed_from": confirmed_offset,
                "data_plane": "fast-v1",
                "stream_bytes": stream_bytes,
                "stream_seconds": round(stream_seconds, 6),
                "stream_rate_bps": round(
                    stream_bytes / stream_seconds, 1)
                    if stream_seconds > 0 else 0.0,
            }
            if basic_source.basic_format is not None:
                result["source_bytes"] = basic_source.source_size
                result["basic_format"] = basic_source.basic_format
                result["basic_normalization"] = basic_source.normalization
            return result
        finally:
            if prepared is not None:
                prepared.cleanup()
            basic_source.cleanup()

    def get_file(self, source, target, *, resume=True,
                 existing_only=False,
                 state_directory=None,
                 timeout=FILE_TRANSFER_PROGRESS_TIMEOUT,
                 progress=None, cancelled=None):
        """Stream one MSX-DOS file to a collision-safe local destination."""
        timeout = self._validate_progress_timeout(timeout)
        self._cancel_before_open_if_requested(cancelled)
        if not isinstance(resume, bool):
            raise TypeError("resume must be a boolean")
        if not isinstance(existing_only, bool):
            raise TypeError("existing_only must be a boolean")
        destination = pathlib.Path(target).expanduser().resolve(strict=False)
        if destination.exists():
            raise FileExistsError(f"local GET destination exists: {destination}")
        if not destination.parent.is_dir():
            raise FileNotFoundError(
                f"local GET destination directory does not exist: "
                f"{destination.parent}")
        state_directory = pathlib.Path(
            self.file_transfer_state_directory
            if state_directory is None else state_directory).expanduser()
        capabilities = self.file_transfer_capabilities()
        transfer_limits = self._require_file_transfer_fast_pump()
        caller_binding = str(destination)
        candidate = TransferDescriptor(
            TransferDirection.GET, TransferEncoding.RAW, new_transfer_id(),
            0, 0, 0, 0, str(source))
        journal = TransferJournal(state_directory)
        record = (journal.find_matching(
            candidate, caller_binding=caller_binding) if resume else None)
        if existing_only and record is None:
            raise RealMSXError(
                "no matching GET journal exists for active-only recovery")
        if record is None:
            descriptor = candidate
            confirmed_offset = 0
            confirmed_crc = 0
        else:
            descriptor = record.resumed_descriptor()
            confirmed_offset = record.confirmed_offset
            confirmed_crc = record.prefix_crc32
        part = destination.parent / (
            f".{destination.name}.{descriptor.transfer_id.hex()}.msxpart")
        if record is None:
            try:
                stream = part.open("xb")
            except FileExistsError as exc:
                raise RealMSXError(
                    f"unbound local GET partial already exists: {part}") from exc
            part_size = 0
        else:
            if not part.is_file():
                raise TransferBindingError(
                    "local GET partial from the resume journal is missing")
            part_size = part.stat().st_size
            if part_size < confirmed_offset:
                raise TransferBindingError(
                    "local GET partial is shorter than its resume journal")
            try:
                actual = crc32_file_prefix(
                    part, confirmed_offset,
                    progress=self._host_transfer_progress(
                        progress, completed=0,
                        total=(descriptor.wire_size or confirmed_offset),
                        label="GET resume validation"),
                    cancelled=cancelled,
                    progress_phase="local GET resume prefix CRC-32").crc32
            except TransferCancelledError as exc:
                self._raise_pre_open_transfer_cancelled(exc)
            if actual != confirmed_crc:
                raise TransferBindingError(
                    "local GET partial CRC differs from its resume journal")
            stream = part.open("r+b")

        try:
            # Persist the random ID even before OPEN/foreground discovery, so
            # an IDE restart in that narrow window can still find the staged
            # GET and its collision-safe local partial.
            journal.save(
                descriptor, confirmed_offset=confirmed_offset,
                prefix_crc32=confirmed_crc,
                caller_binding=caller_binding)
            self._cancel_before_open_if_requested(cancelled)
            status = (self._recover_active_transfer(descriptor)
                      if record is not None else None)
            if existing_only and status is None:
                raise RealMSXError(
                    "the journalled GET is no longer active on the MSX")
            if status is None:
                if part_size > confirmed_offset:
                    # The file write may have reached disk before its ACK or
                    # journal update. With no active worker, return to the last
                    # mutually committed boundary and request that block again.
                    stream.truncate(confirmed_offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    part_size = confirmed_offset
                self._cancel_before_open_if_requested(cancelled)
                opened = self.file_transfer_open(descriptor)
                self._raise_transfer_remote(opened, "GET open")
                status_state = opened.state
            else:
                status_state = status.state

            self._cancel_file_transfer_if_requested(
                descriptor.transfer_id, cancelled)
            if status_state is TransferState.STAGED:
                if existing_only:
                    raise RealMSXError(
                        "the recovered GET is staged but no DOS prompt is "
                        "visible to launch it safely")
                self.type_line(
                    f"MSXAIXF /GET {descriptor.transfer_id.hex().upper()}",
                    timeout=timeout)
                status = None
            if status is None or status.state is TransferState.OPENING:
                status = self._wait_file_transfer(
                    descriptor.transfer_id,
                    (TransferState.READY, TransferState.TRANSFERRING,
                     TransferState.COMPLETE), timeout=timeout,
                    progress=progress,
                    progress_total=(descriptor.wire_size or None),
                    direction="GET", cancelled=cancelled)
            self._validate_transfer_binding(status, descriptor)
            if status.state in (
                    TransferState.READY, TransferState.TRANSFERRING):
                armed = self.file_transfer_fast_begin(descriptor.transfer_id)
                self._raise_transfer_remote(armed, "fast GET begin")
            if descriptor.wire_size == 0 and descriptor.final_size == 0:
                descriptor = TransferDescriptor(
                    TransferDirection.GET, TransferEncoding.RAW,
                    descriptor.transfer_id,
                    status.wire_size, status.wire_crc32,
                    status.final_size, status.final_crc32,
                    descriptor.path,
                    resume_offset=confirmed_offset,
                    resume_prefix_crc32=confirmed_crc,
                    resume=bool(record))
            self._validate_transfer_binding(status, descriptor)
            if status.durable_offset < confirmed_offset:
                raise TransferBindingError(
                    "active MSX GET is behind its committed local journal")
            if status.durable_offset > part_size:
                raise TransferBindingError(
                    "active MSX GET is ahead of its durable local partial")
            if part_size > status.durable_offset:
                # A crash can leave exactly the offered-but-unacknowledged
                # block on disk. Discard it and request the resident's pinned
                # block again from the last ACKed boundary.
                stream.truncate(status.durable_offset)
                stream.flush()
                os.fsync(stream.fileno())
                part_size = status.durable_offset
            # The journal prefix was already hashed before recovery. Reuse it
            # at the same boundary; hash again only if the active MSX worker
            # committed additional bytes that are already durable locally.
            if status.durable_offset == confirmed_offset:
                actual = confirmed_crc
            else:
                try:
                    actual = crc32_file_prefix(
                        part, status.durable_offset,
                        progress=self._host_transfer_progress(
                            progress, completed=status.durable_offset,
                            total=descriptor.wire_size,
                            label="GET recovery validation"),
                        cancelled=cancelled,
                        progress_phase="active GET prefix CRC-32").crc32
                except TransferCancelledError as exc:
                    self._raise_active_local_transfer_cancelled(
                        descriptor.transfer_id, exc)
            if actual != status.prefix_crc32:
                raise TransferBindingError(
                    "MSX GET prefix CRC differs from the local partial")
            confirmed_offset = status.durable_offset
            confirmed_crc = status.prefix_crc32
            self._report_file_transfer(
                progress, confirmed_offset, descriptor.wire_size,
                f"GET: {confirmed_offset}/{descriptor.wire_size} durable bytes")
            journal.save(
                descriptor, confirmed_offset=confirmed_offset,
                prefix_crc32=confirmed_crc,
                caller_binding=caller_binding)

            offset = confirmed_offset
            checksum = confirmed_crc
            journal_checkpoint = confirmed_offset
            progress_deadline = time.monotonic() + timeout
            stream_start_offset = offset
            stream_started_at = time.monotonic()
            stream.seek(offset)
            while offset < descriptor.wire_size:
                self._cancel_file_transfer_if_requested(
                    descriptor.transfer_id, cancelled)
                maximum = min(
                    transfer_limits.max_get_chunk,
                    descriptor.wire_size - offset)
                block = self.file_transfer_get_read(
                    descriptor.transfer_id, offset, maximum)
                self._raise_transfer_remote(block, "GET data")
                if block.offset != offset:
                    raise RealMSXProtocolError(
                        "MSX GET returned a block at the wrong offset")
                if not block.data:
                    if time.monotonic() >= progress_deadline:
                        raise RealMSXTimeoutError(
                            "timeout waiting for the next MSX GET block")
                    time.sleep(FILE_TRANSFER_POLL_INTERVAL)
                    continue
                if offset + len(block.data) > descriptor.wire_size:
                    raise RealMSXProtocolError(
                        "MSX GET block crosses the declared file size")
                self._exact_local_write(stream, block.data)
                checksum = crc32_update(block.data, checksum)
                offset += len(block.data)
                self._report_file_transfer(
                    progress, offset, descriptor.wire_size,
                    f"GET: {offset}/{descriptor.wire_size} received bytes; "
                    f"{journal_checkpoint} durable")
                checkpoint_due = (
                    offset == descriptor.wire_size or
                    offset - journal_checkpoint >=
                    FILE_TRANSFER_JOURNAL_INTERVAL)
                if checkpoint_due:
                    # The ordered TCP stream carries blocks between sparse
                    # restart checkpoints. Only the boundary advertised back
                    # to the MSX needs a local flush+fsync; a crash discards or
                    # truncates any later bytes to the last journalled offset.
                    # The ACK binds that restartable local fsync boundary.
                    stream.flush()
                    os.fsync(stream.fileno())
                    acknowledged = self.file_transfer_get_ack(
                        descriptor.transfer_id, offset, checksum)
                    self._raise_transfer_remote(
                        acknowledged, "fast GET checkpoint")
                    if acknowledged.durable_offset != offset:
                        raise RealMSXProtocolError(
                            "MSX fast GET checkpoint did not commit the "
                            "local offset")
                if checkpoint_due:
                    journal.save(
                        descriptor, confirmed_offset=offset,
                        prefix_crc32=checksum,
                        caller_binding=caller_binding)
                    journal_checkpoint = offset
                progress_deadline = time.monotonic() + timeout

            stream_completed_at = time.monotonic()
            stream_elapsed = stream_completed_at - stream_started_at
            stream_count = descriptor.wire_size - stream_start_offset
            stream_rate_hint = (
                min(0xFFFF, int(stream_count / stream_elapsed + 0.5))
                if stream_elapsed > 0 and stream_count > 0 else 0)
            self._cancel_file_transfer_if_requested(
                descriptor.transfer_id, cancelled)
            closing = self.file_transfer_close(
                descriptor.transfer_id,
                rate_bps=stream_rate_hint)
            self._raise_transfer_remote(closing, "GET end-of-stream")
            status = self._wait_file_transfer(
                descriptor.transfer_id, (TransferState.COMPLETE,),
                timeout=timeout, progress=progress,
                progress_total=descriptor.wire_size,
                direction="GET", cancelled=cancelled)
            self._validate_transfer_binding(status, descriptor)
            required_flags = (
                TransferReplyFlag.WIRE_VERIFIED |
                TransferReplyFlag.FINAL_VERIFIED)
            if (offset != descriptor.wire_size or
                    checksum != descriptor.wire_crc32 or
                    status.durable_offset != descriptor.wire_size or
                    status.prefix_crc32 != descriptor.wire_crc32 or
                    status.flags & required_flags != required_flags):
                raise RealMSXProtocolError(
                    "GET completed without matching size and CRC-32")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            stream = None
            try:
                os.link(part, destination)
            except FileExistsError:
                raise FileExistsError(
                    f"local GET destination appeared during transfer: "
                    f"{destination}")
            except OSError as exc:
                if exc.errno in {
                        errno.EXDEV, errno.EPERM, errno.EACCES,
                        errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    raise RealMSXError(
                        "atomic no-overwrite GET publication requires hard-link "
                        "support in the destination filesystem; the verified "
                        f"partial and resume journal were preserved: {part}") from exc
                raise
            part.unlink()
            journal.remove(descriptor, caller_binding=caller_binding)
            self._report_file_transfer(
                progress, descriptor.wire_size, descriptor.wire_size,
                f"GET complete: {descriptor.wire_size} durable bytes")
            stream_bytes = descriptor.wire_size - stream_start_offset
            stream_seconds = max(
                0.0, stream_completed_at - stream_started_at)
            return {
                "direction": "get",
                "transfer_id": descriptor.transfer_id.hex(),
                "source": descriptor.path,
                "target": str(destination),
                "encoding": "raw",
                "wire_bytes": descriptor.wire_size,
                "final_bytes": descriptor.final_size,
                "wire_crc32": f"{descriptor.wire_crc32:08x}",
                "final_crc32": f"{descriptor.final_crc32:08x}",
                "resumed_from": confirmed_offset,
                "data_plane": "fast-v1",
                "stream_bytes": stream_bytes,
                "stream_seconds": round(stream_seconds, 6),
                "stream_rate_bps": round(
                    stream_bytes / stream_seconds, 1)
                    if stream_seconds > 0 else 0.0,
            }
        finally:
            if stream is not None:
                stream.close()

    def type_line(self, text, timeout=KEYBUF_INPUT_TIMEOUT):
        """Type one text line followed by the MSX Return key."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return self.type(text + "\r", timeout=timeout)

    def press(self, key):
        """Send one BIOS-visible special key through the real agent.

        Character-producing keys use the BIOS keyboard ring. STOP events use
        INTFLG, the documented BIOS work-area byte consumed by MSX-BASIC and
        other cooperative software. This does not emulate the physical matrix
        for software that reads the PPI directly.
        """
        if not isinstance(key, str):
            raise TypeError("key must be a string")
        normalized = key.strip().upper().replace("CONTROL+", "CTRL+")
        if normalized == "RETURN":
            normalized = "RET"
        if normalized in SPECIAL_KEY_INTFLG:
            self.poke(INTFLG, bytes([SPECIAL_KEY_INTFLG[normalized]]))
            return normalized
        try:
            value = SPECIAL_KEY_BYTES[normalized]
        except KeyError as exc:
            supported = sorted(SPECIAL_KEY_BYTES.keys() |
                               SPECIAL_KEY_INTFLG.keys())
            raise ValueError(
                f"unsupported real-agent key {key!r}; expected one of "
                f"{', '.join(supported)}") from exc
        accepted, _pending = self.keybuf_write(bytes([value]))
        if accepted != 1:
            raise RealMSXKeyboardTimeoutError(
                "BIOS keyboard buffer is full; special key was not queued")
        return normalized

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
        self._attachment_quarantine_reason = None


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
