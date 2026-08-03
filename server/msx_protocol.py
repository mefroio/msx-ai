#!/usr/bin/env python3
"""Transport-independent framed protocol for the MSX-AI monitor.

Wire format (protocol v3)
=========================

All multi-byte integers are little-endian, matching the Z80.  A frame is::

    offset  size  field
       0      2   magic: ASCII ``MX`` (4Dh, 58h)
       2      1   version: 03h
       3      1   type: 01h request, 02h response, 03h event
       4      1   flags
       5      2   sequence number
       7      1   opcode
       8      1   status: 00h success/request, non-zero response error
       9      2   payload length (0..65535)
      11      N   payload
    11+N      2   CRC-16/CCITT-FALSE, little-endian

The CRC covers every byte from magic through the end of the payload.  It uses
polynomial 1021h, initial value FFFFh, no reflection and no final XOR.  Sequence
numbers wrap from FFFFh to 0000h.  Request/response pairs use the same sequence
number and opcode; events allocate their own sequence numbers.

``FrameParser`` accepts arbitrary stream fragments.  It records corrupt input
as typed errors while continuing to search for the next valid magic/header/CRC
combination.  This makes the codec usable over TCP, UART, USB or test streams;
none of those transports are referenced here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, IntFlag
import struct
import threading
from typing import Iterable


MAGIC = b"MX"
PROTOCOL_VERSION = 3
MAX_WIRE_PAYLOAD = 0xFFFF

_HEADER = struct.Struct("<2sBBBHBBH")
_CRC = struct.Struct("<H")
HEADER_SIZE = _HEADER.size
CRC_SIZE = _CRC.size
MIN_FRAME_SIZE = HEADER_SIZE + CRC_SIZE
MAX_FRAME_SIZE = HEADER_SIZE + MAX_WIRE_PAYLOAD + CRC_SIZE


class FrameType(IntEnum):
    """Direction/role of a protocol frame."""

    REQUEST = 0x01
    RESPONSE = 0x02
    EVENT = 0x03


class FrameFlag(IntFlag):
    """Flags understood by the common framing layer.

    Opcodes may define additional semantics, but unknown bits must be preserved.
    """

    NONE = 0x00
    ACK_REQUIRED = 0x01
    MORE = 0x02
    ERROR = 0x04


class FrameStatus(IntEnum):
    """Status values common to every opcode.

    Opcode-specific failures may use values from 80h through FFh.  The codec
    intentionally stores status as an integer so newer peers can add values
    without making older hosts unable to decode their response.
    """

    OK = 0x00
    INVALID_OPCODE = 0x01
    INVALID_ARGUMENT = 0x02
    INVALID_STATE = 0x03
    OUT_OF_RANGE = 0x04
    CRC_ERROR = 0x05
    BUSY = 0x06
    UNSUPPORTED = 0x07
    INTERNAL_ERROR = 0x7F


class ProtocolError(Exception):
    """Base class for all protocol errors."""


class FrameValidationError(ProtocolError, ValueError):
    """A caller supplied a value that cannot be represented on the wire."""


class FrameDecodeError(ProtocolError):
    """A received byte stream does not contain a valid frame."""


class InvalidMagicError(FrameDecodeError):
    def __init__(self, actual: bytes):
        self.actual = bytes(actual)
        super().__init__(f"invalid frame magic {self.actual!r}; expected {MAGIC!r}")


class UnsupportedVersionError(FrameDecodeError):
    def __init__(self, actual: int, expected: int = PROTOCOL_VERSION):
        self.actual = actual
        self.expected = expected
        super().__init__(
            f"unsupported protocol version {actual}; expected {expected}")


class InvalidFrameTypeError(FrameDecodeError):
    def __init__(self, actual: int):
        self.actual = actual
        super().__init__(f"invalid frame type 0x{actual:02X}")


class FrameLengthError(FrameDecodeError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(f"frame length is {actual} bytes; expected {expected}")


class TruncatedFrameError(FrameLengthError):
    """A stream boundary or a later valid frame exposed a partial frame."""


class PayloadTooLargeError(FrameValidationError):
    def __init__(self, actual: int, maximum: int):
        self.actual = actual
        self.maximum = maximum
        super().__init__(f"payload is {actual} bytes; maximum is {maximum}")


class CRCMismatchError(FrameDecodeError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"CRC mismatch: received 0x{actual:04X}, calculated 0x{expected:04X}")


class GarbageDataError(FrameDecodeError):
    def __init__(self, data: bytes):
        self.data = bytes(data)
        super().__init__(f"discarded {len(self.data)} non-frame byte(s)")


class SequenceMismatchError(ProtocolError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"response sequence is 0x{actual:04X}; expected 0x{expected:04X}")


class OpcodeMismatchError(ProtocolError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"response opcode is 0x{actual:02X}; expected 0x{expected:02X}")


def _wire_int(name: str, value: int, maximum: int) -> int:
    if not isinstance(value, int):
        raise FrameValidationError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise FrameValidationError(
            f"{name} must be between 0 and {maximum}, got {value}")
    return value


@dataclass(frozen=True)
class Frame:
    """One decoded or ready-to-encode protocol frame."""

    frame_type: FrameType
    sequence: int
    opcode: int
    payload: bytes = b""
    flags: int = 0
    status: int = FrameStatus.OK
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            frame_type = FrameType(self.frame_type)
        except (TypeError, ValueError) as exc:
            raise FrameValidationError(
                f"frame_type must be a valid FrameType, got {self.frame_type!r}") from exc
        object.__setattr__(self, "frame_type", frame_type)
        object.__setattr__(self, "sequence", _wire_int(
            "sequence", self.sequence, 0xFFFF))
        object.__setattr__(self, "opcode", _wire_int(
            "opcode", self.opcode, 0xFF))
        object.__setattr__(self, "flags", _wire_int("flags", self.flags, 0xFF))
        object.__setattr__(self, "status", _wire_int(
            "status", self.status, 0xFF))
        object.__setattr__(self, "version", _wire_int(
            "version", self.version, 0xFF))
        try:
            payload = bytes(self.payload)
        except (TypeError, ValueError) as exc:
            raise FrameValidationError("payload must be bytes-like") from exc
        if len(payload) > MAX_WIRE_PAYLOAD:
            raise PayloadTooLargeError(len(payload), MAX_WIRE_PAYLOAD)
        object.__setattr__(self, "payload", payload)

    @property
    def ok(self) -> bool:
        """Whether the peer reported success for this frame."""

        return self.status == FrameStatus.OK and not (
            self.flags & FrameFlag.ERROR)

    def encode(self) -> bytes:
        return encode_frame(self)


def crc16_ccitt(data: bytes | bytearray | memoryview, initial: int = 0xFFFF) -> int:
    """Return CRC-16/CCITT-FALSE for *data*.

    ``initial`` is exposed for incremental implementations on constrained
    targets; normal frames always start with FFFFh.
    """

    crc = _wire_int("initial CRC", initial, 0xFFFF)
    for byte in memoryview(data).cast("B"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (
                crc << 1) & 0xFFFF
    return crc


def encode_frame(frame: Frame) -> bytes:
    """Encode *frame*, including its CRC."""

    if not isinstance(frame, Frame):
        raise FrameValidationError("encode_frame expects a Frame instance")
    payload_length = len(frame.payload)
    header = _HEADER.pack(
        MAGIC,
        frame.version,
        int(frame.frame_type),
        frame.flags,
        frame.sequence,
        frame.opcode,
        frame.status,
        payload_length,
    )
    body = header + frame.payload
    return body + _CRC.pack(crc16_ccitt(body))


def _decode_header(data: bytes | bytearray | memoryview, *,
                   version: int,
                   max_payload: int) -> tuple[FrameType, int, int, int, int, int]:
    magic, actual_version, type_byte, flags, sequence, opcode, status, length = (
        _HEADER.unpack_from(data))
    if magic != MAGIC:
        raise InvalidMagicError(magic)
    if actual_version != version:
        raise UnsupportedVersionError(actual_version, version)
    try:
        frame_type = FrameType(type_byte)
    except ValueError as exc:
        raise InvalidFrameTypeError(type_byte) from exc
    if length > max_payload:
        raise PayloadTooLargeError(length, max_payload)
    return frame_type, flags, sequence, opcode, status, length


def decode_frame(data: bytes | bytearray | memoryview, *,
                 version: int = PROTOCOL_VERSION,
                 max_payload: int = MAX_WIRE_PAYLOAD) -> Frame:
    """Decode exactly one frame, raising a typed error for invalid input."""

    raw = bytes(data)
    _wire_int("version", version, 0xFF)
    _wire_int("max_payload", max_payload, MAX_WIRE_PAYLOAD)
    if len(raw) < HEADER_SIZE:
        raise TruncatedFrameError(HEADER_SIZE, len(raw))
    frame_type, flags, sequence, opcode, status, length = _decode_header(
        raw, version=version, max_payload=max_payload)
    expected_size = HEADER_SIZE + length + CRC_SIZE
    if len(raw) < expected_size:
        raise TruncatedFrameError(expected_size, len(raw))
    if len(raw) != expected_size:
        raise FrameLengthError(expected_size, len(raw))
    received_crc = _CRC.unpack_from(raw, expected_size - CRC_SIZE)[0]
    calculated_crc = crc16_ccitt(raw[:-CRC_SIZE])
    if received_crc != calculated_crc:
        raise CRCMismatchError(calculated_crc, received_crc)
    return Frame(
        frame_type=frame_type,
        sequence=sequence,
        opcode=opcode,
        payload=raw[HEADER_SIZE:-CRC_SIZE],
        flags=flags,
        status=status,
        version=version,
    )


class FrameParser:
    """Incrementally decode a byte stream and recover after corrupt input.

    Recoverable problems are appended to :attr:`errors`; call ``pop_errors``
    to consume them.  ``finish`` marks a transport/message boundary, reports a
    partial candidate as :class:`TruncatedFrameError`, and resets the buffer.

    A stream cannot distinguish an incomplete frame from a slow one without a
    boundary signal.  Call :meth:`finish` after EOF/reconnect/message boundary;
    it reports the partial frame and can salvage a complete valid frame that
    follows it.  Normal ``feed`` calls never mistake frame-like payload bytes
    for a new frame while the outer frame is still arriving.
    """

    def __init__(self, *, version: int = PROTOCOL_VERSION,
                 max_payload: int = MAX_WIRE_PAYLOAD):
        self.version = _wire_int("version", version, 0xFF)
        self.max_payload = _wire_int(
            "max_payload", max_payload, MAX_WIRE_PAYLOAD)
        self._buffer = bytearray()
        self.errors: list[ProtocolError] = []

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self, *, clear_errors: bool = False) -> None:
        self._buffer.clear()
        if clear_errors:
            self.errors.clear()

    def pop_errors(self) -> list[ProtocolError]:
        errors, self.errors = self.errors, []
        return errors

    def feed(self, data: bytes | bytearray | memoryview = b"") -> list[Frame]:
        """Append stream bytes and return every complete frame now available."""

        try:
            self._buffer.extend(data)
        except (TypeError, ValueError) as exc:
            raise FrameValidationError("parser input must be bytes-like") from exc

        frames: list[Frame] = []
        while self._buffer:
            magic_at = self._buffer.find(MAGIC)
            if magic_at < 0:
                # Retain a possible first half of magic across feed() calls.
                keep = 1 if self._buffer[-1:] == MAGIC[:1] else 0
                discarded = bytes(self._buffer[:-keep] if keep else self._buffer)
                if discarded:
                    self.errors.append(GarbageDataError(discarded))
                    del self._buffer[:len(discarded)]
                break
            if magic_at:
                discarded = bytes(self._buffer[:magic_at])
                self.errors.append(GarbageDataError(discarded))
                del self._buffer[:magic_at]

            if len(self._buffer) < HEADER_SIZE:
                break

            try:
                _, _, _, _, _, payload_length = _decode_header(
                    self._buffer,
                    version=self.version,
                    max_payload=self.max_payload,
                )
            except FrameDecodeError as exc:
                self.errors.append(exc)
                del self._buffer[0]
                continue
            except PayloadTooLargeError as exc:
                # PayloadTooLargeError is a validation error for local frames,
                # but on input it is recoverable malformed wire data.
                self.errors.append(exc)
                del self._buffer[0]
                continue

            expected_size = HEADER_SIZE + payload_length + CRC_SIZE
            if len(self._buffer) < expected_size:
                break

            candidate = bytes(self._buffer[:expected_size])
            try:
                frame = decode_frame(
                    candidate,
                    version=self.version,
                    max_payload=self.max_payload,
                )
            except FrameDecodeError as exc:
                self.errors.append(exc)
                del self._buffer[0]
                continue
            frames.append(frame)
            del self._buffer[:expected_size]
        return frames

    def _find_valid_frame(self, start: int) -> int | None:
        """Return an offset of a later complete valid frame, if present."""

        offset = self._buffer.find(MAGIC, start)
        while offset >= 0:
            available = len(self._buffer) - offset
            if available >= HEADER_SIZE:
                try:
                    _, _, _, _, _, length = _decode_header(
                        memoryview(self._buffer)[offset:],
                        version=self.version,
                        max_payload=self.max_payload,
                    )
                except (FrameDecodeError, PayloadTooLargeError):
                    pass
                else:
                    size = HEADER_SIZE + length + CRC_SIZE
                    if available >= size:
                        try:
                            decode_frame(
                                memoryview(self._buffer)[offset:offset + size],
                                version=self.version,
                                max_payload=self.max_payload,
                            )
                        except FrameDecodeError:
                            pass
                        else:
                            return offset
            offset = self._buffer.find(MAGIC, offset + 1)
        return None

    def finish(self) -> list[Frame]:
        """Finish the current stream, reporting and discarding an incomplete tail."""

        frames = self.feed()
        # At an explicit boundary an incomplete leading candidate cannot become
        # valid later.  Recover a later complete frame before discarding the
        # tail; this handles a severed frame followed by a reconnect burst.
        while self._buffer.startswith(MAGIC) and len(self._buffer) >= HEADER_SIZE:
            try:
                _, _, _, _, _, length = _decode_header(
                    self._buffer,
                    version=self.version,
                    max_payload=self.max_payload,
                )
            except (FrameDecodeError, PayloadTooLargeError):
                break
            expected = HEADER_SIZE + length + CRC_SIZE
            if len(self._buffer) >= expected:
                break
            next_frame = self._find_valid_frame(1)
            if next_frame is None:
                break
            self.errors.append(TruncatedFrameError(expected, next_frame))
            del self._buffer[:next_frame]
            frames.extend(self.feed())

        if self._buffer:
            if (MAGIC.startswith(bytes(self._buffer)) or
                    self._buffer.startswith(MAGIC)):
                expected = HEADER_SIZE
                if len(self._buffer) >= HEADER_SIZE:
                    try:
                        _, _, _, _, _, length = _decode_header(
                            self._buffer,
                            version=self.version,
                            max_payload=self.max_payload,
                        )
                    except (FrameDecodeError, PayloadTooLargeError) as exc:
                        self.errors.append(exc)
                    else:
                        expected = HEADER_SIZE + length + CRC_SIZE
                        self.errors.append(TruncatedFrameError(
                            expected, len(self._buffer)))
                else:
                    self.errors.append(TruncatedFrameError(
                        expected, len(self._buffer)))
            else:
                self.errors.append(GarbageDataError(bytes(self._buffer)))
            self._buffer.clear()
        return frames


class SequenceCounter:
    """Thread-safe 16-bit sequence allocator with defined wraparound."""

    def __init__(self, start: int = 0):
        self._value = _wire_int("sequence", start, 0xFFFF)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            value = self._value
            self._value = (value + 1) & 0xFFFF
            return value


def validate_response(request: Frame, response: Frame) -> None:
    """Validate that *response* belongs to *request*.

    Payload and opcode-specific error flags remain the caller's responsibility.
    """

    if request.frame_type is not FrameType.REQUEST:
        raise FrameValidationError("request frame must have type REQUEST")
    if response.frame_type is not FrameType.RESPONSE:
        raise FrameValidationError("response frame must have type RESPONSE")
    if response.sequence != request.sequence:
        raise SequenceMismatchError(request.sequence, response.sequence)
    if response.opcode != request.opcode:
        raise OpcodeMismatchError(request.opcode, response.opcode)


def encode_many(frames: Iterable[Frame]) -> bytes:
    """Encode a sequence of frames for a stream-oriented transport."""

    return b"".join(encode_frame(frame) for frame in frames)


__all__ = [
    "MAGIC", "PROTOCOL_VERSION", "MAX_WIRE_PAYLOAD", "HEADER_SIZE",
    "CRC_SIZE", "MIN_FRAME_SIZE", "MAX_FRAME_SIZE", "FrameType",
    "FrameFlag", "FrameStatus", "Frame",
    "ProtocolError", "FrameValidationError", "FrameDecodeError",
    "InvalidMagicError", "UnsupportedVersionError", "InvalidFrameTypeError",
    "FrameLengthError", "TruncatedFrameError", "PayloadTooLargeError",
    "CRCMismatchError", "GarbageDataError", "SequenceMismatchError",
    "OpcodeMismatchError", "crc16_ccitt", "encode_frame", "decode_frame",
    "FrameParser", "SequenceCounter", "validate_response", "encode_many",
]
