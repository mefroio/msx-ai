import socket
import sys
import threading
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
    RECONNECT_ESCAPE,
    RealMSX,
    RealMSXError,
    RealMSXRangeError,
)


class FakeV3Resident:
    """Resident monitor double with the real v2 bootstrap and v3 framing."""

    def __init__(self, sock, *, max_payload=64, corrupt_once=(), drop_once=(),
                 vram_size=0x20000, legacy_hello=False, start_framed=False,
                 runtime_mode=1, keybuf_feature=True,
                 consume_keybuf=True):
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
        self.consume_keybuf = bool(consume_keybuf)
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
        self.typed = bytearray()

        self.bootstrap_queries = 0
        self.upgrades = 0
        self.reconnects = 0
        self.requests = []
        self.error = None
        self._response_cache = {}
        self.thread = threading.Thread(target=self._run_checked, daemon=True)
        self.thread.start()

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

    def _run(self):
        if self.start_framed:
            window = bytearray()
            while not window.endswith(RECONNECT_ESCAPE):
                window += self._recv_exact(1)
                del window[:-len(RECONNECT_ESCAPE)]
            self.reconnects += 1
            self.sock.sendall(
                b"M\x02" + bytes([self.capabilities]) + b"\xc8")

        # The monitor always starts in the v2 bootstrap, even after a new TCP
        # connection.  Framed bytes are legal only after the explicit F ACK.
        while True:
            command = self._recv_exact(1)
            if command == b"?":
                self.bootstrap_queries += 1
                self.sock.sendall(
                    b"M\x02" + bytes([self.capabilities]) + b"\xc8")
            elif command == b"F":
                self.upgrades += 1
                self.sock.sendall(
                    b"K\x03" + self.max_payload.to_bytes(2, "little"))
                break
            else:
                raise AssertionError(f"unexpected bootstrap byte {command!r}")

        parser = FrameParser(max_payload=self.max_payload)
        while True:
            data = self.sock.recv(4096)
            if not data:
                return
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
                bytes([3, self.capabilities, 0xC8, 1])
                + self.max_payload.to_bytes(2, "little")
                + b"\x01\x00" + bytes([self.vdp_generation])
            )
            if not self.legacy_hello:
                response_payload += bytes([self.vram_size // 0x4000])
                response_payload += self.vram_size.to_bytes(3, "little")
                response_payload += bytes([self.runtime_mode])
                response_payload += bytes([
                    1 if self.keybuf_feature and self.runtime_mode == 0 else 0])
        elif opcode == "q":
            response_payload = bytes([self.state, 3])
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
        self.assertEqual(self.agent.bootstrap_queries, 1)
        self.assertEqual(self.agent.upgrades, 1)
        self.assertEqual(self.info["bootstrap_protocol"], 2)
        self.assertEqual(self.info["protocol"], 3)
        self.assertEqual(self.info["max_payload"], 64)
        self.assertEqual(self.info["transport"], "uart-16c550")
        self.assertEqual(self.info["agent_transport"], "uart-16c550")
        self.assertEqual(self.info["agent_transport_id"], 1)
        self.assertEqual(self.info["network_transport"], "custom-stream")
        self.assertEqual(self.info["network_role"], "attached")
        self.assertEqual(self.info["resident_base"], 0xC800)
        self.assertEqual(self.info["vram_size"], 0x20000)
        self.assertEqual(self.info["vram_banks"], 8)
        self.assertEqual(self.info["runtime_mode"], "foreground-monitor")
        self.assertEqual(self.info["runtime_mode_id"], 1)
        self.assertEqual(self.info["features"], [])
        self.assertEqual(self.info["feature_bits"], 0)
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

    def test_resident_keyboard_input_is_chunked_and_preserves_cr_boundaries(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(resident, runtime_mode=0)
        info = self.msx.info()
        self.agent.state = 1

        text = "10 PRINT \"A LONG BASIC LINE THAT EXCEEDS THE RING BUFFER\"\rRUN\r"
        self.assertEqual(self.msx.type(text), len(text))
        self.assertEqual(bytes(self.agent.typed), text.encode("ascii"))
        requests = [r.payload for r in self.agent.requests
                    if r.opcode == ord("t") and r.payload]
        self.assertTrue(all(len(payload) <= 39 for payload in requests))
        self.assertTrue(all(
            b"\r" not in payload[:-1] for payload in requests))
        self.assertEqual(info["features"], ["keybuf-input"])

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

    def test_keybuf_partial_acceptance_and_retry_are_idempotent(self):
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

        self.assertEqual(self.msx.keybuf_write(b"abc"), (1, 39))
        self.assertEqual(bytes(self.agent.typed), b"a")
        writes = [r for r in self.agent.requests if r.opcode == ord("t")]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], writes[1])

    def test_old_v3_agent_uses_safe_ram_fallback(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.2).attach_socket(client)
        self.agent = FakeV3Resident(
            resident, runtime_mode=0, keybuf_feature=False)
        info = self.msx.info()
        self.agent.state = 1
        self.agent.ram[0xF3F8:0xF3FC] = b"\xf0\xfb\xf0\xfb"

        self.assertEqual(self.msx.keybuf_write(b"ABC\r"), (4, 4))
        self.assertEqual(self.agent.ram[0xFBF0:0xFBF4], b"ABC\r")
        self.assertEqual(self.agent.ram[0xF3F8:0xF3FA], b"\xf4\xfb")
        self.assertEqual(self.agent.state, 1)
        self.assertEqual(info["features"], [])

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
        self.agent = FakeV3Resident(resident, legacy_hello=True)

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

        self.assertEqual(self.agent.bootstrap_queries, 1)
        self.assertEqual(self.agent.upgrades, 1)
        self.assertEqual(info["bootstrap_protocol"], 2)
        self.assertEqual(info["protocol"], 3)
        self.assertEqual(info["max_payload"], 96)

    def test_persistent_framed_agent_uses_robust_reconnect_escape(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.05).attach_socket(client)
        self.agent = FakeV3Resident(resident, max_payload=80, start_framed=True)

        info = self.msx.info()
        self.assertEqual(self.agent.reconnects, 1)
        self.assertTrue(info["bootstrap_recovered"])
        self.assertEqual(info["protocol"], 3)
        self.assertEqual(info["max_payload"], 80)


if __name__ == "__main__":
    unittest.main()
