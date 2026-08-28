"""Deterministic tests plus one explicitly opt-in openMSXnet E2E."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import openmsx_unapi_validation as harness  # noqa: E402


class OpenMSXUNAPIHarnessUnitTests(unittest.TestCase):
    class _ScreenMachine:
        def __init__(self, screens):
            self.screens = list(screens)
            self.index = 0

        def screen_text(self):
            return self.screens[self.index]

        def advance(self, _seconds):
            if self.index + 1 < len(self.screens):
                self.index += 1

    @staticmethod
    def _resident_trace_fixture() -> str:
        return "\r\n".join((
            "MSXAI TRACE V1",
            "FLAGS=03 COUNT=08 NEXT=08 SEQ=0008",
            "POLLS=0010 CHANGES=0002 TIMI=0100",
            "FIRST DROP E=00 S=00 C=00 X=01 F=2D T=0105 "
            "EXTRA=1E00000000030202",
            "#0001 ENABLE E=00 S=00 C=00 X=00 F=00 T=0001",
            "#0002 OPEN_BEGIN E=00 S=00 C=00 X=00 F=02 T=0002",
            "#0003 OPEN_END E=00 S=01 C=01 X=00 F=00 T=0003",
            "#0004 STATE E=00 S=01 C=01 X=00 F=28 T=0004",
            "#0005 STATE E=00 S=04 C=01 X=00 F=28 T=0005",
            "#0006 DROP E=00 S=00 C=00 X=01 F=2D T=0006",
            "#0007 SYSTEM_SUSPEND E=00 S=00 C=00 X=01 F=39 T=0007",
            "#0008 SYSTEM_RESUME E=00 S=00 C=00 X=01 F=39 T=0008",
            "",
        ))

    @staticmethod
    def _wrapped_resident_trace_fixture(*, next_index: int = 2) -> str:
        sequences = [
            (0xFFF3 + index) & 0xFFFF for index in range(16)
        ]
        records = [
            f"#{sequence:04X} "
            f"{'OPEN_BEGIN' if index % 2 == 0 else 'OPEN_END'} "
            "E=00 S=01 C=01 X=00 F=00 T=0200"
            for index, sequence in enumerate(sequences)
        ]
        return "\n".join((
            "MSXAI TRACE V1",
            f"FLAGS=07 COUNT=10 NEXT={next_index:02X} SEQ=0002",
            "POLLS=FFFF CHANGES=0010 TIMI=FFFF",
            "FIRST DROP E=00 S=00 C=00 X=01 F=2D T=0105 "
            "EXTRA=1E00000000030202",
            *records,
            "",
        ))

    def test_port_default_is_custom_and_ffff_is_never_allowed(self):
        self.assertEqual(harness.DEFAULT_TEST_PORT, 43123)
        self.assertEqual(harness.validate_port("43123"), 43123)
        self.assertEqual(harness.validate_port(1), 1)
        self.assertEqual(harness.validate_port(65534), 65534)
        for invalid in (0, -1, 65535, 65536):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    harness.validate_port(invalid)
        with self.assertRaises(TypeError):
            harness.validate_port(True)
        with self.assertRaises(TypeError):
            harness.validate_port(1.5)
        with self.assertRaises(ValueError):
            harness.validate_port("not-a-port")

    def test_exact_platform_assets_are_pinned(self):
        self.assertEqual(
            harness.asset_name_for("darwin", "arm64"),
            "openmsx-macos-arm64.zip")
        self.assertEqual(
            harness.asset_name_for("linux", "x86_64"),
            "openmsx-linux-x86_64.zip")
        self.assertEqual(
            harness.asset_name_for("win32", "AMD64"),
            "openmsx-windows-x86_64.zip")
        with self.assertRaises(harness.PrerequisiteError):
            harness.asset_name_for("darwin", "x86_64")
        self.assertEqual(set(harness.ASSET_SHA256), {
            "openmsx-macos-arm64.zip",
            "openmsx-linux-x86_64.zip",
            "openmsx-windows-x86_64.zip",
        })
        self.assertEqual(harness.RELEASE, "v0.9.7")

    def test_verify_hash_accepts_exact_bytes_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "asset.bin"
            path.write_bytes(b"pinned bytes")
            digest = hashlib.sha256(b"pinned bytes").hexdigest()
            self.assertEqual(
                harness.verify_hash(path, digest, "fixture"), digest)
            path.write_bytes(b"changed bytes")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "SHA-256 mismatch"):
                harness.verify_hash(path, digest, "fixture")

    def test_safe_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escaped", b"bad")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "unsafe path"):
                harness.safe_extract_zip(archive, root / "out")
            self.assertFalse((root / "escaped").exists())

    def test_safe_extract_rejects_windows_separator_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "bad-windows.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(r"..\escaped", b"bad")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "unsafe path"):
                harness.safe_extract_zip(archive, root / "out")

    def test_safe_extract_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "link.zip"
            link = zipfile.ZipInfo("share/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(link, "../../outside")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "Symbolic link|symbolic link"):
                harness.safe_extract_zip(archive, root / "out")

    def test_install_commands_use_unapi_custom_port(self):
        self.assertEqual(harness.AGENT_PACKAGE_NAMES, (
            "MSXAI.COM", "MSXAIXF.COM", "MCP8251.TSR", "MCP16550.TSR",
            "MCPUNAPI.TSR", "TU.COM", "MP.COM", "MEMMAN.COM", "TL.COM",
            "TK.COM",
        ))
        self.assertEqual(harness.msx_install_commands(43123), (
            r"SET MSXAI_HOME=A:\MSXAI",
            r"PATH A:\MSXAI;%PATH%",
            "UNAPINET",
            "MSXAI /DRIVER:UNAPI /PORT:43123",
        ))
        with self.assertRaises(ValueError):
            harness.msx_install_commands(65535)

    def test_trace_install_uses_compact_resident_cli_without_changing_setup(self):
        normal = harness.msx_install_commands(43123)
        traced = harness.msx_install_commands(43123, trace=True)
        self.assertEqual(traced[:3], normal[:3])
        self.assertEqual(traced[3], "MSXAI 43123 /TRACE")
        self.assertEqual(
            normal[3], "MSXAI /DRIVER:UNAPI /PORT:43123")
        with self.assertRaises(ValueError):
            harness.msx_install_commands(65535, trace=True)

    def test_trace_enable_restores_interrupts_before_a7_configuration(self):
        core = (ROOT / "agent" / "msx_agent_core.asm").read_text(
            encoding="utf-8")
        existing_install = core.split(
            "loader_install_resident:", 1)[1].split(
                "loader_install_resident_new:", 1)[0]
        self.assertRegex(
            existing_install,
            r"(?s)call memman_enable_trace\s+or a\s+"
            r"jr nz,loader_resident_trace_error\s+.*?\bei\s+"
            r"loader_install_resident_reconfigure:\s+.*?"
            r"call memman_reconfigure_agent",
        )

        helper = (ROOT / "agent" / "msx_port_helper.asm").read_text(
            encoding="utf-8")
        trace_handoff = helper.split(
            "port_helper_trace_ready:", 1)[1].split(
                "call EXTBIO", 1)[0]
        self.assertRegex(
            trace_handoff,
            r"(?s)^.*?\bei\s+call prepare_unapi_request\s+"
            r"jr c,port_helper_reconfigure_error_ei\s+.*?"
            r"ld a,MSXAI_TALK_UNAPI_PORT",
        )

    def test_trace_a8_rejects_low_page_zero_request_pointer(self):
        core = (ROOT / "agent" / "msx_agent_core.asm").read_text(
            encoding="utf-8")
        trace_talk = core.split("tsr_talk_trace:", 1)[1].split(
            "tsr_talk_config:", 1)[0]
        self.assertRegex(
            trace_talk,
            r"(?s)ld a,\(in_hook\)\s+or a\s+"
            r"jp nz,tsr_talk_unsupported\s+ld a,h\s+or a\s+"
            r"jp z,tsr_talk_unsupported\s+ld bc,TSR_TRACE_REQUEST_SIZE\s+"
            r"call tsr_talk_page0_range",
        )

    def test_trace_snapshot_bounds_count_and_next_before_formatting(self):
        loader = (ROOT / "agent" / "msx_memman_loader.asm").read_text(
            encoding="utf-8")
        snapshot_validation = loader.split(
            "memman_trace_call:", 1)[1].split(
                "memman_trace_call_ok:", 1)[0]
        self.assertRegex(
            snapshot_validation,
            r"(?s)ld a,\(memman_trace_export \+ TRACE_EXPORT_COUNT\)\s+"
            r"cp TRACE_RECORD_CAPACITY \+ 1\s+"
            r"jr nc,memman_trace_call_failed\s+"
            r"ld a,\(memman_trace_export \+ TRACE_EXPORT_WRITE_INDEX\)\s+"
            r"cp TRACE_RECORD_CAPACITY\s+"
            r"jr nc,memman_trace_call_failed",
        )

    def test_resident_trace_parser_decodes_crlf_records_and_snapshot(self):
        trace = harness.parse_resident_trace(self._resident_trace_fixture())
        self.assertEqual(trace["flags"], 0x03)
        self.assertEqual(trace["count"], 8)
        self.assertEqual(trace["next_index"], 8)
        self.assertEqual(trace["sequence"], 8)
        self.assertEqual(trace["polls"], 0x10)
        self.assertEqual(trace["state_changes"], 2)
        self.assertEqual(trace["timi"], 0x100)
        self.assertEqual(trace["first_incident"], {
            "event": "DROP",
            "error": 0,
            "state": 0,
            "active": 0,
            "cleanup": 1,
            "flags": 0x2D,
            "jiffy": 0x0105,
            "extra": [0x1E, 0, 0, 0, 0, 3, 2, 2],
        })
        self.assertEqual(len(trace["records"]), 8)
        self.assertEqual(
            trace["events"],
            ["ENABLE", "OPEN_BEGIN", "OPEN_END", "STATE", "STATE",
             "DROP", "SYSTEM_SUSPEND", "SYSTEM_RESUME"])
        harness.validate_resident_trace(trace)

    def test_resident_trace_parser_rejects_truncated_or_malformed_text(self):
        valid = self._resident_trace_fixture()
        malformed = (
            "",
            "MSXAI TRACE V1\nFLAGS=03",
            valid.replace("MSXAI TRACE V1", "MSXAI TRACE V2"),
            valid.replace("FLAGS=03", "FLAGS=0g"),
            valid.replace("POLLS=0010", "POLLS=010"),
            valid.replace("FIRST DROP", "FIRST drop"),
            valid.replace("#0008 SYSTEM_RESUME", ""),
            valid.replace("COUNT=08", "COUNT=08\n"),
        )
        for text in malformed:
            with self.subTest(text=text[:40]):
                with self.assertRaises(harness.ValidationError):
                    harness.parse_resident_trace(text)

    def test_resident_trace_validator_accepts_wrapped_sequence_rollover(self):
        trace = harness.parse_resident_trace(
            self._wrapped_resident_trace_fixture())
        self.assertEqual(
            [record["sequence"] for record in trace["records"]],
            list(range(0xFFF3, 0x10000)) + [0, 1, 2])
        harness.validate_resident_trace(trace)

    def test_resident_trace_validator_rejects_ring_and_incident_corruption(self):
        valid = self._resident_trace_fixture()
        corruptions = (
            ("unknown flags", valid.replace("FLAGS=03", "FLAGS=0B")),
            ("not enabled", valid.replace("FLAGS=03", "FLAGS=02")),
            ("count mismatch", valid.replace("COUNT=08", "COUNT=09")),
            ("invalid next", valid.replace("NEXT=08", "NEXT=10")),
            ("inconsistent next", valid.replace("NEXT=08", "NEXT=07")),
            ("wrapped partial", valid.replace("FLAGS=03", "FLAGS=07")),
            ("incident mismatch", valid.replace("FLAGS=03", "FLAGS=01")),
            ("non-incident first", valid.replace("FIRST DROP", "FIRST STATE")),
            ("bad chronology", valid.replace("#0003", "#0004", 1)),
            ("duplicate state", valid.replace(
                "#0005 STATE E=00 S=04", "#0005 STATE E=00 S=01")),
        )
        for label, text in corruptions:
            with self.subTest(label=label):
                trace = harness.parse_resident_trace(text)
                with self.assertRaises(harness.ValidationError):
                    harness.validate_resident_trace(trace)

    def test_resident_trace_validator_rejects_full_ring_next_mismatch(self):
        trace = harness.parse_resident_trace(
            self._wrapped_resident_trace_fixture(next_index=3))
        with self.assertRaisesRegex(
                harness.ValidationError, "NEXT|next"):
            harness.validate_resident_trace(trace)

    def test_resident_trace_validator_accepts_state_error_as_first_incident(self):
        trace = harness.parse_resident_trace(
            self._resident_trace_fixture().replace(
                "FIRST DROP E=00", "FIRST STATE_ERROR E=08"))
        harness.validate_resident_trace(trace)

    def test_resident_trace_validator_rejects_unknown_event(self):
        trace = harness.parse_resident_trace(
            self._resident_trace_fixture().replace(
                "#0002 OPEN_BEGIN", "#0002 MYSTERY"))
        with self.assertRaisesRegex(
                harness.ValidationError, "event|MYSTERY"):
            harness.validate_resident_trace(trace)

    def test_trace_subcommand_dispatches_without_opening_emulator(self):
        namespace = harness.build_parser().parse_args(["trace"])
        self.assertEqual(namespace.command, "trace")
        self.assertFalse(hasattr(namespace, "cycles"))

        settings = object()
        output = io.StringIO()
        with (mock.patch.object(
                  harness, "settings_from_namespace", return_value=settings),
              mock.patch.object(
                  harness, "run_validation",
                  return_value={"ok": True, "resident_trace_validation": True}
              ) as run,
              contextlib.redirect_stdout(output)):
            self.assertEqual(harness.main(["trace"]), 0)
        run.assert_called_once_with(settings, trace_validation=True)
        self.assertTrue(json.loads(output.getvalue())["resident_trace_validation"])

    def test_trace_mode_rejects_conflicting_options_before_prerequisites(self):
        settings = mock.Mock(spec=harness.Settings)
        settings.keep_open = False
        with self.assertRaisesRegex(ValueError, "separate"):
            harness.run_validation(
                settings, fault_cycles=1, trace_validation=True)
        with self.assertRaises(TypeError):
            harness.run_validation(settings, trace_validation=1)
        settings.keep_open = True
        with self.assertRaisesRegex(ValueError, "keep_open"):
            harness.run_validation(settings, trace_validation=True)

    def test_fault_parser_defaults_to_three_cycles(self):
        namespace = harness.build_parser().parse_args(["faults"])
        self.assertEqual(namespace.command, "faults")
        self.assertEqual(namespace.cycles, harness.DEFAULT_FAULT_CYCLES)
        namespace = harness.build_parser().parse_args(
            ["faults", "--cycles", "7"])
        self.assertEqual(namespace.cycles, 7)

    def test_fault_run_rejects_invalid_cycle_count_before_prerequisites(self):
        settings = mock.Mock(spec=harness.Settings)
        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    harness.run_validation(settings, fault_cycles=invalid)

    def test_fresh_command_boundary_does_not_accept_stale_prompt(self):
        before = "A:\\>"
        machine = self._ScreenMachine([
            before,
            before,
            "A:\\>ECHO UNIQUE\nUNIQUE\nA:\\>",
        ])
        screen = harness._wait_dos_command_completion(
            machine, "ECHO UNIQUE", before, timeout=0.2)
        self.assertIn("ECHO UNIQUE", screen)
        self.assertEqual(machine.index, 2)

        stale = self._ScreenMachine([before])
        with self.assertRaisesRegex(
                harness.ValidationError, "fresh command/prompt"):
            harness._wait_dos_command_completion(
                stale, "ECHO NEVER", before, timeout=0.001)

    def test_unapi_trace_phase_rejects_tcl_metacharacters(self):
        machine = mock.Mock()
        harness._set_unapi_trace_phase(machine, "cycle-1.fin")
        machine.cmd.assert_called_once_with(
            "set msxaiunapitrace::phase cycle-1.fin")
        with self.assertRaises(ValueError):
            harness._set_unapi_trace_phase(machine, "bad;set power off")

    def test_contract_names_explain_exact_coverage_and_limit(self):
        contract = "\n".join(harness.CONTRACT_PATH)
        for required in (
                "discovery", "GET_CAPAB", "TCP_OPEN", "TCP_STATE",
                "TCP_SEND", "TCP_RCV", "relisten", "public MCP"):
            self.assertIn(required.lower(), contract.lower())
        self.assertEqual(harness.MCP_TOOLS_EXERCISED, (
            "msx_agent_connect",
            "msx_agent_status",
            "msx_agent_memory_read",
            "msx_agent_disconnect",
        ))
        not_emulated = "\n".join(harness.NOT_EMULATED).lower()
        self.assertIn("firmware", not_emulated)
        self.assertIn("bus timing", not_emulated)
        self.assertTrue(
            harness.PINNED_GET_CAPAB_BLOCK1_HL &
            harness.PASSIVE_UNSPECIFIED_REMOTE_BIT)

    def test_e2e_uses_public_mcp_instead_of_direct_realmsx(self):
        source = pathlib.Path(harness.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from msx_real import RealMSX", source)
        self.assertIn('"-m", "server", "--transport", "stdio"', source)
        for tool_name in harness.MCP_TOOLS_EXERCISED:
            self.assertIn(tool_name, source)

        reconnect = source.split(
            '"public-MCP disconnect and automatic foreground relisten"',
            1,
        )[1].split("return {", 1)[0]
        dos_reconnect, basic_reconnect = reconnect.split(
            '"enter BASIC while second MCP session remains live"', 1)
        type_lines = [
            line.strip() for line in dos_reconnect.splitlines()
            if "machine.type_line(" in line
        ]
        self.assertEqual(type_lines, ['machine.type_line("ER")'])
        self.assertIn('machine.type("V")', reconnect)
        self.assertEqual(source.count("machine.type_line(commands[3])"), 1)
        self.assertEqual(dos_reconnect.count("await _mcp_connect("), 3)
        self.assertEqual(dos_reconnect.count("except ValidationError:"), 2)
        self.assertIn("h_timi_lifecycle_deferred = True", dos_reconnect)
        self.assertIn(
            "bare_chget_lifecycle_deferred = True", dos_reconnect)
        self.assertLess(
            dos_reconnect.index("await _mcp_connect("),
            dos_reconnect.index('machine.type("V")'),
        )
        self.assertLess(
            dos_reconnect.index('machine.type("V")'),
            dos_reconnect.index('machine.type_line("ER")'),
        )
        self.assertLess(
            dos_reconnect.index('machine.type_line("ER")'),
            dos_reconnect.rindex("await _mcp_connect("),
        )
        self.assertLess(
            dos_reconnect.index("await _mcp_connect("),
            dos_reconnect.index("except ValidationError:"),
        )
        self.assertIn('machine.type_line("BASIC")', basic_reconnect)
        self.assertIn('machine.type_line("PRINT 1")', basic_reconnect)
        self.assertIn("BASIC did not finish PRINT 1", basic_reconnect)
        self.assertEqual(basic_reconnect.count("await _mcp_connect("), 2)
        self.assertIn(
            "basic_idle_lifecycle_deferred = True", basic_reconnect)

    def test_screen_check_accepts_passive_banner_and_rejects_real_errors(self):
        harness._assert_command_screen(
            "MSXAI /DRIVER:UNAPI /PORT:43123",
            "Driver: TCP/IP UNAPI passive listener\n  A:\\>",
            prompt=True,
        )
        with self.assertRaisesRegex(
                harness.ValidationError, "Transport initialization failed"):
            harness._assert_command_screen(
                "MSXAI /DRIVER:UNAPI /PORT:43123",
                "Transport initialization failed\nA:\\>",
                prompt=True,
            )
        with self.assertRaisesRegex(
                harness.ValidationError, "UnapiNet extension not found"):
            harness._assert_command_screen(
                "UNAPINET",
                "ERROR: openMSX UnapiNet extension not found.\nA:\\>",
                prompt=True,
            )
        with self.assertRaisesRegex(
                harness.ValidationError, "transport initialization failed"):
            harness._assert_command_screen(
                "MSXAI /DRIVER:UNAPI /PORT:43123",
                "MSX-AI transport initialization faile\n"
                "d\n  A:\\>",
                prompt=True,
            )
        with self.assertRaisesRegex(
                harness.ValidationError, "msxai unapi relisten failed"):
            harness._assert_command_screen(
                "MSXAI /DRIVER:UNAPI /PORT:43123",
                "MP: MSXAI UNAPI relisten failed.\nA:\\>",
                prompt=True,
            )

    def _fake_inputs(self, root: pathlib.Path):
        archive = root / "openmsx-macos-arm64.zip"
        xml = b"<msxconfig><devices><UnapiNet/></devices></msxconfig>\n"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("openmsx", b"fake executable")
            output.writestr("share/extensions/unapinet.xml", xml)
            output.writestr(
                "share/extensions/SunriseIDE_Nextor.xml", b"<msxconfig/>\n")
            output.writestr(
                "share/extensions/slotexpander.xml", b"<msxconfig/>\n")
            output.writestr(
                "share/extensions/ram512k.xml", b"<msxconfig/>\n")
        unapinet_com = root / "UNAPINET.COM"
        unapinet_com.write_bytes(b"U" * harness.UNAPINET_COM_SIZE)
        disk = root / "msxdos.dsk"
        disk.write_bytes(b"disk")
        home = root / "home"
        (home / "share/machines").mkdir(parents=True)
        (home / "share/machines/Gradiente_Expert20.xml").write_text(
            "<msxconfig/>\n", encoding="utf-8")
        (home / "share/systemroms").mkdir(parents=True)
        nextor = home / "share/systemroms/Nextor.rom"
        nextor.write_bytes(b"licensed fixture ROM")
        settings = harness.Settings(
            archive=archive,
            unapinet_com=unapinet_com,
            dos_hdd=disk,
            openmsx_home=home,
            port=43123,
            root=ROOT,
        )
        hashes = {
            "archive": harness.sha256_file(archive),
            "com": harness.sha256_file(unapinet_com),
            "xml": hashlib.sha256(xml).hexdigest(),
            "nextor": harness.sha1_file(nextor),
        }
        return settings, hashes

    def test_preflight_is_reproducible_without_real_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, hashes = self._fake_inputs(pathlib.Path(directory))
            runner = mock.Mock(return_value=subprocess.CompletedProcess(
                args=["openmsx", "--version"], returncode=0,
                stdout="openMSX 21.0\nflavour: opt\n", stderr=""))
            with (mock.patch.dict(
                      harness.ASSET_SHA256,
                      {"openmsx-macos-arm64.zip": hashes["archive"]}),
                  mock.patch.object(
                      harness, "UNAPINET_COM_SHA256", hashes["com"]),
                  mock.patch.object(
                      harness, "UNAPINET_XML_SHA256", hashes["xml"]),
                  mock.patch.object(
                      harness, "NEXTOR_ROM_SHA1S", frozenset({hashes["nextor"]}))):
                report = harness.preflight(
                    settings,
                    platform_name="darwin",
                    architecture="arm64",
                    runner=runner,
                )

            self.assertTrue(report["ready"], report["problems"])
            self.assertEqual(report["custom_port"], 43123)
            self.assertFalse(report["pico_firmware_emulated"])
            runner.assert_called_once()
            call = runner.call_args
            self.assertEqual(call.args[0][1], "--version")
            self.assertEqual(call.kwargs["timeout"], 15)
            self.assertIn("OPENMSX_SYSTEM_DATA", call.kwargs["env"])
            self.assertIn("OPENMSX_HOME", call.kwargs["env"])
            self.assertIn(
                "openmsxnet-", call.kwargs["env"]["OPENMSX_SYSTEM_DATA"])

    def test_preflight_reports_missing_dynamic_library_actionably(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, hashes = self._fake_inputs(pathlib.Path(directory))
            runner = mock.Mock(return_value=subprocess.CompletedProcess(
                args=["openmsx", "--version"], returncode=1, stdout="",
                stderr=(
                    "dyld: Library not loaded: "
                    "/opt/homebrew/opt/libogg/lib/libogg.0.dylib")))
            with (mock.patch.dict(
                      harness.ASSET_SHA256,
                      {"openmsx-macos-arm64.zip": hashes["archive"]}),
                  mock.patch.object(
                      harness, "UNAPINET_COM_SHA256", hashes["com"]),
                  mock.patch.object(
                      harness, "UNAPINET_XML_SHA256", hashes["xml"]),
                  mock.patch.object(
                      harness, "NEXTOR_ROM_SHA1S", frozenset({hashes["nextor"]}))):
                report = harness.preflight(
                    settings,
                    platform_name="darwin",
                    architecture="arm64",
                    runner=runner,
                )
        self.assertFalse(report["ready"])
        problem = "\n".join(report["problems"])
        self.assertIn("libogg.0.dylib", problem)
        self.assertIn("runtime libraries", problem)
        self.assertIn("never installs", problem)


@unittest.skipUnless(
    os.environ.get("MSX_RUN_UNAPI_INTEGRATION") == "1",
    "set MSX_RUN_UNAPI_INTEGRATION=1 and supply pinned v0.9.7 assets",
)
class OpenMSXUNAPIIntegrationTest(unittest.TestCase):
    def test_discovery_passive_tcp_and_automatic_console_relisten(self):
        parser = harness.build_parser()
        namespace = parser.parse_args(["run"])
        try:
            settings = harness.settings_from_namespace(namespace)
        except harness.PrerequisiteError as exc:
            self.skipTest(str(exc))
        report = harness.preflight(settings)
        if not report["ready"]:
            self.skipTest("UNAPI E2E prerequisites unavailable: " +
                          "; ".join(report["problems"]))
        result = harness.run_validation(settings)
        self.assertTrue(result["ok"])
        self.assertEqual(result["custom_port"], settings.port)
        self.assertEqual(
            result["host_control_path"], "public MCP tools over STDIO")
        self.assertEqual(
            result["mcp_tools_exercised"],
            list(harness.MCP_TOOLS_EXERCISED))
        self.assertEqual(
            result["first_connection"]["agent_transport"], "tcpip-unapi")
        self.assertEqual(
            result["second_connection"]["agent_transport"], "tcpip-unapi")
        self.assertEqual(
            result["third_connection"]["agent_transport"], "tcpip-unapi")
        self.assertTrue(
            result["automatic_foreground_relisten_after_console_input"])
        self.assertTrue(result["h_timi_lifecycle_deferred"])
        self.assertTrue(result["bare_chget_lifecycle_deferred"])
        self.assertTrue(result["basic_idle_lifecycle_deferred"])
        self.assertTrue(result["automatic_basic_h_crun_relisten"])
        self.assertTrue(
            result["memory_compare"]["matched_openmsx_debugger"])
        self.assertFalse(result["pico_firmware_emulated"])


if __name__ == "__main__":
    unittest.main()
