import pathlib
import struct
import sys
import tempfile
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

from msx_real import (  # noqa: E402
    FEATURE_FILE_TRANSFER,
    FILE_TRANSFER_FAST_FRAME_TIMEOUT,
    RealMSX,
    RealMSXError,
    RealMSXProtocolError,
    RealMSXRangeError,
)
from msx_transfer import (  # noqa: E402
    TRANSFER_OPCODE,
    TransferCapabilitiesReply,
    TransferCapability,
    TransferDescriptor,
    TransferDirection,
    TransferEncoding,
    TransferFastCapabilitiesReply,
    TransferFastCapability,
    TransferRemoteError,
    TransferReplyFlag,
    TransferState,
)


TRANSFER_ID = bytes(range(16))


class NegotiatedV3Stub:
    def __init__(self, *, local_max_payload=4096, peer_max_payload=320,
                 timeout=1.0):
        self.local_max_payload = local_max_payload
        self.peer_max_payload = peer_max_payload
        self.max_payload = min(local_max_payload, peer_max_payload)
        self.timeout = timeout
        self.limit_history = []

    def negotiate_max_payload(self, peer_max_payload):
        self.peer_max_payload = int(peer_max_payload)
        self.max_payload = min(
            self.local_max_payload, self.peer_max_payload)
        self.limit_history.append(self.max_payload)
        return self.max_payload


class IsolatedV3TransferMSX(RealMSX):
    """Record opcode/payload calls without creating a socket or emulator."""

    def __init__(self, replies=()):
        super().__init__()
        self.runtime_mode = "resident"
        self.feature_bits = FEATURE_FILE_TRANSFER
        self._v3 = NegotiatedV3Stub()
        self.replies = list(replies)
        self.requests = []
        self.request_limits = []

    def _request_v3(self, opcode, payload=b"", *, timeout=None, retries=None):
        self.requests.append((opcode, bytes(payload), timeout, retries))
        self.request_limits.append(self._v3.max_payload)
        if not self.replies:
            raise AssertionError("test did not script a transfer reply")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class TimeoutStreamStub:
    def __init__(self, timeout=15.0):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout):
        self.timeout = timeout


class RebootstrapTransferMSX(IsolatedV3TransferMSX):
    """Exercise RealMSX rebootstrap state without a socket or emulator."""

    def __init__(self):
        super().__init__()
        self.conn = TimeoutStreamStub()

    def _drain_recovery_noise(self):
        return 0

    def _send_reconnect_escape(self):
        return None

    def _scan_bootstrap_hello(self, timeout, byte_limit=None):
        return b"M\x02\x20\x40"

    def _activate_bootstrap_hello(self, reply, *, v3_timeout=None):
        self._v3 = NegotiatedV3Stub(timeout=v3_timeout or 1.0)
        return {"protocol": 3}


def all_capabilities_reply(max_put=10, max_get=12, max_path=63):
    capabilities = int(
        TransferCapability.RAW | TransferCapability.PUT |
        TransferCapability.GET | TransferCapability.RESUME |
        TransferCapability.CRC32 | TransferCapability.PACKBITS_DECODE |
        TransferCapability.PACKBITS_ENCODE)
    return struct.pack("<BIHHH", 1, capabilities,
                       max_put, max_get, max_path)


def fast_capabilities_reply(max_put=2026, max_get=2040):
    return struct.pack(
        "<BBHH", 1,
        int(TransferFastCapability.PUMP | TransferFastCapability.STREAM),
                       max_put, max_get)


def status_reply(transfer_id=TRANSFER_ID):
    return struct.pack(
        "<BBBBB16sIIIIIIIH",
        TransferState.TRANSFERRING,
        TransferDirection.PUT,
        TransferEncoding.PACKBITS,
        TransferRemoteError.NONE,
        int(TransferReplyFlag.ACTIVE | TransferReplyFlag.RESUMABLE),
        transfer_id,
        100, 0x01020304,
        200, 0xAABBCCDD,
        20, 24, 0x11223344,
        10,
    )


class RealMSXTransferWireTest(unittest.TestCase):
    def test_every_low_level_operation_uses_opcode_x_and_exact_payload(self):
        target = IsolatedV3TransferMSX([
            all_capabilities_reply(),
            fast_capabilities_reply(),
            bytes((TransferState.STAGED, TransferRemoteError.NONE)),
            bytes((TransferState.READY, TransferRemoteError.NONE)),
            status_reply(),
            struct.pack(
                "<HIIHBB", 3, 27, 24, 7,
                TransferState.TRANSFERRING, TransferRemoteError.NONE),
            struct.pack(
                "<IHBB", 24, 3,
                TransferState.TRANSFERRING,
                TransferRemoteError.NONE) + b"GET",
            struct.pack(
                "<IBB", 27, TransferState.TRANSFERRING,
                TransferRemoteError.NONE),
            bytes((TransferState.VERIFYING, TransferRemoteError.NONE)),
            bytes((TransferState.CANCELLED, TransferRemoteError.NONE)),
        ])
        descriptor = TransferDescriptor(
            TransferDirection.PUT,
            TransferEncoding.PACKBITS,
            TRANSFER_ID,
            100, 0x01020304,
            200, 0xAABBCCDD,
            "A:\\WIRE.BIN",
            resume_offset=20,
            resume_prefix_crc32=0x11223344,
        )

        capabilities = target.file_transfer_capabilities()
        opened = target.file_transfer_open(descriptor)
        begun = target.file_transfer_fast_begin(TRANSFER_ID)
        status = target.file_transfer_status(TRANSFER_ID)
        progress = target.file_transfer_put_data(TRANSFER_ID, 24, b"PUT")
        block = target.file_transfer_get_read(TRANSFER_ID, 24, 99)
        acknowledged = target.file_transfer_get_ack(
            TRANSFER_ID, 27, 0x55667788)
        closed = target.file_transfer_close(TRANSFER_ID, rate_bps=1559)
        cancelled = target.file_transfer_cancel(TRANSFER_ID)

        self.assertEqual(capabilities.max_put_chunk, 10)
        self.assertEqual(opened.state, TransferState.STAGED)
        self.assertEqual(begun.state, TransferState.READY)
        self.assertEqual(status.transfer_id, TRANSFER_ID)
        self.assertEqual(status.accepted_offset, 24)
        self.assertEqual(progress.accepted, 3)
        self.assertEqual(block.data, b"GET")
        self.assertEqual(acknowledged.durable_offset, 27)
        self.assertEqual(closed.state, TransferState.VERIFYING)
        self.assertEqual(cancelled.state, TransferState.CANCELLED)
        self.assertEqual(target.replies, [])
        self.assertTrue(all(
            opcode == TRANSFER_OPCODE for opcode, _, _, _ in target.requests))
        self.assertEqual([request[1] for request in target.requests], [
            b"\x00",
            b"\x08",
            (b"\x01\x01\x00\x01\x05" + TRANSFER_ID +
             struct.pack(
                 "<IIIIIIH", 100, 0x01020304, 200, 0xAABBCCDD,
                 20, 0x11223344, 11) + b"A:\\WIRE.BIN"),
            b"\x09" + TRANSFER_ID,
            b"\x02" + TRANSFER_ID,
            b"\x03" + TRANSFER_ID + struct.pack("<I", 24) + b"PUT",
            b"\x04" + TRANSFER_ID + struct.pack("<IH", 24, 99),
            (b"\x05" + TRANSFER_ID +
             struct.pack("<II", 27, 0x55667788)),
            b"\x06" + TRANSFER_ID + struct.pack("<H", 1559),
            b"\x07" + TRANSFER_ID,
        ])

    def test_capabilities_are_cached_but_refresh_reissues_caps(self):
        target = IsolatedV3TransferMSX([
            all_capabilities_reply(max_put=20),
            all_capabilities_reply(max_put=30),
        ])

        first = target.file_transfer_capabilities()
        cached = target.file_transfer_capabilities()
        refreshed = target.file_transfer_capabilities(refresh=True)

        self.assertIs(first, cached)
        self.assertEqual(first.max_put_chunk, 20)
        self.assertEqual(refreshed.max_put_chunk, 30)
        self.assertEqual(
            [(opcode, payload) for opcode, payload, _, _ in target.requests],
            [(TRANSFER_OPCODE, b"\x00"), (TRANSFER_OPCODE, b"\x00")])

    def test_fast_capabilities_begin_and_armed_timeout_are_isolated(self):
        target = IsolatedV3TransferMSX([
            fast_capabilities_reply(),
            bytes((TransferState.READY, TransferRemoteError.NONE)),
            status_reply(),
        ])

        capabilities = target.file_transfer_fast_capabilities()
        cached = target.file_transfer_fast_capabilities()
        begun = target.file_transfer_fast_begin(TRANSFER_ID)
        status = target.file_transfer_status(TRANSFER_ID)

        self.assertIs(capabilities, cached)
        self.assertEqual(capabilities.max_put_chunk, 2026)
        self.assertEqual(capabilities.max_get_chunk, 2040)
        self.assertEqual(begun.state, TransferState.READY)
        self.assertEqual(status.transfer_id, TRANSFER_ID)
        self.assertEqual(target._fast_transfer_id, TRANSFER_ID)
        self.assertEqual([request[1] for request in target.requests], [
            b"\x08", b"\x09" + TRANSFER_ID, b"\x02" + TRANSFER_ID,
        ])
        self.assertIsNone(target.requests[0][2])
        self.assertEqual(
            target.requests[1][2], FILE_TRANSFER_FAST_FRAME_TIMEOUT)
        self.assertEqual(
            target.requests[2][2], FILE_TRANSFER_FAST_FRAME_TIMEOUT)

    def test_fast_bulk_temporarily_raises_and_restores_v3_ceiling(self):
        put_reply = struct.pack(
            "<HIIHBB", 2026, 2026, 0, 2026,
            TransferState.TRANSFERRING, TransferRemoteError.NONE)
        get_data = b"G" * 2040
        get_reply = struct.pack(
            "<IHBB", 0, len(get_data),
            TransferState.TRANSFERRING, TransferRemoteError.NONE) + get_data
        target = IsolatedV3TransferMSX([put_reply, get_reply])
        target.transfer_fast_capabilities = TransferFastCapabilitiesReply(
            1, (TransferFastCapability.PUMP |
                TransferFastCapability.STREAM), 2026, 2040)
        target._fast_transfer_id = TRANSFER_ID

        progress = target.file_transfer_put_data(
            TRANSFER_ID, 0, b"P" * 2026)
        self.assertEqual(target._v3.peer_max_payload, 320)
        block = target.file_transfer_get_read(
            TRANSFER_ID, 0, 2040)

        self.assertEqual(progress.accepted, 2026)
        self.assertEqual(block.data, get_data)
        self.assertEqual(target.request_limits, [2047, 2048])
        self.assertEqual(
            target._v3.limit_history, [2047, 320, 2048, 320])
        self.assertEqual(target._v3.peer_max_payload, 320)
        self.assertTrue(all(
            request[2] == FILE_TRANSFER_FAST_FRAME_TIMEOUT
            for request in target.requests))

    def test_fast_bulk_restores_v3_ceiling_when_request_fails(self):
        target = IsolatedV3TransferMSX([
            RealMSXProtocolError("scripted fast failure"),
        ])
        target.transfer_fast_capabilities = TransferFastCapabilitiesReply(
            1, (TransferFastCapability.PUMP |
                TransferFastCapability.STREAM), 2026, 2040)
        target._fast_transfer_id = TRANSFER_ID

        with self.assertRaisesRegex(
                RealMSXProtocolError, "scripted fast failure"):
            target.file_transfer_put_data(
                TRANSFER_ID, 0, b"P" * 2026)

        self.assertEqual(target.request_limits, [2047])
        self.assertEqual(target._v3.limit_history, [2047, 320])
        self.assertEqual(target._v3.peer_max_payload, 320)

    def test_fast_oversized_probe_fails_before_journal_or_open(self):
        target = IsolatedV3TransferMSX([
            fast_capabilities_reply(max_put=5000),
        ])
        target.transfer_capabilities = TransferCapabilitiesReply(
            1,
            TransferCapability.RAW | TransferCapability.PUT |
            TransferCapability.CRC32,
            2026, 2040, 63)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"payload")
            state = root / "state"

            with self.assertRaisesRegex(
                    RealMSXRangeError, "5021-byte framed payload"):
                target.put_file(
                    source, "A:\\TARGET.BIN", compression="raw",
                    state_directory=state)

            self.assertFalse(state.exists())

        self.assertEqual(
            [request[1] for request in target.requests], [b"\x08"])

    def test_rebootstrap_forgets_the_host_side_fast_arm(self):
        target = RebootstrapTransferMSX()
        target._fast_transfer_id = TRANSFER_ID

        target._rebootstrap_v3()

        self.assertIsNone(target._fast_transfer_id)

    def test_malformed_reply_and_status_id_mismatch_fail_as_protocol_errors(self):
        malformed = IsolatedV3TransferMSX([b"\x01"])
        malformed.transfer_capabilities = TransferCapabilitiesReply(
            1,
            TransferCapability.RAW | TransferCapability.PUT |
            TransferCapability.CRC32,
            10, 0, 63)
        descriptor = TransferDescriptor(
            TransferDirection.PUT, TransferEncoding.RAW, TRANSFER_ID,
            1, 1, 1, 1, "A:\\ONE.BIN")
        with self.assertRaisesRegex(
                RealMSXProtocolError, "invalid file-transfer-v2 response"):
            malformed.file_transfer_open(descriptor)

        mismatched = IsolatedV3TransferMSX([status_reply(b"Z" * 16)])
        with self.assertRaisesRegex(
                RealMSXProtocolError, "does not match"):
            mismatched.file_transfer_status(TRANSFER_ID)

    def test_runtime_feature_and_v3_session_are_required_before_any_request(self):
        cases = []
        foreground = IsolatedV3TransferMSX()
        foreground.runtime_mode = "foreground-monitor"
        cases.append((foreground, "resident agent"))

        missing_feature = IsolatedV3TransferMSX()
        missing_feature.feature_bits = 0
        cases.append((missing_feature, "does not advertise"))

        missing_v3 = IsolatedV3TransferMSX()
        missing_v3._v3 = None
        cases.append((missing_v3, "does not advertise"))

        for target, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(RealMSXError, message):
                    target.file_transfer_capabilities()
                self.assertEqual(target.requests, [])

    def test_open_enforces_negotiated_direction_codec_resume_and_path(self):
        descriptor = TransferDescriptor(
            TransferDirection.PUT, TransferEncoding.RAW, TRANSFER_ID,
            1, 1, 1, 1, "A:\\ONE.BIN")

        missing_put = IsolatedV3TransferMSX()
        missing_put.transfer_capabilities = TransferCapabilitiesReply(
            1, TransferCapability.RAW | TransferCapability.CRC32,
            10, 10, 63)
        with self.assertRaisesRegex(RealMSXError, "required raw/CRC32"):
            missing_put.file_transfer_open(descriptor)

        no_resume = IsolatedV3TransferMSX()
        no_resume.transfer_capabilities = TransferCapabilitiesReply(
            1,
            TransferCapability.RAW | TransferCapability.PUT |
            TransferCapability.CRC32,
            10, 10, 63)
        with self.assertRaisesRegex(RealMSXError, "does not support.*resume"):
            no_resume.file_transfer_open(TransferDescriptor(
                TransferDirection.PUT, TransferEncoding.RAW, TRANSFER_ID,
                2, 1, 2, 1, "A:\\ONE.BIN",
                resume_offset=1, resume_prefix_crc32=2))

        no_packbits = IsolatedV3TransferMSX()
        no_packbits.transfer_capabilities = TransferCapabilitiesReply(
            1,
            TransferCapability.RAW | TransferCapability.PUT |
            TransferCapability.CRC32,
            10, 10, 63)
        with self.assertRaisesRegex(RealMSXError, "PackBits decompression"):
            no_packbits.file_transfer_open(TransferDescriptor(
                TransferDirection.PUT, TransferEncoding.PACKBITS, TRANSFER_ID,
                1, 1, 2, 2, "A:\\ONE.BIN"))

        short_path = IsolatedV3TransferMSX()
        short_path.transfer_capabilities = TransferCapabilitiesReply(
            1,
            TransferCapability.RAW | TransferCapability.PUT |
            TransferCapability.CRC32,
            10, 10, 3)
        short_path.transfer_fast_capabilities = TransferFastCapabilitiesReply(
            1,
            TransferFastCapability.PUMP | TransferFastCapability.STREAM,
            2026, 2040)
        with self.assertRaises(RealMSXRangeError):
            short_path.file_transfer_open(descriptor)

        for target in (missing_put, no_resume, no_packbits, short_path):
            self.assertEqual(target.requests, [])

    def test_put_respects_negotiated_chunk_without_sending(self):
        target = IsolatedV3TransferMSX()
        target.transfer_fast_capabilities = TransferFastCapabilitiesReply(
            1,
            TransferFastCapability.PUMP | TransferFastCapability.STREAM,
            3, 10)
        with self.assertRaises(RealMSXRangeError):
            target.file_transfer_put_data(TRANSFER_ID, 0, b"four")
        self.assertEqual(target.requests, [])


if __name__ == "__main__":
    unittest.main()
