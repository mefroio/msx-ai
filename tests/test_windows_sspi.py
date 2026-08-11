import ctypes
import pathlib
import socket
import struct
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import windows_sspi  # noqa: E402


class _MemorySocket:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def recv(self, size):
        if not self.incoming:
            return b""
        amount = min(size, max(1, min(3, len(self.incoming))))
        chunk = bytes(self.incoming[:amount])
        del self.incoming[:amount]
        return chunk

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True


class _FakeSecur32:
    def __init__(self):
        self.initialize_calls = 0
        self.buffers = []
        self.deleted = 0
        self.freed_credentials = 0
        self.freed_buffers = 0

    def acquire(self, *_args):
        return windows_sspi._SEC_E_OK

    def initialize(self, *_args):
        self.initialize_calls += 1
        output_descriptor_arg = _args[9]
        descriptor = ctypes.cast(
            output_descriptor_arg,
            ctypes.POINTER(windows_sspi._SecBufferDesc)).contents
        output = descriptor.pBuffers.contents
        token = (
            b"client-token-one" if self.initialize_calls == 1
            else b"client-token-two")
        storage = ctypes.create_string_buffer(token)
        self.buffers.append(storage)
        output.cbBuffer = len(token)
        output.pvBuffer = ctypes.cast(storage, ctypes.c_void_p)
        return (
            windows_sspi._SEC_I_CONTINUE_NEEDED
            if self.initialize_calls == 1
            else windows_sspi._SEC_E_OK)

    def complete(self, *_args):
        return windows_sspi._SEC_E_OK

    def free_context_buffer(self, _buffer):
        self.freed_buffers += 1
        return windows_sspi._SEC_E_OK

    def delete_context(self, _context):
        self.deleted += 1
        return windows_sspi._SEC_E_OK

    def free_credentials(self, _credentials):
        self.freed_credentials += 1
        return windows_sspi._SEC_E_OK


class WindowsSspiWireTests(unittest.TestCase):
    def test_chunk_helpers_handle_partial_receives(self):
        stream = _MemorySocket(struct.pack("!I", 6) + b"server")
        self.assertEqual(windows_sspi._recv_chunk(stream), b"server")

        windows_sspi._send_chunk(stream, b"client")
        self.assertTrue(bytes(stream.sent).endswith(
            struct.pack("!I", 6) + b"client"))

    def test_chunk_helper_rejects_unbounded_token(self):
        stream = _MemorySocket(struct.pack(
            "!I", windows_sspi._MAX_NEGOTIATE_TOKEN + 1))
        with self.assertRaisesRegex(
                windows_sspi.WindowsSspiError, "invalid SSPI token length"):
            windows_sspi._recv_chunk(stream)

    def test_negotiate_exchange_matches_openmsx_token_framing(self):
        server_token = b"server-token"
        stream = _MemorySocket(
            struct.pack("!I", len(server_token)) + server_token)
        api = _FakeSecur32()

        windows_sspi._authenticate(stream, api=api)

        expected = (
            struct.pack("!I", len(b"client-token-one")) +
            b"client-token-one" +
            struct.pack("!I", len(b"client-token-two")) +
            b"client-token-two")
        self.assertEqual(bytes(stream.sent), expected)
        self.assertEqual(api.initialize_calls, 2)
        self.assertEqual(api.freed_buffers, 2)
        self.assertEqual(api.deleted, 1)
        self.assertEqual(api.freed_credentials, 1)

    def test_connect_rejects_non_windows_host_without_network(self):
        if windows_sspi.os.name == "nt":
            self.skipTest("non-Windows contract")
        with (mock.patch.object(
                  windows_sspi.socket, "create_connection") as connect,
              self.assertRaises(windows_sspi.WindowsSspiUnavailable)):
            windows_sspi.connect_openmsx_tcp("127.0.0.1", 9947)
        connect.assert_not_called()

    def test_connect_closes_socket_when_authentication_fails(self):
        stream = _MemorySocket()
        with (mock.patch.object(windows_sspi.os, "name", "nt"),
              mock.patch.object(
                  windows_sspi.socket, "create_connection",
                  return_value=stream) as connect,
              mock.patch.object(
                  windows_sspi, "_authenticate",
                  side_effect=windows_sspi.WindowsSspiError("denied")),
              self.assertRaisesRegex(windows_sspi.WindowsSspiError, "denied")):
            windows_sspi.connect_openmsx_tcp(
                "127.0.0.1", 9969, timeout=1.25)
        connect.assert_called_once_with(("127.0.0.1", 9969), timeout=1.25)
        self.assertTrue(stream.closed)

    def test_connect_rejects_non_loopback_before_network(self):
        with (mock.patch.object(windows_sspi.os, "name", "nt"),
              mock.patch.object(
                  windows_sspi.socket, "create_connection") as connect,
              self.assertRaisesRegex(
                  windows_sspi.WindowsSspiError, "non-loopback")):
            windows_sspi.connect_openmsx_tcp("192.0.2.1", 9969)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
