import pathlib
import sys
import tempfile
import unittest
from unittest import mock
import zlib


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

import msx_real  # noqa: E402
from msx_real import RealMSX, RealMSXError, RealMSXProtocolError  # noqa: E402
from msx_transfer import (  # noqa: E402
    TransferAckReply,
    TransferCapabilitiesReply,
    TransferCapability,
    TransferDataReply,
    TransferDescriptor,
    TransferDirection,
    TransferEncoding,
    TransferError,
    TransferJournal,
    TransferOpenReply,
    TransferProgressReply,
    TransferRemoteError,
    TransferReplyFlag,
    TransferState,
    TransferStatusReply,
    TransferTerminalReply,
    crc32_file_prefix,
    normalize_msx_basic_text,
)


def decode_packbits(wire):
    output = bytearray()
    offset = 0
    while offset < len(wire):
        control = wire[offset]
        offset += 1
        if control == 0x80 or control == 0xFF:
            raise AssertionError("non-canonical PackBits stream")
        if control < 0x80:
            count = control + 1
            output.extend(wire[offset:offset + count])
            offset += count
        else:
            output.extend((wire[offset],) * (257 - control))
            offset += 1
    return bytes(output)


class ScriptedTransferMSX(RealMSX):
    """Foreground/mailbox double for the host's streaming state machine."""

    def __init__(self, *, get_source=b"", initial_put=b""):
        super().__init__()
        self.caps = TransferCapabilitiesReply(
            1,
            (TransferCapability.RAW | TransferCapability.PUT |
             TransferCapability.GET | TransferCapability.RESUME |
             TransferCapability.CRC32),
            298, 312, 63)
        self.descriptor = None
        self.transfer_state = TransferState.IDLE
        self.put_bytes = bytearray(initial_put)
        self.get_source = bytes(get_source)
        self.get_durable = 0
        self.get_offer = None
        self.close_requested = False
        self.typed_commands = []
        self.closed_ids = []
        self.open_calls = 0
        self.put_blocks = 0
        self.get_blocks = 0

    def file_transfer_capabilities(self, *, refresh=False):
        return self.caps

    def file_transfer_open(self, descriptor):
        self.open_calls += 1
        self.descriptor = descriptor
        self.transfer_state = TransferState.STAGED
        if descriptor.direction is TransferDirection.PUT:
            expected_prefix = zlib.crc32(self.put_bytes) & 0xFFFFFFFF
            if (descriptor.resume_offset != len(self.put_bytes) or
                    descriptor.resume_prefix_crc32 != expected_prefix):
                return TransferOpenReply(
                    TransferState.FAILED, TransferRemoteError.BINDING)
        else:
            self.get_durable = descriptor.resume_offset
            expected_prefix = (
                zlib.crc32(self.get_source[:self.get_durable]) & 0xFFFFFFFF)
            if descriptor.resume_prefix_crc32 != expected_prefix:
                return TransferOpenReply(
                    TransferState.FAILED, TransferRemoteError.BINDING)
        return TransferOpenReply(
            TransferState.STAGED, TransferRemoteError.NONE)

    def type_line(self, text, timeout=10):
        self.typed_commands.append(text)
        self.transfer_state = TransferState.READY
        return len(text) + 1

    def _metadata(self):
        if self.descriptor.direction is TransferDirection.PUT:
            return (self.descriptor.wire_size, self.descriptor.wire_crc32,
                    self.descriptor.final_size, self.descriptor.final_crc32)
        size = len(self.get_source)
        checksum = zlib.crc32(self.get_source) & 0xFFFFFFFF
        return size, checksum, size, checksum

    def file_transfer_status(self, transfer_id):
        if self.descriptor is None:
            raise RealMSXProtocolError("no staged transfer")
        self.assert_id(transfer_id)
        wire_size, wire_crc, final_size, final_crc = self._metadata()
        if self.descriptor.direction is TransferDirection.PUT:
            durable = accepted = len(self.put_bytes)
            prefix = zlib.crc32(self.put_bytes) & 0xFFFFFFFF
            if durable == wire_size and self.close_requested:
                state = TransferState.COMPLETE
                flags = (TransferReplyFlag.WIRE_VERIFIED |
                         TransferReplyFlag.FINAL_VERIFIED |
                         TransferReplyFlag.PUBLISHED)
                credit = 0
            else:
                state = (TransferState.READY if self.put_blocks == 0 else
                         TransferState.TRANSFERRING)
                flags = (TransferReplyFlag.ACTIVE |
                         TransferReplyFlag.RESUMABLE)
                credit = min(self.caps.max_put_chunk, wire_size - durable)
        else:
            durable = accepted = self.get_durable
            prefix = zlib.crc32(
                self.get_source[:self.get_durable]) & 0xFFFFFFFF
            if durable == wire_size and self.close_requested:
                state = TransferState.COMPLETE
                flags = (TransferReplyFlag.WIRE_VERIFIED |
                         TransferReplyFlag.FINAL_VERIFIED)
            else:
                state = (TransferState.READY if self.get_blocks == 0 else
                         TransferState.TRANSFERRING)
                flags = (TransferReplyFlag.ACTIVE |
                         TransferReplyFlag.RESUMABLE)
            credit = 0
        self.transfer_state = state
        return TransferStatusReply(
            state, self.descriptor.direction, self.descriptor.encoding,
            TransferRemoteError.NONE, flags, self.descriptor.transfer_id,
            wire_size, wire_crc, final_size, final_crc, durable, accepted,
            prefix, credit)

    def file_transfer_put_data(self, transfer_id, offset, data):
        self.assert_id(transfer_id)
        if offset != len(self.put_bytes):
            raise AssertionError("host PUT offset diverged")
        self.put_bytes += data
        self.put_blocks += 1
        end = len(self.put_bytes)
        credit = max(0, min(
            self.caps.max_put_chunk, self.descriptor.wire_size - end))
        state = TransferState.TRANSFERRING
        return TransferProgressReply(
            len(data), end, end, credit, state, TransferRemoteError.NONE)

    def file_transfer_get_read(self, transfer_id, offset, maximum):
        self.assert_id(transfer_id)
        if self.get_offer is not None:
            offered_offset, offered = self.get_offer
            if offset != offered_offset:
                raise AssertionError("host skipped a pinned GET block")
            return TransferDataReply(
                offered_offset, offered, TransferState.TRANSFERRING,
                TransferRemoteError.NONE)
        if offset != self.get_durable:
            raise AssertionError("host GET offset diverged")
        data = self.get_source[offset:offset + maximum]
        self.get_offer = (offset, data)
        self.get_blocks += 1
        return TransferDataReply(
            offset, data, TransferState.TRANSFERRING,
            TransferRemoteError.NONE)

    def file_transfer_get_ack(self, transfer_id, next_offset, prefix_crc32):
        self.assert_id(transfer_id)
        offered_offset, data = self.get_offer
        if next_offset != offered_offset + len(data):
            raise AssertionError("host acknowledged the wrong GET boundary")
        expected = zlib.crc32(self.get_source[:next_offset]) & 0xFFFFFFFF
        if prefix_crc32 != expected:
            raise AssertionError("host acknowledged the wrong GET CRC")
        self.get_durable = next_offset
        self.get_offer = None
        state = TransferState.TRANSFERRING
        return TransferAckReply(
            next_offset, state, TransferRemoteError.NONE)

    def file_transfer_close(self, transfer_id):
        self.assert_id(transfer_id)
        self.closed_ids.append(bytes(transfer_id))
        self.close_requested = True
        return TransferTerminalReply(
            TransferState.VERIFYING, TransferRemoteError.NONE)

    def assert_id(self, transfer_id):
        if bytes(transfer_id) != self.descriptor.transfer_id:
            raise AssertionError("host used an unbound transfer ID")


class BatchedPutTransferMSX(ScriptedTransferMSX):
    """PUT double that releases writes before batched durable commits."""

    def __init__(self, *, initial_put=b"", durable=0, sync_threshold=8192):
        super().__init__(initial_put=initial_put)
        self.put_durable = durable
        self.sync_threshold = sync_threshold
        self.commit_offsets = []
        self.maximum_gap = len(initial_put) - durable

    def file_transfer_status(self, transfer_id):
        if self.descriptor is None:
            raise RealMSXProtocolError("no staged transfer")
        self.assert_id(transfer_id)
        accepted = len(self.put_bytes)
        durable = self.put_durable
        self.maximum_gap = max(self.maximum_gap, accepted - durable)
        prefix = zlib.crc32(self.put_bytes[:durable]) & 0xFFFFFFFF
        if durable == self.descriptor.wire_size and self.close_requested:
            state = TransferState.COMPLETE
            flags = (TransferReplyFlag.WIRE_VERIFIED |
                     TransferReplyFlag.FINAL_VERIFIED |
                     TransferReplyFlag.PUBLISHED)
            credit = 0
        else:
            state = (TransferState.READY if self.put_blocks == 0 else
                     TransferState.TRANSFERRING)
            flags = TransferReplyFlag.ACTIVE | TransferReplyFlag.RESUMABLE
            credit = min(
                self.caps.max_put_chunk,
                self.descriptor.wire_size - accepted,
            )
        self.transfer_state = state
        return TransferStatusReply(
            state, self.descriptor.direction, self.descriptor.encoding,
            TransferRemoteError.NONE, flags, self.descriptor.transfer_id,
            self.descriptor.wire_size, self.descriptor.wire_crc32,
            self.descriptor.final_size, self.descriptor.final_crc32,
            durable, accepted, prefix, credit)

    def file_transfer_put_data(self, transfer_id, offset, data):
        self.assert_id(transfer_id)
        if offset != len(self.put_bytes):
            raise AssertionError("host PUT offset diverged")
        self.put_bytes += data
        self.put_blocks += 1
        accepted = len(self.put_bytes)
        self.maximum_gap = max(
            self.maximum_gap, accepted - self.put_durable)
        if (accepted - self.put_durable >= self.sync_threshold or
                accepted == self.descriptor.wire_size):
            self.put_durable = accepted
            self.commit_offsets.append(accepted)
        credit = min(
            self.caps.max_put_chunk,
            self.descriptor.wire_size - accepted,
        )
        return TransferProgressReply(
            len(data), accepted, self.put_durable, credit,
            TransferState.TRANSFERRING, TransferRemoteError.NONE)


class RealMSXStreamingTransferTest(unittest.TestCase):
    def test_put_fsyncs_close_intent_before_sending_close(self):
        class LoseCloseReply(ScriptedTransferMSX):
            def file_transfer_close(self, transfer_id):
                self.assert_id(transfer_id)
                raise RealMSXError("simulated lost CLOSE request")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "empty.bin"
            source.write_bytes(b"")
            state = root / "state"
            target = LoseCloseReply()

            with self.assertRaisesRegex(RealMSXError, "lost CLOSE"):
                target.put_file(
                    source, "A:\\EMPTY.BIN", compression="raw",
                    state_directory=state)

            candidate = TransferDescriptor(
                TransferDirection.PUT, TransferEncoding.RAW, b"N" * 16,
                0, 0, 0, 0, "A:\\EMPTY.BIN")
            record = TransferJournal(state).find_matching(
                candidate, caller_binding=str(source.resolve()))

        self.assertIsNotNone(record)
        self.assertTrue(record.close_intent)
        self.assertTrue(record.resumed_descriptor().receiptless_replay)

    def test_put_streams_past_64k_and_auto_falls_back_without_decoder(self):
        payload = (bytes(range(256)) * 300) + b"tail"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "large.bin"
            source.write_bytes(payload)
            target = ScriptedTransferMSX()

            with (mock.patch(
                    "msx_real.crc32_file_prefix",
                    wraps=crc32_file_prefix) as prefix_hash,
                  mock.patch(
                    "msx_real.prepare_put_payload",
                    wraps=msx_real.prepare_put_payload
                  ) as prepare):
                result = target.put_file(
                    source, "A:\\LARGE.BIN", compression="auto",
                    state_directory=root / "state")

        self.assertEqual(bytes(target.put_bytes), payload)
        self.assertGreater(target.put_blocks, 1)
        self.assertEqual(prefix_hash.call_count, 2)
        self.assertEqual(result["wire_bytes"], len(payload))
        self.assertEqual(result["encoding"], "raw")
        self.assertEqual(result["resumed_from"], 0)
        self.assertEqual(prepare.call_args.kwargs["mode"], "raw")
        self.assertTrue(target.typed_commands[0].startswith("MSXAIXF /PUT "))
        self.assertEqual(len(target.closed_ids), 1)

    def test_put_accepts_batched_durable_leaps_without_unbounded_inflight(self):
        payload = bytes(range(251)) * 100
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "batched.bin"
            source.write_bytes(payload)
            target = BatchedPutTransferMSX()

            result = target.put_file(
                source, "A:\\BATCH.BIN", compression="raw",
                state_directory=root / "state")

        self.assertEqual(bytes(target.put_bytes), payload)
        self.assertEqual(target.commit_offsets[-1], len(payload))
        self.assertGreater(len(target.commit_offsets), 1)
        self.assertGreater(target.maximum_gap, target.caps.max_put_chunk)
        self.assertLessEqual(
            target.maximum_gap,
            target.sync_threshold + target.caps.max_put_chunk - 1,
        )
        self.assertEqual(result["wire_bytes"], len(payload))

    def test_put_failure_before_ensure_journals_only_prior_durable_boundary(self):
        payload = bytes(range(241)) * 50

        class FailBeforeEnsure(BatchedPutTransferMSX):
            def file_transfer_status(self, transfer_id):
                if self.put_blocks >= 5 and self.put_durable == 0:
                    raise RealMSXError("simulated link loss before ENSURE")
                return super().file_transfer_status(transfer_id)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "interrupted.bin"
            source.write_bytes(payload)
            state = root / "state"
            target = FailBeforeEnsure()

            with self.assertRaisesRegex(RealMSXError, "before ENSURE"):
                target.put_file(
                    source, "A:\\BREAK.BIN", compression="raw",
                    state_directory=state)

            record = TransferJournal(state).load(
                target.descriptor, caller_binding=str(source.resolve()))
            self.assertEqual(record.confirmed_offset, 0)
            self.assertEqual(record.prefix_crc32, 0)
            self.assertGreater(len(target.put_bytes), 0)

    def test_put_rejects_an_unbounded_remote_accepted_durable_gap(self):
        payload = bytes(range(251)) * 100
        accepted = msx_real.FILE_TRANSFER_MAX_UNCOMMITTED + 1
        transfer_id = b"W" * 16
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        descriptor = TransferDescriptor(
            TransferDirection.PUT, TransferEncoding.RAW, transfer_id,
            len(payload), checksum, len(payload), checksum,
            "A:\\WINDOW.BIN")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "window.bin"
            source.write_bytes(payload)
            state = root / "state"
            TransferJournal(state).save(
                descriptor, confirmed_offset=0, prefix_crc32=0,
                caller_binding=str(source.resolve()))
            target = BatchedPutTransferMSX(
                initial_put=payload[:accepted], durable=0)
            target.descriptor = descriptor

            with self.assertRaisesRegex(
                    RealMSXProtocolError, "uncommitted window"):
                target.put_file(
                    source, descriptor.path, compression="raw",
                    state_directory=state)

    def test_put_resume_discovers_random_id_and_revalidates_prefix(self):
        payload = b"resumable-" * 1000
        prefix = payload[:4096]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.dat"
            source.write_bytes(payload)
            transfer_id = b"R" * 16
            checksum = zlib.crc32(payload) & 0xFFFFFFFF
            descriptor = TransferDescriptor(
                TransferDirection.PUT, TransferEncoding.RAW, transfer_id,
                len(payload), checksum, len(payload), checksum,
                "A:\\RESUME.DAT")
            TransferJournal(root / "state").save(
                descriptor, confirmed_offset=len(prefix),
                prefix_crc32=zlib.crc32(prefix) & 0xFFFFFFFF,
                caller_binding=str(source.resolve()))
            target = ScriptedTransferMSX(initial_put=prefix)

            result = target.put_file(
                source, descriptor.path, compression="raw",
                state_directory=root / "state")

        self.assertEqual(bytes(target.put_bytes), payload)
        self.assertEqual(result["transfer_id"], transfer_id.hex())
        self.assertEqual(result["resumed_from"], len(prefix))

    def test_put_resume_can_bind_regenerated_staging_to_original_source(self):
        payload = b"10 PRINT 1\r\n20 END\r\n\x1a"
        prefix = payload[:12]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / "game.bas"
            original.write_bytes(b"10 PRINT 1\n20 END\n")
            transfer_id = b"B" * 16
            checksum = zlib.crc32(payload) & 0xFFFFFFFF
            descriptor = TransferDescriptor(
                TransferDirection.PUT, TransferEncoding.RAW, transfer_id,
                len(payload), checksum, len(payload), checksum,
                "A:\\GAME.BAS")
            binding = str(original.resolve())
            TransferJournal(root / "state").save(
                descriptor, confirmed_offset=len(prefix),
                prefix_crc32=zlib.crc32(prefix) & 0xFFFFFFFF,
                caller_binding=binding)
            target = ScriptedTransferMSX(initial_put=prefix)

            result = target.put_file(
                original, descriptor.path, compression="raw",
                state_directory=root / "state")

        self.assertEqual(bytes(target.put_bytes), payload)
        self.assertEqual(result["transfer_id"], transfer_id.hex())
        self.assertEqual(result["resumed_from"], len(prefix))
        self.assertEqual(result["basic_format"], "ascii-msx-dos")

    def test_textual_basic_put_normalizes_before_crc_and_cleans_staging(self):
        source_bytes = b"10 PRINT \x80\n20 END\r"
        expected = b"10 PRINT \x80\r\n20 END\r\n\x1a"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "game.bas"
            source.write_bytes(source_bytes)
            target = ScriptedTransferMSX()

            result = target.put_file(
                source, "A:\\GAME.BAS", compression="raw",
                state_directory=root / "state")
            leftovers = list((root / "state").glob(".msx-basic-*.bas"))

        self.assertEqual(bytes(target.put_bytes), expected)
        self.assertEqual(result["source"], str(source.resolve()))
        self.assertEqual(result["source_bytes"], len(source_bytes))
        self.assertEqual(result["final_bytes"], len(expected))
        self.assertEqual(
            result["final_crc32"], f"{zlib.crc32(expected) & 0xFFFFFFFF:08x}")
        self.assertEqual(result["basic_format"], "ascii-msx-dos")
        self.assertEqual(result["basic_normalization"], "crlf-plus-0x1a")
        self.assertEqual(leftovers, [])

    def test_basic_normalization_precedes_packbits_planning(self):
        source_bytes = b"".join(
            str(10 + index).encode("ascii") + b" REM " + b"A" * 120 + b"\n"
            for index in range(100))
        expected = normalize_msx_basic_text(source_bytes)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "packed.bas"
            source.write_bytes(source_bytes)
            target = ScriptedTransferMSX()
            target.caps = TransferCapabilitiesReply(
                target.caps.version,
                target.caps.capabilities | TransferCapability.PACKBITS_DECODE,
                target.caps.max_put_chunk, target.caps.max_get_chunk,
                target.caps.max_path)

            result = target.put_file(
                source, "A:\\PACKED.BAS", compression="auto",
                state_directory=root / "state")

        self.assertEqual(result["encoding"], "packbits")
        self.assertEqual(decode_packbits(bytes(target.put_bytes)), expected)
        self.assertEqual(result["final_bytes"], len(expected))
        self.assertEqual(
            result["final_crc32"],
            f"{zlib.crc32(expected) & 0xFFFFFFFF:08x}")

    def test_invalid_basic_fails_before_open_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "invalid.bas"
            source.write_bytes(b"RUN\n\x00binary")
            target = ScriptedTransferMSX()

            with self.assertRaisesRegex(TransferError, "line number"):
                target.put_file(
                    source, "A:\\INVALID.BAS", compression="raw",
                    state_directory=root / "state")
            leftovers = list((root / "state").glob(".msx-basic-*.bas"))

        self.assertEqual(target.open_calls, 0)
        self.assertEqual(target.typed_commands, [])
        self.assertEqual(leftovers, [])

    def test_basic_staging_is_removed_after_remote_open_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "valid.bas"
            source.write_bytes(b"10 END\n")
            target = ScriptedTransferMSX(initial_put=b"foreign partial")

            with self.assertRaises(RealMSXError):
                target.put_file(
                    source, "A:\\VALID.BAS", compression="raw",
                    state_directory=root / "state")
            leftovers = list((root / "state").glob(".msx-basic-*.bas"))

        self.assertEqual(leftovers, [])

    def test_put_restart_attaches_to_the_active_foreground_worker(self):
        payload = b"ACTIVE-PUT-" * 1000
        prefix = payload[:3500]
        transfer_id = b"A" * 16
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        descriptor = TransferDescriptor(
            TransferDirection.PUT, TransferEncoding.RAW, transfer_id,
            len(payload), checksum, len(payload), checksum,
            "A:\\ACTIVE.BIN")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "active.bin"
            source.write_bytes(payload)
            TransferJournal(root / "state").save(
                descriptor, confirmed_offset=len(prefix),
                prefix_crc32=zlib.crc32(prefix) & 0xFFFFFFFF,
                caller_binding=str(source.resolve()))
            target = ScriptedTransferMSX(initial_put=prefix)
            target.descriptor = descriptor.with_resume(
                len(prefix), zlib.crc32(prefix) & 0xFFFFFFFF)

            result = target.put_file(
                source, descriptor.path, compression="raw",
                state_directory=root / "state")

        self.assertEqual(bytes(target.put_bytes), payload)
        self.assertEqual(result["transfer_id"], transfer_id.hex())
        self.assertEqual(target.open_calls, 0)
        self.assertEqual(target.typed_commands, [])

    def test_active_only_recovery_never_starts_an_unjournalled_put(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"do not type over a game")
            target = ScriptedTransferMSX()

            with self.assertRaisesRegex(
                    RealMSXError, "no matching PUT journal"):
                target.put_file(
                    source, "A:\\UNSAFE.BIN", compression="raw",
                    existing_only=True, state_directory=root / "state")

        self.assertEqual(target.open_calls, 0)
        self.assertEqual(target.typed_commands, [])

    def test_get_streams_past_64k_and_publishes_atomically(self):
        payload = bytes(range(251)) * 400
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            destination = root / "download.bin"
            target = ScriptedTransferMSX(get_source=payload)

            result = target.get_file(
                "A:\\REMOTE.BIN", destination,
                state_directory=root / "state")

            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(list(root.glob("*.msxpart")), [])
        self.assertGreater(target.get_blocks, 1)
        self.assertEqual(result["wire_bytes"], len(payload))
        self.assertEqual(len(target.closed_ids), 1)

    def test_get_resume_validates_local_prefix_before_requesting_more(self):
        payload = b"GET-RESUME" * 1500
        prefix = payload[:5000]
        transfer_id = b"G" * 16
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            destination = root / "result.bin"
            descriptor = TransferDescriptor(
                TransferDirection.GET, TransferEncoding.RAW, transfer_id,
                len(payload), checksum, len(payload), checksum,
                "A:\\SOURCE.BIN")
            part = root / (
                f".{destination.name}.{transfer_id.hex()}.msxpart")
            part.write_bytes(prefix)
            TransferJournal(root / "state").save(
                descriptor, confirmed_offset=len(prefix),
                prefix_crc32=crc32_file_prefix(part, len(prefix)).crc32,
                caller_binding=str(destination.resolve(strict=False)))
            target = ScriptedTransferMSX(get_source=payload)

            result = target.get_file(
                descriptor.path, destination,
                state_directory=root / "state")

            self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(result["resumed_from"], len(prefix))

    def test_get_restart_reuses_active_worker_and_replays_unacked_block(self):
        payload = b"ACTIVE-GET-" * 1200
        committed = payload[:4096]
        offered = payload[4096:4300]
        transfer_id = b"Q" * 16
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        descriptor = TransferDescriptor(
            TransferDirection.GET, TransferEncoding.RAW, transfer_id,
            len(payload), checksum, len(payload), checksum,
            "A:\\ACTIVE.DAT")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            destination = root / "active.dat"
            part = root / (
                f".{destination.name}.{transfer_id.hex()}.msxpart")
            part.write_bytes(committed + offered)
            TransferJournal(root / "state").save(
                descriptor, confirmed_offset=len(committed),
                prefix_crc32=zlib.crc32(committed) & 0xFFFFFFFF,
                caller_binding=str(destination.resolve(strict=False)))
            target = ScriptedTransferMSX(get_source=payload)
            target.descriptor = descriptor.with_resume(
                len(committed), zlib.crc32(committed) & 0xFFFFFFFF)
            target.get_durable = len(committed)
            target.get_offer = (len(committed), offered)

            result = target.get_file(
                descriptor.path, destination,
                state_directory=root / "state")
            downloaded = destination.read_bytes()

        self.assertEqual(downloaded, payload)
        self.assertEqual(result["transfer_id"], transfer_id.hex())
        self.assertEqual(target.open_calls, 0)
        self.assertEqual(target.typed_commands, [])

    def test_get_refuses_to_overwrite_an_existing_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = pathlib.Path(directory) / "existing.bin"
            destination.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                ScriptedTransferMSX(get_source=b"new").get_file(
                    "A:\\REMOTE.BIN", destination,
                    state_directory=pathlib.Path(directory) / "state")
            self.assertEqual(destination.read_bytes(), b"keep")

    def test_zero_length_put_and_get_use_close_as_end_of_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            empty = root / "empty.bin"
            empty.write_bytes(b"")
            put_target = ScriptedTransferMSX()
            put_result = put_target.put_file(
                empty, "A:\\EMPTY.BIN", compression="raw",
                state_directory=root / "put-state")

            destination = root / "download.bin"
            get_target = ScriptedTransferMSX(get_source=b"")
            get_result = get_target.get_file(
                "A:\\EMPTY.BIN", destination,
                state_directory=root / "get-state")
            downloaded = destination.read_bytes()

        self.assertEqual(put_result["wire_bytes"], 0)
        self.assertEqual(get_result["wire_bytes"], 0)
        self.assertEqual(downloaded, b"")
        self.assertEqual(len(put_target.closed_ids), 1)
        self.assertEqual(len(get_target.closed_ids), 1)

    def test_negotiated_packbits_put_streams_wire_and_reports_final_integrity(self):
        payload = (b"A" * 1024 + b"B" * 511 + b"MSX\n") * 80
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.txt"
            source.write_bytes(payload)
            target = ScriptedTransferMSX()
            target.caps = TransferCapabilitiesReply(
                target.caps.version,
                target.caps.capabilities | TransferCapability.PACKBITS_DECODE,
                target.caps.max_put_chunk, target.caps.max_get_chunk,
                target.caps.max_path)

            result = target.put_file(
                source, "A:\\SOURCE.TXT", compression="auto",
                state_directory=root / "state")

        self.assertEqual(result["encoding"], "packbits")
        self.assertLess(result["wire_bytes"], result["final_bytes"])
        self.assertEqual(decode_packbits(bytes(target.put_bytes)), payload)

    def test_packbits_supports_dos_paths_with_spaces(self):
        payload = (b"A" * 1024 + b"B" * 511 + b"MSX\n") * 16
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.txt"
            source.write_bytes(payload)
            target = ScriptedTransferMSX()
            target.caps = TransferCapabilitiesReply(
                target.caps.version,
                target.caps.capabilities | TransferCapability.PACKBITS_DECODE,
                target.caps.max_put_chunk, target.caps.max_get_chunk,
                target.caps.max_path)

            result = target.put_file(
                source, "A:\\MY DIR\\SOURCE.TXT", compression="auto",
                state_directory=root / "auto-state")
            explicit = ScriptedTransferMSX()
            explicit.caps = target.caps
            forced = explicit.put_file(
                source, "A:\\MY DIR\\SOURCE2.TXT", compression="packbits",
                state_directory=root / "packbits-state")

        self.assertEqual(result["encoding"], "packbits")
        self.assertEqual(forced["encoding"], "packbits")
        self.assertEqual(decode_packbits(bytes(target.put_bytes)), payload)


if __name__ == "__main__":
    unittest.main()
