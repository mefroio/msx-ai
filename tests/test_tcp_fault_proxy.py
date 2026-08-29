"""Unit tests for the deterministic loopback TCP fault proxy."""

from __future__ import annotations

import socket
import threading
import unittest

from tools.tcp_fault_proxy import TCPFaultProxy


class _EchoServer:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(0.1)
        self.endpoint = self.listener.getsockname()
        self.stopping = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopping:
            try:
                stream, _peer = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            stream.settimeout(0.1)
            try:
                while not self.stopping:
                    try:
                        data = stream.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    stream.sendall(data)
            except OSError:
                pass
            finally:
                stream.close()

    def close(self):
        self.stopping = True
        self.listener.close()
        self.thread.join(timeout=2.0)


class TCPFaultProxyTests(unittest.TestCase):
    def setUp(self):
        self.echo = _EchoServer()
        self.proxy = TCPFaultProxy(*self.echo.endpoint)

    def tearDown(self):
        self.proxy.close()
        self.echo.close()

    def _connect(self):
        stream = socket.create_connection(self.proxy.endpoint, timeout=2.0)
        stream.settimeout(1.0)
        return stream

    def test_forwards_bytes_and_accepts_a_new_session_after_fin(self):
        first = self._connect()
        session = self.proxy.wait_for_session()
        first.sendall(b"first")
        self.assertEqual(first.recv(5), b"first")
        cut = self.proxy.cut("fin")
        self.assertEqual(cut.number, session.number)
        self.assertEqual(cut.fault, "fin")
        self.assertEqual(first.recv(1), b"")
        first.close()

        second = self._connect()
        next_session = self.proxy.wait_for_session(after=session.number)
        second.sendall(b"second")
        self.assertEqual(second.recv(6), b"second")
        self.assertGreater(next_session.number, session.number)
        second.close()

    def test_rst_closes_the_active_session_and_records_byte_counts(self):
        stream = self._connect()
        session = self.proxy.wait_for_session()
        stream.sendall(b"payload")
        self.assertEqual(stream.recv(7), b"payload")
        cut = self.proxy.cut("rst")
        self.assertGreaterEqual(cut.client_to_target, 7)
        self.assertGreaterEqual(cut.target_to_client, 7)
        history = self.proxy.history
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].number, session.number)
        self.assertEqual(history[0].fault, "rst")
        self.assertGreaterEqual(history[0].client_to_target, 7)
        self.assertGreaterEqual(history[0].target_to_client, 7)
        stream.close()

    def test_blackhole_keeps_the_session_open_without_forwarding(self):
        stream = self._connect()
        try:
            session = self.proxy.wait_for_session()
            self.proxy.cut("blackhole")
            stream.sendall(b"lost")
            with self.assertRaises(socket.timeout):
                stream.recv(4)
            current = self.proxy.wait_for_session(after=session.number - 1)
            self.assertTrue(current.connected)
            self.assertEqual(current.fault, "blackhole")
            self.assertEqual(current.client_to_target, 0)
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()
