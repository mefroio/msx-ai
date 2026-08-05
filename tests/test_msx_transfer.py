import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from msx_transfer import (  # noqa: E402
    FEATURE_FILE_TRANSFER_V2,
    TRANSFER_OPCODE,
    FileDigest,
    TransferBindingError,
    TransferCapability,
    TransferDescriptor,
    TransferDirection,
    TransferEncoding,
    TransferError,
    TransferJournal,
    TransferJournalError,
    TransferOpenFlag,
    TransferPayloadError,
    TransferRemoteError,
    TransferReplyFlag,
    TransferState,
    TransferSubcommand,
    crc32_file,
    crc32_file_prefix,
    crc32_stream,
    crc32_update,
    decode_open,
    encode_cancel,
    encode_capabilities_request,
    encode_close,
    encode_get_ack,
    encode_get_read,
    encode_open,
    encode_put_data,
    encode_status,
    looks_already_compressed,
    new_transfer_id,
    parse_cancel_reply,
    parse_capabilities_reply,
    parse_close_reply,
    parse_get_ack_reply,
    parse_get_read_reply,
    parse_open_reply,
    parse_put_data_reply,
    parse_status_reply,
    normalize_msx_basic_text,
    prepare_msx_basic_source,
    prepare_put_payload,
)


TRANSFER_ID = bytes(range(16))


def decode_packbits(wire: bytes) -> bytes:
    """Independent strict decoder used to verify the staged host stream."""

    output = bytearray()
    offset = 0
    while offset < len(wire):
        control = wire[offset]
        offset += 1
        if control == 0x80 or control == 0xFF:
            raise ValueError("non-canonical PackBits control")
        if control < 0x80:
            count = control + 1
            end = offset + count
            if end > len(wire):
                raise ValueError("truncated PackBits literal")
            output.extend(wire[offset:end])
            offset = end
        else:
            if offset == len(wire):
                raise ValueError("truncated PackBits run")
            output.extend((wire[offset],) * (257 - control))
            offset += 1
    return bytes(output)


def put_descriptor(**changes):
    values = dict(
        direction=TransferDirection.PUT,
        encoding=TransferEncoding.RAW,
        transfer_id=TRANSFER_ID,
        wire_size=1000,
        wire_crc32=0x11223344,
        final_size=1000,
        final_crc32=0x11223344,
        path="A:\\FILE.BIN",
    )
    values.update(changes)
    return TransferDescriptor(**values)


class TransferOpenWireTest(unittest.TestCase):
    def test_feature_opcode_and_numeric_abi(self):
        self.assertEqual(FEATURE_FILE_TRANSFER_V2, 0x80)
        self.assertEqual(TRANSFER_OPCODE, ord("X"))
        self.assertEqual(
            [int(value) for value in TransferSubcommand], list(range(8)))
        self.assertEqual(TransferDirection.PUT, 0)
        self.assertEqual(TransferDirection.GET, 1)
        self.assertEqual(TransferEncoding.RAW, 0)
        self.assertEqual(TransferEncoding.PACKBITS, 1)
        self.assertEqual(TransferOpenFlag.RESUME, 0x01)
        self.assertEqual(TransferOpenFlag.RECEIPTLESS_REPLAY, 0x02)
        self.assertEqual(int(
            TransferCapability.RAW | TransferCapability.PUT |
            TransferCapability.GET | TransferCapability.RESUME |
            TransferCapability.CRC32 | TransferCapability.PACKBITS_DECODE |
            TransferCapability.PACKBITS_ENCODE), 0x7F)

    def test_open_has_a_fixed_vector_and_roundtrips(self):
        descriptor = TransferDescriptor(
            TransferDirection.PUT, TransferEncoding.PACKBITS, TRANSFER_ID,
            0x01020304, 0xA1B2C3D4, 0x11223344, 0x55667788,
            "A:\\GAME.BIN", 0x1020, 0xDEADBEEF)
        payload = encode_open(descriptor)

        self.assertEqual(len(payload), 58)
        self.assertEqual(payload.hex(),
            "0101000101000102030405060708090a0b0c0d0e0f"
            "04030201d4c3b2a1443322118877665520100000efbeadde"
            "0b00413a5c47414d452e42494e")
        self.assertEqual(decode_open(payload), descriptor)
        self.assertTrue(decode_open(payload).resume)

    def test_get_open_can_discover_metadata_while_resuming_local_prefix(self):
        descriptor = TransferDescriptor(
            TransferDirection.GET, TransferEncoding.RAW, TRANSFER_ID,
            0, 0, 0, 0, "B:\\REMOTE.DAT",
            resume_offset=50000, resume_prefix_crc32=0xCAFEBABE,
            resume=True)
        self.assertEqual(decode_open(encode_open(descriptor)), descriptor)

    def test_descriptor_rejects_unsafe_or_inconsistent_values(self):
        with self.assertRaisesRegex(TransferError, "GET.*raw"):
            put_descriptor(
                direction=TransferDirection.GET,
                encoding=TransferEncoding.PACKBITS)
        with self.assertRaisesRegex(TransferError, "identical"):
            put_descriptor(final_crc32=0)
        with self.assertRaisesRegex(TransferError, "ASCII"):
            put_descriptor(path="A:\\NÃO.BIN")
        with self.assertRaisesRegex(TransferError, "printable"):
            put_descriptor(path="A:\\BAD\n.BIN")
        with self.assertRaisesRegex(TransferError, "16 bytes"):
            put_descriptor(transfer_id=b"short")
        with self.assertRaisesRegex(TransferError, "exceeds wire_size"):
            put_descriptor(resume_offset=1001, resume_prefix_crc32=1)

    def test_decode_open_is_strict(self):
        payload = bytearray(encode_open(put_descriptor()))
        cases = []
        bad = bytearray(payload)
        bad[1] = 2
        cases.append(bad)
        bad = bytearray(payload)
        bad[4] = 0x80
        cases.append(bad)
        cases.append(payload + b"trailing")
        bad = bytearray(payload)
        bad[-1] = 0xFF
        cases.append(bad)
        for malformed in cases:
            with self.subTest(payload=bytes(malformed).hex()[-20:]):
                with self.assertRaises(TransferPayloadError):
                    decode_open(malformed)

    def test_resume_flag_is_preserved_even_at_offset_zero(self):
        descriptor = put_descriptor(resume=True)
        self.assertEqual(encode_open(descriptor)[4], TransferOpenFlag.RESUME)
        self.assertTrue(decode_open(encode_open(descriptor)).resume)

    def test_receiptless_replay_requires_an_exact_complete_put_boundary(self):
        descriptor = put_descriptor(
            resume=True, resume_offset=1000,
            resume_prefix_crc32=0x11223344,
            receiptless_replay=True)
        payload = encode_open(descriptor)
        self.assertEqual(
            payload[4],
            TransferOpenFlag.RESUME |
            TransferOpenFlag.RECEIPTLESS_REPLAY)
        self.assertEqual(decode_open(payload), descriptor)

        with self.assertRaisesRegex(TransferError, "complete CRC-matched PUT"):
            put_descriptor(receiptless_replay=True)
        with self.assertRaisesRegex(TransferError, "complete CRC-matched PUT"):
            put_descriptor(
                direction=TransferDirection.GET,
                resume=True, resume_offset=1000,
                resume_prefix_crc32=0x11223344,
                receiptless_replay=True)


class TransferReplyAndRequestTest(unittest.TestCase):
    def test_capability_reply_exact_layout(self):
        payload = struct.pack("<BIHHH", 1, 0x3F, 1024, 2048, 63)
        result = parse_capabilities_reply(payload)
        self.assertEqual(result.capabilities, TransferCapability(0x3F))
        self.assertEqual((result.max_put_chunk, result.max_get_chunk,
                          result.max_path), (1024, 2048, 63))
        self.assertEqual(encode_capabilities_request(), b"\x00")
        with self.assertRaises(TransferPayloadError):
            parse_capabilities_reply(payload + b"\x00")
        with self.assertRaisesRegex(TransferPayloadError, "unknown bits"):
            parse_capabilities_reply(struct.pack("<BIHHH", 1, 0x80, 1, 1, 1))

    def test_open_and_terminal_replies_are_exact_state_error_pairs(self):
        opened = parse_open_reply(bytes((TransferState.READY,
                                         TransferRemoteError.NONE)))
        self.assertEqual(opened.state, TransferState.READY)
        self.assertEqual(opened.error, TransferRemoteError.NONE)
        self.assertEqual(parse_close_reply(b"\x07\x00").state,
                         TransferState.COMPLETE)
        self.assertEqual(parse_cancel_reply(b"\x0a\x00").state,
                         TransferState.CANCELLED)
        with self.assertRaisesRegex(TransferPayloadError, "unknown state"):
            parse_open_reply(b"\xff\x00")
        with self.assertRaises(TransferPayloadError):
            parse_close_reply(b"\x00")

    def test_request_layouts_include_the_bound_transfer_id(self):
        self.assertEqual(encode_status(TRANSFER_ID), b"\x02" + TRANSFER_ID)
        self.assertEqual(
            encode_put_data(TRANSFER_ID, 0x12345678, b"abc"),
            b"\x03" + TRANSFER_ID + b"\x78\x56\x34\x12abc")
        self.assertEqual(
            encode_get_read(TRANSFER_ID, 0x12345678, 0x0201),
            b"\x04" + TRANSFER_ID + b"\x78\x56\x34\x12\x01\x02")
        self.assertEqual(
            encode_get_ack(TRANSFER_ID, 0x10, 0xAABBCCDD),
            b"\x05" + TRANSFER_ID +
            b"\x10\x00\x00\x00\xdd\xcc\xbb\xaa")
        self.assertEqual(encode_close(TRANSFER_ID), b"\x06" + TRANSFER_ID)
        self.assertEqual(encode_cancel(TRANSFER_ID), b"\x07" + TRANSFER_ID)

    def test_put_get_and_ack_replies_are_strict(self):
        put = parse_put_data_reply(struct.pack(
            "<HIIHBB", 200, 1200, 1000, 300, 4, 0))
        self.assertEqual((put.accepted, put.accepted_end, put.durable_end),
                         (200, 1200, 1000))
        with self.assertRaisesRegex(TransferPayloadError, "durable_end"):
            parse_put_data_reply(struct.pack(
                "<HIIHBB", 1, 9, 10, 1, 4, 0))

        get = parse_get_read_reply(
            struct.pack("<IHBB", 100, 3, 4, 0) + b"abc")
        self.assertEqual((get.offset, get.data), (100, b"abc"))
        with self.assertRaisesRegex(TransferPayloadError, "declared length"):
            parse_get_read_reply(
                struct.pack("<IHBB", 100, 4, 4, 0) + b"abc")

        ack = parse_get_ack_reply(struct.pack("<IBB", 4096, 4, 0))
        self.assertEqual(ack.durable_offset, 4096)

    def test_status_reply_binds_id_and_validates_progress(self):
        payload = struct.pack(
            "<BBBBB16sIIIIIIIH",
            4, 0, 0, 0,
            int(TransferReplyFlag.ACTIVE | TransferReplyFlag.RESUMABLE),
            TRANSFER_ID, 1000, 0x11, 1000, 0x11,
            600, 700, 0x22, 300)
        status = parse_status_reply(payload, expected_transfer_id=TRANSFER_ID)
        self.assertEqual(status.state, TransferState.TRANSFERRING)
        self.assertEqual(status.durable_offset, 600)
        self.assertEqual(status.accepted_offset, 700)
        with self.assertRaises(TransferBindingError):
            parse_status_reply(payload, expected_transfer_id=b"x" * 16)
        invalid = bytearray(payload)
        invalid[4] = 0x80
        with self.assertRaisesRegex(TransferPayloadError, "unknown flag"):
            parse_status_reply(invalid)


class BasicSourcePreparationTest(unittest.TestCase):
    def test_mixed_host_endings_and_graphical_bytes_are_canonical(self):
        source = (
            b"10 PRINT \x80\r20 PRINT \xff\n"
            b"30 PRINT 3\r\n40 END\n\x1a")
        expected = (
            b"10 PRINT \x80\r\n20 PRINT \xff\r\n"
            b"30 PRINT 3\r\n40 END\r\n\x1a")
        self.assertEqual(
            normalize_msx_basic_text(source, chunk_size=1), expected)
        self.assertEqual(normalize_msx_basic_text(expected), expected)

    def test_invalid_or_ambiguous_text_is_rejected(self):
        cases = (
            (b"RUN\n", "line number"),
            (b"10 PRINT\x00X\n", "control byte"),
            (b"10 PRINT\x01X\n", "control byte"),
            (b"10 END\n\x1aX", "after its 0x1A"),
            (b"\xef\xbb\xbf10 END\n", "line number"),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(TransferError, message):
                    normalize_msx_basic_text(payload, chunk_size=1)

    def test_prepare_preserves_tokenized_and_non_basic_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tokenized = root / "token.bas"
            tokenized.write_bytes(b"\xff\x00\x80\x00\x00")
            token = prepare_msx_basic_source(
                tokenized, "A:\\TOKEN.BAS", state_directory=root / "state")
            binary = root / "payload.bin"
            binary.write_bytes(b"\x00\r\n\xff")
            raw = prepare_msx_basic_source(
                binary, "A:\\PAYLOAD.BIN", state_directory=root / "state")

        self.assertEqual(token.transfer_path, tokenized)
        self.assertEqual(token.basic_format, "tokenized")
        self.assertFalse(token.temporary)
        self.assertEqual(raw.transfer_path, binary)
        self.assertIsNone(raw.basic_format)
        self.assertFalse(raw.temporary)

    def test_failed_text_preparation_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.bas"
            source.write_bytes(b"10 END\n\x1aBAD")
            state = root / "state"

            with self.assertRaisesRegex(TransferError, "after its 0x1A"):
                prepare_msx_basic_source(
                    source, "A:\\BAD.BAS", state_directory=state,
                    chunk_size=1)

            self.assertEqual(list(state.glob(".msx-basic-*.bas")), [])


class HashAndCompressionTest(unittest.TestCase):
    def test_crc32_iso_hdlc_reference_and_bounded_reads(self):
        self.assertEqual(crc32_stream(io.BytesIO(b"123456789")),
                         FileDigest(9, 0xCBF43926))
        checksum = crc32_update(b"1234")
        checksum = crc32_update(b"56789", checksum)
        self.assertEqual(checksum, 0xCBF43926)
        stream = io.BytesIO(b"12345")
        with self.assertRaisesRegex(TransferError, "exceeds maximum"):
            crc32_stream(stream, chunk_size=2, max_size=4)

    def test_prefix_crc_hashes_exactly_the_resumable_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(b"abcdefghij")
            self.assertEqual(
                crc32_file_prefix(source, 4, chunk_size=2),
                crc32_stream(io.BytesIO(b"abcd")))
            with self.assertRaisesRegex(TransferError, "ended"):
                crc32_file_prefix(source, 11)

    def test_deterministic_packbits_is_streaming_and_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private-original-name.txt"
            source.write_bytes(b"MSX-AI\n" * 1000)
            first = prepare_put_payload(
                source, state_directory=root / "state1", mode="packbits")
            second = prepare_put_payload(
                source, state_directory=root / "state2", mode="packbits")
            try:
                one = first.wire_path.read_bytes()
                two = second.wire_path.read_bytes()
                self.assertEqual(one, two)
                self.assertEqual(decode_packbits(one), source.read_bytes())
                self.assertEqual(first.final_digest, crc32_file(source))
                self.assertEqual(first.wire_digest, crc32_file(first.wire_path))
            finally:
                first.cleanup()
                second.cleanup()
            self.assertFalse(first.wire_path.exists())
            self.assertFalse(second.wire_path.exists())

    def test_packbits_boundaries_are_chunk_independent(self):
        payload = (
            b"".join(bytes((value,)) * length for value, length in enumerate(
                (1, 2, 3, 127, 128, 129, 255, 256))) +
            bytes(range(256)) * 2 + b"Z" * 130)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "boundaries.bin"
            source.write_bytes(payload)
            encoded = []
            for chunk_size in (1, 2, 3, 127, 128, 129, 4096):
                prepared = prepare_put_payload(
                    source, state_directory=root / f"state-{chunk_size}",
                    mode="packbits", chunk_size=chunk_size)
                try:
                    wire = prepared.wire_path.read_bytes()
                    self.assertEqual(decode_packbits(wire), payload)
                    encoded.append(wire)
                finally:
                    prepared.cleanup()
            self.assertTrue(all(wire == encoded[0] for wire in encoded[1:]))

    def test_auto_compresses_only_when_savings_clear_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compressible = root / "plain.dat"
            compressible.write_bytes(b"A" * 10000)
            selected = prepare_put_payload(
                compressible, state_directory=root / "state", mode="auto")
            try:
                self.assertEqual(selected.encoding, TransferEncoding.PACKBITS)
                self.assertGreaterEqual(
                    selected.final_digest.size - selected.wire_digest.size,
                    max(256, (selected.final_digest.size * 3 + 99) // 100))
            finally:
                selected.cleanup()

            incompressible = root / "noise.dat"
            incompressible.write_bytes(b"".join(
                hashlib.sha256(str(index).encode()).digest()
                for index in range(160)))
            rejected = prepare_put_payload(
                incompressible, state_directory=root / "state", mode="auto")
            self.assertEqual(rejected.encoding, TransferEncoding.RAW)
            self.assertEqual(rejected.wire_path, incompressible)
            self.assertFalse(rejected.temporary)
            self.assertEqual(list((root / "state").glob("*.packbits")), [])

    def test_auto_falls_back_when_packbits_expands_past_wire_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "exact-limit.bin"
            source.write_bytes(b"ABCD")
            prepared = prepare_put_payload(
                source, state_directory=root / "state", mode="auto",
                max_size=4, chunk_size=1)
            self.assertEqual(prepared.encoding, TransferEncoding.RAW)
            self.assertEqual(prepared.wire_path, source)
            self.assertEqual(prepared.wire_digest, FileDigest(
                4, crc32_update(b"ABCD")))
            self.assertFalse(prepared.temporary)
            self.assertEqual(list((root / "state").glob("*.packbits")), [])

            with self.assertRaisesRegex(
                    TransferError, "PackBits stream exceeds"):
                prepare_put_payload(
                    source, state_directory=root / "forced", mode="packbits",
                    max_size=4, chunk_size=1)

    def test_zip_is_sent_unchanged_and_never_unpacked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "archive.zip"
            original = b"PK\x03\x04already-compressed-user-payload"
            source.write_bytes(original)
            self.assertTrue(looks_already_compressed(source))
            planned = prepare_put_payload(
                source, state_directory=root / "state", mode="auto")
            self.assertEqual(planned.encoding, TransferEncoding.RAW)
            self.assertEqual(planned.wire_path, source)
            self.assertEqual(planned.wire_path.read_bytes(), original)
            self.assertEqual(planned.reason, "already compressed")


class TransferJournalTest(unittest.TestCase):
    def test_atomic_roundtrip_and_strict_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            descriptor = put_descriptor()
            path = journal.save(
                descriptor, confirmed_offset=400,
                prefix_crc32=0xABCDEF01,
                caller_binding="/resolved/local/file.bin")
            self.assertEqual(path.parent, Path(directory))
            self.assertEqual(path.name, TRANSFER_ID.hex() + ".json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

            record = journal.load(
                descriptor, caller_binding="/resolved/local/file.bin")
            self.assertEqual(record.confirmed_offset, 400)
            self.assertEqual(record.prefix_crc32, 0xABCDEF01)
            self.assertEqual(record.resumed_descriptor().resume_offset, 400)
            self.assertTrue(record.resumed_descriptor().resume)
            with self.assertRaises(TransferBindingError):
                journal.load(descriptor, caller_binding="/other/file.bin")
            with self.assertRaises(TransferBindingError):
                journal.load(put_descriptor(path="B:\\OTHER.BIN"))
            with self.assertRaisesRegex(TransferBindingError, "replace"):
                journal.save(
                    put_descriptor(path="B:\\OTHER.BIN"),
                    confirmed_offset=0, prefix_crc32=0,
                    caller_binding="/resolved/local/file.bin")

    def test_close_intent_is_durable_monotonic_and_authorizes_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            descriptor = put_descriptor()
            caller = "/resolved/local/file.bin"

            journal.save(
                descriptor, confirmed_offset=descriptor.wire_size,
                prefix_crc32=descriptor.wire_crc32,
                caller_binding=caller, close_intent=True)
            record = journal.load(descriptor, caller_binding=caller)
            resumed = record.resumed_descriptor()

            self.assertTrue(record.close_intent)
            self.assertTrue(resumed.resume)
            self.assertTrue(resumed.receiptless_replay)
            self.assertEqual(
                encode_open(resumed)[4],
                TransferOpenFlag.RESUME |
                TransferOpenFlag.RECEIPTLESS_REPLAY)
            with self.assertRaisesRegex(
                    TransferJournalError, "clear.*close intent"):
                journal.save(
                    descriptor, confirmed_offset=descriptor.wire_size,
                    prefix_crc32=descriptor.wire_crc32,
                    caller_binding=caller, close_intent=False)

    def test_pre_open_empty_journal_cannot_authorize_receiptless_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            descriptor = put_descriptor(
                wire_size=0, wire_crc32=0, final_size=0, final_crc32=0)
            journal.save(
                descriptor, confirmed_offset=0, prefix_crc32=0,
                caller_binding="/resolved/local/empty.bin")

            record = journal.load(
                descriptor, caller_binding="/resolved/local/empty.bin")
            resumed = record.resumed_descriptor()
            self.assertTrue(resumed.resume)
            self.assertFalse(record.close_intent)
            self.assertFalse(resumed.receiptless_replay)
            self.assertEqual(encode_open(resumed)[4], TransferOpenFlag.RESUME)

    def test_close_intent_rejects_partial_or_non_put_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            with self.assertRaisesRegex(
                    TransferJournalError, "complete CRC-matched PUT"):
                journal.save(
                    put_descriptor(), confirmed_offset=999,
                    prefix_crc32=0x11223344, close_intent=True)
            with self.assertRaisesRegex(
                    TransferJournalError, "complete CRC-matched PUT"):
                journal.save(
                    put_descriptor(direction=TransferDirection.GET),
                    confirmed_offset=1000,
                    prefix_crc32=0x11223344, close_intent=True)

    def test_version_one_journal_loads_without_terminal_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            descriptor = put_descriptor()
            path = journal.save(
                descriptor, confirmed_offset=descriptor.wire_size,
                prefix_crc32=descriptor.wire_crc32)
            document = json.loads(path.read_text())
            document["version"] = 1
            del document["close_intent"]
            path.write_text(json.dumps(document))

            record = journal.load(descriptor)
            self.assertFalse(record.close_intent)
            self.assertFalse(record.resumed_descriptor().receiptless_replay)

            document["version"] = True
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(
                    TransferJournalError, "unsupported journal version"):
                journal.load(descriptor)

    def test_restart_discovery_ignores_random_id_but_binds_local_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            saved = put_descriptor(transfer_id=b"a" * 16)
            journal.save(
                saved, confirmed_offset=500, prefix_crc32=0x12345678,
                caller_binding="/local/source.bin")
            expected = put_descriptor(transfer_id=b"b" * 16)
            found = journal.find_matching(
                expected, caller_binding="/local/source.bin")
            self.assertEqual(found.descriptor.transfer_id, b"a" * 16)
            self.assertIsNone(journal.find_matching(
                expected, caller_binding="/not-the-source.bin"))

    def test_get_discovery_treats_zero_metadata_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            saved = put_descriptor(
                direction=TransferDirection.GET, transfer_id=b"c" * 16,
                wire_size=12345, final_size=12345,
                wire_crc32=0x99, final_crc32=0x99,
                path="A:\\REMOTE.BIN")
            journal.save(
                saved, confirmed_offset=2000, prefix_crc32=0x88,
                caller_binding="/local/destination.bin")
            unknown = TransferDescriptor(
                TransferDirection.GET, TransferEncoding.RAW, b"d" * 16,
                0, 0, 0, 0, "A:\\REMOTE.BIN")
            found = journal.find_matching(
                unknown, caller_binding="/local/destination.bin")
            self.assertEqual(found.descriptor.wire_size, 12345)

    def test_staged_get_journal_can_promote_discovered_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            staged = TransferDescriptor(
                TransferDirection.GET, TransferEncoding.RAW, b"s" * 16,
                0, 0, 0, 0, "A:\\REMOTE.BIN")
            journal.save(
                staged, confirmed_offset=0, prefix_crc32=0,
                caller_binding="/local/result.bin")
            discovered = TransferDescriptor(
                TransferDirection.GET, TransferEncoding.RAW, b"s" * 16,
                1234, 0x11223344, 1234, 0x11223344,
                "A:\\REMOTE.BIN")

            journal.save(
                discovered, confirmed_offset=100,
                prefix_crc32=0x55667788,
                caller_binding="/local/result.bin")
            record = journal.load(
                discovered, caller_binding="/local/result.bin")

            self.assertEqual(record.descriptor.wire_size, 1234)
            self.assertEqual(record.confirmed_offset, 100)
            with self.assertRaisesRegex(TransferBindingError, "replace"):
                journal.save(
                    TransferDescriptor(
                        TransferDirection.GET, TransferEncoding.RAW,
                        b"s" * 16, 1234, 0x11223344, 1234, 0x11223344,
                        "B:\\OTHER.BIN"),
                    confirmed_offset=100, prefix_crc32=0x55667788,
                    caller_binding="/local/result.bin")

    def test_discovery_rejects_ambiguity_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = TransferJournal(root)
            first = put_descriptor(transfer_id=b"1" * 16)
            second = put_descriptor(transfer_id=b"2" * 16)
            for descriptor in (first, second):
                journal.save(
                    descriptor, confirmed_offset=1, prefix_crc32=1,
                    caller_binding="/same")
            with self.assertRaisesRegex(TransferJournalError, "multiple"):
                journal.find_matching(
                    put_descriptor(transfer_id=b"3" * 16), caller_binding="/same")

            external = root / "external.json"
            external.write_text("not a journal")
            link = root / (b"4" * 16).hex()
            link = link.with_suffix(".json")
            try:
                link.symlink_to(external)
            except (OSError, NotImplementedError):
                return
            # It remains ambiguous because of the two real journals, but the
            # symlink target was not parsed as a third candidate.
            with self.assertRaisesRegex(TransferJournalError, "multiple"):
                journal.find_matching(
                    put_descriptor(transfer_id=b"3" * 16), caller_binding="/same")

    def test_corrupt_and_oversized_journals_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TransferJournal(directory)
            descriptor = put_descriptor()
            path = journal.save(
                descriptor, confirmed_offset=0, prefix_crc32=0)
            document = json.loads(path.read_text())
            document["wire_crc32"] = "NOT-A-CRC"
            path.write_text(json.dumps(document))
            with self.assertRaises(TransferJournalError):
                journal.load(descriptor)
            path.write_bytes(b"x" * 5000)
            with self.assertRaisesRegex(TransferJournalError, "size limit"):
                journal.load(descriptor)

    def test_new_ids_are_128_bit_and_nonconstant(self):
        first, second = new_transfer_id(), new_transfer_id()
        self.assertEqual(len(first), 16)
        self.assertEqual(len(second), 16)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
