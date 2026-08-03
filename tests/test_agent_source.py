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
        self.assertIn("UART8251_TIMER_DIVISOR: equ 24", source)
        init = source.split("uart8251_init:", 1)[1].split(
            "uart8251_restore:", 1)[0]
        self.assertIn("out (UART8251_TIMER_CONTROL),a", init)
        self.assertIn("out (UART8251_TIMER_RX),a", init)
        self.assertIn("out (UART8251_TIMER_TX),a", init)
        self.assertIn("ld a,0FEh", init)
        self.assertNotIn("and 0FEh", init)

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
