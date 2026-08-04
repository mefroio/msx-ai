import socket
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from msx_real import RealMSX, RealMSXError, RealMSXRangeError
import msx_real
import msx_mcp_server


class FakeResidentAgent:
    def __init__(self, sock, *, hello_prefix=b"", drop_hello_queries=0,
                 capabilities=0xDF):
        self.sock = sock
        self.hello_prefix = bytes(hello_prefix)
        self.drop_hello_queries = int(drop_hello_queries)
        self.capabilities = int(capabilities)
        self.hello_queries = 0
        self.unknown_commands = 0
        self.ram = bytearray(0x10000)
        self.vram = bytearray(0x20000)
        self.io_ports = bytearray(0x100)
        self.slots = [None] * 4
        self.mapper_segments = [None] * 4
        self.state = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(1)

    def recv(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise EOFError
            data += part
        return bytes(data)

    def address(self):
        hi, lo = self.recv(2)
        return (hi << 8) | lo

    def vaddress(self):
        bank = self.recv(1)[0]
        return (bank << 14) | self.address()

    def run(self):
        try:
            while True:
                command = self.recv(1)
                if command == b"?":
                    self.hello_queries += 1
                    if self.drop_hello_queries:
                        self.drop_hello_queries -= 1
                    else:
                        self.sock.sendall(
                            self.hello_prefix
                            + bytes([ord("M"), 2, self.capabilities, 0xC8]))
                elif command == b"q":
                    self.sock.sendall(bytes([ord("K"), self.state, 2]))
                elif command == b"r":
                    address, size = self.address(), self.recv(1)[0]
                    self.sock.sendall(self.ram[address:address + size])
                elif command == b"p":
                    address, size = self.address(), self.recv(1)[0]
                    self.ram[address:address + size] = self.recv(size)
                    self.sock.sendall(b"K")
                elif command == b"v":
                    address, size = self.vaddress(), self.recv(1)[0]
                    self.sock.sendall(self.vram[address:address + size])
                elif command == b"w":
                    address, size = self.vaddress(), self.recv(1)[0]
                    self.vram[address:address + size] = self.recv(size)
                    self.sock.sendall(b"K")
                elif command == b"i":
                    port = self.recv(1)[0]
                    self.sock.sendall(bytes([self.io_ports[port]]))
                elif command == b"o":
                    port, value = self.recv(2)
                    self.io_ports[port] = value
                    self.sock.sendall(b"K")
                elif command == b"l":
                    page, slot_id = self.recv(2)
                    self.slots[page] = slot_id
                    self.sock.sendall(b"K")
                elif command == b"m":
                    page, segment = self.recv(2)
                    self.mapper_segments[page] = segment
                    self.sock.sendall(b"K")
                elif command == b"c":
                    self.address()
                    self.sock.sendall(b"K")
                elif command == b"j":
                    self.address()
                    self.state = 1
                    self.sock.sendall(b"K")
                elif command == b"s":
                    self.state = 2
                    self.sock.sendall(b"K")
                elif command == b"g":
                    self.state = 1
                    self.sock.sendall(b"K")
                elif command == b"k":
                    self.state = 0
                    self.sock.sendall(b"K")
                elif command == b"z":
                    self.sock.sendall(b"K")
                    return
                else:
                    self.unknown_commands += 1
                    self.sock.sendall(b"E\x01")
        except (EOFError, OSError):
            pass


class _FakeTCPListener:
    def __init__(self, accepted_stream, peer=("192.0.2.10", 12345)):
        self.accepted_stream = accepted_stream
        self.peer = peer
        self.bound = None
        self.timeout = None
        self.closed = False

    def setsockopt(self, *_args):
        pass

    def bind(self, endpoint):
        self.bound = endpoint

    def listen(self, _backlog):
        pass

    def getsockname(self):
        return (self.bound[0], 44000)

    def settimeout(self, timeout):
        self.timeout = timeout

    def accept(self):
        return self.accepted_stream, self.peer

    def close(self):
        self.closed = True


class _ConnectedStream:
    def __init__(self, stream, peer=("198.51.100.7", 6603)):
        self.stream = stream
        self.peer = peer
        self.family = socket.AF_INET
        self.socket_options = []

    def setsockopt(self, level, option, value):
        self.socket_options.append((level, option, value))

    def recv(self, size):
        return self.stream.recv(size)

    def sendall(self, data):
        return self.stream.sendall(data)

    def settimeout(self, timeout):
        return self.stream.settimeout(timeout)

    def gettimeout(self):
        return self.stream.gettimeout()

    def getsockname(self):
        return ("192.0.2.20", 40000)

    def getpeername(self):
        return self.peer

    def close(self):
        return self.stream.close()


class _RecordingTimeoutStream:
    def __init__(self):
        self.timeout = None
        self.writes = []
        self.closed = False

    def recv(self, _size):
        raise socket.timeout

    def sendall(self, data):
        self.writes.append(bytes(data))

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def close(self):
        self.closed = True


class RealMSXTransportTest(unittest.TestCase):
    def setUp(self):
        client, agent = socket.socketpair()
        self.msx = RealMSX(socket_timeout=1).attach_socket(client)
        self.agent = FakeResidentAgent(agent)
        self.msx.info()

    def tearDown(self):
        self.msx.close()
        self.agent.close()

    def test_handshake_and_state_cycle(self):
        self.assertEqual(self.msx.protocol_version, 2)
        self.assertEqual(self.msx.resident_base, 0xC800)
        info = self.msx.info()
        self.assertIn("hardware-io", info["capabilities"])
        self.assertIn("mapping", info["capabilities"])
        self.assertEqual(self.msx.status()["state"], "monitor")
        self.assertEqual(self.msx.run(0x8000), "accepted")
        self.assertEqual(self.msx.status()["state"], "running")
        self.assertEqual(self.msx.pause(), "paused")
        self.assertEqual(self.msx.status()["state"], "paused")
        self.assertEqual(self.msx.resume(), "running")
        self.assertEqual(self.msx.stop(), "monitor")
        self.assertEqual(self.msx.status()["state"], "monitor")

    def test_bootstrap_scans_past_noise_before_raw_hello(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.05).attach_socket(client)
        self.agent = FakeResidentAgent(
            resident, hello_prefix=b"uart-noise\x00E\x01")

        info = self.msx.info()

        self.assertEqual(info["protocol"], 2)
        self.assertEqual(self.agent.hello_queries, 1)
        self.assertFalse(self.msx.bootstrap_recovered)

    def test_bootstrap_stops_after_a_dropped_raw_hello(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=0.03).attach_socket(client)
        self.agent = FakeResidentAgent(resident, drop_hello_queries=1)

        with self.assertRaisesRegex(
                RealMSXError, "safe bootstrap probe timed out"):
            self.msx.info()

        self.assertEqual(self.agent.hello_queries, 1)
        self.assertEqual(self.agent.unknown_commands, 1)
        self.assertFalse(self.msx.bootstrap_recovered)

    def test_failed_bootstrap_quarantines_repeated_info_without_more_writes(self):
        self.msx.close()
        self.agent.close()
        stream = _RecordingTimeoutStream()
        self.msx = RealMSX(socket_timeout=0.01).attach_stream(stream)

        with self.assertRaisesRegex(
                RealMSXError, "safe bootstrap probe timed out"):
            self.msx.info()

        self.assertTrue(self.msx.write_quarantined)
        self.assertEqual(stream.writes, [msx_real.RECONNECT_ESCAPE[:1]])
        with self.assertRaisesRegex(RealMSXError, "write-quarantined"):
            self.msx.info()
        self.assertEqual(stream.writes, [msx_real.RECONNECT_ESCAPE[:1]])

    def test_same_stream_reattach_preserves_v3_quarantine_until_close(self):
        self.msx.close()
        self.agent.close()
        stream = _RecordingTimeoutStream()
        self.msx = RealMSX(socket_timeout=0.01).attach_stream(stream)
        reason = RuntimeError("indeterminate v3 request")
        self.msx._v3 = mock.Mock(
            write_quarantined=True, quarantine_reason=reason)

        self.msx.attach_stream(stream)

        self.assertIsNone(self.msx._v3)
        self.assertTrue(self.msx.write_quarantined)
        self.assertIs(self.msx.quarantine_reason, reason)
        with self.assertRaisesRegex(RealMSXError, "write-quarantined"):
            self.msx._send(b"?")
        self.assertEqual(stream.writes, [])

        self.msx.close()
        replacement = _RecordingTimeoutStream()
        self.msx.attach_stream(replacement)
        self.assertFalse(self.msx.write_quarantined)
        self.msx._send(b"?")
        self.assertEqual(replacement.writes, [b"?"])

    def test_new_stream_clears_all_negotiated_metadata(self):
        self.msx.close()
        self.agent.close()
        self.msx = RealMSX(socket_timeout=0.05)
        self.msx.debug = True
        self.msx.control_level = 2
        self.msx.simulation = "stale-simulation"
        self.msx.agent_transport_id = 1
        self.msx.runtime_mode_id = 1
        self.msx.bootstrap_feature_bits = 0x18
        self.msx.bootstrap_features_known = True
        client, resident = socket.socketpair()
        self.agent = FakeResidentAgent(resident)

        self.msx.attach_socket(client)

        self.assertIsNone(self.msx.debug)
        self.assertIsNone(self.msx.control_level)
        self.assertIsNone(self.msx.simulation)
        self.assertIsNone(self.msx.agent_transport_id)
        self.assertIsNone(self.msx.runtime_mode_id)
        self.assertEqual(self.msx.bootstrap_feature_bits, 0)
        self.assertFalse(self.msx.bootstrap_features_known)

    def test_pause_and_resume_reject_monitor_state(self):
        with self.assertRaises(RealMSXError):
            self.msx.pause()
        with self.assertRaises(RealMSXError):
            self.msx.resume()

    def test_mcp_atomic_write_refuses_legacy_running_agent(self):
        self.msx.run(0x8000)
        previous = (msx_mcp_server.SESSION.msx,
                    msx_mcp_server.SESSION.profile)
        msx_mcp_server.SESSION.msx = self.msx
        msx_mcp_server.SESSION.profile = "real"
        try:
            with self.assertRaisesRegex(RealMSXError, "snapshot-lease"):
                msx_mcp_server.t_memory_write(
                    "ram", 0x4000, "4d4350", verify=True)
            self.assertEqual(self.msx.status()["state"], "running")
        finally:
            (msx_mcp_server.SESSION.msx,
             msx_mcp_server.SESSION.profile) = previous

    def test_mcp_status_does_not_boot_when_disconnected(self):
        previous = (msx_mcp_server.SESSION.msx,
                    msx_mcp_server.SESSION.profile)
        msx_mcp_server.SESSION.msx = None
        msx_mcp_server.SESSION.profile = None
        try:
            status = json.loads(msx_mcp_server.t_status())
            self.assertEqual(status, {"backend": "none", "state": "disconnected"})
        finally:
            (msx_mcp_server.SESSION.msx,
             msx_mcp_server.SESSION.profile) = previous

    def test_ram_roundtrip_and_chunking(self):
        payload = bytes(range(256)) + b"tail"
        self.assertEqual(self.msx.poke(0x4000, payload), len(payload))
        self.assertEqual(self.msx.peek(0x4000, len(payload)), payload)

    def test_vram_roundtrip_across_16k_bank(self):
        payload = bytes(range(32))
        self.msx.vpoke(0x3FF8, payload)
        self.assertEqual(self.msx.vpeek(0x3FF8, len(payload)), payload)

    def test_hardware_io_roundtrip_bulk_and_verify(self):
        self.assertEqual(self.msx.io_write(0x98, 0x42, verify=True), b"K")
        self.assertEqual(self.msx.io_read(0x98), 0x42)
        self.assertEqual(self.msx.io_write_many(
            [(0x99, 0x01), (0x9A, 0x23), (0x9B, 0x45)], verify=True), 3)
        self.assertEqual(
            self.msx.io_read_many([0x98, 0x99, 0x9A, 0x9B]),
            b"\x42\x01\x23\x45")

    def test_slot_and_mapper_controls(self):
        self.assertEqual(self.msx.slot_select(0, 0x83), b"K")
        self.assertEqual(self.msx.slot_select(1, 0x02), b"K")
        self.assertEqual(self.msx.mapper_select(0, 7), b"K")
        self.assertEqual(self.msx.mapper_select(1, 31), b"K")
        self.assertEqual(self.agent.slots[:3], [0x83, 0x02, None])
        self.assertEqual(self.agent.mapper_segments[:3], [7, 31, None])

    def test_hardware_control_validation(self):
        for invalid in (-1, 0x100):
            with self.assertRaises(RealMSXRangeError):
                self.msx.io_read(invalid)
            with self.assertRaises(RealMSXRangeError):
                self.msx.io_write(0, invalid)
            with self.assertRaises(RealMSXRangeError):
                self.msx.slot_select(0, invalid)
            with self.assertRaises(RealMSXRangeError):
                self.msx.mapper_select(0, invalid)

        for invalid_page in (-1, 2, 3, 4):
            with self.assertRaises(RealMSXRangeError):
                self.msx.slot_select(invalid_page, 0)
            with self.assertRaises(RealMSXRangeError):
                self.msx.mapper_select(invalid_page, 0)

        with self.assertRaises(TypeError):
            self.msx.io_read("98")
        with self.assertRaises(ValueError):
            self.msx.io_write_many([(0x98,)])

    def test_ranges_and_resident_protection(self):
        with self.assertRaises(RealMSXRangeError):
            self.msx.peek(0xFFFF, 2)
        with self.assertRaises(RealMSXRangeError):
            self.msx.vpeek(0x1FFFF, 2)
        with self.assertRaises(RealMSXRangeError):
            self.msx.poke(0xC7FF, b"XX")

    def test_raw_resident_capabilities_reserve_only_page1(self):
        self.msx.close()
        self.agent.close()
        client, resident = socket.socketpair()
        self.msx = RealMSX(socket_timeout=1).attach_socket(client)
        self.agent = FakeResidentAgent(
            resident, capabilities=0xDF & ~0x08 & ~0x80)
        info = self.msx.info()
        self.assertNotIn("mapping", info["capabilities"])

        with self.assertRaisesRegex(RealMSXRangeError, "page 1"):
            self.msx.peek(0x4000, 1)
        with self.assertRaisesRegex(RealMSXRangeError, "page 1"):
            self.msx.poke(0x7FFF, b"XX")
        self.assertEqual(self.msx.poke(0x8000, b"page2"), 5)
        self.assertEqual(self.msx.peek(0x8000, 5), b"page2")
        self.assertEqual(self.msx.poke(0xC000, b"page3"), 5)
        self.assertEqual(self.msx.peek(0xC000, 5), b"page3")

        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.slot_select(0, 0x83)
        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.mapper_select(0, 7)
        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.slot_select(1, 0x02)
        with self.assertRaisesRegex(RealMSXError, "unavailable in resident"):
            self.msx.mapper_select(1, 31)


class RealMSXTCPConnectionModesTest(unittest.TestCase):
    def test_tcp_listener_accepts_adapter_client(self):
        accepted_socket, resident = socket.socketpair()
        accepted = _ConnectedStream(accepted_socket)
        listener = _FakeTCPListener(accepted)
        agent = FakeResidentAgent(resident)
        msx = RealMSX(host="0.0.0.0", port=0, socket_timeout=1)
        try:
            with mock.patch.object(
                    msx_real.socket, "socket", return_value=listener):
                msx.listen()
                peer = msx.accept(timeout=1)

            self.assertEqual(peer, ("192.0.2.10", 12345))
            self.assertEqual(listener.bound, ("0.0.0.0", 0))
            self.assertEqual(msx.network_transport, "tcp")
            self.assertEqual(msx.network_role, "listen")
            self.assertIn(
                (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                accepted.socket_options)
            self.assertEqual(msx.info()["network_transport"], "tcp")
            self.assertEqual(msx.status()["state"], "monitor")
        finally:
            msx.close()
            agent.close()

    def test_tcp_connector_reaches_adapter_server(self):
        connected, resident = socket.socketpair()
        stream = _ConnectedStream(connected)
        agent = FakeResidentAgent(resident)
        msx = RealMSX(socket_timeout=1)
        try:
            with mock.patch.object(
                    msx_real.socket, "gethostbyname",
                    return_value="198.51.100.7") as gethostbyname, \
                 mock.patch.object(
                    msx_real.socket, "create_connection",
                    return_value=stream) as create_connection:
                peer = msx.connect("adapter.example", 6603, timeout=1)

            gethostbyname.assert_called_once_with("adapter.example")
            create_connection.assert_called_once_with(
                ("198.51.100.7", 6603), timeout=1.0)
            self.assertEqual(peer, ("198.51.100.7", 6603))
            self.assertEqual(msx.network_transport, "tcp")
            self.assertEqual(msx.network_role, "connect")
            self.assertIn(
                (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                stream.socket_options)
            self.assertEqual(msx.info()["network_role"], "connect")
            self.assertEqual(msx.status()["state"], "monitor")
        finally:
            msx.close()
            agent.close()

    def test_tcp_connector_rejects_ipv6_target(self):
        with self.assertRaisesRegex(RealMSXError, "IPv6 is not supported"):
            RealMSX(socket_timeout=1).connect("::1", 6603, timeout=1)

    def test_stream_contract_is_explicit(self):
        with self.assertRaisesRegex(TypeError, "recv"):
            RealMSX().attach_stream(object())

        class StreamWithoutClose:
            def recv(self, _size):
                return b""

            def sendall(self, _data):
                pass

            def settimeout(self, timeout):
                self.timeout = timeout

            def gettimeout(self):
                return self.timeout

        msx = RealMSX().attach_stream(StreamWithoutClose())
        msx.close()


if __name__ == "__main__":
    unittest.main()
