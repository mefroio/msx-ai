import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "agent" / "msx_agent_core.asm"
MEMMAN_LOADER = ROOT / "agent" / "msx_memman_loader.asm"
PORT_HELPER = ROOT / "agent" / "msx_port_helper.asm"
PUBLIC_WRAPPER = ROOT / "agent" / "msx_agent.asm"
TRACE_WRAPPER = ROOT / "agent" / "msx_agent_trace.asm"
UNAPI_TRANSPORT = ROOT / "agent" / "transports" / "msx_transport_unapi.inc"


def _equ_literal(source, name):
    match = re.search(
        rf"(?m)^{re.escape(name)}:\s+equ\s+"
        r"(0[0-9A-Fa-f]+h|[0-9]+)\b",
        source,
    )
    if match is None:
        raise AssertionError(f"missing literal equate {name}")
    token = match.group(1)
    return int(token[:-1], 16) if token.endswith("h") else int(token)


def _section(source, start, end):
    try:
        return source.split(start, 1)[1].split(end, 1)[0]
    except IndexError as exc:
        raise AssertionError(f"cannot find section {start!r} .. {end!r}") from exc


def _code_only(source):
    return "\n".join(line.split(";", 1)[0] for line in source.splitlines())


def _label_window(source, label, limit=5000):
    try:
        return source.split(label + ":", 1)[1][:limit]
    except IndexError as exc:
        raise AssertionError(f"cannot find label {label!r}") from exc


class ResidentTraceSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CORE.read_text(encoding="utf-8")
        cls.loader = MEMMAN_LOADER.read_text(encoding="utf-8")
        cls.port_helper = PORT_HELPER.read_text(encoding="utf-8")
        cls.unapi = UNAPI_TRANSPORT.read_text(encoding="utf-8")

    def test_trace_cli_is_distinct_and_compact_port_reuses_decimal_parser(self):
        self.assertIn('option_trace:\n    db "/TRACE",0', self.source)
        self.assertIn('option_dumptrace:\n    db "/DUMPTRACE",0', self.source)
        usage = _section(
            self.source, "usage_message:", "driver_required_message:")
        self.assertIn('MSXAI <1..65534> [/TRACE]', usage)
        self.assertIn('MSXAI /DUMPTRACE <file>', usage)

        dispatch = _section(
            self.source, "loader_parse_token_loop:",
            "loader_parse_implicit_unapi_port:")
        self.assertRegex(
            dispatch,
            r"(?s)ld de,option_trace\s+call loader_token_equals\s+"
            r"if MSXAI_DEVELOPMENT_TRACE\s+"
            r"jp z,loader_parse_trace\s+else\s+"
            r"jp z,loader_parse_unknown\s+endif.*"
            r"ld de,option_dumptrace\s+call loader_token_equals\s+"
            r"if MSXAI_DEVELOPMENT_TRACE\s+"
            r"jp z,loader_parse_dumptrace\s+else\s+"
            r"jp z,loader_parse_unknown\s+endif")
        self.assertIn(
            "MSXAI_DEVELOPMENT_TRACE: equ 0",
            PUBLIC_WRAPPER.read_text(encoding="utf-8"))
        self.assertIn(
            "MSXAI_DEVELOPMENT_TRACE: equ 1",
            TRACE_WRAPPER.read_text(encoding="utf-8"))
        implicit = _section(
            self.source, "loader_parse_implicit_unapi_port:",
            "loader_parse_8251:")
        self.assertIn("ld a,UNAPI_ID", implicit)
        self.assertIn("ld (loader_port_seen),a", implicit)
        self.assertIn("jp loader_parse_port_digits", implicit)

        trace = _section(
            self.source, "loader_parse_trace:", "loader_parse_dumptrace:")
        self.assertIn("ld (loader_trace_enabled),a", trace)
        validation = _section(
            self.source, "loader_parse_port_driver_ok:",
            "loader_parse_uninstall_done:")
        self.assertRegex(
            validation,
            r"(?s)ld a,\(loader_trace_enabled\).*"
            r"ld a,\(loader_runtime_mode\).*"
            r"ld a,\(loader_transport_id\)\s+cp UNAPI_ID")
        self.assertIn("/TRACE requires resident /DRIVER:UNAPI", self.source)

    def test_dumptrace_requires_one_bare_path_and_is_an_isolated_action(self):
        parser = _section(
            self.source, "loader_parse_dumptrace:",
            "loader_parse_uninstall:")
        self.assertIn("ld a,LOADER_ACTION_DUMPTRACE", parser)
        self.assertIn("ld (loader_trace_path),hl", parser)
        self.assertRegex(
            parser,
            r"(?s)loader_parse_dumptrace_path_loop:.*"
            r"cp ' '\s+jp z,loader_parse_dumptrace_error\s+"
            r"cp 9\s+jp z,loader_parse_dumptrace_error")

        validation = _section(
            self.source, "loader_parse_dumptrace_done:",
            "loader_parse_ok:")
        for state in (
                "loader_port_seen", "loader_transport_id",
                "loader_runtime_mode", "loader_debug_enabled",
                "loader_trace_enabled"):
            self.assertIn(state, validation)
        entry = _section(self.source, "installer:", "install_banner:")
        self.assertRegex(
            entry,
            r"(?s)cp LOADER_ACTION_DUMPTRACE\s+jp z,loader_dump_trace")

    def test_trace_export_layout_is_fixed_resident_and_versioned(self):
        record_size = _equ_literal(self.source, "TRACE_RECORD_SIZE")
        capacity = _equ_literal(self.source, "TRACE_RECORD_CAPACITY")
        header_size = _equ_literal(self.source, "TRACE_HEADER_SIZE")
        snapshot_size = _equ_literal(self.source, "TRACE_SNAPSHOT_SIZE")
        self.assertEqual((record_size, capacity), (8, 20))
        self.assertEqual((header_size, snapshot_size), (16, 16))
        self.assertEqual(
            header_size + snapshot_size + record_size * capacity, 192)

        layout = _section(
            self.source, "trace_export_begin:", "trace_export_end:")
        self.assertRegex(
            layout,
            r"(?s)trace_magic:\s+dw TRACE_FORMAT_MAGIC.*"
            r"trace_format_version:\s+db TRACE_FORMAT_VERSION.*"
            r"trace_record_size:\s+db TRACE_RECORD_SIZE.*"
            r"trace_record_capacity:\s+db TRACE_RECORD_CAPACITY.*"
            r"trace_failure_snapshot:\s+ds TRACE_SNAPSHOT_SIZE,0.*"
            r"trace_records:\s+ds TRACE_RECORD_SIZE \* "
            r"TRACE_RECORD_CAPACITY,0")
        self.assertLess(
            self.source.index("resident_start:"),
            self.source.index("trace_export_begin:"))
        self.assertLess(
            self.source.index("trace_export_end:"),
            self.source.index("resident_end:"))

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_assembled_resident_trace_block_is_exactly_192_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            wrapper = temporary / "trace_layout.asm"
            binary = temporary / "trace_layout.bin"
            wrapper.write_text(
                "MSXAI_TSR_BUILD: equ 1\n"
                "MSXAI_DEVELOPMENT_TRACE: equ 1\n"
                "TRANSPORT_STATE_SIZE: equ 5\n"
                "TSR_BUILD_BASE: equ 04024h\n"
                "include 'agent/msx_agent_core.asm'\n",
                encoding="ascii",
            )
            process = subprocess.run(
                [shutil.which("z80asm"), "-L", str(wrapper),
                 "-o", str(binary)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                process.returncode, 0,
                process.stderr or process.stdout)
            labels = {
                name: int(value, 16)
                for name, value in re.findall(
                    r"(?m)^([A-Za-z_][A-Za-z0-9_]*):\s+equ\s+"
                    r"\$([0-9A-Fa-f]+)\s*$",
                    process.stdout + "\n" + process.stderr,
                )
            }
            begin = labels["trace_export_begin"]
            self.assertEqual(labels["trace_failure_snapshot"] - begin, 16)
            self.assertEqual(labels["trace_records"] - begin, 32)
            self.assertEqual(labels["trace_export_end"] - begin, 192)
            self.assertLessEqual(
                labels["trace_export_end"], labels["resident_end"])

    def test_trace_enable_is_idempotent_and_ring_preserves_first_failure(self):
        reset = _section(self.source, "trace_reset:", "trace_enable:")
        for last_value in ("trace_last_tcp_result", "trace_last_tcp_state"):
            self.assertIn(last_value, reset)
        self.assertRegex(reset, r"(?s)(?:ld a,0FFh|xor a.*dec a)")

        enable = _section(
            self.source, "trace_enable:", "trace_record_tcp_state:")
        self.assertRegex(
            enable,
            r"(?s)ld a,\(trace_flags\)\s+and TRACE_FLAG_ENABLED\s+"
            r"(?:ret nz|jr nz,trace_enable_done)")
        self.assertIn("call trace_reset", enable)
        self.assertIn("TRACE_EVENT_ENABLE", enable)

        state = _section(
            self.source, "trace_record_tcp_state:", "trace_record:")
        self.assertGreaterEqual(state.count("trace_last_tcp_state"), 2)
        self.assertGreaterEqual(state.count("trace_last_tcp_result"), 2)
        self.assertRegex(
            state, r"(?:ret z|jr z,trace_record_tcp_state_done)")

        record = _section(
            self.source, "trace_record:", "trace_freeze_first_failure:")
        self.assertIn("TRACE_RECORD_CAPACITY", record)
        self.assertIn("trace_write_index", record)
        self.assertIn("trace_record_count", record)
        self.assertIn("TRACE_FLAG_WRAPPED", record)
        self.assertRegex(
            record,
            r"(?s)cp TRACE_EVENT_DROP\s+jr z,trace_record_freeze\s+"
            r"cp TRACE_EVENT_STATE_ERROR\s+jr z,trace_record_freeze")
        self.assertNotIn("ei", _code_only(record).lower())

        freeze = _section(
            self.source, "trace_freeze_first_failure:",
            "; ------------------------------------------------------------ BIOS hooks")
        self.assertRegex(
            freeze,
            r"(?s)ld a,\(trace_flags\)\s+and TRACE_FLAG_INCIDENT\s+"
            r"(?:ret nz|jr nz,trace_freeze_first_failure_done)")
        self.assertIn("trace_failure_snapshot", freeze)
        self.assertIn("trace_failure_snapshot + TRACE_RECORD_SIZE", freeze)
        self.assertIn("TRACE_FLAG_INCIDENT", freeze)

    def test_trace_covers_tcp_lifecycle_and_safe_foreground_contexts(self):
        combined = self.source + "\n" + self.unapi
        events = (
            "TRACE_EVENT_STATE",
            "TRACE_EVENT_STATE_ERROR",
            "TRACE_EVENT_DROP",
            "TRACE_EVENT_DOS_RELISTEN",
            "TRACE_EVENT_BASIC_RELISTEN",
            "TRACE_EVENT_OPEN_BEGIN",
            "TRACE_EVENT_OPEN_END",
            "TRACE_EVENT_ABORT_BEGIN",
            "TRACE_EVENT_ABORT_END",
            "TRACE_EVENT_SYSTEM_SUSPEND",
            "TRACE_EVENT_SYSTEM_RESUME",
            "TRACE_EVENT_RECONFIG_BEGIN",
            "TRACE_EVENT_RECONFIG_END",
            "TRACE_EVENT_AUTO_RELISTEN",
        )
        for event in events:
            with self.subTest(event=event):
                # One occurrence defines the event; another must record it.
                self.assertGreaterEqual(
                    len(re.findall(rf"\b{event}\b", combined)), 2)

        self.assertIn("call trace_record_tcp_state", self.unapi)
        receive_error = _section(
            self.unapi, "unapi_receive_error:",
            "; ------------------------------------------------------------ private state")
        self.assertIn("call trace_record_tcp_state", receive_error)
        drop = _section(
            self.unapi, "unapi_drop_connection:", "unapi_service:")
        self.assertLess(
            drop.index("ld (unapi_relisten_pending),a"),
            drop.index("TRACE_EVENT_DROP"))
        for lifecycle in (
                "unapi_open_listener:", "unapi_abort_current:",
                "unapi_drop_connection:", "unapi_relisten_checkpoint:"):
            with self.subTest(lifecycle=lifecycle):
                self.assertIn(lifecycle, self.unapi)
                tail = self.unapi.split(lifecycle, 1)[1]
                self.assertIn("TRACE_EVENT_", tail[:2500])

        self.assertEqual(
            _equ_literal(self.source, "TRACE_EVENT_AUTO_RELISTEN"), 15)
        auto = _section(
            self.unapi, "unapi_service_relisten:",
            "unapi_service_relisten_checkpoint:")
        self.assertRegex(
            auto,
            r"(?s)ld a,TRACE_EVENT_AUTO_RELISTEN\s+"
            r"call trace_record")

        event_formatter = _section(
            self.loader, "trace_line_append_event_name:",
            "trace_line_reset:")
        self.assertIn("cp TRACE_EVENT_AUTO_RELISTEN", event_formatter)
        event_names = _section(
            self.loader, "trace_event_name_table:",
            "trace_event_unknown:")
        self.assertIn("dw trace_event_auto_relisten", event_names)
        self.assertIn(
            'trace_event_auto_relisten:   db "AUTO_RELISTEN",0',
            event_names)

    def test_a7_v2_divisor_and_a8_enable_precede_reconfiguration(self):
        self.assertEqual(
            _equ_literal(self.source, "TSR_UNAPI_REQUEST_VERSION"), 2)
        self.assertEqual(
            _equ_literal(
                self.source, "TSR_UNAPI_REQUEST_16C550_DIVISOR"), 15)
        self.assertEqual(_equ_literal(self.source, "TSR_TALK_TRACE"), 0xA8)
        for source, divisor in (
                (self.loader, "memman_unapi_request_16c550_divisor"),
                (self.port_helper, "unapi_request_16c550_divisor")):
            self.assertRegex(source, rf"{divisor}:\s+db 0\b")

        dispatch = _section(
            self.source, "tsr_talk:", "tsr_talk_config:")
        self.assertRegex(
            dispatch,
            r"(?s)cp TSR_TALK_TRACE\s+"
            r"if MSXAI_DEVELOPMENT_TRACE\s+"
            r"jp z,tsr_talk_trace\s+else\s+"
            r"jp z,tsr_talk_unsupported\s+endif")
        talk = _label_window(self.source, "tsr_talk_trace")
        self.assertIn("TSR_TRACE_ACTION_ENABLE", talk)
        self.assertIn("TSR_TRACE_ACTION_SNAPSHOT", talk)
        self.assertIn("call tsr_talk_page0_range", talk)
        self.assertIn("TRACE_EXPORT_SIZE", talk)
        self.assertNotRegex(
            _code_only(talk), r"(?i)\b(?:0*5h|bdos_proxy|DOS_)\b")

        existing = _section(
            self.source, "loader_install_resident:",
            "loader_install_resident_new:")
        self.assertLess(
            existing.index("memman_enable_trace"),
            existing.index("memman_reconfigure_agent"))
        helper_entry = _section(
            self.port_helper, "port_helper_start:",
            "port_helper_bad_version:")
        self.assertLess(
            helper_entry.index("MSXAI_TALK_TRACE"),
            helper_entry.index("MSXAI_TALK_UNAPI_PORT"))

    def test_compact_first_install_trace_marker_does_not_grow_the_tail(self):
        builder = _section(
            self.loader, "suite_build_install_command:",
            "suite_build_install_command_length:")
        self.assertIn("loader_trace_enabled", builder)
        self.assertIn("'G'", builder)
        self.assertIn("'V'", self.port_helper)
        decoder = _section(
            self.port_helper, "parse_port_hex_begin:",
            "parse_port_hex_complete:")
        self.assertIn("trace", decoder.lower())
        self.assertIn("'G'", decoder)
        self.assertIn("'V'", decoder)
        self.assertIn("cp 4", decoder)
        # The private COMMAND2 handoff remains exactly four encoded port bytes.
        self.assertRegex(
            self.loader,
            r"install_command_buffer:\s+ds .*install_port_helper_prefix_length"
            r" \+ 5 \+ 1,0")

    def test_dos_file_io_is_transient_and_never_runs_from_hooks(self):
        transient_core = self.source.split("resident_source:", 1)[0]
        transient = transient_core + "\n" + self.loader
        self.assertIn("loader_dump_trace:", transient)
        dump = _label_window(transient, "loader_dump_trace", limit=10000)
        for operation in ("DOS_CREATE", "DOS_WRITE", "DOS_CLOSE"):
            self.assertIn(operation, dump)
        self.assertIn("call memman_snapshot_trace", dump)
        self.assertIn("ld b,CREATE_NEW", dump)
        self.assertNotIn("trace_reset", dump)

        snapshot = _section(
            self.loader, "memman_snapshot_trace:",
            "memman_prepare_unapi_request:")
        self.assertIn("TSR_TRACE_ACTION_SNAPSHOT", snapshot)
        self.assertIn("TSR_TALK_TRACE", snapshot)

        hooks = _section(
            self.source,
            "; ------------------------------------------------------------ BIOS hooks",
            "debug_trace_command:")
        hook_code = _code_only(hooks)
        self.assertNotRegex(
            hook_code,
            r"(?i)\b(?:0*5h|bdos_proxy)\b")
        self.assertNotRegex(hook_code, r"(?im)^\s*ld\s+c\s*,\s*DOS_")
        self.assertNotIn("loader_dump_trace", hook_code)

        trace_code = _section(
            self.source, "trace_reset:",
            "; TsrKill calls this only after MemMan has detached")
        self.assertNotRegex(
            _code_only(trace_code),
            r"(?im)^\s*(?:call|jp)\s+(?:0*5h|bdos_proxy)\b")


if __name__ == "__main__":
    unittest.main()
