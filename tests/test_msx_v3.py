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


class PacedFakeStream:
    """Collect split writes and optionally ignore complete request attempts."""

    def __init__(self, respond_on_attempt=1):
        self.respond_on_attempt = respond_on_attempt
        self.timeout = None
        self.chunks = []
        self.requests = []
        self.pending = b""
        self.parser = FrameParser()

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.chunks.append(bytes(data))
        frames = self.parser.feed(data)
        if not frames:
            return
        if len(frames) != 1:
            raise AssertionError("one request attempt produced multiple frames")
        request = frames[0]
        self.requests.append(request)
        if len(self.requests) >= self.respond_on_attempt:
            self.pending = Frame(
                FrameType.RESPONSE, request.sequence, request.opcode,
                request.payload.upper()).encode()

    def recv(self, size):
        if not self.pending:
            raise socket.timeout
        data = self.pending[:size]
        self.pending = self.pending[size:]
        return data


class WakeAckFakeStream(PacedFakeStream):
    """A stream whose peer acknowledges the leading frame magic byte."""

    def __init__(self, ack=b"A", ack_on_attempt=1, respond_on_attempt=1):
        super().__init__(respond_on_attempt=respond_on_attempt)
        self.ack = ack
        self.ack_on_attempt = ack_on_attempt
        self.wake_attempts = 0
        self.ack_pending = False

    def sendall(self, data):
        if data == b"M":
            self.chunks.append(bytes(data))
            if self.parser.feed(data):
                raise AssertionError("leading magic byte completed a frame")
            self.wake_attempts += 1
            self.ack_pending = self.wake_attempts >= self.ack_on_attempt
            return
        super().sendall(data)

    def recv(self, size):
        if self.ack_pending:
            self.ack_pending = False
            return self.ack
        return super().recv(size)


class ClockedWakeAckFakeStream(WakeAckFakeStream):
    """Advance a test clock by the configured timeout when no ACK arrives."""

    def __init__(self, clock, *, timeout_scale=1):
        super().__init__(ack_on_attempt=999)
        self.clock = clock
        self.timeout_scale = timeout_scale
        self.timeouts = []

    def settimeout(self, value):
        super().settimeout(value)
        self.timeouts.append(value)

    def recv(self, size):
        if not self.ack_pending and not self.pending:
            self.clock[0] += self.timeout * self.timeout_scale
            raise socket.timeout
        return super().recv(size)


class V3SessionTest(unittest.TestCase):
    def test_frame_wake_delay_defaults_to_one_unsplit_write(self):
        stream = PacedFakeStream()
        session = V3Session(stream, timeout=0.5)

        with mock.patch("msx_v3.time.sleep") as sleep:
            result = session.request(0x20, b"hello")

        self.assertEqual(result, b"HELLO")
        self.assertEqual(len(stream.chunks), 1)
        self.assertTrue(stream.chunks[0].startswith(b"MX"))
        sleep.assert_not_called()

    def test_frame_wake_delay_sends_magic_then_remainder(self):
        stream = PacedFakeStream()
        session = V3Session(
            stream, timeout=0.5, frame_wake_delay=0.002)

        with mock.patch("msx_v3.time.sleep") as sleep:
            result = session.request(0x20, b"hello")

        self.assertEqual(result, b"HELLO")
        self.assertEqual(stream.chunks[0], b"M")
        self.assertTrue(stream.chunks[1].startswith(b"X"))
        self.assertEqual(len(stream.chunks), 2)
        self.assertEqual(
            FrameParser().feed(b"".join(stream.chunks)), stream.requests)
        sleep.assert_called_once_with(0.002)

    def test_frame_wake_delay_repeats_identical_split_wire_on_retry(self):
        stream = PacedFakeStream(respond_on_attempt=2)
        session = V3Session(
            stream, timeout=0.5, retries=1, frame_wake_delay=0.002)

        with mock.patch("msx_v3.time.sleep") as sleep:
            result = session.request(0x20, b"retry")

        self.assertEqual(result, b"RETRY")
        self.assertEqual(len(stream.chunks), 4)
        first_wire = b"".join(stream.chunks[:2])
        second_wire = b"".join(stream.chunks[2:])
        self.assertEqual(first_wire, second_wire)
        self.assertEqual(
            [request.sequence for request in stream.requests], [0, 0])
        self.assertEqual(sleep.call_args_list, [mock.call(0.002)] * 2)

    def test_frame_wake_delay_must_be_finite_and_non_negative(self):
        stream = PacedFakeStream()
        for value in (-0.001, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    V3Session(stream, frame_wake_delay=value)
        with self.assertRaises(TypeError):
            V3Session(stream, frame_wake_delay=True)

    def test_frame_wake_delay_never_sends_remainder_past_deadline(self):
        stream = PacedFakeStream()
        session = V3Session(
            stream, timeout=0.001, retries=0, frame_wake_delay=0.002)

        with (mock.patch("msx_v3.time.sleep") as sleep,
              self.assertRaises(V3TimeoutError)):
            session.request(0x20, b"deadline")

        self.assertEqual(stream.chunks, [b"M"])
        sleep.assert_not_called()

    def test_frame_wake_ack_consumes_ack_before_sending_remainder(self):
        stream = WakeAckFakeStream(ack=b"A")
        session = V3Session(
            stream, timeout=0.5, frame_wake_ack=b"A")

        result = session.request(0x20, b"hello")

        self.assertEqual(result, b"HELLO")
        self.assertEqual(stream.chunks[0], b"M")
        self.assertTrue(stream.chunks[1].startswith(b"X"))
        self.assertEqual(len(stream.chunks), 2)
        self.assertEqual(session.pop_protocol_errors(), [])

    def test_frame_wake_ack_timeout_retries_magic_without_remainder(self):
        stream = WakeAckFakeStream(ack=b"A", ack_on_attempt=2)
        session = V3Session(
            stream, timeout=0.5, retries=1, frame_wake_ack=b"A")

        result = session.request(0x20, b"retry")

        self.assertEqual(result, b"RETRY")
        self.assertEqual(stream.chunks[0:2], [b"M", b"M"])
        self.assertTrue(stream.chunks[2].startswith(b"X"))
        self.assertEqual(len(stream.requests), 1)
        self.assertEqual(stream.requests[0].sequence, 0)

    def test_frame_wake_ack_repeats_identical_frame_after_response_timeout(self):
        stream = WakeAckFakeStream(ack=b"A", respond_on_attempt=2)
        session = V3Session(
            stream, timeout=0.5, retries=1, frame_wake_ack=b"A")

        result = session.request(0x20, b"retry")

        self.assertEqual(result, b"RETRY")
        self.assertEqual(stream.chunks[0], b"M")
        self.assertEqual(stream.chunks[2], b"M")
        self.assertEqual(stream.chunks[1], stream.chunks[3])
        self.assertEqual(
            [request.sequence for request in stream.requests], [0, 0])

    def test_frame_wake_ack_must_be_exactly_one_byte(self):
        stream = WakeAckFakeStream()
        for value in (b"", b"AB"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    V3Session(stream, frame_wake_ack=value)
        for value in (True, 0x06, "A"):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    V3Session(stream, frame_wake_ack=value)

    def test_frame_wake_ack_rejects_wrong_ack_without_sending_remainder(self):
        stream = WakeAckFakeStream(ack=b"N")
        session = V3Session(
            stream, timeout=0.5, retries=0, frame_wake_ack=b"A",
            frame_wake_ack_optional=True)

        with self.assertRaises(V3TimeoutError):
            session.request(0x20, b"blocked")

        self.assertEqual(stream.chunks, [b"M"])

    def test_optional_frame_wake_ack_falls_back_for_old_agent(self):
        stream = WakeAckFakeStream(ack_on_attempt=999)
        session = V3Session(
            stream, timeout=0.5, retries=0, frame_wake_ack=b"A",
            frame_wake_ack_optional=True, frame_wake_ack_timeout=0.01)

        result = session.request(0x20, b"legacy")

        self.assertEqual(result, b"LEGACY")
        self.assertEqual(stream.chunks[0], b"M")
        self.assertTrue(stream.chunks[1].startswith(b"X"))
        self.assertEqual(len(stream.requests), 1)

    def test_optional_frame_wake_ack_reserves_half_short_deadline(self):
        clock = [0.0]
        stream = ClockedWakeAckFakeStream(clock)
        with mock.patch("msx_v3.time.monotonic", side_effect=lambda: clock[0]):
            session = V3Session(
                stream, timeout=0.05, retries=0, frame_wake_ack=b"A",
                frame_wake_ack_optional=True, frame_wake_ack_timeout=0.1)
            result = session.request(0x20, b"short")

        self.assertEqual(result, b"SHORT")
        self.assertAlmostEqual(clock[0], 0.025)
        self.assertTrue(any(abs(value - 0.025) < 1e-9
                            for value in stream.timeouts))

    def test_optional_frame_wake_ack_never_falls_back_past_deadline(self):
        clock = [0.0]
        stream = ClockedWakeAckFakeStream(clock, timeout_scale=2)
        with (mock.patch("msx_v3.time.monotonic",
                         side_effect=lambda: clock[0]),
              self.assertRaises(V3TimeoutError)):
            session = V3Session(
                stream, timeout=0.05, retries=0, frame_wake_ack=b"A",
                frame_wake_ack_optional=True, frame_wake_ack_timeout=0.1)
            session.request(0x20, b"expired")

        self.assertEqual(stream.chunks, [b"M"])

    def test_frame_wake_ack_optional_and_timeout_validation(self):
        stream = WakeAckFakeStream()
        with self.assertRaises(TypeError):
            V3Session(stream, frame_wake_ack_optional=1)
        for value in (0, -0.1, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    V3Session(stream, frame_wake_ack_timeout=value)
        with self.assertRaises(TypeError):
            V3Session(stream, frame_wake_ack_timeout=True)

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
