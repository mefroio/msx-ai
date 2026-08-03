#!/usr/bin/env python3
"""Synchronous host session for the transport-independent MSX-AI v3 protocol.

``V3Session`` only requires a socket-like object with ``recv`` and ``sendall``.
It deliberately does not know whether the byte stream is TCP, a serial bridge,
USB or an in-memory test double.  Requests are serialized, framed, retried with
the same sequence number, and correlated with their response.
"""
from __future__ import annotations

from collections import deque
import socket
import threading
import time
from typing import Deque

try:  # Support both ``import server.msx_v3`` and a server/ sys.path entry.
    from .msx_protocol import (
        MAX_WIRE_PAYLOAD,
        Frame,
        FrameFlag,
        FrameParser,
        FrameStatus,
        FrameType,
        OpcodeMismatchError,
        PayloadTooLargeError,
        SequenceCounter,
        SequenceMismatchError,
        validate_response,
    )
except ImportError:  # pragma: no cover - exercised by this repository's tests
    from msx_protocol import (
        MAX_WIRE_PAYLOAD,
        Frame,
        FrameFlag,
        FrameParser,
        FrameStatus,
        FrameType,
        OpcodeMismatchError,
        PayloadTooLargeError,
        SequenceCounter,
        SequenceMismatchError,
        validate_response,
    )


DEFAULT_TIMEOUT = 1.0
DEFAULT_RETRIES = 2
DEFAULT_MAX_PAYLOAD = 4096
DEFAULT_RECV_SIZE = 4096


class V3SessionError(Exception):
    """Base class for v3 session and transport failures."""


class V3TransportError(V3SessionError):
    """The underlying stream failed while sending or receiving."""


class V3DisconnectedError(V3TransportError):
    """The peer closed the byte stream."""


class V3TimeoutError(V3TransportError):
    """No correlated response arrived within the configured attempts."""

    def __init__(self, sequence: int, opcode: int, attempts: int,
                 timeout: float):
        self.sequence = sequence
        self.opcode = opcode
        self.attempts = attempts
        self.timeout = timeout
        super().__init__(
            f"timeout waiting for opcode 0x{opcode:02X}, sequence "
            f"0x{sequence:04X}, after {attempts} attempt(s) of "
            f"{timeout:g}s")


class UnexpectedFrameError(V3SessionError):
    """A peer sent a frame type that is invalid for a host session."""

    def __init__(self, frame: Frame):
        self.frame = frame
        super().__init__(
            f"unexpected {frame.frame_type.name.lower()} frame from peer")


class RemoteStatusError(V3SessionError):
    """The correlated response reports a non-success status or error flag."""

    def __init__(self, response: Frame):
        self.response = response
        self.status = int(response.status)
        self.sequence = response.sequence
        self.opcode = response.opcode
        self.payload = response.payload
        try:
            status_name = FrameStatus(self.status).name
        except ValueError:
            status_name = f"0x{self.status:02X}"
        detail = response.payload.decode("utf-8", "replace").strip()
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"remote opcode 0x{self.opcode:02X} failed with "
            f"{status_name}{suffix}")


class RemoteInvalidOpcodeError(RemoteStatusError):
    pass


class RemoteInvalidArgumentError(RemoteStatusError):
    pass


class RemoteInvalidStateError(RemoteStatusError):
    pass


class RemoteOutOfRangeError(RemoteStatusError):
    pass


class RemoteCRCError(RemoteStatusError):
    pass


class RemoteBusyError(RemoteStatusError):
    pass


class RemoteUnsupportedError(RemoteStatusError):
    pass


class RemoteInternalError(RemoteStatusError):
    pass


_STATUS_ERRORS = {
    int(FrameStatus.INVALID_OPCODE): RemoteInvalidOpcodeError,
    int(FrameStatus.INVALID_ARGUMENT): RemoteInvalidArgumentError,
    int(FrameStatus.INVALID_STATE): RemoteInvalidStateError,
    int(FrameStatus.OUT_OF_RANGE): RemoteOutOfRangeError,
    int(FrameStatus.CRC_ERROR): RemoteCRCError,
    int(FrameStatus.BUSY): RemoteBusyError,
    int(FrameStatus.UNSUPPORTED): RemoteUnsupportedError,
    int(FrameStatus.INTERNAL_ERROR): RemoteInternalError,
}


class _AttemptTimedOut(Exception):
    """Private control-flow exception for one request attempt."""


def _payload_limit(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= MAX_WIRE_PAYLOAD:
        raise ValueError(
            f"{name} must be in range 0..{MAX_WIRE_PAYLOAD}")
    return value


def _positive_float(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a number") from exc
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _retry_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("retries must be an integer")
    if value < 0:
        raise ValueError("retries must not be negative")
    return value


class V3Session:
    """One serialized request/response session over a socket-like stream.

    ``retries`` is the number of retransmissions after the initial attempt.
    Each retransmission sends the exact same encoded request, including its
    sequence number.  The agent can therefore de-duplicate a request safely.

    Parser, correlation, and unexpected-frame errors are recoverable and can be
    inspected with :meth:`pop_protocol_errors`.  Asynchronous event frames and
    unrelated/late responses have separate bounded queues.
    """

    def __init__(self, stream, *, timeout: float = DEFAULT_TIMEOUT,
                 retries: int = DEFAULT_RETRIES,
                 max_payload: int = DEFAULT_MAX_PAYLOAD,
                 peer_max_payload: int | None = None,
                 recv_size: int = DEFAULT_RECV_SIZE,
                 sequence_start: int = 0,
                 queue_limit: int = 256):
        if not callable(getattr(stream, "recv", None)):
            raise TypeError("stream must provide recv(size)")
        if not callable(getattr(stream, "sendall", None)):
            raise TypeError("stream must provide sendall(data)")
        if (isinstance(recv_size, bool) or not isinstance(recv_size, int)
                or recv_size <= 0):
            raise ValueError("recv_size must be a positive integer")
        if (isinstance(queue_limit, bool) or not isinstance(queue_limit, int)
                or queue_limit <= 0):
            raise ValueError("queue_limit must be a positive integer")

        self.stream = stream
        self.timeout = _positive_float(timeout, "timeout")
        self.retries = _retry_count(retries)
        self.local_max_payload = _payload_limit(max_payload, "max_payload")
        self.peer_max_payload = (
            MAX_WIRE_PAYLOAD if peer_max_payload is None else
            _payload_limit(peer_max_payload, "peer_max_payload"))
        self.max_payload = min(
            self.local_max_payload, self.peer_max_payload)
        self.recv_size = recv_size

        self._sequences = SequenceCounter(sequence_start)
        self._parser = FrameParser(max_payload=self.max_payload)
        self._lock = threading.RLock()
        self._events: Deque[Frame] = deque(maxlen=queue_limit)
        self._orphan_responses: Deque[Frame] = deque(maxlen=queue_limit)
        self._protocol_errors: Deque[Exception] = deque(maxlen=queue_limit)

        self._set_stream_timeout(self.timeout)

    def negotiate_max_payload(self, peer_max_payload: int) -> int:
        """Apply the peer's advertised payload limit and return the effective one."""

        peer_limit = _payload_limit(peer_max_payload, "peer_max_payload")
        with self._lock:
            self.peer_max_payload = peer_limit
            self.max_payload = min(self.local_max_payload, peer_limit)
            self._parser.max_payload = self.max_payload
            return self.max_payload

    def pop_events(self) -> list[Frame]:
        with self._lock:
            result = list(self._events)
            self._events.clear()
            return result

    def pop_orphan_responses(self) -> list[Frame]:
        with self._lock:
            result = list(self._orphan_responses)
            self._orphan_responses.clear()
            return result

    def pop_protocol_errors(self) -> list[Exception]:
        with self._lock:
            result = list(self._protocol_errors)
            self._protocol_errors.clear()
            return result

    def request(self, opcode: int, payload=b"", *, timeout: float | None = None,
                retries: int | None = None,
                flags: int = FrameFlag.ACK_REQUIRED) -> bytes:
        """Send one request and return its successful response payload."""

        return self.request_frame(
            opcode, payload, timeout=timeout, retries=retries,
            flags=flags).payload

    def request_frame(self, opcode: int, payload=b"", *,
                      timeout: float | None = None,
                      retries: int | None = None,
                      flags: int = FrameFlag.ACK_REQUIRED) -> Frame:
        """Send one request and return the complete correlated response frame."""

        request_timeout = (
            self.timeout if timeout is None else
            _positive_float(timeout, "timeout"))
        retry_limit = (
            self.retries if retries is None else _retry_count(retries))

        # Frame validates opcode, flags and bytes-like payload before the stream
        # is touched.  The negotiated limit is stricter than the wire limit.
        request = Frame(
            FrameType.REQUEST,
            sequence=0,  # Replaced under the session lock below.
            opcode=opcode,
            payload=payload,
            flags=flags,
        )
        with self._lock:
            # Negotiation also holds this lock, so a concurrent limit update
            # cannot make an already-validated request oversized on the wire.
            if len(request.payload) > self.max_payload:
                raise PayloadTooLargeError(
                    len(request.payload), self.max_payload)
            sequence = self._sequences.next()
            request = Frame(
                FrameType.REQUEST,
                sequence=sequence,
                opcode=request.opcode,
                payload=request.payload,
                flags=request.flags,
            )
            wire = request.encode()
            attempts = retry_limit + 1

            try:
                for attempt in range(1, attempts + 1):
                    deadline = time.monotonic() + request_timeout
                    try:
                        self._send_wire(wire, deadline)
                        response = self._wait_for_response(request, deadline)
                    except _AttemptTimedOut:
                        # Timeout is a useful framing boundary: discard a truly
                        # partial response, but first salvage a valid frame that
                        # may have followed corrupt/truncated bytes.
                        response = self._finish_attempt(request)
                        if response is not None:
                            if (response.status == FrameStatus.CRC_ERROR and
                                    attempt < attempts):
                                continue
                            return self._successful(response)
                        if attempt < attempts:
                            continue
                        raise V3TimeoutError(
                            sequence, request.opcode, attempts,
                            request_timeout) from None
                    if (response.status == FrameStatus.CRC_ERROR and
                            attempt < attempts):
                        continue
                    return self._successful(response)
            finally:
                self._set_stream_timeout(self.timeout)

        raise AssertionError("unreachable request state")

    def close(self) -> None:
        """Close the owned stream if it exposes ``close``."""

        with self._lock:
            close = getattr(self.stream, "close", None)
            if callable(close):
                close()

    def _set_stream_timeout(self, value: float) -> None:
        setter = getattr(self.stream, "settimeout", None)
        if callable(setter):
            try:
                setter(value)
            except (OSError, ValueError) as exc:
                raise V3TransportError(
                    f"could not configure stream timeout: {exc}") from exc

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _AttemptTimedOut
        return remaining

    def _send_wire(self, wire: bytes, deadline: float) -> None:
        self._set_stream_timeout(self._remaining(deadline))
        try:
            self.stream.sendall(wire)
        except (socket.timeout, TimeoutError) as exc:
            raise _AttemptTimedOut from exc
        except OSError as exc:
            raise V3TransportError(f"v3 send failed: {exc}") from exc

    def _wait_for_response(self, request: Frame, deadline: float) -> Frame:
        while True:
            self._set_stream_timeout(self._remaining(deadline))
            try:
                chunk = self.stream.recv(self.recv_size)
            except (socket.timeout, TimeoutError) as exc:
                raise _AttemptTimedOut from exc
            except OSError as exc:
                raise V3TransportError(f"v3 receive failed: {exc}") from exc
            if not chunk:
                raise V3DisconnectedError("v3 peer disconnected")
            try:
                data = bytes(chunk)
            except (TypeError, ValueError) as exc:
                raise V3TransportError(
                    "stream recv() returned a non-bytes value") from exc

            response = self._route_frames(self._parser.feed(data), request)
            self._collect_parser_errors()
            if response is not None:
                return response

    def _finish_attempt(self, request: Frame) -> Frame | None:
        response = self._route_frames(self._parser.finish(), request)
        self._collect_parser_errors()
        return response

    def _collect_parser_errors(self) -> None:
        self._protocol_errors.extend(self._parser.pop_errors())

    def _route_frames(self, frames: list[Frame],
                      request: Frame) -> Frame | None:
        correlated = None
        for frame in frames:
            if frame.frame_type is FrameType.EVENT:
                self._events.append(frame)
                continue
            if frame.frame_type is not FrameType.RESPONSE:
                self._protocol_errors.append(UnexpectedFrameError(frame))
                continue
            try:
                validate_response(request, frame)
            except (SequenceMismatchError, OpcodeMismatchError) as exc:
                self._protocol_errors.append(exc)
                self._orphan_responses.append(frame)
                continue
            if correlated is None:
                correlated = frame
            else:
                # A duplicate can legitimately be produced after retry.  Keep
                # it observable, but never let it answer a later request.
                self._orphan_responses.append(frame)
        return correlated

    @staticmethod
    def _successful(response: Frame) -> Frame:
        if not response.ok:
            error_class = _STATUS_ERRORS.get(
                int(response.status), RemoteStatusError)
            raise error_class(response)
        return response


__all__ = [
    "DEFAULT_TIMEOUT", "DEFAULT_RETRIES", "DEFAULT_MAX_PAYLOAD",
    "DEFAULT_RECV_SIZE", "V3Session", "V3SessionError",
    "V3TransportError", "V3DisconnectedError", "V3TimeoutError",
    "UnexpectedFrameError", "RemoteStatusError",
    "RemoteInvalidOpcodeError", "RemoteInvalidArgumentError",
    "RemoteInvalidStateError", "RemoteOutOfRangeError", "RemoteCRCError",
    "RemoteBusyError", "RemoteUnsupportedError", "RemoteInternalError",
]
