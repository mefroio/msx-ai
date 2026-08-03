import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from msx_protocol import (  # noqa: E402
    CRCMismatchError,
    Frame,
    FrameFlag,
    FrameStatus,
    FrameLengthError,
    FrameParser,
    FrameType,
    GarbageDataError,
    HEADER_SIZE,
    InvalidFrameTypeError,
    MAGIC,
    MAX_FRAME_SIZE,
    MAX_WIRE_PAYLOAD,
    OpcodeMismatchError,
    PROTOCOL_VERSION,
    PayloadTooLargeError,
    SequenceCounter,
    SequenceMismatchError,
    TruncatedFrameError,
    UnsupportedVersionError,
    crc16_ccitt,
    decode_frame,
    encode_frame,
    encode_many,
    validate_response,
)


class ProtocolFrameTest(unittest.TestCase):
    def test_crc_ccitt_false_reference_vector(self):
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)

    def test_wire_layout_and_roundtrip(self):
        frame = Frame(
            FrameType.REQUEST,
            sequence=0x1234,
            opcode=0x56,
            payload=b"abc",
            flags=FrameFlag.ACK_REQUIRED | 0x80,
        )
        wire = encode_frame(frame)

        self.assertEqual(wire[:2], MAGIC)
        self.assertEqual(wire[2], PROTOCOL_VERSION)
        self.assertEqual(wire[3], FrameType.REQUEST)
        self.assertEqual(wire[4], 0x81)
        self.assertEqual(int.from_bytes(wire[5:7], "little"), 0x1234)
        self.assertEqual(wire[7], 0x56)
        self.assertEqual(wire[8], FrameStatus.OK)
        self.assertEqual(int.from_bytes(wire[9:11], "little"), 3)
        self.assertEqual(wire[HEADER_SIZE:-2], b"abc")
        self.assertEqual(
            int.from_bytes(wire[-2:], "little"), crc16_ccitt(wire[:-2]))
        self.assertEqual(decode_frame(wire), frame)
        self.assertEqual(frame.encode(), wire)
        # A fixed vector makes accidental changes to the on-wire ABI visible.
        self.assertEqual(wire.hex(), "4d58030181341256000300616263aa1c")

    def test_response_status_is_explicit_and_extensible(self):
        response = Frame(
            FrameType.RESPONSE,
            sequence=9,
            opcode=0x80,
            payload=b"bad address",
            flags=FrameFlag.ERROR,
            status=FrameStatus.OUT_OF_RANGE,
        )
        decoded = decode_frame(response.encode())

        self.assertEqual(decoded.status, FrameStatus.OUT_OF_RANGE)
        self.assertFalse(decoded.ok)

        future = Frame(FrameType.RESPONSE, 10, 0x80, status=0xE1)
        self.assertEqual(decode_frame(future.encode()).status, 0xE1)
        self.assertFalse(future.ok)

    def test_full_16_bit_payload_length_and_configured_limit(self):
        payload = bytes(range(256)) * 255 + bytes(range(255))
        self.assertEqual(len(payload), MAX_WIRE_PAYLOAD)
        wire = Frame(FrameType.REQUEST, 1, 2, payload).encode()

        self.assertEqual(len(wire), MAX_FRAME_SIZE)
        self.assertEqual(decode_frame(wire).payload, payload)
        with self.assertRaises(PayloadTooLargeError):
            Frame(FrameType.REQUEST, 1, 2, payload + b"x")
        with self.assertRaises(PayloadTooLargeError):
            decode_frame(wire, max_payload=4096)

    def test_decode_reports_typed_wire_errors(self):
        wire = bytearray(Frame(FrameType.EVENT, 7, 0x20, b"state").encode())

        bad_crc = bytearray(wire)
        bad_crc[HEADER_SIZE] ^= 0x40
        with self.assertRaises(CRCMismatchError):
            decode_frame(bad_crc)

        bad_version = bytearray(wire)
        bad_version[2] = 4
        with self.assertRaises(UnsupportedVersionError):
            decode_frame(bad_version)

        bad_type = bytearray(wire)
        bad_type[3] = 0x7F
        with self.assertRaises(InvalidFrameTypeError):
            decode_frame(bad_type)

        with self.assertRaises(TruncatedFrameError):
            decode_frame(wire[:-1])
        with self.assertRaises(FrameLengthError):
            decode_frame(wire + b"extra")
        with self.assertRaises(PayloadTooLargeError):
            decode_frame(wire, max_payload=4)


class IncrementalParserTest(unittest.TestCase):
    def test_every_two_chunk_split_decodes_once(self):
        expected = Frame(FrameType.RESPONSE, 0x0102, 0xA0, b"chunked")
        wire = expected.encode()
        for split_at in range(len(wire) + 1):
            with self.subTest(split_at=split_at):
                parser = FrameParser()
                decoded = parser.feed(wire[:split_at])
                decoded.extend(parser.feed(wire[split_at:]))
                self.assertEqual(decoded, [expected])
                self.assertEqual(parser.pop_errors(), [])

    def test_fragmentation_byte_by_byte(self):
        # Include a complete encoded frame inside the payload.  The parser must
        # not mistake it for resynchronization while the outer frame is slow.
        nested = Frame(FrameType.EVENT, 99, 0x77, b"nested").encode()
        expected = Frame(
            FrameType.REQUEST, 0xCAFE, 0x31, b"prefix" + nested + b"suffix")
        parser = FrameParser()
        decoded = []
        for byte in expected.encode():
            decoded.extend(parser.feed(bytes([byte])))

        self.assertEqual(decoded, [expected])
        self.assertEqual(parser.buffered_bytes, 0)
        self.assertEqual(parser.pop_errors(), [])

    def test_garbage_prefix_and_split_magic(self):
        frame = Frame(FrameType.RESPONSE, 11, 0x01, b"ok")
        parser = FrameParser()

        self.assertEqual(parser.feed(b"noise\x00M"), [])
        decoded = parser.feed(frame.encode()[1:])

        self.assertEqual(decoded, [frame])
        errors = parser.pop_errors()
        self.assertTrue(any(isinstance(error, GarbageDataError)
                            for error in errors))

    def test_invalid_crc_resynchronizes_to_following_frame(self):
        damaged = bytearray(
            Frame(FrameType.RESPONSE, 1, 0x10, b"damaged").encode())
        damaged[HEADER_SIZE + 1] ^= 0xFF
        good = Frame(FrameType.EVENT, 2, 0x11, b"good")
        parser = FrameParser()

        self.assertEqual(parser.feed(bytes(damaged) + good.encode()), [good])
        self.assertTrue(any(isinstance(error, CRCMismatchError)
                            for error in parser.pop_errors()))

    def test_invalid_header_and_oversized_candidate_resynchronize(self):
        wrong_version = bytearray(
            Frame(FrameType.REQUEST, 1, 0x10, b"old").encode())
        wrong_version[2] = PROTOCOL_VERSION + 1
        too_large_for_session = Frame(
            FrameType.REQUEST, 2, 0x11, b"0123456789").encode()
        good = Frame(FrameType.EVENT, 3, 0x12, b"ok")
        parser = FrameParser(max_payload=8)

        decoded = parser.feed(
            bytes(wrong_version) + too_large_for_session + good.encode())

        self.assertEqual(decoded, [good])
        errors = parser.pop_errors()
        self.assertTrue(any(isinstance(error, UnsupportedVersionError)
                            for error in errors))
        self.assertTrue(any(isinstance(error, PayloadTooLargeError)
                            for error in errors))

    def test_concatenated_frames_are_returned_in_order(self):
        frames = [
            Frame(FrameType.REQUEST, 0xFFFE, 1, b"one"),
            Frame(FrameType.RESPONSE, 0xFFFE, 1, b"two"),
            Frame(FrameType.EVENT, 0xFFFF, 2, b"three"),
        ]
        parser = FrameParser()
        self.assertEqual(parser.feed(encode_many(frames)), frames)

    def test_truncated_frame_boundary_then_reuse(self):
        first = Frame(FrameType.REQUEST, 10, 0x30, b"incomplete").encode()
        second = Frame(FrameType.EVENT, 11, 0x31, b"next")
        parser = FrameParser()

        self.assertEqual(parser.feed(first[:-4]), [])
        self.assertEqual(parser.finish(), [])
        errors = parser.pop_errors()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TruncatedFrameError)
        self.assertEqual(parser.buffered_bytes, 0)

        self.assertEqual(parser.feed(second.encode()), [second])
        self.assertEqual(parser.pop_errors(), [])

    def test_finish_salvages_frame_after_large_truncation(self):
        # The first header promises 200 bytes but is severed before its payload.
        # A valid reconnect frame follows.  It becomes unambiguous at finish().
        incomplete = bytearray(
            Frame(FrameType.REQUEST, 1, 0x44, b"x" * 200).encode())
        incomplete = incomplete[:HEADER_SIZE + 3]
        good = Frame(FrameType.EVENT, 2, 0x45, b"reconnected")
        parser = FrameParser()

        self.assertEqual(parser.feed(incomplete + good.encode()), [])
        self.assertEqual(parser.finish(), [good])
        self.assertTrue(any(isinstance(error, TruncatedFrameError)
                            for error in parser.pop_errors()))

    def test_half_magic_at_eof_is_truncated(self):
        parser = FrameParser()
        parser.feed(MAGIC[:1])
        parser.finish()
        self.assertIsInstance(parser.pop_errors()[0], TruncatedFrameError)

    def test_every_nonempty_prefix_is_reported_truncated_at_eof(self):
        wire = Frame(FrameType.REQUEST, 4, 0x20, b"payload").encode()
        for cut_at in range(1, len(wire)):
            with self.subTest(cut_at=cut_at):
                parser = FrameParser()
                parser.feed(wire[:cut_at])
                self.assertEqual(parser.finish(), [])
                self.assertTrue(any(
                    isinstance(error, TruncatedFrameError)
                    for error in parser.pop_errors()))


class SequenceTest(unittest.TestCase):
    def test_counter_wraps_and_response_is_correlated(self):
        counter = SequenceCounter(0xFFFE)
        self.assertEqual([counter.next() for _ in range(4)],
                         [0xFFFE, 0xFFFF, 0x0000, 0x0001])

        request = Frame(FrameType.REQUEST, 0xBEEF, 0x22)
        response = Frame(FrameType.RESPONSE, 0xBEEF, 0x22, b"ok")
        validate_response(request, response)

        with self.assertRaises(SequenceMismatchError):
            validate_response(
                request, Frame(FrameType.RESPONSE, 0xBEF0, 0x22))
        with self.assertRaises(OpcodeMismatchError):
            validate_response(
                request, Frame(FrameType.RESPONSE, 0xBEEF, 0x23))


if __name__ == "__main__":
    unittest.main()
