import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from msx_protocol import (  # noqa: E402
    CRCMismatchError,
    Frame,
    FrameFlag,
    FrameParser,
    FrameStatus,
    FrameType,
    GarbageDataError,
)
from msx_real import (  # noqa: E402
    FEATURE_CPU_SNAPSHOT,
    FEATURE_KEYBUF_SPOOL,
    FEATURE_SNAPSHOT_LEASE,
    FEATURE_TIMI_POLL_SAFE,
    FRAME_WAKE_ACK,
    INTFLG,
    RECONNECT_ESCAPE,
    UART8251_FRAME_WAKE_DELAY,
    RealMSX,
    RealMSXError,
    RealMSXProtocolError,
    RealMSXRangeError,
)
from msx_cpu import CPU_CONTEXT_SIZE, CPU_CONTEXT_VERSION  # noqa: E402


class FakeV3Resident:
    """Resident monitor double with the real v2 bootstrap and v3 framing."""

    def __init__(self, sock, *, max_payload=64, corrupt_once=(), drop_once=(),
                 vram_size=0x20000, legacy_hello=False, start_framed=False,
                 runtime_mode=1, keybuf_feature=True,
                 keybuf_spool_feature=True,
                 consume_keybuf=True, debug=False,
                 debug_peer_feature=False, frame_wake_ack_feature=None,
                 timi_poll_safe_feature=None,
                 cpu_snapshot_feature=False, cpu_context=None,
                 bootstrap_features_supported=True,
                 bootstrap_feature_bits=None,
                 transport_id=1):
        self.sock = sock
        self.max_payload = max_payload
        self.corrupt_once = set(corrupt_once)
        self.drop_once = set(drop_once)
        self.corrupted = set()
        self.dropped = set()
        self.vram_size = vram_size
        self.legacy_hello = legacy_hello
        self.start_framed = start_framed
        self.runtime_mode = runtime_mode
        self.keybuf_feature = bool(keybuf_feature)
        self.keybuf_spool_feature = bool(keybuf_spool_feature)
        self.consume_keybuf = bool(consume_keybuf)
        self.debug = bool(debug)
        self.debug_peer_feature = bool(debug_peer_feature)
        self.cpu_snapshot_feature = bool(cpu_snapshot_feature)
        self.cpu_context = (
            self._default_cpu_context() if cpu_context is None
            else bytes(cpu_context))
        self.frame_wake_ack_feature = (
            True if frame_wake_ack_feature is None
            else bool(frame_wake_ack_feature))
        self.timi_poll_safe_feature = (
            runtime_mode == 0 if timi_poll_safe_feature is None
            else bool(timi_poll_safe_feature))
        self.bootstrap_features_supported = bool(bootstrap_features_supported)
        self.bootstrap_feature_bits = bootstrap_feature_bits
        self.transport_id = int(transport_id)
        self.capabilities = 0xFF if runtime_mode != 0 else 0x77
        self.vdp_generation = 0 if vram_size == 0x4000 else 2

        self.ram = bytearray(0x10000)
        self.vram = bytearray(vram_size)
        self.io_ports = bytearray(0x100)
        self.slots = [None] * 4
        self.mapper_segments = [None] * 4
        self.state = 0
        self.last_call = None
        self.last_run = None
        self.keybuf = bytearray()
        self.keybuf_spool = bytearray()
        self.keybuf_spool_barrier = False
        self.keybuf_spool_active = False
        self.keybuf_spool_authorized = False
        self.typed = bytearray()
        self.debug_peer_labels = []

        self.bootstrap_queries = 0
        self.bootstrap_feature_queries = 0
        self.bootstrap_probe_rejections = 0
        self.upgrades = 0
        self.reconnects = 0
        self.reconnect_credits = 0
        self.requests = []
        self.error = None
        self._response_cache = {}
        self.thread = threading.Thread(target=self._run_checked, daemon=True)
        self.thread.start()

    @staticmethod
    def _default_cpu_context():
        payload = bytearray(CPU_CONTEXT_SIZE)
        payload[:8] = bytes([
            CPU_CONTEXT_VERSION, 1, CPU_CONTEXT_SIZE, 0x3F,
            1, 1, 0, 1,
        ])
        for offset, value in (
                (8, 0x8888), (10, 0x7777), (12, 0x6666),
                (14, 0x55A5), (16, 0x4444), (18, 0x3333),
                (20, 0x2222), (22, 0x1111), (24, 0xABCD),
                (26, 0x12A5), (28, 0xF234), (30, 0x4567)):
            payload[offset:offset + 2] = value.to_bytes(2, "little")
        payload[32:34] = bytes([0xD1, 0x42])
        payload[34:36] = (0xBEEF).to_bytes(2, "little")
        payload[36:40] = bytes([5, 2, 1, 0])
        return bytes(payload)

    def _run_checked(self):
        try:
            self._run()
        except (EOFError, OSError):
            pass
        except BaseException as exc:  # Propagate failures from the daemon.
            self.error = exc

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(1)
        if self.thread.is_alive():
            raise AssertionError("fake v3 resident did not stop")
        if self.error is not None:
            raise self.error

    def _recv_exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise EOFError
            data += part
        return bytes(data)

    def _feature_bits(self):
        features = 1 if self.keybuf_feature and self.runtime_mode == 0 else 0
        if self.runtime_mode == 0:
            features |= FEATURE_SNAPSHOT_LEASE
            if self.keybuf_spool_feature:
                features |= FEATURE_KEYBUF_SPOOL
        if (self.debug_peer_feature and self.runtime_mode == 1 and
                self.debug):
            features |= 2
        if self.frame_wake_ack_feature:
            features |= 8
        if self.timi_poll_safe_feature:
            features |= FEATURE_TIMI_POLL_SAFE
        if self.cpu_snapshot_feature:
            features |= FEATURE_CPU_SNAPSHOT
        return features

    def _send_raw_hello(self):
        self.sock.sendall(
            b"M\x02" + bytes([self.capabilities]) + b"\xc8")

    def _accept_credited_reconnect(self, first_byte=None):
        marker = bytearray()
        if first_byte is not None:
            marker += first_byte
            self.sock.sendall(FRAME_WAKE_ACK)
            self.reconnect_credits += 1
        while len(marker) < len(RECONNECT_ESCAPE):
            byte = self._recv_exact(1)
            if byte != RECONNECT_ESCAPE[:1]:
                raise AssertionError(
                    f"unexpected reconnect byte {byte!r}")
            marker += byte
            self.sock.sendall(FRAME_WAKE_ACK)
            self.reconnect_credits += 1
        self.assert_reconnect_marker(marker)
        self.reconnects += 1
        self._send_raw_hello()

    @staticmethod
    def assert_reconnect_marker(marker):
        if bytes(marker) != RECONNECT_ESCAPE:
            raise AssertionError(f"invalid reconnect marker {bytes(marker)!r}")

    def _run_raw_bootstrap(self):
        # Raw mode rejects the one-byte ESC probe before the host sends '?'.
        while True:
            command = self._recv_exact(1)
            if command == RECONNECT_ESCAPE[:1]:
                self.bootstrap_probe_rejections += 1
                self.sock.sendall(b"E\x01")
            elif command == b"?":
                self.bootstrap_queries += 1
                self._send_raw_hello()
            elif command == b"N":
                self.bootstrap_feature_queries += 1
                if self.bootstrap_features_supported:
                    features = (
                        self._feature_bits()
                        if self.bootstrap_feature_bits is None
                        else int(self.bootstrap_feature_bits))
                    self.sock.sendall(b"K" + bytes([features]))
                else:
                    self.sock.sendall(b"E\x01")
            elif command == b"F":
                self.upgrades += 1
                self.sock.sendall(
                    b"K\x03" + self.max_payload.to_bytes(2, "little"))
                return
            else:
                raise AssertionError(f"unexpected bootstrap byte {command!r}")

    def _run_framed(self):
        parser = FrameParser(max_payload=self.max_payload)
        while True:
            data = self.sock.recv(4096)
            if not data:
                return False
            if (self.frame_wake_ack_feature and
                    data == RECONNECT_ESCAPE[:1]):
                self._accept_credited_reconnect(first_byte=data)
                return True
            if self.frame_wake_ack_feature and data == b"M":
                self.sock.sendall(FRAME_WAKE_ACK)
            for request in parser.feed(data):
                if request.frame_type is not FrameType.REQUEST:
                    raise AssertionError(f"unexpected host frame {request!r}")
                self.requests.append(request)
                key = (request.sequence, request.opcode, request.payload)
                wire = self._response_cache.get(key)
                if wire is None:
                    wire = self._dispatch(request).encode()
                    self._response_cache[key] = wire
                self._send_response(request.opcode, wire)

    def _run(self):
        if self.start_framed:
            self._accept_credited_reconnect()

        while True:
            self._run_raw_bootstrap()
            if not self._run_framed():
                return

    def _send_response(self, opcode, wire):
        if opcode in self.drop_once and opcode not in self.dropped:
            self.dropped.add(opcode)
            return
        if opcode in self.corrupt_once and opcode not in self.corrupted:
            self.corrupted.add(opcode)
            damaged = bytearray(wire)
            damaged[-1] ^= 0x80
            # A valid correlated response immediately after damaged serial data
            # proves that the host can find the next frame without reconnecting.
            self.sock.sendall(b"uart-noise\x00" + bytes(damaged) + wire)
            return
        self.sock.sendall(wire)

    @staticmethod
    def _le16(data):
        return int.from_bytes(data, "little")

    @staticmethod
    def _le24(data):
        return int.from_bytes(data, "little")

    def _dispatch(self, request):
        opcode = chr(request.opcode)
        payload = request.payload
        response_payload = b""
        status = FrameStatus.OK
        flags = 0

        if opcode == "?":
            # version, caps, resident page, transport, MTU, control, debug, VDP,
            # banks and directly addressable bytes.
            response_payload = (
                bytes([3, self.capabilities, 0xC8, self.transport_id])
                + self.max_payload.to_bytes(2, "little")
                + bytes([1, int(self.debug), self.vdp_generation])
            )
            if not self.legacy_hello:
                response_payload += bytes([self.vram_size // 0x4000])
                response_payload += self.vram_size.to_bytes(3, "little")
                response_payload += bytes([self.runtime_mode])
                response_payload += bytes([self._feature_bits()])
        elif opcode == "q":
            response_payload = bytes([self.state, 3])
        elif opcode == "D" and self.cpu_snapshot_feature:
            if payload != bytes([CPU_CONTEXT_VERSION]):
                status = FrameStatus.INVALID_ARGUMENT
                flags = FrameFlag.ERROR
            else:
                response_payload = self.cpu_context
        elif opcode == "I" and self.debug_peer_feature:
            self.debug_peer_labels.append(payload)
        elif opcode == "t" and self.keybuf_feature:
            if self.runtime_mode != 0 or self.state != 1:
                status = FrameStatus.INVALID_STATE
                flags = FrameFlag.ERROR
            elif len(payload) > 39:
                status = FrameStatus.OUT_OF_RANGE
                flags = FrameFlag.ERROR
            else:
                accepted = min(len(payload), 39 - len(self.keybuf))
                self.keybuf += payload[:accepted]
                self.typed += payload[:accepted]
                if self.consume_keybuf:
                    self.keybuf.clear()
                response_payload = bytes([accepted, len(self.keybuf)])
        elif opcode == "T" and self.keybuf_spool_feature:
            if self.runtime_mode != 0 or self.state != 1:
                status = FrameStatus.INVALID_STATE
                flags = FrameFlag.ERROR
            elif len(payload) > 256:
                status = FrameStatus.OUT_OF_RANGE
                flags = FrameFlag.ERROR
            else:
                self._drain_keyboard_spool()
                control = payload[0] if payload else 0
                data = payload[1:] if payload else b""
                if control & ~3 or control & 2 and (control != 2 or data):
                    status = FrameStatus.INVALID_ARGUMENT
                    flags = FrameFlag.ERROR
                    return Frame(
                        FrameType.RESPONSE, request.sequence, request.opcode,
                        status=status, flags=flags)
                if control & 2:
                    self.keybuf_spool.clear()
                    self.keybuf.clear()
                    self.keybuf_spool_barrier = False
                    self.keybuf_spool_active = False
                    self.keybuf_spool_authorized = False
                accepted = min(len(data), 255 - len(self.keybuf_spool))
                self.keybuf_spool += data[:accepted]
                if (control & 1 and self.keybuf_spool and
                        not self.keybuf_spool_barrier and
                        not self.keybuf_spool_active):
                    self.keybuf_spool_authorized = True
                pending = len(self.keybuf_spool) + len(self.keybuf)
                credits = 255 - len(self.keybuf_spool)
                spool_flags = (int(self.keybuf_spool_barrier) |
                               int(self.keybuf_spool_active) << 1 |
                               int(self.keybuf_spool_authorized) << 2)
                response_payload = (
                    accepted.to_bytes(2, "little")
                    + pending.to_bytes(2, "little")
                    + credits.to_bytes(2, "little")
                    + bytes([spool_flags])
                )
        elif opcode == "p" and len(payload) >= 2:
            address = self._le16(payload[:2])
            self.ram[address:address + len(payload) - 2] = payload[2:]
        elif opcode == "r" and len(payload) == 4:
            address = self._le16(payload[:2])
            size = self._le16(payload[2:])
            response_payload = bytes(self.ram[address:address + size])
        elif opcode == "w" and len(payload) >= 3:
            address = self._le24(payload[:3])
            self.vram[address:address + len(payload) - 3] = payload[3:]
        elif opcode == "v" and len(payload) == 5:
            address = self._le24(payload[:3])
            size = self._le16(payload[3:])
            response_payload = bytes(self.vram[address:address + size])
        elif opcode == "i" and len(payload) == 1:
            response_payload = bytes([self.io_ports[payload[0]]])
        elif opcode == "o" and len(payload) == 2:
            self.io_ports[payload[0]] = payload[1]
        elif opcode == "l" and len(payload) == 2:
            self.slots[payload[0]] = payload[1]
        elif opcode == "m" and len(payload) == 2:
            self.mapper_segments[payload[0]] = payload[1]
        elif opcode == "c" and len(payload) == 2:
            self.last_call = self._le16(payload)
        elif opcode == "j" and len(payload) == 2:
            self.last_run = self._le16(payload)
            self.state = 1
        elif opcode == "s" and not payload:
            self.state = 2
        elif opcode == "g" and not payload:
            self.state = 1
        elif opcode == "k" and not payload:
            self.state = 0
        else:
            status = FrameStatus.INVALID_ARGUMENT
            flags = FrameFlag.ERROR

        return Frame(
            FrameType.RESPONSE,
            request.sequence,
            request.opcode,
            response_payload,
            flags=flags,
            status=status,
        )

    def _drain_keyboard_spool(self):
        if not self.consume_keybuf:
            return
        if self.keybuf_spool_barrier:
            self.keybuf_spool_barrier = False
            return
        if not self.keybuf_spool_authorized or not self.keybuf_spool:
            return
        self.keybuf_spool_authorized = False
        self.keybuf_spool_active = True
        try:
            end = self.keybuf_spool.index(13) + 1
        except ValueError:
            end = len(self.keybuf_spool)
        segment = bytes(self.keybuf_spool[:end])
        del self.keybuf_spool[:end]
        self.typed += segment
        self.keybuf_spool_active = False
        if segment.endswith(b"\r"):
            self.keybuf_spool_barrier = True


class RealMSXV3Test(unittest.TestCase):
    def setUp(self):
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(resident, max_payload=64)
        self.info = self.msx.info()

    def tearDown(self):
        self.msx.close()
        self.agent.close()

    def test_automatic_upgrade_and_negotiated_hello(self):
        self.assertEqual(self.agent.bootstrap_probe_rejections, 1)
        self.assertEqual(self.agent.bootstrap_queries, 1)
        self.assertEqual(self.agent.bootstrap_feature_queries, 1)
        self.assertEqual(self.agent.upgrades, 1)
        self.assertTrue(self.msx.bootstrap_features_known)
        self.assertEqual(self.msx.bootstrap_feature_bits, 8)
        self.assertEqual(self.info["bootstrap_protocol"], 2)
        self.assertEqual(self.info["protocol"], 3)
        self.assertEqual(self.info["max_payload"], 64)
        self.assertEqual(self.info["transport"], "uart-16c550")
        self.assertEqual(self.info["agent_transport"], "uart-16c550")
        self.assertEqual(self.info["agent_transport_id"], 1)
        self.assertEqual(self.msx._v3.frame_wake_delay, 0.0)
        self.assertEqual(self.info["network_transport"], "custom-stream")
        self.assertEqual(self.info["network_role"], "attached")
        self.assertEqual(self.info["resident_base"], 0xC800)
        self.assertEqual(self.info["vram_size"], 0x20000)
        self.assertEqual(self.info["vram_banks"], 8)
        self.assertEqual(self.info["runtime_mode"], "foreground-monitor")
        self.assertEqual(self.info["runtime_mode_id"], 1)
        self.assertEqual(self.info["features"], ["frame-wake-ack"])
        self.assertEqual(self.info["feature_bits"], 8)
        self.assertFalse(self.info["bootstrap_recovered"])
        self.assertIn("framed-v3", self.info["capabilities"])
        self.assertIn("hardware-io", self.info["capabilities"])
        self.assertIn("mapping", self.info["capabilities"])

        # Once upgraded, info() itself must stay framed and never emit another
        # raw question mark into the v3 byte stream.
        again = self.msx.info()
        self.assertEqual(again["protocol"], 3)
        self.assertEqual(self.agent.bootstrap_queries, 1)
        self.assertEqual(self.agent.upgrades, 1)

    def test_cpu_snapshot_is_feature_gated_versioned_and_parsed(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, cpu_snapshot_feature=True)

        info = self.msx.info()
        snapshot = self.msx.cpu_snapshot()

        self.assertIn("cpu-snapshot-v1", info["features"])
        self.assertEqual(snapshot["backend"], "real")
        self.assertEqual(
            snapshot["capture"]["source"], "bios-h-timi-hook-entry")
        self.assertEqual(snapshot["registers"]["af"], "0x12A5")
        self.assertEqual(snapshot["registers"]["bc"], "0xABCD")
        self.assertIsNone(snapshot["registers"]["pc"])
        self.assertEqual(snapshot["debug"]["jiffy"], 0xBEEF)
        requests = [request for request in self.agent.requests
                    if request.opcode == ord("D")]
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0].payload, bytes([CPU_CONTEXT_VERSION]))

    def test_cpu_snapshot_missing_feature_sends_no_request(self):
        before = len(self.agent.requests)
        with self.assertRaisesRegex(RealMSXError, "cpu-snapshot-v1"):
            self.msx.cpu_snapshot()
        self.assertFalse(any(
            request.opcode == ord("D")
            for request in self.agent.requests[before:]))

    def test_cpu_snapshot_rejects_malformed_agent_record(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, cpu_snapshot_feature=True,
            cpu_context=FakeV3Resident._default_cpu_context()[:-1])
        self.msx.info()

        with self.assertRaisesRegex(
                RealMSXProtocolError, "invalid CPU snapshot response"):
            self.msx.cpu_snapshot()

    def test_cpu_snapshot_retry_reuses_the_identical_versioned_request(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, cpu_snapshot_feature=True, drop_once=(ord("D"),))
        self.msx.info()
        self.msx._v3.timeout = 0.03

        snapshot = self.msx.cpu_snapshot()

        self.assertEqual(snapshot["registers"]["af"], "0x12A5")
        requests = [request for request in self.agent.requests
                    if request.opcode == ord("D")]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])

    def test_legacy_8251_negotiation_retains_first_byte_wake_delay(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, transport_id=0, frame_wake_ack_feature=False,
            bootstrap_features_supported=False)

        info = self.msx.info()

        self.assertEqual(info["agent_transport"], "uart-8251")
        self.assertEqual(UART8251_FRAME_WAKE_DELAY, 0.050)
        self.assertEqual(
            self.msx._v3.frame_wake_delay, UART8251_FRAME_WAKE_DELAY)
        self.assertIsNone(self.msx._v3.frame_wake_ack)

    def test_8251_negotiation_uses_explicit_frame_wake_ack(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, transport_id=0, frame_wake_ack_feature=True)

        info = self.msx.info()

        self.assertIn("frame-wake-ack", info["features"])
        self.assertEqual(self.msx._v3.frame_wake_ack, FRAME_WAKE_ACK)
        self.assertEqual(self.msx._v3.frame_wake_delay, 0.0)
        self.assertEqual(self.msx.status()["state"], "monitor")

    def test_16c550_negotiation_uses_explicit_frame_wake_ack(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, transport_id=1, frame_wake_ack_feature=True)

        info = self.msx.info()

        self.assertEqual(info["agent_transport"], "uart-16c550")
        self.assertIn("frame-wake-ack", info["features"])
        self.assertEqual(self.msx._v3.frame_wake_ack, FRAME_WAKE_ACK)
        self.assertEqual(self.msx._v3.frame_wake_delay, 0.0)

    def test_unapi_transport_is_named_in_negotiated_status(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, transport_id=2, frame_wake_ack_feature=True)

        info = self.msx.info()

        self.assertEqual(info["transport"], "tcpip-unapi")
        self.assertEqual(info["agent_transport"], "tcpip-unapi")
        self.assertEqual(info["agent_transport_id"], 2)

    def test_safe_resident_negotiates_single_attempt_write_quarantine(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, runtime_mode=0, frame_wake_ack_feature=True,
            timi_poll_safe_feature=True)

        info = self.msx.info()

        self.assertEqual(info["runtime_mode"], "resident")
        self.assertIn("frame-wake-ack", info["features"])
        self.assertIn("timi-poll-safe", info["features"])
        self.assertEqual(
            info["feature_bits"] & FEATURE_TIMI_POLL_SAFE,
            FEATURE_TIMI_POLL_SAFE)
        self.assertTrue(self.msx.bootstrap_features_known)
        self.assertEqual(self.msx.bootstrap_feature_bits, info["feature_bits"])
        self.assertEqual(self.agent.bootstrap_feature_queries, 1)
        self.assertEqual(self.agent.bootstrap_probe_rejections, 1)
        self.assertEqual(self.msx._v3.retries, 0)
        self.assertTrue(self.msx._v3.quarantine_on_timeout)
        self.assertFalse(self.msx._v3.write_quarantined)

    def test_timi_poll_safe_requires_resident_mode_and_wake_ack(self):
        for runtime_mode, wake_ack, message in (
                (1, True, "outside resident mode"),
                (0, False, "requires frame-wake-ack during bootstrap")):
            with self.subTest(runtime_mode=runtime_mode, wake_ack=wake_ack):
                self.msx.close()
                self.agent.close()
                client, resident = socket.socketpair()
                self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
                self.agent = FakeV3Resident(
                    resident, runtime_mode=runtime_mode,
                    frame_wake_ack_feature=wake_ack,
                    timi_poll_safe_feature=True)

                with self.assertRaisesRegex(RealMSXError, message):
                    self.msx.info()

    def test_v3_upgrade_rejects_bootstrap_safety_downgrade(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, runtime_mode=0, frame_wake_ack_feature=True,
            timi_poll_safe_feature=False,
            bootstrap_feature_bits=8 | FEATURE_TIMI_POLL_SAFE)

        with self.assertRaisesRegex(
                RealMSXError, "changed its safety features"):
            self.msx.info()

        request_count = len(self.agent.requests)
        self.assertFalse(self.msx._v3.write_quarantined)
        self.assertTrue(self.msx.write_quarantined)
        with self.assertRaisesRegex(RealMSXError, "write-quarantined"):
            self.msx.info()
        self.assertEqual(len(self.agent.requests), request_count)

    def test_debug_peer_ip_is_announced_once_after_v3_hello(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_stream(
            client, peer=("203.0.113.7", 49152),
            network_transport="tcp", network_role="listen")
        self.agent = FakeV3Resident(
            resident, debug=True, debug_peer_feature=True)

        info = self.msx.info()
        self.assertIn("debug-peer-label", info["features"])
        self.assertEqual(
            self.agent.debug_peer_labels, [b"203.0.113.7:49152"])
        self.msx.info()
        self.assertEqual(
            self.agent.debug_peer_labels, [b"203.0.113.7:49152"])

    def test_ipv6_peer_is_not_announced_to_debug(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_stream(
            client, peer=("2001:db8::1", 49152),
            network_transport="tcp", network_role="listen")
        self.agent = FakeV3Resident(
            resident, debug=True, debug_peer_feature=True)

        info = self.msx.info()
        self.assertIn("debug-peer-label", info["features"])
        self.assertEqual(self.agent.debug_peer_labels, [])
        self.msx.info()
        self.assertEqual(self.agent.debug_peer_labels, [])

    def test_negotiated_ram_chunks_roundtrip(self):
        payload = bytes(range(200))
        self.assertEqual(self.msx.poke(0x4000, payload), len(payload))
        self.assertEqual(self.msx.peek(0x4000, len(payload)), payload)

        writes = [r for r in self.agent.requests if r.opcode == ord("p")]
        reads = [r for r in self.agent.requests if r.opcode == ord("r")]
        self.assertEqual([len(r.payload) - 2 for r in writes], [62, 62, 62, 14])
        self.assertEqual(
            [self.agent._le16(r.payload[2:]) for r in reads],
            [64, 64, 64, 8],
        )
        self.assertTrue(all(len(r.payload) <= 64 for r in writes))

    def test_explicit_resident_runtime_reserves_page1_only(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(resident, runtime_mode=0)
        info = self.msx.info()

        self.assertEqual(info["runtime_mode"], "resident")
        self.assertNotIn("mapping", info["capabilities"])
        for address, size in ((0x4000, 1), (0x3FFF, 2), (0x7FFF, 2)):
            with self.assertRaisesRegex(RealMSXRangeError, "page 1"):
                self.msx.peek(address, size)
            with self.assertRaisesRegex(RealMSXRangeError, "page 1"):
                self.msx.poke(address, bytes(size))

        for address, payload in ((0x8000, b"page2"), (0xC000, b"page3")):
            self.assertEqual(self.msx.poke(address, payload), len(payload))
            self.assertEqual(self.msx.peek(address, len(payload)), payload)

        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.slot_select(0, 0x83)
        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.mapper_select(0, 7)
        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.slot_select(1, 0x02)
        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.mapper_select(1, 31)

    def test_resident_keyboard_spool_uses_wire_sized_batches(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, runtime_mode=0, max_payload=320)
        info = self.msx.info()
        self.agent.state = 1

        lines = ["10 REM " + ("X" * 280), "20 PRINT \"MCP\"", "RUN"]
        text = "\r".join(lines) + "\r"
        self.assertEqual(self.msx.type_lines(lines), len(text))
        self.assertEqual(bytes(self.agent.typed), text.encode("ascii"))
        requests = [r.payload for r in self.agent.requests
                    if r.opcode == ord("T") and len(r.payload) > 1]
        self.assertEqual([len(payload) for payload in requests], [256, 53])
        self.assertTrue(any(b"\r" in payload[1:] for payload in requests))
        self.assertEqual(
            info["features"],
            ["keybuf-input", "snapshot-lease", "frame-wake-ack",
             "timi-poll-safe", "keybuf-spool"])

    def test_old_resident_keyboard_fallback_preserves_cr_boundaries(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, runtime_mode=0, keybuf_spool_feature=False)
        self.msx.info()
        self.agent.state = 1

        text = "10 PRINT \"A LONG BASIC LINE THAT EXCEEDS THE RING BUFFER\"\rRUN\r"
        self.assertEqual(self.msx.type(text), len(text))
        self.assertEqual(bytes(self.agent.typed), text.encode("ascii"))
        requests = [r.payload for r in self.agent.requests
                    if r.opcode == ord("t") and r.payload]
        self.assertTrue(all(len(payload) <= 39 for payload in requests))
        self.assertTrue(all(b"\r" not in payload[:-1] for payload in requests))

    def test_keyboard_spool_uses_reported_credits_until_final_drain(self):
        self.msx.feature_bits = FEATURE_KEYBUF_SPOOL
        self.msx._v3.max_payload = 320
        replies = iter([
            (100, 100, 155, False, False, True),
            (80, 180, 75, False, True, False),
            (20, 200, 55, False, True, False),
            (0, 0, 255, True, False, False),
            (0, 0, 255, False, False, False),
        ])
        with (mock.patch.object(
                  self.msx, "keybuf_spool_write", side_effect=replies) as write,
              mock.patch.object(time, "sleep")):
            self.assertEqual(self.msx.type("X" * 200), 200)

        payloads = [call.args[0] for call in write.call_args_list]
        self.assertEqual([len(payload) for payload in payloads],
                         [200, 100, 20, 0, 0])
        self.assertEqual(
            [call.kwargs.get("pump", False) for call in write.call_args_list],
            [True, False, False, False, False])

    def test_keyboard_spool_timeout_attempts_explicit_cancellation(self):
        self.msx.feature_bits = FEATURE_KEYBUF_SPOOL
        self.msx._v3.max_payload = 320
        with (mock.patch.object(
                  self.msx, "keybuf_spool_write",
                  return_value=(2, 2, 253, False, False, True)),
              mock.patch.object(
                  self.msx, "cancel_keybuf_spool") as cancel,
              mock.patch.object(
                  time, "monotonic", side_effect=(0.0, 0.0, 0.0, 11.0)),
              mock.patch.object(time, "sleep")):
            with self.assertRaisesRegex(Exception, "keyboard-spool progress"):
                self.msx.type("AB", timeout=10.0)

        cancel.assert_called_once_with(timeout=0.5)

    def test_legacy_protocol_u_host_api_is_absent(self):
        self.assertFalse(hasattr(self.msx, "file_upload_write"))
        self.assertFalse(hasattr(self.msx, "put_dos_file"))
        self.assertNotIn("file-upload", self.msx.info()["features"])

    def test_resident_special_keys_use_keybuf_or_bios_interrupt_flag(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(resident, runtime_mode=0)
        self.msx.info()
        self.agent.state = 1

        self.assertEqual(self.msx.press("ESC"), "ESC")
        self.assertEqual(bytes(self.agent.typed), b"\x1b")

        self.assertEqual(self.msx.press("STOP"), "STOP")
        self.assertEqual(self.agent.ram[INTFLG], 4)
        self.assertEqual(self.msx.press("control+c"), "CTRL+C")
        self.assertEqual(self.agent.ram[INTFLG], 3)
        self.assertEqual(self.msx.press("CTRL+STOP"), "CTRL+STOP")
        self.assertEqual(self.agent.ram[INTFLG], 3)
        with self.assertRaisesRegex(ValueError, "unsupported real-agent key"):
            self.msx.press("FIRE")

    def test_screen_text_distributes_timeout_and_restores_default(self):
        original_timeout = self.msx._v3.timeout
        observed_timeouts = []

        def fake_peek(_address, _length):
            observed_timeouts.append(self.msx._v3.timeout)
            return bytes([0x10, 0x00])

        def fake_vpeek(_address, length):
            observed_timeouts.append(self.msx._v3.timeout)
            return b" " * length

        budget = 1.2
        operations = 1 + ((40 * 24 + 63) // 64)
        expected_per_attempt = budget / (
            operations * (self.msx._v3.retries + 1))
        with (mock.patch.object(self.msx, "peek", side_effect=fake_peek),
              mock.patch.object(self.msx, "vpeek", side_effect=fake_vpeek)):
            self.msx.screen_text(timeout=budget)

        self.assertEqual(len(observed_timeouts), 2)
        self.assertTrue(all(
            timeout <= expected_per_attempt
            for timeout in observed_timeouts))
        self.assertEqual(self.msx._v3.timeout, original_timeout)

    def test_safe_resident_keybuf_timeout_is_not_retried(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.03).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, runtime_mode=0, consume_keybuf=False,
            drop_once=(ord("t"),))
        self.msx.info()
        self.agent.state = 1
        self.agent.keybuf[:] = b"x" * 38

        with self.assertRaisesRegex(
                RealMSXError, "framed agent request failed"):
            self.msx.keybuf_write(b"abc")
        self.assertEqual(bytes(self.agent.typed), b"a")
        writes = [r for r in self.agent.requests if r.opcode == ord("t")]
        self.assertEqual(len(writes), 1)
        self.assertTrue(self.msx.write_quarantined)

    def test_old_resident_without_bootstrap_safety_is_rejected(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, runtime_mode=0, keybuf_feature=False,
            bootstrap_features_supported=False)

        with self.assertRaisesRegex(
                RealMSXError, "lacks safe pre-v3 negotiation"):
            self.msx.info()

        self.assertEqual(self.agent.bootstrap_feature_queries, 1)
        self.assertEqual(self.agent.upgrades, 0)

    def test_negotiated_vram_chunks_honor_bank_boundary(self):
        payload = bytes((index * 17) & 0xFF for index in range(180))
        self.assertEqual(self.msx.vpoke(0x3FE0, payload), len(payload))
        self.assertEqual(self.msx.vpeek(0x3FE0, len(payload)), payload)

        writes = [r for r in self.agent.requests if r.opcode == ord("w")]
        reads = [r for r in self.agent.requests if r.opcode == ord("v")]
        self.assertEqual([len(r.payload) - 3 for r in writes], [32, 61, 61, 26])
        self.assertEqual(
            [self.agent._le16(r.payload[3:]) for r in reads],
            [32, 64, 64, 20],
        )
        self.assertEqual(
            [self.agent._le24(r.payload[:3]) for r in writes],
            [0x3FE0, 0x4000, 0x403D, 0x407A],
        )

    def test_explicit_64k_vram_capacity_limits_host_ranges(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(resident, vram_size=0x10000)

        info = self.msx.info()
        self.assertEqual(info["vram_size"], 0x10000)
        self.assertEqual(info["vram_banks"], 4)
        with self.assertRaises(RealMSXRangeError):
            self.msx.vpeek(0x10000, 1)

    def test_explicit_16k_vram_capacity_limits_host_ranges(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(resident, vram_size=0x4000)

        info = self.msx.info()
        self.assertEqual(info["vram_size"], 0x4000)
        self.assertEqual(info["vram_banks"], 1)
        with self.assertRaises(RealMSXRangeError):
            self.msx.vpoke(0x4000, b"x")

    def test_legacy_nine_byte_hello_remains_supported(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, legacy_hello=True, frame_wake_ack_feature=False,
            bootstrap_features_supported=False)

        info = self.msx.info()
        self.assertEqual(info["vram_size"], 0x20000)
        self.assertEqual(info["vram_banks"], 8)

    def test_state_execution_and_hardware_controls(self):
        self.assertEqual(self.msx.status()["state"], "monitor")
        self.assertEqual(self.msx.call(0x8123), b"K")
        self.assertEqual(self.agent.last_call, 0x8123)
        self.assertEqual(self.msx.run(0x8234), "accepted")
        self.assertEqual(self.agent.last_run, 0x8234)
        self.assertEqual(self.msx.status()["state"], "running")
        self.assertEqual(self.msx.pause(), "paused")
        self.assertEqual(self.msx.resume(), "running")
        self.assertEqual(self.msx.stop(), "monitor")

        self.assertEqual(self.msx.io_write(0x98, 0x42, verify=True), b"K")
        self.assertEqual(self.msx.io_read(0x98), 0x42)
        self.assertEqual(
            self.msx.io_write_many([(0x99, 1), (0x9A, 2)], verify=True), 2)
        self.assertEqual(self.msx.io_read_many([0x98, 0x99, 0x9A]), b"\x42\x01\x02")
        self.assertEqual(self.msx.slot_select(0, 0x83), b"K")
        self.assertEqual(self.msx.mapper_select(1, 31), b"K")
        self.assertEqual(self.agent.slots[:3], [0x83, None, None])
        self.assertEqual(self.agent.mapper_segments[:3], [None, 31, None])

    def test_corrupted_response_resynchronizes_without_reconnect(self):
        self.agent.corrupt_once.add(ord("i"))
        self.agent.io_ports[0xA8] = 0x5A

        self.assertEqual(self.msx.io_read(0xA8), 0x5A)
        errors = self.msx._v3.pop_protocol_errors()
        self.assertTrue(any(isinstance(error, GarbageDataError) for error in errors))
        self.assertTrue(any(isinstance(error, CRCMismatchError) for error in errors))
        self.assertEqual(self.msx.status()["state"], "monitor")

    def test_timeout_retries_identical_request(self):
        self.agent.drop_once.add(ord("q"))
        self.msx._v3.timeout = 0.03
        before = len(self.agent.requests)

        self.assertEqual(self.msx.status()["state"], "monitor")
        requests = self.agent.requests[before:]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0].opcode, ord("q"))

    def test_keyboard_timeout_budget_is_split_across_v3_attempts(self):
        self.msx.feature_bits = 1
        with mock.patch.object(
                self.msx, "_request_v3", return_value=b"\x01\x01") as request:
            self.assertEqual(
                self.msx.keybuf_write(b"A", timeout=3.0), (1, 1))

        request.assert_called_once_with("t", b"A", timeout=1.0)

    def test_new_connection_restarts_with_raw_bootstrap(self):
        self.msx.close()
        self.agent.close()

        client, resident = socket.socketpair()
        self.msx.attach_socket(client)
        self.agent = FakeV3Resident(resident, max_payload=96)
        info = self.msx.info()

        self.assertEqual(self.agent.bootstrap_probe_rejections, 1)
        self.assertEqual(self.agent.bootstrap_queries, 1)
        self.assertEqual(self.agent.bootstrap_feature_queries, 1)
        self.assertEqual(self.agent.upgrades, 1)
        self.assertEqual(info["bootstrap_protocol"], 2)
        self.assertEqual(info["protocol"], 3)
        self.assertEqual(info["max_payload"], 96)

    def test_persistent_framed_agent_uses_robust_reconnect_escape(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.05).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, max_payload=80, start_framed=True,
            frame_wake_ack_feature=True)

        info = self.msx.info()
        self.assertEqual(self.agent.reconnects, 1)
        self.assertEqual(
            self.agent.reconnect_credits, len(RECONNECT_ESCAPE))
        self.assertEqual(self.agent.bootstrap_feature_queries, 1)
        self.assertTrue(info["bootstrap_recovered"])
        self.assertIn("frame-wake-ack", info["features"])
        self.assertEqual(info["protocol"], 3)
        self.assertEqual(info["max_payload"], 80)


if __name__ == "__main__":
    unittest.main()
