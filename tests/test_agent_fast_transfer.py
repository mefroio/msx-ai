import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = (ROOT / "agent" / "msx_agent_core.asm").read_text(encoding="utf-8")
HELPER = (ROOT / "agent" / "msx_xfer.asm").read_text(encoding="utf-8")
ENGINE = (ROOT / "agent" / "msx_xfer_engine.inc").read_text(
    encoding="utf-8")
LOADER = (ROOT / "agent" / "msx_memman_loader.asm").read_text(
    encoding="utf-8")
PROTOCOL = (ROOT / "agent" / "msx_xfer_protocol.inc").read_text(
    encoding="utf-8")
REAL = (ROOT / "server" / "msx_real.py").read_text(encoding="utf-8")


def decimal_constant(name):
    match = re.search(
        rf"(?m)^{re.escape(name)}:\s+equ\s+(\d+)$", PROTOCOL)
    if match is None:
        raise AssertionError(f"missing decimal constant {name}")
    return int(match.group(1))


class AgentFastTransferSourceTest(unittest.TestCase):
    def test_fast_accumulator_owns_exactly_transient_page_one(self):
        self.assertIn("XFER_FAST_ACCUMULATOR_BASE: equ 04000h", HELPER)
        self.assertEqual(
            decimal_constant("XFER_FAST_ACCUMULATOR_CAPACITY"), 16 * 1024)
        self.assertIn("XFER_FAST_STACK_HEADROOM: equ 00800h", HELPER)
        self.assertRegex(
            HELPER,
            r"loader_xfer_accumulator:\s+equ\s+"
            r"XFER_FAST_ACCUMULATOR_BASE")
        self.assertNotRegex(
            HELPER,
            r"loader_xfer_accumulator:\s+ds\s+"
            r"XFER_FAST_ACCUMULATOR_CAPACITY")
        guard = ENGINE.split(
            "loader_xfer_require_fast_accumulator:", 1)[1].split(
            "loader_xfer_publish_ready:", 1)[0]
        self.assertIn("TPA_TOP_POINTER", guard)
        self.assertIn("xfer_helper_entry_sp", guard)
        self.assertEqual(
            guard.count(
                "XFER_FAST_ACCUMULATOR_END + XFER_FAST_STACK_HEADROOM"),
            2,
        )
        self.assertIn("ERR_NO_MEMORY", guard)
        resident_storage = CORE.split("xfer_descriptor:", 1)[1].split(
            "resident_initialize:", 1)[0]
        self.assertNotIn("loader_xfer_accumulator", resident_storage)
        self.assertIn(
            "FILE_TRANSFER_MAX_UNCOMMITTED = 16 * 1024", REAL)

    def test_fast_put_accumulates_before_one_crc_write_and_ensure(self):
        fast_put = ENGINE.split(
            "loader_xfer_put_fast_accumulate:", 1)[1].split(
            "loader_xfer_put_close:", 1)[0]
        flush = fast_put.split("loader_xfer_put_fast_flush:", 1)[1]

        self.assertIn("loader_xfer_accumulated", fast_put)
        self.assertIn("loader_xfer_accumulator", fast_put)
        self.assertIn("TSR_TALK_XFER_PUT_RELEASE", fast_put)
        self.assertLess(flush.index("call loader_xfer_crc_update"),
                        flush.index("call write_exact"))
        self.assertLess(flush.index("call write_exact"),
                        flush.index("DOS_ENSURE"))
        self.assertLess(flush.index("DOS_ENSURE"),
                        flush.index("TSR_TALK_XFER_PUT_COMMIT"))
        self.assertLess(
            flush.index("ld (loader_xfer_primary_ensured),a"),
            flush.index("call write_exact"),
        )
        ensured = flush.index(
            "ld (loader_xfer_primary_ensured),a",
            flush.index("DOS_ENSURE"),
        )
        self.assertLess(flush.index("DOS_ENSURE"), ensured)
        self.assertLess(ensured, flush.index("TSR_TALK_XFER_PUT_COMMIT"))
        before_flush = fast_put.split("loader_xfer_put_fast_flush:", 1)[0]
        self.assertNotIn("call loader_xfer_crc_update", before_flush)
        self.assertNotIn("call write_exact", before_flush)

        self.assertIn("XFER_FAST_ACCUMULATOR_HIGH_WATER", before_flush)
        finalize = ENGINE.split(
            "loader_xfer_put_finalize:", 1)[1].split(
            "loader_xfer_put_final_close:", 1)[0]
        self.assertIn("loader_xfer_accumulated", finalize)
        self.assertIn("loader_xfer_primary_ensured", finalize)
        self.assertIn("DOS_ENSURE", finalize)
        self.assertNotIn("XFER_FLAG_FAST_PUMP", finalize)

    def test_resident_enforces_the_fast_put_durability_window(self):
        put_guard = CORE.split(
            "frame_xfer_put_fits:", 1)[1].split(
            "frame_xfer_put_reply:", 1)[0]
        credit = CORE.split("xfer_credit:", 1)[1].split(
            "xfer_fast_put_window_remaining:", 1)[0]
        remaining = CORE.split(
            "xfer_fast_put_window_remaining:", 1)[1].split(
            "xfer_add_accepted:", 1)[0]

        self.assertNotIn("XFER_FLAG_FAST_PUMP", put_guard)
        self.assertIn("call xfer_fast_put_window_remaining", put_guard)
        self.assertIn("ld de,(xfer_request_count)", put_guard)
        self.assertIn("call xfer_fast_put_window_remaining", credit)
        self.assertIn("call current_xfer_fast_put_capacity", credit)
        self.assertIn("xfer_accepted", remaining)
        self.assertIn("xfer_durable", remaining)
        self.assertIn("ld hl,XFER_FAST_ACCUMULATOR_CAPACITY", remaining)

    def test_fast_get_refills_16k_once_then_emits_existing_frames(self):
        fast_get = ENGINE.split(
            "loader_xfer_get_fast_loop:", 1)[1].split(
            "loader_xfer_get_wait_ack:", 1)[0]
        refill = fast_get.split(
            "loader_xfer_get_fast_refill_count_ready:", 1)[1].split(
            "loader_xfer_get_fast_emit:", 1)[0]
        emit = fast_get.split("loader_xfer_get_fast_emit:", 1)[1]

        self.assertIn("ld hl,XFER_FAST_ACCUMULATOR_CAPACITY", fast_get)
        self.assertIn("ld de,loader_xfer_accumulator", refill)
        self.assertIn("ld c,DOS_READ", refill)
        self.assertIn("ld de,XFER_FAST_GET_CAPACITY", emit)
        self.assertIn("XFER_DESC_RESERVED", emit)
        self.assertIn("loader_xfer_get_fast_limit_ready:", emit)
        self.assertIn("ld hl,loader_xfer_accumulator", emit)
        self.assertIn("ld de,loader_xfer_buffer + 6", emit)
        self.assertIn("ldir", emit)
        self.assertNotIn("call loader_xfer_crc_update", fast_get)

    def test_transient_crc32_uses_the_exact_ieee_lookup_table(self):
        section = ENGINE.split("loader_xfer_crc_table:", 1)[1].split(
            "loader_xfer_crc_final_to:", 1)[0]
        table = bytes(int(value, 16) for value in re.findall(
            r"\b0([0-9A-Fa-f]{2})h\b", section))
        self.assertEqual(len(table), 256 * 4)

        crc = 0xFFFFFFFF
        for value in b"123456789":
            index = (crc ^ value) & 0xFF
            entry = int.from_bytes(table[index * 4:index * 4 + 4], "little")
            crc = ((crc >> 8) ^ entry) & 0xFFFFFFFF
        self.assertEqual(crc ^ 0xFFFFFFFF, 0xCBF43926)

        update = ENGINE.split("loader_xfer_crc_update:", 1)[1].split(
            "loader_xfer_crc_table:", 1)[0]
        self.assertIn("ld hl,loader_xfer_crc_table", update)
        self.assertNotIn("loader_xfer_crc_bit:", update)

    def test_fast_chunks_fill_the_2048_byte_frame(self):
        self.assertNotRegex(
            PROTOCOL, r"(?m)^XFER_(?:PUT|GET)_CAPACITY:")
        fast_put = decimal_constant("XFER_FAST_PUT_CAPACITY")
        fast_get = decimal_constant("XFER_FAST_GET_CAPACITY")
        self.assertEqual(fast_put + 21, 2047)
        self.assertEqual(fast_get + 8, 2048)

    def test_16c550_fast_chunks_stay_inside_128_byte_payloads(self):
        self.assertRegex(
            CORE,
            r"(?m)^UART16C550_FRAMED_SAFE_MAX:\s+equ\s+0080h$")
        self.assertRegex(
            CORE,
            r"(?m)^UART16C550_XFER_FAST_PUT_CAPACITY:\s+equ\s+"
            r"UART16C550_FRAMED_SAFE_MAX - 22$")
        self.assertRegex(
            CORE,
            r"(?m)^UART16C550_XFER_FAST_GET_CAPACITY:\s+equ\s+"
            r"UART16C550_FRAMED_SAFE_MAX - 8$")

        claim = CORE.split("tsr_talk_xfer_claim:", 1)[1].split(
            "tsr_talk_xfer_ready:", 1)[0]
        self.assertRegex(
            claim,
            r"(?s)ld \(hl\),a.*cp UART16C550_ID.*"
            r"ld \(hl\),UART16C550_XFER_FAST_GET_CAPACITY")

        put = CORE.split("frame_xfer_put_data:", 1)[1].split(
            "frame_xfer_put_length_ok:", 1)[0]
        get = CORE.split("frame_xfer_get_read:", 1)[1].split(
            "frame_xfer_get_max_ok:", 1)[0]
        self.assertIn("call current_xfer_fast_put_capacity", put)
        self.assertIn("call current_xfer_fast_get_capacity", get)

    def test_caps_and_open_expose_only_the_fast_data_plane(self):
        caps = CORE.split("frame_xfer_caps:", 1)[1].split(
            "frame_xfer_open:", 1)[0]
        fast_caps = CORE.split("frame_xfer_fast_caps:", 1)[1].split(
            "frame_xfer_fast_begin:", 1)[0]
        open_flags = CORE.split(
            "frame_xfer_open_encoding_ok:", 1)[1].split(
            "frame_xfer_open_resume_flag_ok:", 1)[0]
        for section in (caps, fast_caps):
            self.assertIn("call current_xfer_fast_put_capacity", section)
            self.assertIn("call current_xfer_fast_get_capacity", section)
        put_limit = CORE.split(
            "current_xfer_fast_put_capacity:", 1)[1].split(
            "current_xfer_fast_get_capacity:", 1)[0]
        get_limit = CORE.split(
            "current_xfer_fast_get_capacity:", 1)[1].split(
            "cmd_status:", 1)[0]
        self.assertIn("ld de,XFER_FAST_PUT_CAPACITY", put_limit)
        self.assertIn("ld de,XFER_FAST_GET_CAPACITY", get_limit)
        self.assertIn("cp UART16C550_ID", put_limit)
        self.assertIn("cp UART16C550_ID", get_limit)
        self.assertNotIn("XFER_PUT_CAPACITY", CORE + PROTOCOL)
        self.assertNotIn("XFER_GET_CAPACITY", CORE + PROTOCOL)
        self.assertIn("and XFER_FLAG_FAST_PUMP", open_flags)
        self.assertIn("jp z,frame_reply_unsupported", open_flags)

    def test_large_parser_window_is_x_only_armed_and_pumped(self):
        bounds = CORE.split("frame_request_status_ok:", 1)[1].split(
            "frame_payload_store:", 1)[0]
        self.assertIn("call current_framed_max", bounds)
        self.assertRegex(
            bounds,
            r"(?s)cp UART16C550_ID\s+jr z,frame_payload_range.*cp 'X'")
        self.assertIn("cp 'X'", bounds)
        self.assertIn("xfer_fast_pump_active", bounds)
        self.assertIn("xfer_fast_armed", bounds)
        self.assertIn("XFER_FLAG_FAST_PUMP", bounds)
        self.assertIn("ld hl,FRAMED_MAX", bounds)

        ram_read = CORE.split("frame_cmd_ram_read:", 1)[1].split(
            "frame_cmd_ram_write:", 1)[0]
        vram_read = CORE.split("frame_cmd_vram_read:", 1)[1].split(
            "frame_cmd_vram_write:", 1)[0]
        for ordinary_read in (ram_read, vram_read):
            self.assertIn("call current_framed_max", ordinary_read)
            self.assertNotIn("ld hl,FRAMED_MAX", ordinary_read)

    def test_foreground_pump_has_an_independent_timeout_stack(self):
        pump = CORE.split("tsr_talk_xfer_pump:", 1)[1].split(
            "tsr_talk_xfer_claim:", 1)[0]
        timeout = CORE.split("xfer_fast_pump_timeout:", 1)[1].split(
            "frame_request_buffer:", 1)[0]
        self.assertIn("ld (xfer_fast_pump_sp),sp", pump)
        self.assertIn("call frame_receive", pump)
        self.assertIn("ld sp,(xfer_fast_pump_sp)", timeout)
        self.assertIn("call transport_timeout_recover", timeout)
        self.assertIn("ld (xfer_fast_get_commit_after_send),a", timeout)
        self.assertIn("ld (frame_external_response),a", timeout)
        self.assertNotIn("hook_done", timeout)

    def test_fast_get_reconnect_rewinds_to_the_durable_checkpoint(self):
        begin = CORE.split("frame_xfer_fast_begin_ready:", 1)[1].split(
            "frame_xfer_fast_begin_arm:", 1)[0]
        poll = CORE.split("tsr_talk_xfer_get_poll:", 1)[1].split(
            "tsr_talk_xfer_get_poll_ack_pending:", 1)[0]
        rewind = ENGINE.split("loader_xfer_get_rewind:", 1)[1].split(
            "loader_xfer_get_finalize:", 1)[0]

        self.assertIn("xfer_fast_get_rewind_pending", begin)
        self.assertIn("ld hl,xfer_durable", begin)
        self.assertIn("ld de,xfer_accepted", begin)
        self.assertIn("ld de,xfer_fast_get_sent_offset", begin)
        self.assertIn("xfer_fast_get_rewind_pending", poll)
        self.assertIn("ld hl,xfer_durable", poll)
        self.assertIn("ld a,3", poll)
        self.assertIn("loader_xfer_buffer", rewind)
        self.assertIn("loader_xfer_position", rewind)
        self.assertIn("call loader_xfer_seek_position", rewind)
        self.assertIn("jp loader_xfer_get_loop", rewind)

    def test_fast_close_renders_the_host_measured_final_rate(self):
        close = CORE.split("frame_xfer_close:", 1)[1].split(
            "frame_xfer_cancel:", 1)[0]
        apply_rate = ENGINE.split(
            "loader_xfer_apply_fast_final_rate:", 1)[1].split(
            "loader_xfer_progress_format_u16:", 1)[0]

        self.assertIn("ld de,19", close)
        self.assertIn("xfer_fast_rate_hint", close)
        self.assertNotIn("XFER_FLAG_FAST_PUMP", apply_rate)
        self.assertIn("ld (loader_xfer_progress_rate),de", apply_rate)
        self.assertIn("loader_xfer_progress_render", apply_rate)

    def test_helper_pumps_put_get_ack_and_close_waits(self):
        put = ENGINE.split("loader_xfer_put_loop:", 1)[1].split(
            "loader_xfer_put_fast_accumulate:", 1)[0]
        get_ack = ENGINE.split("loader_xfer_get_wait_ack:", 1)[1].split(
            "loader_xfer_get_acked:", 1)[0]
        get_close = ENGINE.split("loader_xfer_get_wait_close:", 1)[1].split(
            "loader_xfer_get_finalize:", 1)[0]
        for section in (put, get_ack, get_close):
            self.assertIn("call loader_xfer_fast_pump", section)
        self.assertIn("TSR_TALK_XFER_PUMP", ENGINE)
        self.assertIn("XFER_FAST_GET_CAPACITY", ENGINE)

    def test_fast_mailbox_is_transient_page_zero_not_resident_bss(self):
        storage = CORE.split("xfer_descriptor:", 1)[1].split(
            "resident_initialize:", 1)[0]
        self.assertIn("xfer_fast_page0_buffer:", storage)
        self.assertNotRegex(storage, r"(?m)^xfer_buffer:")
        self.assertNotRegex(storage, r"(?m)^xfer_buffer_crc:")
        self.assertNotRegex(
            storage,
            r"(?m)^\s*ds XFER_FAST_GET_CAPACITY,0$")

        pump = CORE.split("tsr_talk_xfer_pump:", 1)[1].split(
            "tsr_talk_xfer_claim:", 1)[0]
        self.assertIn("ld bc,XFER_FAST_PAGE0_CAPACITY", pump)
        self.assertIn("call tsr_talk_page0_range", pump)
        self.assertIn("ld (xfer_fast_page0_buffer),hl", pump)
        self.assertIn("ld (xfer_fast_page0_frame),hl", pump)
        helper_pump = ENGINE.split("loader_xfer_fast_pump:", 1)[1].split(
            "loader_xfer_initialize:", 1)[0]
        self.assertIn("ld hl,loader_xfer_buffer", helper_pump)
        self.assertRegex(
            HELPER,
            r"(?s)loader_xfer_buffer:\s+ds XFER_WORK_CAPACITY,0\s+"
            r".*loader_xfer_frame_buffer:\s+"
            r"ds XFER_FAST_FRAME_CAPACITY,0")

        publish = CORE.split("tsr_talk_xfer_get_publish:", 1)[1].split(
            "tsr_talk_xfer_get_poll:", 1)[0]
        self.assertIn("ld (xfer_fast_page0_buffer),hl", publish)
        self.assertIn("call current_xfer_fast_get_capacity", publish)
        self.assertNotIn("XFER_FLAG_FAST_PUMP", publish)
        self.assertNotIn("ld de,xfer_buffer\n", publish)
        self.assertNotIn("xfer_buffer_crc", publish)

    def test_fast_frame_workspace_is_transient_and_emitted_explicitly(self):
        resident_frame = CORE.split("frame_request_buffer:", 1)[1].split(
            "last_response_small:", 1)[0]
        self.assertIn("ds FRAMED_SAFE_MAX,0", resident_frame)
        self.assertNotIn("ds FRAMED_MAX,0", resident_frame)

        receive = CORE.split("frame_payload_store:", 1)[1].split(
            "frame_payload_complete:", 1)[0]
        self.assertIn("ld hl,(xfer_fast_page0_frame)", receive)
        self.assertIn("ld (frame_external_request),a", receive)
        accepted_put = CORE.split("frame_xfer_put_fits:", 1)[1].split(
            "frame_xfer_put_reply:", 1)[0]
        self.assertIn("ld hl,(xfer_fast_page0_frame)", accepted_put)
        self.assertIn("ld de,(xfer_fast_page0_buffer)", accepted_put)

        get = CORE.split("frame_xfer_get_length_ready:", 1)[1].split(
            "frame_xfer_get_copy_done:", 1)[0]
        self.assertIn("ld de,(xfer_fast_page0_frame)", get)
        self.assertIn("ld (frame_external_response),a", get)
        emitter = CORE.split("frame_emit_response:", 1)[1].split(
            "frame_emit_crc:", 1)[0]
        self.assertIn("ld a,(frame_external_response)", emitter)
        self.assertIn("ld hl,(xfer_fast_page0_frame)", emitter)

    def test_reconnect_and_terminal_paths_release_hook_ownership(self):
        reconnect = CORE.split("frame_rebootstrap:", 1)[1].split(
            "frame_magic_found:", 1)[0]
        reset = CORE.split("xfer_reset:", 1)[1].split(
            "keybuf_spool_drain:", 1)[0]
        postprocess = CORE.split("tsr_talk_xfer_postprocess:", 1)[1].split(
            "tsr_talk_xfer_finish:", 1)[0]
        self.assertIn("ld (xfer_fast_armed),a", reconnect)
        self.assertIn("ld (xfer_fast_armed),a", reset)
        self.assertIn("ld (xfer_fast_pump_active),a", reset)
        self.assertIn("ld (xfer_fast_armed),a", postprocess)

    def test_cpu_snapshot_remains_hook_only(self):
        snapshot = CORE.split("frame_cmd_cpu_context:", 1)[1].split(
            "frame_cmd_debug_peer:", 1)[0]
        self.assertIn("ld a,(in_hook)", snapshot)
        self.assertIn("jp z,frame_reply_bad_state", snapshot)
        self.assertNotIn("xfer_fast_pump", snapshot)

    def test_transfer_engine_is_not_hidden_in_memman_lifecycle(self):
        self.assertIn("include 'agent/msx_xfer_engine.inc'", HELPER)
        self.assertIn("loader_xfer_put_file:", ENGINE)
        self.assertIn("loader_xfer_get_file:", ENGINE)
        self.assertNotIn("loader_xfer_put_file:", LOADER)
        self.assertNotIn("loader_xfer_get_file:", LOADER)


if __name__ == "__main__":
    unittest.main()
