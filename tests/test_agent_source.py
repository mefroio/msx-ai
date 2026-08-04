import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "agent" / "msx_agent_core.asm"
GENERIC_WRAPPER = ROOT / "agent" / "msx_agent.asm"
GENERIC_TRANSPORT = ROOT / "agent" / "transports" / "msx_transport_8251.inc"
UART16C550_TRANSPORT = (
    ROOT / "agent" / "transports" / "msx_transport_16c550.inc")
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
        self.assertIn('db "ON",0', self.source)
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
            r"(?s)call transport_rx_ready.*jr z,hook_done.*"
            r"ld a,\(hook_kind\).*jr nz,hook_dispatch_frame.*"
            r"ld \(chain_keyi\),a.*hook_dispatch_frame:.*"
            r"call receive_dispatch")

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

    def test_memman_exclusive_keyi_never_chains_the_old_serial_handler(self):
        hooks = self.source.split(
            "; ---------------------------------------------------------------- H.KEYI", 1
        )[1].split("else", 1)[0]
        policy = hooks.split("ld (in_hook),a", 1)[1].split(
            "call transport_rx_ready", 1)[0]
        self.assertIn("and TRANSPORT_FLAG_KEYI_EXCLUSIVE", policy)
        self.assertIn("ld (chain_keyi),a", policy)
        self.assertLess(
            policy.index("and TRANSPORT_FLAG_KEYI_EXCLUSIVE"),
            policy.index("ld (chain_keyi),a"))

    def test_memman_nested_hooks_return_through_quithook_without_resaving(self):
        hooks = self.source.split(
            "; ---------------------------------------------------------------- H.KEYI", 1
        )[1].split("else", 1)[0]
        keyi_entry = hooks.split("resident_keyi_hook:", 1)[1].split(
            "resident_timi_hook:", 1)[0]
        timi_entry = hooks.split("resident_timi_hook:", 1)[1].split(
            "; A nested MemMan hook", 1)[0]
        self.assertRegex(
            keyi_entry,
            r"(?s)push af.*ld a,\(in_hook\).*or a.*"
            r"jr nz,memman_nested_keyi_return.*ld a,1.*"
            r"ld \(in_hook\),a.*ld \(hook_kind\),a")
        self.assertRegex(
            timi_entry,
            r"(?s)push af.*ld a,\(in_hook\).*or a.*"
            r"jr nz,memman_nested_timi_return.*ld a,1.*"
            r"ld \(in_hook\),a.*ld \(hook_kind\),a")

        before_full_save = hooks.split("resident_hook_saved_af:", 1)[0]
        self.assertNotIn("push bc", before_full_save)
        nested_keyi = hooks.split("memman_nested_keyi_return:", 1)[1].split(
            "memman_nested_timi_return:", 1)[0]
        self.assertNotIn("ld (hook_kind),a", nested_keyi)
        self.assertNotIn("ld (hook_dispatch_sp),sp", nested_keyi)
        self.assertRegex(
            nested_keyi,
            r"(?s)and TRANSPORT_FLAG_KEYI_EXCLUSIVE.*"
            r"pop af.*ex af,af'.*ld a,1.*ex af,af'.*ret")
        nested_timi = hooks.split("memman_nested_timi_return:", 1)[1].split(
            "resident_hook_saved_af:", 1)[0]
        self.assertNotIn("ld (hook_kind),a", nested_timi)
        self.assertNotIn("ld (hook_dispatch_sp),sp", nested_timi)
        self.assertRegex(
            nested_timi,
            r"(?s)pop af.*ex af,af'.*xor a.*ex af,af'.*ret")

    def test_hook_stack_comment_matches_reserved_bytes(self):
        reserve = re.search(
            r"(?m)^STACK_RESERVE:\s+equ\s+([0-9A-Fa-f]+)h", self.source)
        self.assertIsNotNone(reserve)
        self.assertEqual(int(reserve.group(1), 16), 224)
        stack_comment = self.source.split("; The hook stack", 1)[1].split(
            "STACK_RESERVE:", 1)[0]
        self.assertIn("224 bytes", stack_comment)
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
        self.assertIn("FEATURE_FRAME_WAKE_ACK", features)
        magic = self.source.split("frame_have_magic_m:", 1)[1].split(
            "frame_reconnect_byte:", 1)[0]
        self.assertRegex(
            magic,
            r"(?s)ld a,FRAME_WAKE_ACK.*call ser_put.*call ser_get")

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
        self.assertIn('db "/DRIVER:8251",0', self.source)
        self.assertIn('db "/DRIVER:16C550",0', self.source)
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
                self.assertIn(f"{prefix}_FLAGS:", source)
                self.assertIn(f"{prefix}_CONTROL_LEVEL:", source)
                label_prefix = prefix.lower().replace("uart", "uart")
                for operation in ("init", "restore", "rx_ready", "tx_ready",
                                  "read", "write"):
                    self.assertIn(f"{label_prefix}_{operation}:", source)

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
        self.assertIn("ld a,0FEh", init)
        self.assertNotIn("and 0FEh", init)
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
