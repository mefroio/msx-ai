#!/usr/bin/env python3
"""Wire contract and host-side helpers for resumable MSX file transfers.

The transfer protocol is carried inside framed-v3 opcode ``X`` (58h).  All
multi-byte integers are little-endian.  The outer frame provides per-message
CRC-16 and sequencing; this module adds streaming CRC-32/ISO-HDLC for the
complete wire representation and for the final DOS file.

OPEN request payload (protocol version 1)::

    u8   subcommand       01h
    u8   version          01h
    u8   direction        00h PUT, 01h GET
    u8   encoding         00h raw, 01h PackBits
    u8   flags            bit 0 resume, bit 1 receiptless terminal replay,
                          bit 2 required foreground stream pump
    u8   transfer_id[16]  opaque 128-bit identifier
    u32  wire_size
    u32  wire_crc32
    u32  final_size
    u32  final_crc32
    u32  resume_offset
    u32  resume_prefix_crc32
    u16  path_length
    u8   path[path_length] printable ASCII DOS path

PackBits is deliberately asymmetric in version 1. A PUT may carry canonical
PackBits data when the MSX advertises
:class:`TransferCapability.PACKBITS_DECODE`; GET remains raw until a separately
negotiated target encoder is implemented. ZIP and other already-compressed
files are ordinary raw payloads and are never unpacked implicitly.

Receiptless replay is deliberately narrower than ordinary resume. The host may
set bit 1 only after it has fsync'd a journal at the complete durable PUT
boundary before CLOSE. This distinguishes a lost terminal reply from a journal
created before OPEN, including the otherwise ambiguous zero-byte case.

The ``fast-v1`` stream pump is the only file-transfer data plane. It is
negotiated with separate ``FAST_CAPABILITIES`` and ``FAST_BEGIN`` subcommands,
reuses the outer frame CRC-16 rather than adding a transfer-block checksum,
sends PUT without a separate STATUS poll, and checkpoints GET durability only
at 64 KiB or EOF. Transfer identity, whole-file CRC-32, resume, compression,
and publication semantics remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum, IntFlag
import io
import json
import os
from pathlib import Path
import secrets
import stat
import struct
import tempfile
from typing import BinaryIO
import zlib


FEATURE_FILE_TRANSFER_V2 = 0x80
TRANSFER_OPCODE = ord("X")
FEATURE_FILE_TRANSFER = FEATURE_FILE_TRANSFER_V2
OPCODE_FILE_TRANSFER = TRANSFER_OPCODE
TRANSFER_PROTOCOL_VERSION = 1
UINT32_MAX = 0xFFFFFFFF
MAX_PATH_BYTES = 255
DEFAULT_IO_CHUNK_SIZE = 64 * 1024
MAX_JOURNAL_BYTES = 4096


class TransferSubcommand(IntEnum):
    """Payload command byte used with :data:`TRANSFER_OPCODE`."""

    CAPABILITIES = 0x00
    OPEN = 0x01
    STATUS = 0x02
    PUT_DATA = 0x03
    GET_READ = 0x04
    GET_ACK = 0x05
    CLOSE = 0x06
    CANCEL = 0x07
    FAST_CAPABILITIES = 0x08
    FAST_BEGIN = 0x09


class TransferDirection(IntEnum):
    PUT = 0x00
    GET = 0x01


class TransferEncoding(IntEnum):
    RAW = 0x00
    PACKBITS = 0x01


class TransferCapability(IntFlag):
    """Independently negotiated target-side transfer facilities."""

    RAW = 0x00000001
    PUT = 0x00000002
    GET = 0x00000004
    RESUME = 0x00000008
    CRC32 = 0x00000010
    PACKBITS_DECODE = 0x00000020
    PACKBITS_ENCODE = 0x00000040


class TransferFastCapability(IntFlag):
    """Foreground stream-pump facilities negotiated separately from CAPS."""

    PUMP = 0x01
    STREAM = 0x02


class TransferOpenFlag(IntFlag):
    NONE = 0x00
    RESUME = 0x01
    RECEIPTLESS_REPLAY = 0x02
    FAST_PUMP = 0x04


class TransferState(IntEnum):
    IDLE = 0
    STAGED = 1
    OPENING = 2
    READY = 3
    TRANSFERRING = 4
    VERIFYING = 5
    POSTPROCESS = 6
    COMPLETE = 7
    SUSPENDED = 8
    FAILED = 9
    CANCELLED = 10


class TransferRemoteError(IntEnum):
    NONE = 0
    BINDING = 1
    IO = 2
    CRC = 3
    EXISTS = 4
    TIMEOUT = 5
    UNSUPPORTED = 6
    METADATA = 7
    RANGE = 8


class TransferReplyFlag(IntFlag):
    NONE = 0x00
    ACTIVE = 0x01
    RESUMABLE = 0x02
    WIRE_VERIFIED = 0x04
    FINAL_VERIFIED = 0x08
    PUBLISHED = 0x10


class TransferError(ValueError):
    """A transfer value or payload violates the versioned contract."""


class TransferPayloadError(TransferError):
    """A peer reply has a malformed or unexpected payload."""


class TransferBindingError(TransferError):
    """A reply or saved journal belongs to a different transfer."""


class TransferJournalError(TransferError):
    """A local resume journal is corrupt, unsafe, or inconsistent."""


class TransferJournalLegacyError(TransferJournalError):
    """A valid journal uses a retired data protocol."""

    def __init__(self, message: str, record):
        super().__init__(message)
        self.record = record


def _u32(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransferError(f"{name} must be an integer")
    if not 0 <= value <= UINT32_MAX:
        raise TransferError(
            f"{name} must be between 0 and {UINT32_MAX}, got {value}")
    return value


def _u16(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransferError(f"{name} must be an integer")
    if not 0 <= value <= 0xFFFF:
        raise TransferError(f"{name} must be between 0 and 65535, got {value}")
    return value


def _transfer_id(value: bytes | bytearray | memoryview) -> bytes:
    try:
        result = bytes(value)
    except (TypeError, ValueError) as exc:
        raise TransferError("transfer_id must be 16 bytes") from exc
    if len(result) != 16:
        raise TransferError(
            f"transfer_id must be exactly 16 bytes, got {len(result)}")
    return result


def new_transfer_id() -> bytes:
    """Return a cryptographically strong opaque 128-bit transfer identifier."""

    return secrets.token_bytes(16)


def _path_bytes(path: str) -> bytes:
    if not isinstance(path, str):
        raise TransferError("path must be a string")
    try:
        encoded = path.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TransferError("path must contain ASCII characters only") from exc
    if not encoded:
        raise TransferError("path must not be empty")
    if len(encoded) > MAX_PATH_BYTES:
        raise TransferError(
            f"path is {len(encoded)} bytes; maximum is {MAX_PATH_BYTES}")
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise TransferError("path must contain printable ASCII characters only")
    return encoded


@dataclass(frozen=True)
class TransferDescriptor:
    """Immutable identity and integrity declaration for one transfer."""

    direction: TransferDirection
    encoding: TransferEncoding
    transfer_id: bytes
    wire_size: int
    wire_crc32: int
    final_size: int
    final_crc32: int
    path: str
    resume_offset: int = 0
    resume_prefix_crc32: int = 0
    resume: bool = False
    receiptless_replay: bool = False

    def __post_init__(self) -> None:
        try:
            direction = TransferDirection(self.direction)
        except (TypeError, ValueError) as exc:
            raise TransferError(f"invalid transfer direction {self.direction!r}") from exc
        try:
            encoding = TransferEncoding(self.encoding)
        except (TypeError, ValueError) as exc:
            raise TransferError(f"invalid transfer encoding {self.encoding!r}") from exc
        transfer_id = _transfer_id(self.transfer_id)
        wire_size = _u32("wire_size", self.wire_size)
        wire_crc32 = _u32("wire_crc32", self.wire_crc32)
        final_size = _u32("final_size", self.final_size)
        final_crc32 = _u32("final_crc32", self.final_crc32)
        resume_offset = _u32("resume_offset", self.resume_offset)
        resume_crc = _u32("resume_prefix_crc32", self.resume_prefix_crc32)
        _path_bytes(self.path)

        get_metadata_unknown = (
            direction is TransferDirection.GET and
            wire_size == wire_crc32 == final_size == final_crc32 == 0)
        if resume_offset > wire_size and not get_metadata_unknown:
            raise TransferError("resume_offset exceeds wire_size")
        if resume_offset == 0 and resume_crc != 0:
            raise TransferError(
                "resume_prefix_crc32 must be zero when resume_offset is zero")
        if direction is TransferDirection.GET and encoding is not TransferEncoding.RAW:
            raise TransferError("protocol version 1 GET transfers must use raw encoding")
        if encoding is TransferEncoding.RAW and not get_metadata_unknown and (
                wire_size != final_size or wire_crc32 != final_crc32):
            raise TransferError(
                "raw transfers require identical wire/final size and CRC-32")
        if not isinstance(self.resume, bool):
            raise TransferError("resume must be a boolean")
        resume = self.resume or resume_offset > 0
        if not isinstance(self.receiptless_replay, bool):
            raise TransferError("receiptless_replay must be a boolean")
        if self.receiptless_replay and (
                direction is not TransferDirection.PUT or not resume or
                resume_offset != wire_size or resume_crc != wire_crc32):
            raise TransferError(
                "receiptless_replay requires a complete CRC-matched PUT resume")
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "encoding", encoding)
        object.__setattr__(self, "transfer_id", transfer_id)
        object.__setattr__(self, "wire_size", wire_size)
        object.__setattr__(self, "wire_crc32", wire_crc32)
        object.__setattr__(self, "final_size", final_size)
        object.__setattr__(self, "final_crc32", final_crc32)
        object.__setattr__(self, "resume_offset", resume_offset)
        object.__setattr__(self, "resume_prefix_crc32", resume_crc)
        object.__setattr__(self, "resume", resume)

    @property
    def transfer_id_hex(self) -> str:
        return self.transfer_id.hex()

    def with_resume(self, offset: int, prefix_crc32: int) -> "TransferDescriptor":
        return replace(
            self, resume_offset=_u32("offset", offset),
            resume_prefix_crc32=_u32("prefix_crc32", prefix_crc32))

    def binding(self) -> tuple[object, ...]:
        """Fields that must match before a partial transfer can be resumed."""

        return (
            self.direction, self.encoding, self.transfer_id, self.wire_size,
            self.wire_crc32, self.final_size, self.final_crc32, self.path,
        )


_OPEN_HEADER = struct.Struct("<BBBBB16sIIIIIIH")
_CAPABILITIES_REPLY = struct.Struct("<BIHHH")
_FAST_CAPABILITIES_REPLY = struct.Struct("<BBHH")
_OPEN_REPLY = struct.Struct("<BB")
_STATUS_REPLY = struct.Struct("<BBBBB16sIIIIIIIH")
_PUT_DATA_REPLY = struct.Struct("<HIIHBB")
_GET_READ_HEADER = struct.Struct("<IHBB")
_GET_ACK_REPLY = struct.Struct("<IBB")
_TERMINAL_REPLY = struct.Struct("<BB")
_ID_REQUEST = struct.Struct("<B16s")
_FAST_CLOSE_REQUEST = struct.Struct("<B16sH")
_GET_READ_REQUEST = struct.Struct("<B16sIH")
_GET_ACK_REQUEST = struct.Struct("<B16sII")
_PUT_DATA_REQUEST = struct.Struct("<B16sI")


def encode_capabilities_request() -> bytes:
    return bytes((TransferSubcommand.CAPABILITIES,))


encode_caps_request = encode_capabilities_request


def encode_fast_capabilities_request() -> bytes:
    """Probe the required fast-v1 stream-pump capabilities."""

    return bytes((TransferSubcommand.FAST_CAPABILITIES,))


def encode_open(descriptor: TransferDescriptor) -> bytes:
    """Encode the canonical OPEN payload documented at module level."""

    if not isinstance(descriptor, TransferDescriptor):
        raise TransferError("descriptor must be a TransferDescriptor")
    path = _path_bytes(descriptor.path)
    flags = TransferOpenFlag.FAST_PUMP
    if descriptor.resume:
        flags |= TransferOpenFlag.RESUME
    if descriptor.receiptless_replay:
        flags |= TransferOpenFlag.RECEIPTLESS_REPLAY
    return _OPEN_HEADER.pack(
        TransferSubcommand.OPEN,
        TRANSFER_PROTOCOL_VERSION,
        descriptor.direction,
        descriptor.encoding,
        flags,
        descriptor.transfer_id,
        descriptor.wire_size,
        descriptor.wire_crc32,
        descriptor.final_size,
        descriptor.final_crc32,
        descriptor.resume_offset,
        descriptor.resume_prefix_crc32,
        len(path),
    ) + path


def decode_open(payload: bytes | bytearray | memoryview) -> TransferDescriptor:
    """Decode OPEN and reject unknown versions, flags, or trailing bytes."""

    payload = bytes(payload)
    if len(payload) < _OPEN_HEADER.size:
        raise TransferPayloadError(
            f"OPEN payload is {len(payload)} bytes; minimum is {_OPEN_HEADER.size}")
    (command, version, direction, encoding, flags, transfer_id, wire_size,
     wire_crc32, final_size, final_crc32, resume_offset, resume_crc,
     path_length) = _OPEN_HEADER.unpack_from(payload)
    if command != TransferSubcommand.OPEN:
        raise TransferPayloadError(
            f"expected OPEN subcommand, got 0x{command:02X}")
    if version != TRANSFER_PROTOCOL_VERSION:
        raise TransferPayloadError(
            f"unsupported transfer version {version}; expected "
            f"{TRANSFER_PROTOCOL_VERSION}")
    supported_flags = int(
        TransferOpenFlag.RESUME | TransferOpenFlag.RECEIPTLESS_REPLAY |
        TransferOpenFlag.FAST_PUMP)
    if flags & ~supported_flags:
        raise TransferPayloadError(
            f"OPEN contains unknown flag bits: 0x{flags:02X}")
    if not flags & TransferOpenFlag.FAST_PUMP:
        raise TransferPayloadError(
            "OPEN does not select the required fast-v1 stream pump")
    expected = _OPEN_HEADER.size + path_length
    if len(payload) != expected:
        raise TransferPayloadError(
            f"OPEN payload is {len(payload)} bytes; declared length requires {expected}")
    path_bytes = payload[_OPEN_HEADER.size:]
    try:
        path = path_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TransferPayloadError("OPEN path is not ASCII") from exc
    try:
        return TransferDescriptor(
            direction=direction,
            encoding=encoding,
            transfer_id=transfer_id,
            wire_size=wire_size,
            wire_crc32=wire_crc32,
            final_size=final_size,
            final_crc32=final_crc32,
            path=path,
            resume_offset=resume_offset,
            resume_prefix_crc32=resume_crc,
            resume=bool(flags & TransferOpenFlag.RESUME),
            receiptless_replay=bool(
                flags & TransferOpenFlag.RECEIPTLESS_REPLAY),
        )
    except TransferError as exc:
        raise TransferPayloadError(f"invalid OPEN descriptor: {exc}") from exc


@dataclass(frozen=True)
class TransferCapabilitiesReply:
    version: int
    capabilities: TransferCapability
    max_put_chunk: int
    max_get_chunk: int
    max_path: int


@dataclass(frozen=True)
class TransferFastCapabilitiesReply:
    version: int
    capabilities: TransferFastCapability
    max_put_chunk: int
    max_get_chunk: int


@dataclass(frozen=True)
class TransferOpenReply:
    state: TransferState
    error: TransferRemoteError


@dataclass(frozen=True)
class TransferProgressReply:
    accepted: int
    accepted_end: int
    durable_end: int
    credit: int
    state: TransferState
    error: TransferRemoteError


@dataclass(frozen=True)
class TransferDataReply:
    offset: int
    data: bytes
    state: TransferState
    error: TransferRemoteError


@dataclass(frozen=True)
class TransferAckReply:
    durable_offset: int
    state: TransferState
    error: TransferRemoteError


@dataclass(frozen=True)
class TransferStatusReply:
    state: TransferState
    direction: TransferDirection
    encoding: TransferEncoding
    error: TransferRemoteError
    flags: TransferReplyFlag
    transfer_id: bytes
    wire_size: int
    wire_crc32: int
    final_size: int
    final_crc32: int
    durable_offset: int
    accepted_offset: int
    prefix_crc32: int
    credit: int


@dataclass(frozen=True)
class TransferTerminalReply:
    state: TransferState
    error: TransferRemoteError


# Explicit ABI names retained alongside the shorter public dataclass names.
TransferPutDataReply = TransferProgressReply
TransferGetReadReply = TransferDataReply


def _payload(payload: bytes | bytearray | memoryview, size: int, name: str) -> bytes:
    try:
        result = bytes(payload)
    except (TypeError, ValueError) as exc:
        raise TransferPayloadError(f"{name} payload must be bytes-like") from exc
    if len(result) != size:
        raise TransferPayloadError(
            f"{name} reply is {len(result)} bytes; expected exactly {size}")
    return result


def _expected_id(actual: bytes, expected: bytes | None) -> bytes:
    actual = _transfer_id(actual)
    if expected is not None and actual != _transfer_id(expected):
        raise TransferBindingError(
            f"reply transfer ID {actual.hex()} does not match {bytes(expected).hex()}")
    return actual


def _reply_state(value: int, name: str) -> TransferState:
    try:
        return TransferState(value)
    except ValueError as exc:
        raise TransferPayloadError(
            f"{name} reply has unknown state {value}") from exc


def _reply_error(value: int, name: str) -> TransferRemoteError:
    try:
        return TransferRemoteError(value)
    except ValueError as exc:
        raise TransferPayloadError(
            f"{name} reply has unknown error {value}") from exc


def _reply_flags(value: int, name: str) -> TransferReplyFlag:
    known = int(
        TransferReplyFlag.ACTIVE | TransferReplyFlag.RESUMABLE |
        TransferReplyFlag.WIRE_VERIFIED | TransferReplyFlag.FINAL_VERIFIED |
        TransferReplyFlag.PUBLISHED)
    if value & ~known:
        raise TransferPayloadError(
            f"{name} reply has unknown flag bits 0x{value:02X}")
    return TransferReplyFlag(value)


def parse_capabilities_reply(payload: bytes | bytearray | memoryview
                             ) -> TransferCapabilitiesReply:
    payload = _payload(
        payload, _CAPABILITIES_REPLY.size, "CAPABILITIES")
    version, capability_bits, put_chunk, get_chunk, max_path = (
        _CAPABILITIES_REPLY.unpack(payload))
    if version != TRANSFER_PROTOCOL_VERSION:
        raise TransferPayloadError(
            f"unsupported transfer version {version}; expected "
            f"{TRANSFER_PROTOCOL_VERSION}")
    known = int(
        TransferCapability.RAW | TransferCapability.PUT |
        TransferCapability.GET | TransferCapability.RESUME |
        TransferCapability.CRC32 | TransferCapability.PACKBITS_DECODE |
        TransferCapability.PACKBITS_ENCODE)
    if capability_bits & ~known:
        raise TransferPayloadError(
            f"CAPABILITIES contains unknown bits 0x{capability_bits:08X}")
    capabilities = TransferCapability(capability_bits)
    if capabilities & TransferCapability.PUT and put_chunk == 0:
        raise TransferPayloadError("PUT capability advertises a zero-byte chunk")
    if capabilities & TransferCapability.GET and get_chunk == 0:
        raise TransferPayloadError("GET capability advertises a zero-byte chunk")
    if (capabilities & (TransferCapability.PUT | TransferCapability.GET) and
            max_path == 0):
        raise TransferPayloadError("file transfer advertises a zero-byte path limit")
    return TransferCapabilitiesReply(
        version, capabilities, put_chunk, get_chunk, max_path)


parse_caps_reply = parse_capabilities_reply


def parse_fast_capabilities_reply(payload: bytes | bytearray | memoryview
                                  ) -> TransferFastCapabilitiesReply:
    """Parse the required fast-v1 stream-pump capability response."""

    payload = _payload(payload, _FAST_CAPABILITIES_REPLY.size,
                       "FAST_CAPABILITIES")
    version, capability_bits, put_chunk, get_chunk = (
        _FAST_CAPABILITIES_REPLY.unpack(payload))
    if version != 1:
        raise TransferPayloadError(
            f"unsupported fast transfer version {version}; expected 1")
    known = int(
        TransferFastCapability.PUMP | TransferFastCapability.STREAM)
    if capability_bits & ~known:
        raise TransferPayloadError(
            f"FAST_CAPABILITIES contains unknown bits 0x{capability_bits:02X}")
    capabilities = TransferFastCapability(capability_bits)
    if capabilities & TransferFastCapability.PUMP and (
            put_chunk == 0 or get_chunk == 0):
        raise TransferPayloadError(
            "FAST_CAPABILITIES advertises a zero-byte chunk")
    return TransferFastCapabilitiesReply(
        version, capabilities, put_chunk, get_chunk)


def encode_fast_begin(transfer_id: bytes) -> bytes:
    """Arm fast pumping only after the DOS helper has reached READY."""

    return _ID_REQUEST.pack(
        TransferSubcommand.FAST_BEGIN, _transfer_id(transfer_id))


def parse_open_reply(payload: bytes | bytearray | memoryview) -> TransferOpenReply:
    payload = _payload(payload, _OPEN_REPLY.size, "OPEN")
    state, error = _OPEN_REPLY.unpack(payload)
    return TransferOpenReply(
        _reply_state(state, "OPEN"), _reply_error(error, "OPEN"))


def encode_put_data(transfer_id: bytes, offset: int,
                    data: bytes | bytearray | memoryview) -> bytes:
    data = bytes(data)
    if not data:
        raise TransferError("PUT_DATA must contain at least one byte")
    # The framed-v3 payload is capped at 65535 bytes; this request has a
    # 21-byte fixed prefix and intentionally carries no redundant length.
    if len(data) > 0xFFFF - _PUT_DATA_REQUEST.size:
        raise TransferError("PUT_DATA exceeds the framed-v3 payload limit")
    return _PUT_DATA_REQUEST.pack(
        TransferSubcommand.PUT_DATA, _transfer_id(transfer_id),
        _u32("offset", offset)) + data


def parse_put_data_reply(payload: bytes | bytearray | memoryview
                         ) -> TransferProgressReply:
    payload = _payload(payload, _PUT_DATA_REPLY.size, "PUT_DATA")
    accepted, accepted_end, durable_end, credit, state, error = (
        _PUT_DATA_REPLY.unpack(payload))
    if durable_end > accepted_end:
        raise TransferPayloadError("PUT_DATA durable_end exceeds accepted_end")
    return TransferProgressReply(
        accepted, accepted_end, durable_end, credit,
        _reply_state(state, "PUT_DATA"), _reply_error(error, "PUT_DATA"))


parse_progress_reply = parse_put_data_reply


def encode_get_read(transfer_id: bytes, offset: int, maximum: int) -> bytes:
    maximum = _u16("maximum", maximum)
    if maximum == 0:
        raise TransferError("GET_READ maximum must be greater than zero")
    return _GET_READ_REQUEST.pack(
        TransferSubcommand.GET_READ, _transfer_id(transfer_id),
        _u32("offset", offset), maximum)


def parse_get_read_reply(payload: bytes | bytearray | memoryview
                         ) -> TransferDataReply:
    payload = bytes(payload)
    if len(payload) < _GET_READ_HEADER.size:
        raise TransferPayloadError(
            f"GET_READ reply is {len(payload)} bytes; minimum is "
            f"{_GET_READ_HEADER.size}")
    offset, length, state, error = _GET_READ_HEADER.unpack_from(payload)
    expected = _GET_READ_HEADER.size + length
    if len(payload) != expected:
        raise TransferPayloadError(
            f"GET_READ reply is {len(payload)} bytes; declared length requires {expected}")
    return TransferDataReply(
        offset, payload[_GET_READ_HEADER.size:],
        _reply_state(state, "GET_READ"), _reply_error(error, "GET_READ"))


def encode_get_ack(transfer_id: bytes, next_offset: int,
                   prefix_crc32: int) -> bytes:
    return _GET_ACK_REQUEST.pack(
        TransferSubcommand.GET_ACK, _transfer_id(transfer_id),
        _u32("next_offset", next_offset), _u32("prefix_crc32", prefix_crc32))


def parse_get_ack_reply(payload: bytes | bytearray | memoryview
                        ) -> TransferAckReply:
    payload = _payload(payload, _GET_ACK_REPLY.size, "GET_ACK")
    durable, state, error = _GET_ACK_REPLY.unpack(payload)
    return TransferAckReply(
        durable, _reply_state(state, "GET_ACK"),
        _reply_error(error, "GET_ACK"))


def encode_status(transfer_id: bytes) -> bytes:
    return _ID_REQUEST.pack(
        TransferSubcommand.STATUS, _transfer_id(transfer_id))


def parse_status_reply(payload: bytes | bytearray | memoryview, *,
                       expected_transfer_id: bytes | None = None
                       ) -> TransferStatusReply:
    payload = _payload(payload, _STATUS_REPLY.size, "STATUS")
    (state, direction, encoding, error, flags, transfer_id, wire_size,
     wire_crc, final_size, final_crc, durable_offset, accepted_offset,
     prefix_crc, credit) = (
        _STATUS_REPLY.unpack(payload))
    try:
        direction = TransferDirection(direction)
        encoding = TransferEncoding(encoding)
    except ValueError as exc:
        raise TransferPayloadError(
            "STATUS reply has unknown direction or encoding") from exc
    if durable_offset > accepted_offset:
        raise TransferPayloadError("STATUS durable offset exceeds accepted offset")
    if accepted_offset > wire_size:
        raise TransferPayloadError("STATUS accepted offset exceeds wire size")
    return TransferStatusReply(
        _reply_state(state, "STATUS"), direction, encoding,
        _reply_error(error, "STATUS"), _reply_flags(flags, "STATUS"),
        _expected_id(transfer_id, expected_transfer_id), wire_size, wire_crc,
        final_size, final_crc, durable_offset, accepted_offset, prefix_crc,
        credit)


def encode_close(transfer_id: bytes, *, rate_bps: int) -> bytes:
    return _FAST_CLOSE_REQUEST.pack(
        TransferSubcommand.CLOSE, _transfer_id(transfer_id),
        _u16("rate_bps", rate_bps))


def encode_cancel(transfer_id: bytes) -> bytes:
    return _ID_REQUEST.pack(
        TransferSubcommand.CANCEL, _transfer_id(transfer_id))


def _parse_terminal_reply(payload: bytes | bytearray | memoryview,
                          name: str) -> TransferTerminalReply:
    payload = _payload(payload, _TERMINAL_REPLY.size, name)
    state, error = _TERMINAL_REPLY.unpack(payload)
    return TransferTerminalReply(
        _reply_state(state, name), _reply_error(error, name))


def parse_close_reply(payload: bytes | bytearray | memoryview
                      ) -> TransferTerminalReply:
    return _parse_terminal_reply(payload, "CLOSE")


def parse_cancel_reply(payload: bytes | bytearray | memoryview
                       ) -> TransferTerminalReply:
    return _parse_terminal_reply(payload, "CANCEL")


@dataclass(frozen=True)
class FileDigest:
    size: int
    crc32: int


def crc32_update(data: bytes | bytearray | memoryview,
                 checksum: int = 0) -> int:
    """Increment a CRC-32/ISO-HDLC value with one bytes-like block."""

    checksum = _u32("checksum", checksum)
    try:
        data = bytes(data)
    except (TypeError, ValueError) as exc:
        raise TransferError("CRC-32 data must be bytes-like") from exc
    return zlib.crc32(data, checksum) & UINT32_MAX


def crc32_stream(stream: BinaryIO, *, chunk_size: int = DEFAULT_IO_CHUNK_SIZE,
                 max_size: int = UINT32_MAX) -> FileDigest:
    """Hash a stream incrementally, refusing to read beyond ``max_size``."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise TransferError("chunk_size must be a positive integer")
    max_size = _u32("max_size", max_size)
    size = 0
    checksum = 0
    while True:
        remaining = max_size - size
        # One extra byte distinguishes an exact-limit file from an oversized one.
        block = stream.read(min(chunk_size, remaining + 1))
        if not block:
            break
        if not isinstance(block, (bytes, bytearray, memoryview)):
            raise TransferError("binary stream read returned non-bytes data")
        block = bytes(block)
        if len(block) > remaining:
            raise TransferError(f"stream exceeds maximum size {max_size}")
        size += len(block)
        checksum = crc32_update(block, checksum)
    return FileDigest(size, checksum & UINT32_MAX)


def crc32_file(path: str | os.PathLike[str], *,
               chunk_size: int = DEFAULT_IO_CHUNK_SIZE,
               max_size: int = UINT32_MAX) -> FileDigest:
    """Hash a file without loading it into memory."""

    with open(path, "rb") as source:
        return crc32_stream(source, chunk_size=chunk_size, max_size=max_size)


def crc32_file_prefix(path: str | os.PathLike[str], length: int, *,
                      chunk_size: int = DEFAULT_IO_CHUNK_SIZE) -> FileDigest:
    """Hash exactly ``length`` leading bytes for safe resume reconciliation."""

    length = _u32("length", length)
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise TransferError("chunk_size must be a positive integer")
    size = 0
    checksum = 0
    with open(path, "rb") as source:
        while size < length:
            block = source.read(min(chunk_size, length - size))
            if not block:
                raise TransferError(
                    f"file ended at {size} bytes; resume prefix requires {length}")
            size += len(block)
            checksum = crc32_update(block, checksum)
    return FileDigest(size, checksum)


_COMPRESSED_EXTENSIONS = frozenset({
    ".7z", ".avi", ".bz2", ".flac", ".gif", ".gz", ".jpeg", ".jpg",
    ".lz4", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".png", ".rar",
    ".tgz", ".webm", ".webp", ".xz", ".zip", ".zst",
})
_COMPRESSED_MAGIC = (
    b"\x1f\x8b",                 # gzip
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",  # ZIP
    b"BZh",                       # bzip2
    b"\xfd7zXZ\x00",             # xz
    b"7z\xbc\xaf\x27\x1c",      # 7-Zip
    b"Rar!\x1a\x07",             # RAR
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",             # JPEG
    b"GIF87a", b"GIF89a",
    b"\x28\xb5\x2f\xfd",         # zstd
    b"\x04\x22\x4d\x18",         # LZ4 frame
)


def looks_already_compressed(path: str | os.PathLike[str]) -> bool:
    """Identify common compressed/archive/media files without parsing them."""

    source = Path(path)
    if source.suffix.lower() in _COMPRESSED_EXTENSIONS:
        return True
    with source.open("rb") as handle:
        prefix = handle.read(max(map(len, _COMPRESSED_MAGIC)))
    return any(prefix.startswith(magic) for magic in _COMPRESSED_MAGIC)


@dataclass(frozen=True)
class PreparedPayload:
    """A raw source or an owned deterministic-PackBits staging file."""

    source_path: Path
    wire_path: Path
    encoding: TransferEncoding
    final_digest: FileDigest
    wire_digest: FileDigest
    temporary: bool
    reason: str

    def cleanup(self) -> None:
        if self.temporary:
            try:
                self.wire_path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "PreparedPayload":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.cleanup()


@dataclass(frozen=True)
class PreparedBasicSource:
    """Original or owned canonical MSX-DOS BASIC source image."""

    source_path: Path
    transfer_path: Path
    source_size: int
    basic_format: str | None
    normalization: str | None
    temporary: bool

    def cleanup(self) -> None:
        if self.temporary:
            try:
                self.transfer_path.unlink()
            except FileNotFoundError:
                pass


def _normalize_msx_basic_stream(source: BinaryIO, output: BinaryIO, *,
                                chunk_size: int) -> int:
    """Stream a numbered 8-bit listing into canonical MSX-DOS text."""
    if (isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or
            chunk_size <= 0):
        raise TransferError("chunk_size must be a positive integer")

    source_size = 0
    line_start = output.tell()
    line_numbered = False
    saw_statement = False
    pending_cr = False
    eof_seen = False
    pending_output = bytearray()

    def flush_pending() -> None:
        if pending_output:
            _write_stream_exact(output, pending_output)
            pending_output.clear()

    def finish_line() -> None:
        nonlocal line_start, line_numbered, saw_statement
        if line_numbered:
            flush_pending()
            _write_stream_exact(output, b"\r\n")
            saw_statement = True
        else:
            # Whitespace-only host rows are not BASIC program lines. Remove
            # them rather than creating a direct statement in the file.
            output.seek(line_start)
            output.truncate()
        line_start = output.tell()
        line_numbered = False

    while True:
        block = source.read(chunk_size)
        if not block:
            break
        source_size += len(block)
        for value in block:
            if eof_seen:
                if value != 0x1A:
                    raise TransferError(
                        "ASCII MSX BASIC contains data after its 0x1A EOF marker")
                continue

            if pending_cr:
                finish_line()
                pending_cr = False
                if value == 0x0A:
                    continue

            if value == 0x1A:
                finish_line()
                eof_seen = True
            elif value == 0x0D:
                pending_cr = True
            elif value == 0x0A:
                finish_line()
            elif value < 0x20 and value != 0x09:
                raise TransferError(
                    f"ASCII MSX BASIC contains unsupported control byte "
                    f"0x{value:02X}")
            else:
                if not line_numbered:
                    if value in (0x09, 0x20):
                        _write_stream_exact(output, bytes((value,)))
                        continue
                    if not 0x30 <= value <= 0x39:
                        raise TransferError(
                            "ambiguous textual .BAS contains a source line "
                            "without an MSX BASIC line number")
                    line_numbered = True
                pending_output.append(value)
                if len(pending_output) >= chunk_size:
                    flush_pending()

    if pending_cr:
        finish_line()
    elif not eof_seen:
        finish_line()
    if not saw_statement:
        raise TransferError("ASCII MSX BASIC contains no numbered program lines")
    _write_stream_exact(output, b"\x1a")
    return source_size


def normalize_msx_basic_text(data: bytes, *,
                             chunk_size: int = DEFAULT_IO_CHUNK_SIZE) -> bytes:
    """Normalize an in-memory 8-bit listing with the streaming implementation."""
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    source = io.BytesIO(data)
    output = io.BytesIO()
    _normalize_msx_basic_stream(source, output, chunk_size=chunk_size)
    return output.getvalue()


def prepare_msx_basic_source(
        source: str | os.PathLike[str], target: str, *,
        state_directory: str | os.PathLike[str],
        chunk_size: int = DEFAULT_IO_CHUNK_SIZE) -> PreparedBasicSource:
    """Stage textual `.BAS` as canonical MSX-DOS text when unambiguous.

    Non-BASIC targets and tokenized BASIC beginning with FFh remain byte-exact.
    Text normalization is completed before compression, hashing, journalling,
    or any target I/O.
    """
    source_path = Path(source)
    source_stat = source_path.stat()
    source_size = source_stat.st_size
    target_name = str(target).replace("\\", "/").rsplit("/", 1)[-1]
    if not target_name.lower().endswith(".bas"):
        return PreparedBasicSource(
            source_path, source_path, source_size, None, None, False)
    with source_path.open("rb") as handle:
        if handle.read(1) == b"\xff":
            return PreparedBasicSource(
                source_path, source_path, source_size,
                "tokenized", "none", False)

    directory = Path(state_directory)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".msx-basic-", suffix=".bas", dir=directory)
    transfer_path = Path(name)
    try:
        with source_path.open("rb") as input_file, os.fdopen(
                descriptor, "w+b") as output_file:
            counted_size = _normalize_msx_basic_stream(
                input_file, output_file, chunk_size=chunk_size)
        final_stat = source_path.stat()
        if (counted_size != source_size or
                (final_stat.st_dev, final_stat.st_ino, final_stat.st_size,
                 final_stat.st_mtime_ns) !=
                (source_stat.st_dev, source_stat.st_ino, source_stat.st_size,
                 source_stat.st_mtime_ns)):
            raise TransferBindingError(
                "BASIC source changed while it was being normalized")
        return PreparedBasicSource(
            source_path, transfer_path, source_size,
            "ascii-msx-dos", "crlf-plus-0x1a", True)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            transfer_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_stream_exact(output: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = output.write(view[offset:])
        if written is None:
            written = len(view) - offset
        if isinstance(written, bool) or not isinstance(written, int) or written <= 0:
            raise OSError("short write while staging transfer data")
        offset += written


class _PackBitsWireLimit(TransferError):
    """The encoded representation overflowed a valid raw size field."""


def _deterministic_packbits(source_path: Path, directory: Path, *,
                            chunk_size: int, max_size: int) -> PreparedPayload:
    """Create a canonical bounded-memory PackBits stream.

    Literal packets contain 1..128 bytes. Runs are emitted only for lengths
    3..128, so reserved control 80h and the ambiguous two-byte run FFh never
    occur. The encoder carries at most one 128-byte literal and one run across
    host read boundaries.
    """

    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".msx-transfer-", suffix=".packbits", dir=directory)
    wire_path = Path(name)
    final_size = 0
    final_crc = 0
    wire_size = 0
    wire_crc = 0
    literal = bytearray()
    run_byte: int | None = None
    run_length = 0

    def emit(output: BinaryIO, packet: bytes) -> None:
        nonlocal wire_size, wire_crc
        if len(packet) > max_size - wire_size:
            raise _PackBitsWireLimit(
                f"PackBits stream exceeds maximum size {max_size}")
        _write_stream_exact(output, packet)
        wire_size += len(packet)
        wire_crc = zlib.crc32(packet, wire_crc)

    def flush_literals(output: BinaryIO, *, final: bool) -> None:
        while len(literal) >= 128 or final and literal:
            count = min(128, len(literal))
            packet = bytes((count - 1,)) + bytes(literal[:count])
            del literal[:count]
            emit(output, packet)

    def settle_run(output: BinaryIO) -> None:
        nonlocal run_byte, run_length
        if run_length >= 3:
            flush_literals(output, final=True)
            emit(output, bytes((257 - run_length, run_byte)))
        elif run_length:
            literal.extend((run_byte,) * run_length)
            flush_literals(output, final=False)
        run_byte = None
        run_length = 0

    try:
        with source_path.open("rb") as source, os.fdopen(descriptor, "wb") as output:
            while True:
                remaining = max_size - final_size
                block = source.read(min(chunk_size, remaining + 1))
                if not block:
                    break
                if len(block) > remaining:
                    raise TransferError(f"source exceeds maximum size {max_size}")
                final_size += len(block)
                final_crc = zlib.crc32(block, final_crc)
                for value in block:
                    if run_length and value == run_byte:
                        if run_length == 128:
                            settle_run(output)
                            run_byte = value
                            run_length = 1
                        else:
                            run_length += 1
                    else:
                        settle_run(output)
                        run_byte = value
                        run_length = 1
            settle_run(output)
            flush_literals(output, final=True)
        return PreparedPayload(
            source_path=source_path,
            wire_path=wire_path,
            encoding=TransferEncoding.PACKBITS,
            final_digest=FileDigest(final_size, final_crc & UINT32_MAX),
            wire_digest=FileDigest(wire_size, wire_crc & UINT32_MAX),
            temporary=True,
            reason="PackBits requested",
        )
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            wire_path.unlink()
        except FileNotFoundError:
            pass
        raise


def prepare_put_payload(
        source: str | os.PathLike[str], *,
        state_directory: str | os.PathLike[str], mode: str = "auto",
        chunk_size: int = DEFAULT_IO_CHUNK_SIZE,
        max_size: int = UINT32_MAX) -> PreparedPayload:
    """Choose raw or deterministic PackBits for a PUT.

    ``auto`` keeps known compressed formats (including ZIP) byte-for-byte raw.
    Otherwise PackBits is selected only if it saves at least the larger of 256
    bytes or three percent of the original size.  ``raw`` never stages a file;
    ``packbits`` forces PackBits and is intended for an explicit override.
    """

    source_path = Path(source)
    if mode not in {"auto", "raw", "packbits"}:
        raise TransferError("mode must be 'auto', 'raw', or 'packbits'")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise TransferError("chunk_size must be a positive integer")
    max_size = _u32("max_size", max_size)

    def raw_result(reason: str, digest: FileDigest | None = None) -> PreparedPayload:
        digest = digest or crc32_file(
            source_path, chunk_size=chunk_size, max_size=max_size)
        return PreparedPayload(
            source_path, source_path, TransferEncoding.RAW,
            digest, digest, False, reason)

    if mode == "raw":
        return raw_result("raw requested")
    if mode == "auto" and looks_already_compressed(source_path):
        return raw_result("already compressed")

    try:
        compressed = _deterministic_packbits(
            source_path, Path(state_directory), chunk_size=chunk_size,
            max_size=max_size)
    except _PackBitsWireLimit:
        if mode == "packbits":
            raise
        return raw_result("PackBits expansion exceeds the wire-size limit")
    if mode == "packbits":
        return compressed

    savings = compressed.final_digest.size - compressed.wire_digest.size
    required = max(256, (compressed.final_digest.size * 3 + 99) // 100)
    if savings >= required:
        return replace(compressed, reason=f"PackBits saves {savings} bytes")
    compressed.cleanup()
    return raw_result(
        f"PackBits savings {savings} bytes are below {required}",
        compressed.final_digest)


@dataclass(frozen=True)
class TransferJournalRecord:
    descriptor: TransferDescriptor
    confirmed_offset: int
    prefix_crc32: int
    caller_binding: str | None = None
    close_intent: bool = False

    def resumed_descriptor(self) -> TransferDescriptor:
        return replace(
            self.descriptor, resume_offset=self.confirmed_offset,
            resume_prefix_crc32=self.prefix_crc32, resume=True,
            receiptless_replay=self.close_intent)


class TransferJournal:
    """Atomic host-side resume metadata stored below a caller-owned directory."""

    _VERSION = 4
    _V1_KEYS = frozenset({
        "version", "transfer_id", "direction", "encoding", "wire_size",
        "wire_crc32", "final_size", "final_crc32", "path",
        "confirmed_offset", "prefix_crc32", "caller_binding",
    })
    _V2_KEYS = _V1_KEYS | {"close_intent"}
    _V3_KEYS = _V2_KEYS | {"data_plane"}
    _KEYS = _V2_KEYS

    def __init__(self, state_directory: str | os.PathLike[str]):
        self.state_directory = Path(state_directory)

    def path_for(self, transfer_id: bytes) -> Path:
        return self.state_directory / f"{_transfer_id(transfer_id).hex()}.json"

    @staticmethod
    def _document(descriptor: TransferDescriptor, offset: int,
                  prefix_crc32: int,
                  caller_binding: str | None,
                  close_intent: bool) -> dict[str, object]:
        return {
            "version": TransferJournal._VERSION,
            "transfer_id": descriptor.transfer_id_hex,
            "direction": descriptor.direction.name.lower(),
            "encoding": descriptor.encoding.name.lower(),
            "wire_size": descriptor.wire_size,
            "wire_crc32": f"{descriptor.wire_crc32:08x}",
            "final_size": descriptor.final_size,
            "final_crc32": f"{descriptor.final_crc32:08x}",
            "path": descriptor.path,
            "confirmed_offset": offset,
            "prefix_crc32": f"{prefix_crc32:08x}",
            "caller_binding": caller_binding,
            "close_intent": close_intent,
        }

    @staticmethod
    def _caller_binding(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TransferJournalError("caller_binding must be a string or None")
        if not value or len(value) > 2048 or "\x00" in value:
            raise TransferJournalError(
                "caller_binding must contain 1 through 2048 non-NUL characters")
        return value

    def save(self, descriptor: TransferDescriptor, *, confirmed_offset: int,
             prefix_crc32: int, caller_binding: str | None = None,
             close_intent: bool = False) -> Path:
        if not isinstance(descriptor, TransferDescriptor):
            raise TransferJournalError("descriptor must be a TransferDescriptor")
        confirmed_offset = _u32("confirmed_offset", confirmed_offset)
        prefix_crc32 = _u32("prefix_crc32", prefix_crc32)
        metadata_unknown = (
            descriptor.direction is TransferDirection.GET and
            descriptor.wire_size == descriptor.wire_crc32 ==
            descriptor.final_size == descriptor.final_crc32 == 0)
        if confirmed_offset > descriptor.wire_size and not metadata_unknown:
            raise TransferJournalError("confirmed_offset exceeds wire_size")
        if confirmed_offset == 0 and prefix_crc32 != 0:
            raise TransferJournalError(
                "prefix_crc32 must be zero when confirmed_offset is zero")
        if not isinstance(close_intent, bool):
            raise TransferJournalError("close_intent must be a boolean")
        if close_intent and (
                descriptor.direction is not TransferDirection.PUT or
                confirmed_offset != descriptor.wire_size or
                prefix_crc32 != descriptor.wire_crc32):
            raise TransferJournalError(
                "close_intent requires a complete CRC-matched PUT boundary")

        self.state_directory.mkdir(parents=True, exist_ok=True)
        destination = self.path_for(descriptor.transfer_id)
        caller_binding = self._caller_binding(caller_binding)
        try:
            existing = self._load_path(destination)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            old = existing.descriptor
            get_metadata_promotion = (
                old.direction is TransferDirection.GET and
                descriptor.direction is TransferDirection.GET and
                old.encoding is descriptor.encoding and
                old.transfer_id == descriptor.transfer_id and
                old.path == descriptor.path and
                old.wire_size == old.wire_crc32 ==
                old.final_size == old.final_crc32 == 0)
            if ((old.binding() != descriptor.binding() and
                 not get_metadata_promotion) or
                    existing.caller_binding != caller_binding):
                raise TransferBindingError(
                    "refusing to replace a journal with a different binding")
            if existing.close_intent and not close_intent:
                raise TransferJournalError(
                    "refusing to clear a durable PUT close intent")
        document = self._document(
            descriptor, confirmed_offset, prefix_crc32, caller_binding,
            close_intent)
        encoded = (json.dumps(
            document, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise TransferJournalError("journal document exceeds size limit")

        descriptor_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{descriptor.transfer_id_hex}.", suffix=".tmp",
            dir=self.state_directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor_fd, 0o600)
            with os.fdopen(descriptor_fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            try:
                directory_fd = os.open(self.state_directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not support directory fsync.  The file
                # replacement remains atomic even there.
                pass
        except BaseException:
            try:
                os.close(descriptor_fd)
            except OSError:
                pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return destination

    @staticmethod
    def _hex_crc(document: dict[str, object], key: str) -> int:
        value = document[key]
        if (not isinstance(value, str) or len(value) != 8 or
                any(character not in "0123456789abcdef" for character in value)):
            raise TransferJournalError(f"journal {key} must be 8 lowercase hex digits")
        return int(value, 16)

    @staticmethod
    def _read_no_follow(path: Path) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor_fd = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise TransferJournalError(
                f"cannot safely open journal {path.name}: {exc}") from exc
        try:
            details = os.fstat(descriptor_fd)
            if not stat.S_ISREG(details.st_mode):
                raise TransferJournalError("journal is not a regular file")
            with os.fdopen(descriptor_fd, "rb") as handle:
                descriptor_fd = -1
                return handle.read(MAX_JOURNAL_BYTES + 1)
        finally:
            if descriptor_fd >= 0:
                os.close(descriptor_fd)

    def _load_path(self, path: Path) -> TransferJournalRecord:
        encoded = self._read_no_follow(path)
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise TransferJournalError("journal exceeds size limit")
        try:
            document = json.loads(encoded.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransferJournalError("journal is not canonical ASCII JSON") from exc
        if not isinstance(document, dict):
            raise TransferJournalError("journal document must be an object")
        version = document.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise TransferJournalError("unsupported journal version")
        legacy_protocol = False
        if version == 1:
            expected_keys = self._V1_KEYS
            close_intent = False
            legacy_protocol = True
        elif version == 2:
            expected_keys = self._V2_KEYS
            close_intent = document.get("close_intent")
            if not isinstance(close_intent, bool):
                raise TransferJournalError(
                    "journal close_intent must be a boolean")
            legacy_protocol = True
        elif version == 3:
            expected_keys = self._V3_KEYS
            if set(document) != expected_keys:
                raise TransferJournalError(
                    "journal fields do not match version 3 schema")
            if document.get("data_plane") != "fast-v1":
                legacy_protocol = True
            close_intent = document.get("close_intent")
            if not isinstance(close_intent, bool):
                raise TransferJournalError(
                    "journal close_intent must be a boolean")
        elif version == self._VERSION:
            expected_keys = self._KEYS
            close_intent = document.get("close_intent")
            if not isinstance(close_intent, bool):
                raise TransferJournalError(
                    "journal close_intent must be a boolean")
        else:
            raise TransferJournalError("unsupported journal version")
        if set(document) != expected_keys:
            raise TransferJournalError(
                f"journal fields do not match version {version} schema")
        try:
            transfer_id_text = document["transfer_id"]
            if (not isinstance(transfer_id_text, str) or len(transfer_id_text) != 32 or
                    any(character not in "0123456789abcdef"
                        for character in transfer_id_text)):
                raise TransferJournalError(
                    "journal transfer_id must be 32 lowercase hex digits")
            if (len(path.name) == 37 and path.name.endswith(".json") and
                    path.name[:32] != transfer_id_text):
                raise TransferJournalError(
                    "journal filename does not match its transfer_id")
            direction_name = document["direction"]
            encoding_name = document["encoding"]
            if not isinstance(direction_name, str) or not isinstance(encoding_name, str):
                raise TransferJournalError("journal direction/encoding must be strings")
            direction = TransferDirection[direction_name.upper()]
            encoding = TransferEncoding[encoding_name.upper()]
            wire_size = document["wire_size"]
            final_size = document["final_size"]
            offset = document["confirmed_offset"]
            if any(isinstance(value, bool) or not isinstance(value, int)
                   for value in (wire_size, final_size, offset)):
                raise TransferJournalError("journal sizes/offset must be integers")
            caller_binding = self._caller_binding(document["caller_binding"])
            saved = TransferDescriptor(
                direction=direction,
                encoding=encoding,
                transfer_id=bytes.fromhex(transfer_id_text),
                wire_size=wire_size,
                wire_crc32=self._hex_crc(document, "wire_crc32"),
                final_size=final_size,
                final_crc32=self._hex_crc(document, "final_crc32"),
                path=document["path"],
            )
            prefix_crc = self._hex_crc(document, "prefix_crc32")
            record = TransferJournalRecord(
                descriptor=saved,
                confirmed_offset=_u32("confirmed_offset", offset),
                prefix_crc32=prefix_crc,
                caller_binding=caller_binding,
                close_intent=close_intent)
        except (KeyError, TypeError, ValueError, TransferError) as exc:
            if isinstance(exc, TransferJournalError):
                raise
            raise TransferJournalError(f"invalid journal value: {exc}") from exc
        metadata_unknown = (
            saved.direction is TransferDirection.GET and
            saved.wire_size == saved.wire_crc32 ==
            saved.final_size == saved.final_crc32 == 0)
        if record.confirmed_offset > saved.wire_size and not metadata_unknown:
            raise TransferJournalError("journal confirmed_offset exceeds wire_size")
        if record.confirmed_offset == 0 and record.prefix_crc32 != 0:
            raise TransferJournalError(
                "journal prefix_crc32 must be zero at offset zero")
        if record.close_intent and (
                saved.direction is not TransferDirection.PUT or
                record.confirmed_offset != saved.wire_size or
                record.prefix_crc32 != saved.wire_crc32):
            raise TransferJournalError(
                "journal close_intent lacks a complete PUT boundary")
        if legacy_protocol:
            raise TransferJournalLegacyError(
                "journal belongs to the removed legacy transfer protocol",
                record)
        return record

    def load(self, expected: TransferDescriptor, *,
             caller_binding: str | None = None) -> TransferJournalRecord | None:
        if not isinstance(expected, TransferDescriptor):
            raise TransferJournalError("expected must be a TransferDescriptor")
        path = self.path_for(expected.transfer_id)
        try:
            record = self._load_path(path)
        except FileNotFoundError:
            return None
        if record.descriptor.binding() != expected.binding():
            raise TransferBindingError(
                "journal descriptor does not exactly match the requested transfer")
        caller_binding = self._caller_binding(caller_binding)
        if (caller_binding is not None and
                record.caller_binding != caller_binding):
            raise TransferBindingError(
                "journal caller binding does not match the requested local file")
        return record

    @staticmethod
    def _matches_without_id(saved: TransferDescriptor,
                            expected: TransferDescriptor) -> bool:
        if (saved.direction != expected.direction or
                saved.encoding != expected.encoding or
                saved.path != expected.path):
            return False
        get_discovery = (
            expected.direction is TransferDirection.GET and
            expected.wire_size == expected.wire_crc32 ==
            expected.final_size == expected.final_crc32 == 0)
        return get_discovery or (
            saved.wire_size == expected.wire_size and
            saved.wire_crc32 == expected.wire_crc32 and
            saved.final_size == expected.final_size and
            saved.final_crc32 == expected.final_crc32)

    def find_matching(
            self, expected: TransferDescriptor, *,
            caller_binding: str | None = None,
            max_entries: int = 256) -> TransferJournalRecord | None:
        """Discover a restart journal while deliberately ignoring its random ID.

        Only regular ``<32 lowercase hex>.json`` entries are inspected.  The
        scan is capped and refuses ambiguity instead of choosing an arbitrary
        partial transfer.
        """

        if not isinstance(expected, TransferDescriptor):
            raise TransferJournalError("expected must be a TransferDescriptor")
        if (isinstance(max_entries, bool) or not isinstance(max_entries, int) or
                not 1 <= max_entries <= 256):
            raise TransferJournalError("max_entries must be between 1 and 256")
        caller_binding = self._caller_binding(caller_binding)
        try:
            iterator = os.scandir(self.state_directory)
        except FileNotFoundError:
            return None
        matches: list[TransferJournalRecord] = []
        inspected = 0
        with iterator:
            for entry in iterator:
                name = entry.name
                if (len(name) != 37 or not name.endswith(".json") or
                        any(character not in "0123456789abcdef"
                            for character in name[:32])):
                    continue
                inspected += 1
                if inspected > max_entries:
                    raise TransferJournalError(
                        f"journal scan exceeds the {max_entries}-entry limit")
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    record = self._load_path(Path(entry.path))
                except TransferJournalLegacyError as exc:
                    if (self._matches_without_id(
                            exc.record.descriptor, expected) and
                            (caller_binding is None or
                             exc.record.caller_binding == caller_binding)):
                        raise
                    continue
                if not self._matches_without_id(record.descriptor, expected):
                    continue
                if (caller_binding is not None and
                        record.caller_binding != caller_binding):
                    continue
                matches.append(record)
        if len(matches) > 1:
            raise TransferJournalError(
                "multiple resume journals match the same transfer binding")
        return matches[0] if matches else None

    def remove(self, expected: TransferDescriptor, *,
               caller_binding: str | None = None) -> bool:
        """Remove a journal only after validating its descriptor binding."""

        record = self.load(expected, caller_binding=caller_binding)
        if record is None:
            return False
        self.path_for(expected.transfer_id).unlink()
        return True
