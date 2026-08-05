import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
CORE = (ROOT / "agent" / "msx_agent_core.asm").read_text(encoding="utf-8")
LOADER = (ROOT / "agent" / "msx_memman_loader.asm").read_text(
    encoding="utf-8")
HELPER = (ROOT / "agent" / "msx_xfer.asm").read_text(encoding="utf-8")
PROTOCOL = (ROOT / "agent" / "msx_xfer_protocol.inc").read_text(
    encoding="utf-8")
REAL = (ROOT / "server" / "msx_real.py").read_text(encoding="utf-8")


class AgentPackBitsTransferSourceTest(unittest.TestCase):
    def test_transfer_helper_is_a_normal_safe_build_artifact(self):
        self.assertIn("AGENT_XFER_COM := work/agent/MSXAIXF.COM", MAKEFILE)
        self.assertRegex(MAKEFILE, r"(?m)^agent: .*\$\(AGENT_XFER_COM\)")
        self.assertIn("MSX_DOS_BENCH_COM_MAX := 36760", MAKEFILE)
        self.assertEqual(
            MAKEFILE.count("tools/check_msx_com_size.py $@ "), 2)
        self.assertIn("Usage: MSXAIXF /PUT|/GET", HELPER)
        self.assertIn("include 'agent/msx_memman_loader.asm'", HELPER)

    def test_transfer_helper_initializes_memman_inichk_control_code(self):
        discovery = HELPER.split("memman_find_agent:", 1)[1].split(
            "memman_find_agent_ready:", 1)[0]
        self.assertRegex(
            discovery,
            r"(?s)^\s*xor a\s+.*ld d,'M'\s+ld e,30\s+call EXTBIO")

    def test_zero_length_transfers_never_enter_z80_block_copy(self):
        path_copy = LOADER.split("loader_xfer_build_one_path:", 1)[1].split(
            "loader_xfer_build_prefix_done:", 1)[0]
        self.assertRegex(
            path_copy,
            r"(?s)ld bc,\(loader_xfer_prefix_length\)\s+"
            r"ld a,b\s+or c\s+jr z,loader_xfer_build_prefix_done\s+ldir")

        get_copy = CORE.split("frame_xfer_get_length_ready:", 1)[1].split(
            "frame_xfer_get_copy_done:", 1)[0]
        self.assertRegex(
            get_copy,
            r"(?s)ld bc,\(frame_response_buffer \+ 4\)\s+"
            r"ld a,b\s+or c\s+jr z,frame_xfer_get_copy_done\s+"
            r"ld hl,xfer_buffer\s+ld de,frame_response_buffer \+ 8\s+ldir")

    def test_tsr_finish_returns_success_after_publishing_result_flags(self):
        finish = CORE.split("tsr_talk_xfer_finish:", 1)[1].split(
            "tsr_talk_unsupported:", 1)[0]
        successful = finish.split(
            "tsr_talk_xfer_finish_cancelled:", 1)[0]
        failed = finish.split(
            "tsr_talk_xfer_finish_failed:", 1)[1]
        self.assertRegex(
            successful,
            r"(?s)ld \(xfer_result_flags\),a\s+xor a\s+ret\s*$")
        self.assertRegex(
            failed,
            r"(?s)ld \(xfer_result_flags\),a\s+xor a\s+ret\s*$")

    def test_postprocess_does_not_claim_final_verification_or_publication(self):
        postprocess = CORE.split("tsr_talk_xfer_postprocess:", 1)[1].split(
            "tsr_talk_xfer_finish:", 1)[0]
        finish = CORE.split("tsr_talk_xfer_finish:", 1)[1].split(
            "tsr_talk_xfer_finish_cancelled:", 1)[0]

        self.assertIn(
            "ld a,XFER_STATUS_ACTIVE | XFER_STATUS_RESUMABLE | "
            "XFER_STATUS_WIRE_VERIFIED",
            postprocess,
        )
        self.assertNotIn("XFER_STATUS_FINAL_VERIFIED", postprocess)
        self.assertNotIn("XFER_STATUS_PUBLISHED", postprocess)
        self.assertIn("XFER_STATUS_FINAL_VERIFIED", finish)
        self.assertIn("XFER_STATUS_PUBLISHED", finish)

    def test_packbits_is_put_only_until_target_encoder_exists(self):
        self.assertIn("XFER_ENCODING_PACKBITS: equ 1", PROTOCOL)
        self.assertIn("XFER_CAP_PACKBITS_DECODE: equ 32", PROTOCOL)
        self.assertIn("XFER_CAP_PACKBITS_ENCODE: equ 64", PROTOCOL)
        capabilities = re.search(
            r"(?m)^XFER_CAPABILITIES:.*$", PROTOCOL).group(0)
        self.assertIn("XFER_CAP_PACKBITS_DECODE", capabilities)
        self.assertNotIn("XFER_CAP_PACKBITS_ENCODE", capabilities)
        self.assertIn("MSXAIXF /PUT", REAL)
        self.assertIn("MSXAIXF /GET", REAL)

    def test_decoder_rejects_reserved_and_noncanonical_controls(self):
        decoder = LOADER.split("loader_xfer_packbits_loop:", 1)[1].split(
            "loader_xfer_packbits_complete:", 1)[0]
        self.assertIn("cp 080h", decoder)
        self.assertIn("reserved no-op is non-canonical", decoder)
        self.assertIn("cp 0FFh", decoder)
        self.assertIn("canonical runs are at least 3", decoder)
        self.assertIn("call loader_xfer_packbits_final_fits", decoder)

    def test_decoder_verifies_exact_wire_and_final_streams(self):
        loop = LOADER.split("loader_xfer_packbits_loop:", 1)[1].split(
            "loader_xfer_packbits_complete:", 1)[0]
        complete = LOADER.split("loader_xfer_packbits_complete:", 1)[1].split(
            "loader_xfer_packbits_create_error:", 1)[0]
        self.assertIn("call loader_xfer_position_equals_wire", loop)
        self.assertIn("call loader_xfer_packbits_read_exact", loop)
        self.assertIn("XFER_DESC_FINAL_SIZE", complete)
        self.assertIn("XFER_DESC_FINAL_CRC", complete)
        self.assertIn("call loader_xfer_crc_matches", complete)
        self.assertIn("DOS_ENSURE", complete)
        self.assertIn("call loader_xfer_publish_output", complete)

    def test_failure_deletes_only_create_new_owned_output(self):
        cleanup = LOADER.split("loader_xfer_delete_owned_output:", 1)[1].split(
            "loader_xfer_mark_progress:", 1)[0]
        self.assertIn("loader_xfer_output_owned", cleanup)
        self.assertIn("loader_xfer_output_path", cleanup)
        self.assertNotIn("loader_xfer_temp_path", cleanup)
        self.assertNotIn("loader_xfer_meta_path", cleanup)

    def test_put_release_and_batched_commit_separate_write_from_durability(self):
        release = CORE.split("tsr_talk_xfer_put_release:", 1)[1].split(
            "tsr_talk_xfer_put_commit:", 1)[0]
        commit = CORE.split("tsr_talk_xfer_put_commit:", 1)[1].split(
            "tsr_talk_xfer_get_publish:", 1)[0]
        ensure = LOADER.split("loader_xfer_put_ensure:", 1)[1].split(
            "loader_xfer_put_finalize:", 1)[0]

        self.assertIn("TSR_TALK_XFER_PUT_RELEASE: equ 0B1h", PROTOCOL)
        self.assertIn("ld (xfer_pending),a", release)
        self.assertIn("ld (xfer_buffer_length),a", release)
        self.assertNotIn("xfer_durable", release)
        self.assertNotIn("xfer_prefix_crc", release)
        self.assertLess(ensure.index("DOS_ENSURE"),
                        ensure.index("TSR_TALK_XFER_PUT_COMMIT"))
        self.assertEqual(LOADER.count("TSR_TALK_XFER_PUT_COMMIT"), 1)
        put_loop = LOADER.split("loader_xfer_put_loop:", 1)[1].split(
            "loader_xfer_put_ensure:", 1)[0]
        after_position = put_loop.split(
            "call loader_xfer_add_position", 1)[1]
        self.assertLess(
            after_position.index("ld hl,(loader_xfer_block_length)"),
            after_position.index("ld de,(loader_xfer_unflushed)"),
        )
        self.assertRegex(
            commit,
            r"(?s)xfer_accepted.*xfer_durable.*xfer_math32.*"
            r"ld hl,xfer_accepted\s+ld de,xfer_durable\s+ld bc,4\s+ldir")

        threshold = int(re.search(
            r"XFER_ENSURE_BATCH_BYTES:\s+equ\s+(\d+)", LOADER).group(1))
        capacity = int(re.search(
            r"XFER_PUT_CAPACITY:\s+equ\s+(\d+)", PROTOCOL).group(1))
        self.assertLessEqual(threshold + capacity - 1, 0xFFFF)

    def test_foreground_progress_updates_after_every_confirmed_block(self):
        put_before_ensure = LOADER.split(
            "loader_xfer_put_loop:", 1)[1].split(
            "loader_xfer_put_ensure:", 1)[0]
        put_ensure = LOADER.split(
            "loader_xfer_put_ensure:", 1)[1].split(
            "loader_xfer_put_finalize:", 1)[0]
        get_ack = LOADER.split(
            "loader_xfer_get_acked:", 1)[1].split(
            "loader_xfer_get_wait_close:", 1)[0]

        self.assertLess(
            put_before_ensure.index("TSR_TALK_XFER_PUT_RELEASE"),
            put_before_ensure.index("call loader_xfer_progress_after_block"))
        self.assertLess(put_ensure.index("DOS_ENSURE"),
                        put_ensure.index("TSR_TALK_XFER_PUT_COMMIT"))
        self.assertLess(
            put_ensure.index("TSR_TALK_XFER_PUT_COMMIT"),
            put_ensure.index("call loader_xfer_progress_after_block"))
        self.assertRegex(
            get_ack,
            r"(?s)call loader_xfer_add_position\s+"
            r"call loader_xfer_progress_after_block\s+"
            r"call loader_xfer_mark_progress")
        self.assertEqual(
            LOADER.count("call loader_xfer_progress_after_block"), 3)

    def test_progress_display_is_fixed_width_and_uses_32_bit_thresholds(self):
        progress = LOADER.split(
            "loader_xfer_progress_begin:", 1)[1].split(
            "loader_xfer_mark_progress:", 1)[0]
        message = LOADER.split(
            "loader_xfer_progress_message:", 1)[1].split(
            "loader_xfer_expected_direction:", 1)[0]

        self.assertIn("XFER_PROGRESS_BAR_WIDTH: equ 18", LOADER)
        self.assertIn("XFER_PROGRESS_DIVISOR:   equ 100", LOADER)
        self.assertIn("loader_xfer_progress_step + 2", progress)
        self.assertIn("loader_xfer_progress_next + 3", progress)
        self.assertIn("loader_xfer_position + 3", progress)
        self.assertIn("ld b,32", progress)
        self.assertIn("loader_xfer_progress_div100_loop", progress)
        self.assertIn("loader_xfer_progress_percent_digits", message)
        self.assertIn("loader_xfer_progress_rate_digits", message)
        self.assertIn('db "    0 B/s$"', message)
        self.assertNotIn("13,10", message)
        self.assertIn("add hl,bc                  ; percent * 18", progress)
        self.assertIn("ld de,100", progress)

        # 35 visible characters leave two columns unused on machines such as
        # the Gradiente Expert whose DOS LINLEN is 37. Reaching the final
        # column may auto-wrap before the following carriage return.
        rendered_width = 1 + 18 + 2 + 5 + 9
        self.assertEqual(rendered_width, 35)

    def test_rate_uses_confirmed_bytes_jiffies_and_pal_ntsc_frequency(self):
        rate = LOADER.split(
            "loader_xfer_progress_after_block:", 1)[1].split(
            "loader_xfer_progress_reached_next:", 1)[0]
        begin = LOADER.split(
            "loader_xfer_progress_begin:", 1)[1].split(
            "loader_xfer_progress_after_block:", 1)[0]

        self.assertIn("RG9SAV:                  equ 0FFE8h", LOADER)
        self.assertIn("bit 1,a", begin)
        self.assertIn("ld a,50", begin)
        self.assertIn("ld a,60", begin)
        self.assertIn("loader_xfer_block_length", rate)
        self.assertIn("loader_xfer_progress_pending", rate)
        self.assertIn("loader_xfer_progress_last_jiffy", rate)
        self.assertIn("loader_xfer_progress_divide_u16", rate)
        self.assertIn("ld hl,0FFFFh", rate)

    def test_progress_integer_algorithms_cover_protocol_boundaries(self):
        def divide_u16(dividend, divisor):
            quotient = dividend
            remainder = 0
            for _ in range(16):
                next_bit = (quotient >> 15) & 1
                quotient = (quotient << 1) & 0xFFFF
                remainder = (remainder << 1) | next_bit
                if remainder >= divisor:
                    remainder -= divisor
                    quotient += 1
            return quotient, remainder

        for dividend in (0, 1, 59, 60, 298, 17880, 65535):
            for divisor in (1, 2, 3, 10, 50, 60, 65535):
                self.assertEqual(
                    divide_u16(dividend, divisor), divmod(dividend, divisor))

        self.assertEqual(
            [divide_u16(percent * 18, 100)[0]
             for percent in range(101)],
            [(percent * 18) // 100 for percent in range(101)])
        self.assertEqual(divide_u16(100 * 18, 100)[0], 18)

        for size in (1, 2, 99, 100, 101, 65535, 65536, 0xFFFFFFFF):
            step, remainder = divmod(size, 100)
            fraction = 99 + remainder
            threshold = step
            if fraction >= 100:
                fraction -= 100
                threshold += 1
            thresholds = []
            for percent in range(1, 101):
                thresholds.append(threshold)
                if percent == 100:
                    break
                threshold += step
                fraction += remainder
                if fraction >= 100:
                    fraction -= 100
                    threshold += 1
            self.assertEqual(
                thresholds,
                [(percent * size + 99) // 100
                 for percent in range(1, 101)])

    def test_transaction_phases_recover_owned_output_and_completed_rename(self):
        metadata = LOADER.split("loader_xfer_create_metadata:", 1)[1].split(
            "loader_xfer_compare_bytes:", 1)[0]
        phases = LOADER.split("loader_xfer_set_phase:", 1)[1].split(
            "; Decode a complete standard PackBits", 1)[0]
        packbits = LOADER.split(
            "loader_xfer_put_packbits_finalize:", 1)[1].split(
            "loader_xfer_packbits_loop:", 1)[0]
        publish = LOADER.split("loader_xfer_publish_selected:", 1)[1].split(
            "loader_xfer_scan_exact:", 1)[0]

        self.assertIn('db "MXAI2MT2"', LOADER)
        self.assertIn("XFER_META_PHASE_INV", metadata)
        self.assertIn("DOS_ENSURE", metadata)
        self.assertLess(phases.index("write_exact"), phases.index("DOS_ENSURE"))
        self.assertLess(phases.index("DOS_ENSURE"),
                        phases.index("ld (loader_xfer_phase),a"))
        self.assertLess(packbits.index("loader_xfer_require_absent"),
                        packbits.index("ld a,XFER_PHASE_DECODING"))
        restart = packbits.split(
            "loader_xfer_packbits_restart_decode:", 1)[1]
        self.assertIn("DOS_DELETE", restart)
        self.assertIn("loader_xfer_output_path", restart)
        self.assertLess(publish.index("loader_xfer_require_absent"),
                        publish.index("ld a,XFER_PHASE_PUBLISHING"))
        self.assertLess(publish.index("ld a,XFER_PHASE_PUBLISHING"),
                        publish.index("DOS_RENAME"))
        self.assertLess(publish.index("DOS_RENAME"),
                        publish.index("ld a,XFER_PHASE_PUBLISHED"))
        recovery = publish.split("loader_xfer_publish_recover:", 1)[1]
        self.assertIn("loader_xfer_validate_final_path", recovery)
        self.assertIn("XFER_DESC_FINAL_SIZE", publish)
        self.assertIn("XFER_DESC_FINAL_CRC", publish)

    def test_lost_terminal_reply_uses_full_journal_and_exact_target_replay(self):
        raw_success = LOADER.split("call loader_xfer_publish_temp", 1)[1].split(
            "loader_xfer_get_file:", 1)[0]
        packed_success = LOADER.split(
            "loader_xfer_packbits_complete:", 1)[1].split(
            "loader_xfer_packbits_create_error:", 1)[0]
        replay = LOADER.split(
            "loader_xfer_prepare_receiptless_published:", 1)[1].split(
            "loader_xfer_prepare_metadata_error:", 1)[0]
        self.assertIn("ld de,loader_xfer_meta_path", raw_success)
        self.assertIn("ld de,loader_xfer_meta_path", packed_success)
        self.assertIn("XFER_DESC_RESUME_OFFSET", replay)
        self.assertIn("XFER_DESC_WIRE_SIZE", replay)
        self.assertIn("XFER_DESC_PREFIX_CRC", replay)
        self.assertIn("XFER_DESC_WIRE_CRC", replay)
        self.assertIn("call loader_xfer_validate_final_path", replay)
        self.assertNotIn("DOS_RENAME", replay)
        self.assertNotIn("DOS_DELETE", replay)

    def test_terminal_success_follows_cleanup_and_final_console_output(self):
        success_exit = LOADER.split("loader_xfer_success_exit:", 1)[1].split(
            "loader_xfer_timeout_error:", 1)[0]
        self.assertEqual(LOADER.count("call loader_xfer_finish_success"), 1)
        self.assertLess(
            success_exit.index("call loader_xfer_print"),
            success_exit.index("call loader_xfer_finish_success"))
        self.assertLess(
            success_exit.index("call loader_xfer_finish_success"),
            success_exit.index("ld c,0"))

        raw_put = LOADER.split("loader_xfer_put_finalize:", 1)[1].split(
            "loader_xfer_get_file:", 1)[0]
        get = LOADER.split("loader_xfer_get_finalize:", 1)[1].split(
            "loader_xfer_wait:", 1)[0]
        packed = LOADER.split("loader_xfer_packbits_complete:", 1)[1].split(
            "loader_xfer_packbits_create_error:", 1)[0]
        for path in (raw_put, get, packed):
            self.assertIn("jp loader_xfer_success_exit", path)
            self.assertNotIn("call loader_xfer_finish_success", path)

    def test_missing_sidecar_replays_an_empty_completed_put(self):
        prepare = LOADER.split("loader_xfer_prepare_put:", 1)[1].split(
            "loader_xfer_prepare_new:", 1)[0]
        self.assertIn("XFER_FLAG_RECEIPTLESS_REPLAY", prepare)
        self.assertIn("loader_xfer_requested_resume_equals_wire", prepare)
        self.assertLess(
            prepare.index("XFER_FLAG_RECEIPTLESS_REPLAY"),
            prepare.index("loader_xfer_requested_resume_equals_wire"),
        )
        self.assertLess(
            prepare.index("loader_xfer_requested_resume_equals_wire"),
            prepare.index("loader_xfer_prepare_receiptless_published"),
        )
        self.assertLess(
            prepare.index("loader_xfer_prepare_receiptless_published"),
            prepare.index("loader_xfer_requested_resume_is_zero"),
        )
        self.assertIn("cp ERR_NO_FILE", prepare)
        empty_fallback = prepare.split(
            "call loader_xfer_prepare_receiptless_published", 1)[1]
        self.assertIn(
            "jp nz,loader_xfer_prepare_metadata_error", empty_fallback)

    def test_resident_rejects_unproven_receiptless_open_requests(self):
        validation = CORE.split("frame_xfer_open_encoding_ok:", 1)[1].split(
            "; A running transfer owns the resident descriptor", 1)[0]
        self.assertIn("XFER_FLAGS_SUPPORTED", validation)
        self.assertIn("XFER_FLAG_RECEIPTLESS_REPLAY", validation)
        self.assertIn("XFER_DIRECTION_PUT", validation)
        self.assertIn("XFER_FLAG_RESUME", validation)
        self.assertIn("frame_request_buffer + 37", validation)
        self.assertIn("frame_request_buffer + 21", validation)
        self.assertIn("frame_request_buffer + 41", validation)
        self.assertIn("frame_request_buffer + 25", validation)

    def test_main_agent_contains_no_embedded_external_decompressor(self):
        combined = (CORE + LOADER).lower()
        self.assertNotIn("incbin 'work/agent/vendor/gunzip", combined)
        self.assertNotIn("gunzip_high", combined)


if __name__ == "__main__":
    unittest.main()
