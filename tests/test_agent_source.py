import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "agent" / "msx_agent_core.asm"
GENERIC_WRAPPER = ROOT / "agent" / "msx_agent.asm"
GENERIC_TRANSPORT = ROOT / "agent" / "transports" / "msx_transport_8251.inc"
UART16C550_TRANSPORT = (
    ROOT / "agent" / "transports" / "msx_transport_16c550.inc")
UNAPI_TRANSPORT = (
    ROOT / "agent" / "transports" / "msx_transport_unapi.inc")
TSR_BUILDER = ROOT / "tools" / "build_agent_tsr.py"
MAKEFILE = ROOT / "Makefile"


def _hex_bytes(source, start_label, end_marker):
    section = source.split(start_label + ":", 1)[1].split(end_marker, 1)[0]
    result = []
    for line in section.splitlines():
        code = line.split(";", 1)[0].strip()
        if code.startswith("db "):
            result.extend(int(value, 16) for value in
                          re.findall(r"\b([0-9A-Fa-f]+)h\b", code))
    return result


def _crc_table_entry(index):
    crc = index << 8
    for _ in range(8):
        crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (
            crc << 1) & 0xFFFF
    return crc


class ResidentAgentSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CORE.read_text(encoding="utf-8")
        cls.tsr_builder_source = TSR_BUILDER.read_text(encoding="utf-8")

    def test_split_crc_tables_are_complete_and_correct(self):
        low = _hex_bytes(
            self.source, "frame_crc_table_low", "frame_crc_table_high:")
        high = _hex_bytes(
            self.source, "frame_crc_table_high", "; ----------------------------------------------------- framed commands")
        expected = [_crc_table_entry(index) for index in range(256)]
        self.assertEqual(low, [value & 0xFF for value in expected])
        self.assertEqual(high, [value >> 8 for value in expected])

        crc = 0xFFFF
        for byte in b"123456789":
            index = byte ^ (crc >> 8)
            crc = ((crc << 8) & 0xFFFF) ^ (high[index] << 8) ^ low[index]
        self.assertEqual(crc, 0x29B1)

    def test_debug_is_runtime_opt_in_and_foreground_only(self):
        self.assertIn("debug_enabled:", self.source)
        self.assertIn('db "DEBUG",0', self.source)
        self.assertNotIn('db "ON",0', self.source)
        self.assertNotIn("option_on:", self.source)
        parser = self.source.split("loader_parse_debug:", 1)[1].split(
            "loader_parse_uninstall:", 1)[0]
        self.assertIn("ld (loader_debug_enabled),a", parser)
        self.assertIn("call loader_skip_token", parser)
        self.assertNotIn("loader_token_equals", parser)
        usage = self.source.split("usage_message:", 1)[1].split(
            "driver_required_message:", 1)[0]
        self.assertIn(
            "include 'agent/msx_version.inc'", usage)
        self.assertIn("[/MONITOR] [DEBUG]", usage)
        self.assertNotIn("DEBUG ON", usage)
        self.assertIn("debug_trace_command:", self.source)
        debug = self.source.split("debug_trace_command:", 1)[1].split(
            "; --------------------------------------------------------------- protocol", 1)[0]
        self.assertIn("ld a,(runtime_mode)", debug)
        self.assertIn("ld a,(in_hook)", debug)
        self.assertIn("call debug_putchar", debug)
        self.assertIn("call bdos_proxy", debug)
        self.assertRegex(
            debug,
            r"(?s)ld \(in_hook\),a.*call bdos_proxy.*di.*ld \(in_hook\),a")
        self.assertNotIn("call 00A2h", debug)
        self.assertNotIn("jp 00A2h", debug)
        self.assertIn("call debug_trace_hex_nibble", debug)
        self.assertIn("and 00Fh", debug)
        raw_dispatch = self.source.split("dispatch:", 1)[1].split(
            "cmd_hello:", 1)[0]
        self.assertRegex(
            raw_dispatch,
            r"(?s)cp '\?'.*jr z,cmd_hello.*call debug_trace_command")
        self.assertIn("FEATURE_DEBUG_PEER:", self.source)
        self.assertIn("frame_cmd_debug_peer:", self.source)
        self.assertIn('db "MCP client: ",0', self.source)
        self.assertRegex(
            self.source,
            r"(?s)cp 'I'.*jp z,frame_cmd_debug_peer.*"
            r"frame_cmd_debug_peer:.*cp 020h.*cp 07Fh.*call debug_putchar")
        hello = self.source.split("frame_cmd_hello:", 1)[1].split(
            "frame_cmd_status:", 1)[0]
        self.assertIn("ld a,(debug_enabled)", hello)

    def test_timi_chains_after_servicing_a_protocol_frame(self):
        hook = self.source.split("hook_chain_ready:", 1)[1].split(
            "hook_done:", 1)[0]
        self.assertRegex(
            hook,
            r"(?s)ld \(chain_keyi\),a.*ld a,\(hook_kind\).*"
            r"jr nz,hook_poll_transport.*hook_poll_transport:.*"
            r"call transport_rx_ready.*jr z,hook_done.*"
            r"ld a,\(hook_kind\).*jr nz,hook_dispatch_frame.*"
            r"hook_dispatch_frame:.*call receive_dispatch")
        unwind = self.source.split("hook_done:", 1)[1].split("else", 1)[0]
        self.assertRegex(
            unwind,
            r"(?s)ld a,\(chain_keyi\).*jr nz,memman_hook_continue.*"
            r"memman_hook_continue:.*xor a.*ret")

    def test_timi_only_keyi_guard_suppresses_firmware_without_polling(self):
        memman_hooks = self.source.split(
            "; ------------------------------------------------------------ BIOS hooks", 1
        )[1].split("else", 1)[0]
        hook = memman_hooks.split("resident_hook_saved_af:", 1)[1].split(
            "hook_done:", 1)[0]
        self.assertRegex(
            hook,
            r"(?s)and TRANSPORT_FLAG_KEYI_EXCLUSIVE.*"
            r"xor a.*ld \(chain_keyi\),a.*"
            r"and TRANSPORT_FLAG_TIMI_ONLY.*jr nz,hook_done.*"
            r"hook_poll_transport:.*call transport_rx_ready")
        unwind = memman_hooks.split("hook_done:", 1)[1]
        self.assertRegex(
            unwind,
            r"(?s)ld a,\(chain_keyi\).*jr nz,memman_hook_continue.*"
            r"ld a,1.*suppress remaining H\.KEYI.*ret")

    def test_framed_keybuf_input_is_atomic_resident_only_and_advertised(self):
        dispatch = self.source.split("frame_dispatch:", 1)[1].split(
            "frame_require_length:", 1)[0]
        self.assertRegex(dispatch, r"(?s)cp 't'.*jp z,frame_cmd_keybuf_input")

        hello = self.source.split("frame_cmd_hello:", 1)[1].split(
            "frame_cmd_status:", 1)[0]
        self.assertIn("call current_features", hello)
        self.assertIn("ld hl,15", hello)

        features = self.source.split("current_features:", 1)[1].split(
            "cmd_status:", 1)[0]
        self.assertIn("cp RUNTIME_RESIDENT", features)
        self.assertIn("FEATURE_KEYBUF_INPUT", features)

        keybuf = self.source.split("frame_cmd_keybuf_input:", 1)[1].split(
            "frame_cmd_ram_read:", 1)[0]
        self.assertRegex(
            keybuf,
            r"(?s)ld a,\(runtime_mode\).*cp RUNTIME_RESIDENT.*"
            r"ld a,\(in_hook\).*ld a,\(run_state\).*cp 1")
        self.assertIn("cp KEYBUF_SIZE", keybuf)
        self.assertIn("ld a,KEYBUF_SIZE - 1", keybuf)
        self.assertIn("ld hl,KEYBUF", keybuf)
        self.assertIn("ld (PUTPNT),hl", keybuf)
        self.assertLess(
            keybuf.index("frame_keybuf_copy_loop:"),
            keybuf.index("ld (PUTPNT),hl"))
        self.assertGreater(
            keybuf.index("ld (frame_response_buffer),a"),
            keybuf.index("ld (PUTPNT),hl"))
        self.assertIn("buffers alias", keybuf)
        self.assertIn("ld (frame_response_buffer + 1),a", keybuf)
        for forbidden in ("call bdos_proxy", "call CHGET", "call CHPUT"):
            self.assertNotIn(forbidden, keybuf)

    def test_credit_controlled_keyboard_spool_is_bounded_and_return_safe(self):
        self.assertRegex(
            self.source,
            r"(?m)^FEATURE_KEYBUF_SPOOL:\s+equ\s+020h\b")
        self.assertRegex(
            self.source,
            r"(?m)^KEYBUF_SPOOL_CAPACITY:\s+equ\s+255\b")
        features = self.source.split(
            "current_features_resident:", 1)[1].split("cmd_status:", 1)[0]
        self.assertIn("FEATURE_KEYBUF_SPOOL", features)
        dispatch = self.source.split("frame_dispatch:", 1)[1].split(
            "frame_require_length:", 1)[0]
        self.assertRegex(
            dispatch, r"(?s)cp 't'.*frame_cmd_keybuf_input.*"
                      r"cp 'T'.*frame_cmd_keybuf_spool")

        storage = self.source.split("keybuf_spool_get:", 1)[1].split(
            "resident_initialize:", 1)[0]
        self.assertIn("ds 256,0", storage)
        self.assertIn("dw keybuf_spool_buffer", storage)
        self.assertEqual(self.source.count("call nz,keybuf_spool_drain"), 2)

        spool = self.source.split("keybuf_spool_reset:", 1)[1].split(
            "frame_cmd_ram_read:", 1)[0]
        self.assertIn("cp 13", spool)
        self.assertIn("KEYBUF_SPOOL_SETTLE_TICKS", spool)
        self.assertIn("ld (PUTPNT),hl", spool)
        self.assertIn("ld hl,7", spool)
        self.assertIn("cpl", spool)
        self.assertRegex(
            spool,
            r"cpl\s+; 255 - count = writable spool credits\s+"
            r"or a\s+; CPL preserves Z on Z80; test the credits now\s+"
            r"jr z,frame_keybuf_spool_authorize")
        self.assertIn("KEYBUF_SPOOL_REQUEST_PUMP", spool)
        self.assertIn("KEYBUF_SPOOL_REQUEST_CANCEL", spool)
        self.assertIn("keybuf_spool_authorized", spool)
        self.assertIn("the host must authorize the next line", spool)
        reconnect = self.source.split("frame_rebootstrap:", 1)[1].split(
            "frame_magic_found:", 1)[0]
        self.assertIn("call keybuf_spool_reset", reconnect)
        for forbidden in ("call bdos_proxy", "call CHGET", "call CHPUT"):
            self.assertNotIn(forbidden, spool)

    def test_protocol_x_is_the_only_resident_file_transfer_path(self):
        self.assertNotIn("FEATURE_FILE_UPLOAD", self.source)
        features = self.source.split(
            "current_features_resident:", 1)[1].split("cmd_status:", 1)[0]
        self.assertIn("FEATURE_FILE_TRANSFER", features)
        dispatch = self.source.split("frame_dispatch:", 1)[1].split(
            "frame_require_length:", 1)[0]
        self.assertRegex(dispatch, r"(?s)cp 'X'.*frame_cmd_file_transfer")
        self.assertNotRegex(dispatch, r"cp 'U'")
        for obsolete in (
                "frame_cmd_file_upload:", "UPLOAD_CHUNK_CAPACITY",
                "UPLOAD_FLAG_ACTIVE", "UPLOAD_RESULT_SUCCEEDED",
                "upload_active:", "upload_buffer:", "upload_reset:"):
            self.assertNotIn(obsolete, self.source)

        storage = self.source.split("xfer_descriptor:", 1)[1].split(
            "resident_initialize:", 1)[0]
        self.assertIn("xfer_fast_page0_buffer:", storage)
        self.assertIn("xfer_fast_page0_frame:", storage)
        self.assertNotRegex(storage, r"(?m)^xfer_buffer:")
        self.assertNotRegex(storage, r"(?m)^xfer_buffer_crc:")
        self.assertNotIn("XFER_GET_CAPACITY", self.source)
        self.assertNotIn("XFER_PUT_CAPACITY", self.source)
        self.assertNotIn("equ upload_buffer", storage)

        talk = self.source.split("tsr_talk:", 1)[1].split(
            "tsr_talk_unsupported:", 1)[0]
        for action in (
                "TSR_TALK_XFER_CLAIM", "TSR_TALK_XFER_READY",
                "TSR_TALK_XFER_PUT_POLL", "TSR_TALK_XFER_GET_PUBLISH",
                "TSR_TALK_XFER_FINISH", "TSR_TALK_XFER_PUMP"):
            self.assertIn(action, talk)
        for obsolete in (
                "TSR_TALK_UPLOAD_BEGIN", "TSR_TALK_UPLOAD_POLL",
                "TSR_TALK_UPLOAD_END", "tsr_talk_upload_begin:",
                "tsr_talk_upload_poll:", "tsr_talk_upload_end:"):
            self.assertNotIn(obsolete, self.source)
        self.assertIn("tsr_talk_page0_range:", self.source)

    def test_memman_registers_keyi_guard_and_timi_dispatch(self):
        hook_spec = self.tsr_builder_source.split("hooks=(", 1)[1].split(
            "record_size=", 1)[0]
        self.assertIn("Hook(H_KEYI, offsets[\"resident_keyi_hook\"])",
                      hook_spec)
        self.assertIn("Hook(H_TIMI, offsets[\"resident_timi_hook\"])",
                      hook_spec)
        self.assertLess(hook_spec.index("Hook(H_KEYI"),
                        hook_spec.index("Hook(H_TIMI"))

    def test_memman_nested_timi_returns_through_quithook_without_resaving(self):
        hooks = self.source.split(
            "; ------------------------------------------------------------ BIOS hooks", 1
        )[1].split("else", 1)[0]
        timi_entry = hooks.split("resident_timi_hook:", 1)[1].split(
            "; A nested MemMan hook", 1)[0]
        self.assertRegex(
            timi_entry,
            r"(?s)push af.*ld a,\(in_hook\).*or a.*"
            r"jr nz,memman_nested_timi_return.*ld a,1.*"
            r"ld \(in_hook\),a.*ld \(hook_kind\),a")
        self.assertNotIn("push bc", timi_entry)
        nested_timi = hooks.split("memman_nested_timi_return:", 1)[1].split(
            "resident_hook_saved_af:", 1)[0]
        self.assertNotIn("ld (hook_kind),a", nested_timi)
        self.assertNotIn("ld (hook_dispatch_sp),sp", nested_timi)
        self.assertRegex(
            nested_timi,
            r"(?s)pop af.*ex af,af'.*xor a.*ex af,af'.*ret")

    def test_foreground_idle_hooks_leave_uart_with_monitor_loop(self):
        hooks = self.source.split(
            "; ------------------------------------------------------------ BIOS hooks", 1
        )[1].split("; DEBUG is deliberately", 1)[0]
        foreground = hooks.split("else\nresident_keyi_hook:", 1)[1]
        keyi = foreground.split("resident_timi_hook:", 1)[0]
        timi = foreground.split("resident_timi_hook:", 1)[1].split(
            "resident_hook_saved_af:", 1)[0]
        for entry in (keyi, timi):
            self.assertIn("ld a,(runtime_mode)", entry)
            self.assertIn("cp RUNTIME_MONITOR", entry)
            self.assertIn("ld a,(run_state)", entry)
            self.assertIn("or a", entry)
        self.assertIn("and TRANSPORT_FLAG_KEYI_EXCLUSIVE", keyi)
        self.assertIn("jp old_keyi", keyi)
        self.assertIn("jp old_timi", timi)

    def test_monitor_call_restores_interrupt_invariant(self):
        raw_call = self.source.split("cmd_call:", 1)[1].split(
            "cmd_run:", 1)[0]
        framed_call = self.source.split("frame_call_allowed:", 1)[1].split(
            "frame_cmd_run:", 1)[0]
        self.assertLess(raw_call.index("call jump_hl"), raw_call.index("di"))
        self.assertLess(raw_call.index("di"), raw_call.index("ld a,'K'"))
        self.assertLess(
            framed_call.index("call jump_hl"), framed_call.index("di"))
        self.assertLess(
            framed_call.index("di"), framed_call.index("jp frame_reply_ok"))

    def test_ctrl_stop_exits_foreground_monitor_safely(self):
        main = self.source.split("main_loop:", 1)[1].split(
            "; MODE is the BIOS-owned", 1)[0]
        self.assertRegex(
            main,
            r"(?s)call transport_rx_ready.*"
            r"jr nz,main_loop_receive.*call monitor_ctrl_stop_pressed.*"
            r"jp c,monitor_exit_to_dos.*jr main_loop")
        key_scan = self.source.split(
            "monitor_ctrl_stop_pressed:", 1)[1].split(
            "; MODE is the BIOS-owned", 1)[0]
        self.assertRegex(
            key_scan,
            r"(?s)push ix.*push iy.*ld ix,BREAKX.*"
            r"ld iy,\(EXPTBL - 1\).*call CALSLT.*pop iy.*pop ix.*ret")
        self.assertNotIn("call BREAKX", key_scan)
        uninstall = self.source.split("cmd_uninstall:", 1)[1].split(
            "error_busy:", 1)[0]
        exit_path = uninstall.split("monitor_exit_to_dos:", 1)[1]
        self.assertIn("ld hl,old_keyi", exit_path)
        self.assertIn("ld hl,old_timi", exit_path)
        self.assertIn("ld hl,(old_bdos)", exit_path)
        self.assertIn("call transport_restore", exit_path)
        self.assertIn("jp 0005h", exit_path)
        self.assertIn("Press CTRL+STOP to cancel", self.source)

    def test_hook_serial_waits_use_an_explicit_bounded_budget(self):
        budget = re.search(
            r"(?m)^HOOK_IO_BUDGET:\s+equ\s+0([0-9A-Fa-f]+)h\s*$",
            self.source)
        self.assertIsNotNone(budget)
        self.assertEqual(int(budget.group(1), 16), 0x1000)
        put = self.source.split("ser_put:", 1)[1].split("ser_get:", 1)[0]
        get = self.source.split("ser_get:", 1)[1].split(
            "hook_transport_timeout:", 1)[0]
        for section in (put, get):
            self.assertIn("ld bc,HOOK_IO_BUDGET", section)
            self.assertNotRegex(section, r"(?m)^\s*ld bc,0\s*$")
            self.assertIn("jp hook_transport_timeout", section)

    def test_hook_stack_comment_matches_reserved_bytes(self):
        reserve = re.search(
            r"(?m)^STACK_RESERVE:\s+equ\s+([0-9A-Fa-f]+)h", self.source)
        self.assertIsNotNone(reserve)
        self.assertEqual(int(reserve.group(1), 16), 224)
        stack_comment = self.source.split("; The hook stack", 1)[1].split(
            "STACK_RESERVE:", 1)[0]
        self.assertIn("224 bytes", stack_comment)
        self.assertIn("64-byte non-page-1 UNAPI staging", stack_comment)
        self.assertNotIn("384 bytes", stack_comment)

    def test_reconnect_marker_is_not_a_single_raw_byte(self):
        match = re.search(r"(?m)^RECONNECT_LENGTH:\s+equ\s+(\d+)\s*$",
                          self.source)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 4)

    def test_tsr_page1_is_hidden_and_mapping_is_monitor_only(self):
        raw_read = self.source.split("cmd_ram_read:", 1)[1].split(
            "cmd_ram_write:", 1)[0]
        self.assertIn("call tsr_page1_overlap", raw_read)
        self.assertIn("ram_read_zero_fill_loop:", raw_read)
        slot = self.source.split("cmd_slot_select:", 1)[1].split(
            "cmd_mapper_select:", 1)[0]
        mapper = self.source.split("cmd_mapper_select:", 1)[1].split(
            "error_protected_page:", 1)[0]
        self.assertGreaterEqual(slot.count("call ser_get"), 2)
        self.assertGreaterEqual(mapper.count("call ser_get"), 2)
        self.assertIn("jp error_protected_page", slot)
        self.assertIn("jp error_protected_page", mapper)
        capabilities = self.source.split("current_capabilities:", 1)[1].split(
            "cmd_status:", 1)[0]
        self.assertIn("CAPABILITY_MAPPING", capabilities)

        framed_slot = self.source.split("frame_cmd_slot_select:", 1)[1].split(
            "frame_cmd_mapper_select:", 1)[0]
        framed_mapper = self.source.split(
            "frame_cmd_mapper_select:", 1)[1].split(
                "; ----------------------------------------------------------- VDP helpers", 1)[0]
        self.assertIn("jp frame_reply_unsupported", framed_slot)
        self.assertIn("jp frame_reply_unsupported", framed_mapper)

    def test_raw_ram_and_io_do_not_wrap_hidden_high_address_bits(self):
        raw_read = self.source.split("cmd_ram_read:", 1)[1].split(
            "cmd_ram_write:", 1)[0]
        raw_write = self.source.split("cmd_ram_write:", 1)[1].split(
            "cmd_vram_read:", 1)[0]
        self.assertIn("call raw_range_wraps", raw_read)
        self.assertIn("jr c,ram_read_zero_fill", raw_read)
        self.assertIn("call raw_range_wraps", raw_write)
        self.assertIn("jr c,ram_write_reject", raw_write)

        io_read = self.source.split("cmd_io_read:", 1)[1].split(
            "cmd_io_write:", 1)[0]
        io_write = self.source.split("cmd_io_write:", 1)[1].split(
            "raw_range_wraps:", 1)[0]
        self.assertRegex(io_read, r"(?s)ld c,a.*ld b,0.*in a,\(c\)")
        self.assertRegex(
            io_write,
            r"(?s)ld c,a.*call ser_get.*ld b,0.*out \(c\),a")

        framed_read = self.source.split("frame_cmd_io_read:", 1)[1].split(
            "frame_cmd_io_write:", 1)[0]
        framed_write = self.source.split("frame_cmd_io_write:", 1)[1].split(
            "frame_cmd_slot_select:", 1)[0]
        self.assertRegex(framed_read, r"(?s)ld c,a.*ld b,0.*in a,\(c\)")
        self.assertRegex(framed_write, r"(?s)ld c,a.*ld b,0.*out \(c\),a")

    def test_tsr_page0_slot_calls_preserve_live_loop_registers(self):
        raw_read = self.source.split("ram_read_page0_loop:", 1)[1].split(
            "ram_read_protected:", 1)[0]
        self.assertRegex(
            raw_read,
            r"(?s)push bc.*ld a,\(RAMAD0\).*call RDSLT.*pop bc.*djnz")

        raw_write = self.source.split("ram_write_page0_loop:", 1)[1].split(
            "cmd_vram_read:", 1)[0]
        self.assertRegex(
            raw_write,
            r"(?s)push bc.*ld a,\(RAMAD0\).*call WRSLT.*pop bc.*djnz")

        framed_read = self.source.split(
            "frame_ram_read_page0_loop:", 1)[1].split(
                "frame_ram_read_range_bad_pop:", 1)[0]
        self.assertRegex(
            framed_read,
            r"(?s)push bc.*push de.*call RDSLT.*pop de.*pop bc.*dec bc")

        framed_write = self.source.split(
            "frame_ram_write_page0_loop:", 1)[1].split(
                "frame_decode_vram_address:", 1)[0]
        self.assertRegex(
            framed_write,
            r"(?s)push bc.*call WRSLT.*pop bc.*dec bc")

    def test_framed_ram_exact_end_at_64k_is_accepted(self):
        write = self.source.split("frame_cmd_ram_write:", 1)[1].split(
            "frame_ram_write_range_bad_pop:", 1)[0]
        self.assertRegex(
            write,
            r"(?s)add hl,bc.*jr nc,frame_ram_write_range_ok.*"
            r"ld a,h.*or l.*jr nz,frame_ram_write_range_bad_pop.*"
            r"jr frame_ram_write_range_ok")

    def test_resident_pause_survives_transport_idle_until_resume(self):
        pause = self.source.split("frame_pause_service_loop:", 1)[1].split(
            "frame_cmd_resume:", 1)[0]
        self.assertIn("frame_pause_complete:", pause)
        self.assertIn("ld sp,(hook_dispatch_sp)", pause)
        self.assertIn("jp hook_done", pause)

        timeout = self.source.split("hook_transport_timeout:", 1)[1].split(
            "frame_request_buffer:", 1)[0]
        before_state = timeout.split("ld a,(run_state)", 1)[0]
        self.assertIn("ld (frame_reconnect_count),a", before_state)
        self.assertRegex(
            timeout,
            r"(?s)ld a,\(run_state\).*cp 2.*"
            r"ld a,\(resume_requested\).*"
            r"jp nz,frame_pause_complete.*jp frame_pause_service_loop")

    def test_snapshot_pause_is_bounded_resident_v3_feature(self):
        self.assertRegex(
            self.source,
            r"(?m)^FEATURE_SNAPSHOT_LEASE:\s+equ\s+004h\b")
        features = self.source.split("current_features_resident:", 1)[1].split(
            "cmd_status:", 1)[0]
        self.assertIn("FEATURE_KEYBUF_INPUT", features)
        self.assertIn("FEATURE_SNAPSHOT_LEASE", features)
        dispatch = self.source.split("frame_dispatch:", 1)[1].split(
            "frame_require_length:", 1)[0]
        self.assertRegex(
            dispatch,
            r"(?s)cp 'S'.*jp z,frame_cmd_snapshot_pause.*"
            r"cp 's'.*jp z,frame_cmd_pause")

        snapshot = self.source.split(
            "frame_cmd_snapshot_pause:", 1)[1].split(
                "frame_cmd_pause:", 1)[0]
        self.assertRegex(
            snapshot,
            r"(?s)ld de,1.*call frame_require_length.*"
            r"ld a,\(in_hook\).*jp z,frame_reply_bad_state.*"
            r"ld a,\(run_state\).*cp 1.*jp nz,frame_reply_bad_state.*"
            r"ld a,\(frame_request_buffer\).*or a.*"
            r"jp z,frame_reply_bad_arg")
        self.assertRegex(
            snapshot,
            r"(?s)call frame_cache_and_send.*ld a,2.*"
            r"ld \(run_state\),a.*ld \(resume_requested\),a.*"
            r"ld a,\(frame_request_buffer\).*ld \(snapshot_lease\),a.*"
            r"ld \(snapshot_lease_reload\),a.*"
            r"jr frame_pause_service_loop")

    def test_cpu_snapshot_is_versioned_cached_h_timi_context(self):
        self.assertRegex(
            self.source,
            r"(?m)^FEATURE_CPU_SNAPSHOT:\s+equ\s+040h\b")
        self.assertRegex(
            self.source,
            r"(?m)^CPU_CONTEXT_VERSION:\s+equ\s+1\b")
        self.assertRegex(
            self.source,
            r"(?m)^CPU_CONTEXT_SIZE:\s+equ\s+40\b")
        features = self.source.split("current_features:", 1)[1].split(
            "cmd_status:", 1)[0]
        self.assertIn("ld b,FEATURE_CPU_SNAPSHOT", features)

        raw_dispatch = self.source.split("dispatch:", 1)[1].split(
            "cmd_hello:", 1)[0]
        self.assertNotRegex(raw_dispatch, r"cp 'D'")
        framed_dispatch = self.source.split("frame_dispatch:", 1)[1].split(
            "frame_require_length:", 1)[0]
        self.assertRegex(
            framed_dispatch,
            r"(?s)cp 'D'.*jp z,frame_cmd_cpu_context")

        context = self.source.split("frame_cmd_cpu_context:", 1)[1].split(
            "frame_cmd_debug_peer:", 1)[0]
        self.assertRegex(
            context,
            r"(?s)ld de,1.*call frame_require_length.*"
            r"ld a,\(frame_request_buffer\).*cp CPU_CONTEXT_VERSION")
        self.assertRegex(
            context,
            r"(?s)ld a,\(in_hook\).*or a.*"
            r"ld a,\(hook_kind\).*cp 1")
        self.assertRegex(
            context,
            r"(?s)ld hl,\(hook_context_sp\).*ld bc,20.*ldir")
        self.assertIn("ld a,i", context)
        self.assertIn("jp po,frame_cpu_context_iff2_clear", context)
        self.assertIn("ld a,r", context)
        self.assertIn("ld bc,2", context)
        self.assertIn("ld hl,CPU_CONTEXT_SIZE", context)
        for unsafe in ("call bdos_proxy", "call debug_putchar", "call 0005h"):
            self.assertNotIn(unsafe, context)

        self.assertEqual(
            self.source.count("ld (hook_context_sp),sp"), 2)
        cache = self.source.split("frame_cache_and_send:", 1)[1].split(
            "frame_emit_response:", 1)[0]
        self.assertIn("cp FRAME_CACHE_MAX + 1", cache)
        storage = self.source.split("last_response_small:", 1)[1].split(
            "if MSXAI_TSR_BUILD", 1)[0]
        self.assertIn("ds FRAME_CACHE_MAX,0", storage)

    def test_snapshot_lease_timeout_and_manual_pause_semantics(self):
        raw_manual = self.source.split("cmd_pause:", 1)[1].split(
            "pause_service_loop:", 1)[0]
        self.assertRegex(
            raw_manual,
            r"(?s)ld a,2.*ld \(run_state\),a.*xor a.*"
            r"ld \(resume_requested\),a.*ld \(snapshot_lease\),a.*"
            r"ld \(snapshot_lease_reload\),a")

        manual = self.source.split("frame_cmd_pause:", 1)[1].split(
            "frame_pause_service_loop:", 1)[0]
        self.assertRegex(
            manual,
            r"(?s)ld a,2.*ld \(run_state\),a.*xor a.*"
            r"ld \(resume_requested\),a.*ld \(snapshot_lease\),a.*"
            r"ld \(snapshot_lease_reload\),a")

        service = self.source.split("frame_pause_service_loop:", 1)[1].split(
            "frame_pause_complete:", 1)[0]
        self.assertRegex(
            service,
            r"(?s)call receive_dispatch.*ld a,\(resume_requested\).*"
            r"jr nz,frame_pause_complete.*"
            r"ld a,\(snapshot_lease_reload\).*"
            r"ld \(snapshot_lease\),a.*jr frame_pause_service_loop")

        timeout = self.source.split("hook_transport_timeout:", 1)[1].split(
            "frame_request_buffer:", 1)[0]
        self.assertRegex(
            timeout,
            r"(?s)ld a,\(run_state\).*cp 2.*"
            r"ld a,\(resume_requested\).*jp nz,frame_pause_complete.*"
            r"ld a,\(snapshot_lease\).*or a.*"
            r"jp z,frame_pause_service_loop.*dec a.*"
            r"ld \(snapshot_lease\),a.*jp z,frame_pause_complete")
        paused_timeout = timeout.split("hook_timeout_state_done:", 1)[0]
        self.assertNotIn("ld (snapshot_lease_reload),a", paused_timeout)

    def test_framed_parser_advertises_and_emits_wake_ack(self):
        self.assertRegex(
            self.source,
            r"(?m)^FEATURE_FRAME_WAKE_ACK:\s+equ\s+008h\b")
        self.assertRegex(
            self.source,
            r"(?m)^FRAME_WAKE_ACK:\s+equ\s+006h\b")
        features = self.source.split("current_features:", 1)[1].split(
            "cmd_status:", 1)[0]
        self.assertRegex(
            features,
            r"(?s)ld a,\(active_transport_flags\).*"
            r"and TRANSPORT_FLAG_FRAME_WAKE_ACK.*"
            r"ld b,FEATURE_FRAME_WAKE_ACK")
        self.assertIn("FEATURE_TIMI_POLL_SAFE", features)
        magic = self.source.split("frame_have_magic_m:", 1)[1].split(
            "frame_reconnect_byte:", 1)[0]
        self.assertRegex(
            magic,
            r"(?s)ld a,\(active_transport_flags\).*"
            r"and TRANSPORT_FLAG_FRAME_WAKE_ACK.*"
            r"jr z,frame_wait_second_magic.*ld a,FRAME_WAKE_ACK.*"
            r"call ser_put.*call ser_get")
        reconnect = self.source.split("frame_reconnect_byte:", 1)[1].split(
            "frame_rebootstrap:", 1)[0]
        self.assertRegex(
            reconnect,
            r"(?s)and TRANSPORT_FLAG_FRAME_WAKE_ACK.*"
            r"ld a,FRAME_WAKE_ACK.*call ser_put.*"
            r"ld a,\(frame_reconnect_count\)")

    def test_raw_features_are_available_before_v3_upgrade(self):
        dispatch = self.source.split("dispatch:", 1)[1].split(
            "cmd_status:", 1)[0]
        self.assertRegex(
            dispatch,
            r"(?s)cp 'N'.*jr z,cmd_bootstrap_features.*"
            r"cmd_bootstrap_features:.*ld a,'K'.*call current_features")

    def test_snapshot_lease_is_cleared_by_lifecycle_and_exit_paths(self):
        initialize = self.source.split("resident_initialize:", 1)[1].split(
            "resident_main:", 1)[0]
        reset = self.source.split("monitor_reset:", 1)[1].split(
            "main_loop:", 1)[0]
        raw_resume = self.source.split("cmd_resume:", 1)[1].split(
            "cmd_stop:", 1)[0]
        raw_stop = self.source.split("cmd_stop:", 1)[1].split(
            "; ------------------------------------------------------ hardware control", 1)[0]
        pause_complete = self.source.split("frame_pause_complete:", 1)[1].split(
            "frame_cmd_resume:", 1)[0]
        frame_resume = self.source.split("frame_cmd_resume:", 1)[1].split(
            "frame_cmd_stop:", 1)[0]
        frame_stop = self.source.split("frame_cmd_stop:", 1)[1].split(
            "frame_cmd_io_read:", 1)[0]
        for section in (
                initialize, reset, raw_resume, raw_stop, pause_complete,
                frame_resume, frame_stop):
            self.assertIn("ld (snapshot_lease),a", section)
            self.assertIn("ld (snapshot_lease_reload),a", section)

    def test_protocol_core_has_no_uart_specific_registers(self):
        for token in ("UART_DATA", "UART_STATUS", "UART_LSR", "COMMSK"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.source)
        self.assertIn("transport_bind:", self.source)
        self.assertIn("transport_init:\n    jp 0000h", self.source)
        self.assertIn("active_transport_flags", self.source)
        self.assertIn("active_transport_control_level", self.source)

    def test_single_wrapper_and_runtime_driver_selection(self):
        wrapper = GENERIC_WRAPPER.read_text(encoding="utf-8")
        self.assertIn("msx_agent_core.asm", wrapper)
        self.assertIn("msx_transport_8251.inc", self.source)
        self.assertIn("msx_transport_16c550.inc", self.source)
        self.assertIn("msx_transport_unapi.inc", self.source)
        self.assertIn('db "/DRIVER:8251",0', self.source)
        self.assertIn('db "/DRIVER:16C550",0', self.source)
        self.assertIn('db "/DRIVER:UNAPI",0', self.source)
        makefile = MAKEFILE.read_text(encoding="utf-8")
        self.assertIn("work/agent/MSXAI.COM", makefile)
        self.assertNotIn("MSXAI2.COM", makefile)
        self.assertNotIn("MSXAIBD.COM", makefile)

    def test_each_transport_implements_the_byte_stream_contract(self):
        for path, prefix, transport_id in (
                (GENERIC_TRANSPORT, "UART8251", 0),
                (UART16C550_TRANSPORT, "UART16C550", 1)):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertRegex(
                    source,
                    rf"(?m)^{prefix}_ID:\s+equ\s+{transport_id}\s*$")
                self.assertIn(f"{prefix}_STATE_SIZE:", source)
                self.assertRegex(
                    source,
                    rf"(?m)^{prefix}_FLAGS:\s+equ\s+"
                    r"TRANSPORT_FLAG_KEYI_EXCLUSIVE\s+\|\s+"
                    r"TRANSPORT_FLAG_TIMI_ONLY\s+\|\s+"
                    r"TRANSPORT_FLAG_FRAME_WAKE_ACK\s*$")
                self.assertIn(f"{prefix}_CONTROL_LEVEL:", source)
                label_prefix = prefix.lower().replace("uart", "uart")
                for operation in ("init", "restore", "rx_ready", "tx_ready",
                                  "read", "write", "service", "flush"):
                    self.assertIn(f"{label_prefix}_{operation}:", source)
                restore = source.split(
                    f"{label_prefix}_restore:", 1)[1].split(
                        f"{label_prefix}_rx_ready:", 1)[0]
                self.assertIn("xor a", restore)

    def test_unapi_transport_is_passive_buffered_and_reconnectable(self):
        source = UNAPI_TRANSPORT.read_text(encoding="utf-8")
        self.assertRegex(source, r"(?m)^UNAPI_ID:\s+equ\s+2\s*$")
        self.assertRegex(
            source,
            r"(?m)^UNAPI_FLAGS:\s+equ\s+TRANSPORT_FLAG_TIMI_ONLY\s+\|\s+"
            r"TRANSPORT_FLAG_FRAME_WAKE_ACK\s*$")
        for operation in ("init", "restore", "rx_ready", "tx_ready",
                          "read", "write", "service", "flush"):
            self.assertIn(f"unapi_{operation}:", source)

        discovery = source.split("unapi_discover:", 1)[1].split(
            "; ----------------------------------------------------- connection lifecycle", 1)[0]
        self.assertIn("bit 5,l", discovery)
        self.assertIn("and 008h", discovery)
        self.assertIn("ld (unapi_open_blocking),a", discovery)
        self.assertIn("cp 0C0h", discovery)
        self.assertIn("call CALSLT", discovery)
        self.assertIn("unapi_mapped_dispatch:", discovery)

        listener = source.split("unapi_open_listener:", 1)[1].split(
            "unapi_abort_current:", 1)[0]
        self.assertIn("ld de,13", listener)
        self.assertIn("ld de,(unapi_listen_port)", listener)
        self.assertIn("ld (hl),003h", listener)
        self.assertIn("ld a,UNAPI_TCP_OPEN", listener)
        open_result = listener.split("ld a,UNAPI_TCP_OPEN", 1)[1]
        self.assertIn("ld a,b\n    or a", open_result)
        self.assertLess(open_result.index("ld a,UNAPI_ERR_NO_CONN"),
                        open_result.index("ld (unapi_connection),a"))

        abort = source.split("unapi_abort_current:", 1)[1].split(
            "unapi_drop_connection:", 1)[0]
        current_abort = abort.split("unapi_abort_deferred:", 1)[0]
        self.assertLess(current_abort.index("cp UNAPI_ERR_OK"),
                        current_abort.index("cp UNAPI_ERR_NO_CONN"))
        self.assertLess(current_abort.index("cp UNAPI_ERR_NO_CONN"),
                        current_abort.index("ret nz"))
        self.assertLess(current_abort.index("ret nz"),
                        current_abort.index("ld (unapi_connection),a"))
        cleanup_abort = abort.split("unapi_abort_deferred:", 1)[1]
        self.assertLess(cleanup_abort.index("cp UNAPI_ERR_OK"),
                        cleanup_abort.index("cp UNAPI_ERR_NO_CONN"))
        self.assertLess(cleanup_abort.index("ret nz"),
                        cleanup_abort.index(
                            "ld (unapi_cleanup_connection),a"))

        restore = source.split("unapi_restore:", 1)[1].split(
            "unapi_clear_runtime:", 1)[0]
        self.assertRegex(
            restore,
            r"(?s)call unapi_abort_current\s+or a\s+"
            r"jr nz,unapi_restore_finish.*unapi_restore_clear:.*"
            r"call unapi_clear_runtime")

        lifecycle = source.split("unapi_service:", 1)[1].split(
            "; ---------------------------------------------------------- buffered bytes", 1)[0]
        self.assertIn("cp UNAPI_TCP_CLOSE_WAIT", lifecycle)
        self.assertIn("call unapi_drop_connection", lifecycle)
        self.assertIn("call unapi_open_listener", lifecycle)
        self.assertIn("ld a,UNAPI_TCP_WAIT", lifecycle)

        drop = source.split("unapi_drop_connection:", 1)[1].split(
            "unapi_service:", 1)[0]
        self.assertIn("ld (unapi_cleanup_connection),a", drop)
        self.assertIn("ld (transport_session_lost),a", drop)
        self.assertNotIn("call unapi_abort_current", drop)
        self.assertNotIn("call transport_session_reset", drop)

        relisten = source.split("unapi_service_relisten:", 1)[1].split(
            "unapi_service_have_connection:", 1)[0]
        self.assertIn("ld a,(transport_session_lost)", relisten)
        self.assertIn("ld a,(in_hook)", relisten)
        self.assertRegex(
            relisten,
            r"(?s)ld a,\(in_hook\).*ret nz.*call unapi_abort_current\s+"
            r"or a\s+jr nz,unapi_service_relisten_abort_failed\s+"
            r"call unapi_open_listener")

        flush = source.split("unapi_flush:", 1)[1].split(
            "unapi_receive_block:", 1)[0]
        self.assertIn("UNAPI_FLUSH_RETRIES", flush)
        self.assertIn("unapi_flush_retry:", flush)
        self.assertIn("call unapi_drop_connection", flush)

        buffers = source.split("unapi_runtime_start:", 1)[1]
        self.assertEqual(buffers.count("ds UNAPI_BLOCK_SIZE,0"), 2)
        self.assertIn("dw UNAPI_DEFAULT_PORT", buffers)

    def test_stream_loss_unwinds_before_relisten_and_preserves_xfer(self):
        reset = self.source.split("transport_session_reset:", 1)[1].split(
            "transport_session_finalize:", 1)[0]
        self.assertIn("ld (transport_session_lost),a", reset)
        self.assertIn("ld (xfer_fast_armed),a", reset)
        self.assertNotIn("call xfer_reset", reset)
        self.assertNotIn("xfer_descriptor", reset)
        self.assertNotIn("xfer_accepted", reset)
        self.assertNotIn("xfer_durable", reset)

        guard = self.source.split("transport_session_guard:", 1)[1].split(
            "include 'agent/transports/msx_transport_8251.inc'", 1)[0]
        self.assertIn("transport_flush_checked:", guard)
        self.assertIn("jp transport_session_abort", guard)
        self.assertIn("jp nz,xfer_fast_pump_session_lost", guard)
        self.assertIn("jp hook_transport_session_lost", guard)
        self.assertRegex(
            guard,
            r"(?s)ld sp,\(hook_dispatch_sp\).*ld a,\(vram_active\).*"
            r"call nz,restore_r14.*call transport_session_finalize.*"
            r"jp main_loop")

        hook_loss = self.source.split(
            "hook_transport_session_lost:", 1)[1].split(
                "hook_transport_timeout:", 1)[0]
        self.assertIn("ld sp,(hook_dispatch_sp)", hook_loss)
        self.assertIn("call transport_session_finalize", hook_loss)

        pump_loss = self.source.split(
            "xfer_fast_pump_session_lost:", 1)[1].split(
                "xfer_fast_pump_timeout:", 1)[0]
        self.assertLess(
            pump_loss.index("ld sp,(xfer_fast_pump_sp)"),
            pump_loss.index("call transport_session_finalize"))

        self.assertEqual(self.source.count("call transport_flush_checked"), 3)

    def test_unapi_port_and_tsr_reconfiguration_fail_closed(self):
        parser = self.source.split("loader_parse_port:", 1)[1].split(
            "loader_parse_monitor:", 1)[0]
        self.assertIn("ld (loader_unapi_port),bc", parser)
        self.assertIn("cp 0FFh", parser)
        self.assertIn("jp z,loader_parse_port_error", parser)

        talk = self.source.split("tsr_talk_config:", 1)[1].split(
            "; Validate that an entire caller range", 1)[0]
        self.assertIn("cp UNAPI_ID + 1", talk)
        self.assertIn("tsr_talk_unapi_port:", talk)
        self.assertIn("ld (tsr_config_requested_port),hl", talk)
        self.assertIn("TSR_TALK_UNAPI_PORT: equ 0A6h", self.source)
        self.assertIn("cp TSR_TALK_UNAPI_PORT", self.source)
        self.assertIn("call unapi_init_current_port", talk)
        self.assertIn("call xfer_reconfigure_detach", talk)
        self.assertIn("call xfer_reset", talk)
        rollback = talk.split("tsr_talk_config_failed:", 1)[1]
        begin = talk.split("tsr_talk_config_begin_common:", 1)[1].split(
            "tsr_talk_config_failed:", 1)[0]
        self.assertRegex(
            begin,
            r"(?s)call transport_restore\s+or a\s+"
            r"jp nz,tsr_talk_config_abort_failed.*"
            r"ld \(active_transport_id\),a")
        self.assertRegex(
            begin,
            r"(?s)ld \(tsr_config_old_unapi_live\),a.*"
            r"cp UNAPI_ID.*ld a,\(unapi_connection\).*"
            r"ld \(tsr_config_old_unapi_live\),a.*"
            r"call transport_restore")
        old_live_capture = begin.split(
            "tsr_talk_config_old_live_ready:", 1)[0]
        self.assertNotIn("unapi_cleanup_connection", old_live_capture)
        self.assertRegex(
            rollback,
            r"(?s)call transport_restore\s+or a\s+"
            r"jr nz,tsr_talk_config_failed_detach.*"
            r"ld hl,\(tsr_config_old_port\)")
        self.assertRegex(
            rollback,
            r"(?s)ld a,\(tsr_config_old_unapi_live\)\s+or a\s+"
            r"jr z,tsr_talk_config_restore_unapi_idle\s+"
            r"call unapi_init_current_port.*"
            r"tsr_talk_config_restore_unapi_idle:\s+"
            r".*call unapi_clear_runtime")
        self.assertIn("call transport_session_reset", rollback)
        self.assertIn("call xfer_reconfigure_detach", rollback)
        self.assertIn("ld a,0FFh", talk)
        self.assertIn("tsr_config_old_port", talk)
        self.assertNotIn("UNAPI_DOS_GET_ENV", self.source)
        self.assertNotIn("unapi_port_environment_name", self.source)

        detach = self.source.split("xfer_reconfigure_detach:", 1)[1].split(
            "endif", 1)[0]
        for stale_field in (
                "xfer_pending", "xfer_foreground_ready",
                "xfer_fast_page0_buffer", "xfer_fast_page0_frame",
                "xfer_fast_pump_sp", "xfer_get_ack_pending"):
            self.assertIn(stale_field, detach)
        self.assertRegex(
            detach,
            r"(?s)ld hl,xfer_durable\s+ld de,xfer_accepted\s+ld bc,4\s+ldir")
        self.assertIn(
            "ld de,xfer_descriptor + XFER_DESC_RESUME_OFFSET", detach)
        self.assertIn(
            "ld de,xfer_descriptor + XFER_DESC_PREFIX_CRC", detach)
        self.assertIn("or XFER_FLAG_RESUME", detach)
        self.assertIn("cp XFER_STATE_POSTPROCESS", detach)
        self.assertIn("ld a,XFER_STATE_FAILED", detach)

        initializer = self.source.split("tsr_init:", 1)[1].split(
            "tsr_intro_message:", 1)[0]
        self.assertIn("call resident_initialize", initializer)
        self.assertIn("jr nz,tsr_init_failed", initializer)
        self.assertIn("ld a,3", initializer)

    def test_foreground_teardown_bounds_restore_retries_before_image_release(self):
        retry = self.source.split(
            "transport_restore_foreground_retry:", 1)[1].split(
                "transport_bind:", 1)[0]
        self.assertRegex(
            retry,
            r"(?s)ld b,3\s+transport_restore_foreground_retry_loop:\s+"
            r"push bc\s+call transport_restore\s+pop bc\s+or a\s+"
            r"ret z\s+djnz transport_restore_foreground_retry_loop\s+ret")
        self.assertIn("orphan socket", self.source)

        kill = self.source.split("tsr_kill:", 1)[1].split(
            "; Foreground suite programs use TsrCall", 1)[0]
        self.assertIn("call transport_restore_foreground_retry", kill)

        monitor_exit = self.source.split("monitor_exit_to_dos:", 1)[1].split(
            "endif", 1)[0]
        self.assertIn("call transport_restore_foreground_retry", monitor_exit)

        transactional = self.source.split(
            "tsr_talk_config_begin_common:", 1)[1].split(
                "tsr_talk_done:", 1)[0]
        self.assertNotIn("transport_restore_foreground_retry", transactional)

    def test_8251_driver_programs_the_standard_baud_generator(self):
        source = GENERIC_TRANSPORT.read_text(encoding="utf-8")
        self.assertIn("UART8251_TIMER_RX:      equ 084h", source)
        self.assertIn("UART8251_TIMER_TX:      equ 085h", source)
        self.assertIn("UART8251_TIMER_CONTROL: equ 087h", source)
        self.assertIn("UART8251_TIMER_DIVISOR: equ 6", source)
        init = source.split("uart8251_init:", 1)[1].split(
            "uart8251_restore:", 1)[0]
        self.assertIn("out (UART8251_TIMER_CONTROL),a", init)
        self.assertIn("out (UART8251_TIMER_RX),a", init)
        self.assertIn("out (UART8251_TIMER_TX),a", init)
        self.assertRegex(
            init,
            r"(?s)ld a,\(UART8251_COMMSK\).*"
            r"ld \(transport_state \+ UART8251_SAVED_COMMSK\),a.*"
            r"ld a,0FFh.*ld \(UART8251_COMMSK\),a.*out \(082h\),a")
        self.assertNotIn("ld a,0FEh", init)
        self.assertLess(init.index("ld a,0FFh"), init.index("ld a,037h"))
        restore = source.split("uart8251_restore:", 1)[1].split(
            "uart8251_rx_ready:", 1)[0]
        self.assertRegex(
            restore,
            r"(?s)ld a,\(transport_state \+ UART8251_SAVED_COMMSK\).*"
            r"ld \(UART8251_COMMSK\),a.*out \(082h\),a")
        receive = source.split("uart8251_rx_ready:", 1)[1].split(
            "uart8251_tx_ready:", 1)[0]
        self.assertIn("and 038h", receive)
        self.assertIn("ld a,037h", receive)
        self.assertIn("out (UART8251_STATUS),a", receive)

    def test_16c550_driver_remains_product_neutral(self):
        source = UART16C550_TRANSPORT.read_text(encoding="utf-8")
        self.assertIn("Generic 16C550-compatible UART driver", source)
        self.assertIn("A BaDCaT SMD is one known device", source)
        self.assertIn("restores the prior UART setup", source)
        self.assertNotIn("restores the previous user", source)
        init = source.split("uart16c550_init:", 1)[1].split(
            "uart16c550_restore:", 1)[0]
        self.assertRegex(
            init,
            r"(?s)ld a,083h.*out \(UART16C550_LCR\),a.*"
            r"ld a,1\s+; 1\.8432 MHz / \(16 \* 1\) = 115200 baud.*"
            r"out \(UART16C550_DATA\),a.*"
            r"xor a.*out \(UART16C550_IER\),a")
        self.assertIn("ld a,087h", init)
        self.assertIn("ld a,02Fh", init)
        self.assertRegex(
            init,
            r"(?s)ld a,02Fh.*out \(UART16C550_MCR\),a.*"
            r"xor a.*out \(UART16C550_IER\),a.*ret")
        self.assertNotIn("received-data interrupt", init)
        restore = source.split("uart16c550_restore:", 1)[1].split(
            "uart16c550_rx_ready:", 1)[0]
        self.assertRegex(
            restore,
            r"(?s)ld a,\(transport_state \+ UART16C550_SAVED_IER\).*"
            r"out \(UART16C550_IER\),a")

    def test_default_is_true_memman_resident_and_monitor_is_explicit(self):
        self.assertRegex(
            self.source, r"(?m)^RUNTIME_RESIDENT:\s+equ\s+0$")
        self.assertIn('db "/MONITOR",0', self.source)
        self.assertIn('db "/UNINSTALL",0', self.source)
        entry = self.source.split("installer:", 1)[1].split(
            "install_check_partial:", 1)[0]
        self.assertIn("jp z,loader_install_resident", entry)
        lifecycle = self.source.split("loader_install_resident:", 1)[1].split(
            "install_banner:", 1)[0]
        self.assertIn("call memman_find_agent", lifecycle)
        self.assertIn("jp memman_loader_install", lifecycle)
        self.assertIn("jp memman_loader_uninstall", lifecycle)
        self.assertIn("jp resident_main", self.source)
        self.assertNotIn("resident_launch_shell", self.source)
        self.assertIn("include 'agent/msx_memman_loader.asm'", self.source)


if __name__ == "__main__":
    unittest.main()
