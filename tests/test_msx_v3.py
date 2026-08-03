import socket
import sys
import threading
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from msx_protocol import (  # noqa: E402
    CRCMismatchError,
    Frame,
    FrameFlag,
    FrameParser,
    FrameStatus,
    FrameType,
    GarbageDataError,
    PayloadTooLargeError,
    SequenceMismatchError,
)
from msx_v3 import (  # noqa: E402
    RemoteOutOfRangeError,
    V3Session,
    V3TimeoutError,
)


def receive_frame(sock, parser=None):
    parser = parser or FrameParser()
    while True:
        frames = parser.feed(sock.recv(4096))
        if frames:
            return frames[0]


class AgentThread:
    def __init__(self, target):
        self.error = None

        def checked_target():
            try:
                target()
            except (EOFError, OSError):
                pass
            except BaseException as exc:  # Surface daemon-thread assertions.
                self.error = exc

        self.thread = threading.Thread(target=checked_target, daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(1)
        if self.thread.is_alive():
            raise AssertionError("fake v3 agent did not stop")
        if self.error is not None:
            raise self.error


class ResponsiveFakeStream:
    """Minimal non-socket stream used to verify the generic session contract."""

    def __init__(self, responder=None, delay=0):
        self.responder = responder or (
            lambda request: Frame(
                FrameType.RESPONSE, request.sequence, request.opcode,
                request.payload.upper()))
        self.delay = delay
        self.timeout = None
        self.pending = None
        self.in_flight = False
        self.sent_sequences = []
        self.guard = threading.Lock()

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        request = FrameParser().feed(data)[0]
        with self.guard:
            if self.in_flight:
                raise AssertionError("concurrent requests reached the stream")
            self.in_flight = True
            self.sent_sequences.append(request.sequence)
            self.pending = self.responder(request).encode()

    def recv(self, size):
        time.sleep(self.delay)
        with self.guard:
            data = self.pending[:size]
            self.pending = self.pending[size:]
            if not self.pending:
                self.in_flight = False
            return data


class V3SessionTest(unittest.TestCase):
    def test_generic_stream_roundtrip_sequence_and_lock(self):
        stream = ResponsiveFakeStream(delay=0.02)
        session = V3Session(stream, sequence_start=0xFFFE, timeout=0.5)
        results = {}

        def worker(key, payload):
            results[key] = session.request(0x20, payload)

        first = threading.Thread(target=worker, args=("first", b"one"))
        second = threading.Thread(target=worker, args=("second", b"two"))
        first.start()
        second.start()
        first.join(1)
        second.join(1)

        self.assertEqual(results, {"first": b"ONE", "second": b"TWO"})
        self.assertEqual(stream.sent_sequences, [0xFFFE, 0xFFFF])
        self.assertGreater(stream.timeout, 0)

    def test_socketpair_resynchronizes_and_correlates(self):
        host, peer = socket.socketpair()
        peer.settimeout(1)

        def agent():
            request = receive_frame(peer)
            damaged = bytearray(Frame(
                FrameType.RESPONSE, request.sequence, request.opcode,
                b"broken").encode())
            damaged[11] ^= 0x80
            wrong = Frame(
                FrameType.RESPONSE, (request.sequence + 1) & 0xFFFF,
                request.opcode, b"wrong")
            event = Frame(FrameType.EVENT, 0x8888, 0xE0, b"tick")
            good = Frame(
                FrameType.RESPONSE, request.sequence, request.opcode, b"ok")
            peer.sendall(
                b"serial-noise\x00" + bytes(damaged) + wrong.encode() +
                event.encode() + good.encode())
            peer.close()

        thread = AgentThread(agent)
        try:
            session = V3Session(host, timeout=0.5, retries=0)
            self.assertEqual(session.request(0x31, b"go"), b"ok")
            self.assertEqual(session.pop_events()[0].payload, b"tick")
            self.assertEqual(
                session.pop_orphan_responses()[0].payload, b"wrong")
            errors = session.pop_protocol_errors()
            self.assertTrue(any(isinstance(e, GarbageDataError)
                                for e in errors))
            self.assertTrue(any(isinstance(e, CRCMismatchError)
                                for e in errors))
            self.assertTrue(any(isinstance(e, SequenceMismatchError)
                                for e in errors))
        finally:
            host.close()
            thread.join()

    def test_timeout_retries_the_identical_sequence(self):
        host, peer = socket.socketpair()
        peer.settimeout(1)
        seen = []

        def agent():
            parser = FrameParser()
            first = receive_frame(peer, parser)
            seen.append(first)
            second = receive_frame(peer, parser)
            seen.append(second)
            peer.sendall(Frame(
                FrameType.RESPONSE, second.sequence, second.opcode,
                b"after-retry").encode())
            peer.close()

        thread = AgentThread(agent)
        try:
            session = V3Session(host, timeout=0.04, retries=1)
            self.assertEqual(session.request(0x42, b"once"), b"after-retry")
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0], seen[1])
            self.assertEqual(seen[0].sequence, 0)
        finally:
            host.close()
            thread.join()

    def test_remote_crc_status_retries_identical_request(self):
        seen = []

        def responder(request):
            seen.append(request)
            if len(seen) == 1:
                return Frame(
                    FrameType.RESPONSE, request.sequence, request.opcode,
                    flags=FrameFlag.ERROR, status=FrameStatus.CRC_ERROR)
            return Frame(
                FrameType.RESPONSE, request.sequence, request.opcode, b"ok")

        session = V3Session(ResponsiveFakeStream(responder), timeout=0.5,
                            retries=1)
        self.assertEqual(session.request(0x43, b"same"), b"ok")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def test_retry_exhaustion_is_typed(self):
        class TimeoutStream:
            def __init__(self):
                self.frames = []

            def settimeout(self, value):
                pass

            def sendall(self, data):
                self.frames.append(FrameParser().feed(data)[0])

            def recv(self, size):
                raise socket.timeout

        stream = TimeoutStream()
        session = V3Session(stream, timeout=0.01, retries=2)
        with self.assertRaises(V3TimeoutError) as caught:
            session.request(0x55, b"payload")
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(len(stream.frames), 3)
        self.assertEqual({frame.sequence for frame in stream.frames}, {0})
        self.assertEqual(len({frame.encode() for frame in stream.frames}), 1)

    def test_remote_status_raises_specific_error(self):
        def rejected(request):
            return Frame(
                FrameType.RESPONSE, request.sequence, request.opcode,
                b"VRAM address", flags=FrameFlag.ERROR,
                status=FrameStatus.OUT_OF_RANGE)

        session = V3Session(ResponsiveFakeStream(rejected), timeout=0.5)
        with self.assertRaises(RemoteOutOfRangeError) as caught:
            session.request(0x70)
        self.assertEqual(caught.exception.status, FrameStatus.OUT_OF_RANGE)
        self.assertEqual(caught.exception.opcode, 0x70)
        self.assertEqual(caught.exception.payload, b"VRAM address")

    def test_negotiated_payload_limit_is_enforced_before_send(self):
        stream = ResponsiveFakeStream()
        session = V3Session(stream, max_payload=4096)
        self.assertEqual(session.negotiate_max_payload(64), 64)
        self.assertEqual(session.max_payload, 64)
        with self.assertRaises(PayloadTooLargeError) as caught:
            session.request(0x01, b"x" * 65)
        self.assertEqual(caught.exception.maximum, 64)
        self.assertEqual(stream.sent_sequences, [])


if __name__ == "__main__":
    unittest.main()
