import pathlib
import struct
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

from msx_real import (  # noqa: E402
    FEATURE_FILE_TRANSFER,
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
    TransferRemoteError,
    TransferReplyFlag,
    TransferState,
)


TRANSFER_ID = bytes(range(16))


class IsolatedV3TransferMSX(RealMSX):
    """Record opcode/payload calls without creating a socket or emulator."""

    def __init__(self, replies=()):
        super().__init__()
        self.runtime_mode = "resident"
        self.feature_bits = FEATURE_FILE_TRANSFER
        self._v3 = object()
        self.replies = list(replies)
        self.requests = []

    def _request_v3(self, opcode, payload=b"", *, timeout=None, retries=None):
        self.requests.append((opcode, bytes(payload), timeout, retries))
        if not self.replies:
            raise AssertionError("test did not script a transfer reply")
        return self.replies.pop(0)


def all_capabilities_reply(max_put=10, max_get=12, max_path=63):
    capabilities = int(
        TransferCapability.RAW | TransferCapability.PUT |
        TransferCapability.GET | TransferCapability.RESUME |
        TransferCapability.CRC32 | TransferCapability.PACKBITS_DECODE |
        TransferCapability.PACKBITS_ENCODE)
    return struct.pack("<BIHHH", 1, capabilities,
                       max_put, max_get, max_path)


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
            bytes((TransferState.STAGED, TransferRemoteError.NONE)),
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
        status = target.file_transfer_status(TRANSFER_ID)
        progress = target.file_transfer_put_data(TRANSFER_ID, 24, b"PUT")
        block = target.file_transfer_get_read(TRANSFER_ID, 24, 99)
        acknowledged = target.file_transfer_get_ack(
            TRANSFER_ID, 27, 0x55667788)
        closed = target.file_transfer_close(TRANSFER_ID)
        cancelled = target.file_transfer_cancel(TRANSFER_ID)

        self.assertEqual(capabilities.max_put_chunk, 10)
        self.assertEqual(opened.state, TransferState.STAGED)
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
            (b"\x01\x01\x00\x01\x01" + TRANSFER_ID +
             struct.pack(
                 "<IIIIIIH", 100, 0x01020304, 200, 0xAABBCCDD,
                 20, 0x11223344, 11) + b"A:\\WIRE.BIN"),
            b"\x02" + TRANSFER_ID,
            b"\x03" + TRANSFER_ID + struct.pack("<I", 24) + b"PUT",
            b"\x04" + TRANSFER_ID + struct.pack("<IH", 24, 12),
            (b"\x05" + TRANSFER_ID +
             struct.pack("<II", 27, 0x55667788)),
            b"\x06" + TRANSFER_ID,
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
        with self.assertRaises(RealMSXRangeError):
            short_path.file_transfer_open(descriptor)

        for target in (missing_put, no_resume, no_packbits, short_path):
            self.assertEqual(target.requests, [])

    def test_put_respects_negotiated_chunk_without_sending(self):
        target = IsolatedV3TransferMSX()
        target.transfer_capabilities = TransferCapabilitiesReply(
            1,
            TransferCapability.RAW | TransferCapability.PUT |
            TransferCapability.CRC32,
            3, 10, 63)
        with self.assertRaises(RealMSXRangeError):
            target.file_transfer_put_data(TRANSFER_ID, 0, b"four")
        self.assertEqual(target.requests, [])


if __name__ == "__main__":
    unittest.main()
