import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import msx_mcp_server  # noqa: E402
from execution import bind_execution_hooks  # noqa: E402


class FakeRealMSX:
    def __init__(self):
        self.ram = bytearray(0x10000)
        self.vram = bytearray(0x20000)
        self.calls = []
        self.stops = 0
        self.hardware = []
        self.typed = []
        self.put_payloads = []
        self.transfers = []
        self.feature_bits = msx_mcp_server.FEATURE_FILE_TRANSFER
        self.screen = "MSX-DOS 2\nA:\\>"

    def stop(self):
        self.stops += 1
        return "monitor"

    def poke(self, address, data):
        self.ram[address:address + len(data)] = data
        return len(data)

    def peek(self, address, length):
        return bytes(self.ram[address:address + length])

    def vpoke(self, address, data):
        self.vram[address:address + len(data)] = data
        return len(data)

    def vpeek(self, address, length):
        return bytes(self.vram[address:address + length])

    def call(self, address):
        self.calls.append(("call", address))

    def run(self, address):
        self.calls.append(("run", address))

    def io_read(self, port):
        self.hardware.append(("io_read", port))
        return 0xA5

    def io_write(self, port, value, *, verify=False):
        self.hardware.append(("io_write", port, value, verify))

    def slot_select(self, page, slot_id):
        self.hardware.append(("slot", page, slot_id))

    def mapper_select(self, page, segment):
        self.hardware.append(("mapper", page, segment))

    def type(self, text):
        self.typed.append(("type", text))
        return len(text.encode("ascii"))

    def type_line(self, text):
        self.typed.append(("line", text))
        if text == "BASIC":
            self.screen = "Microsoft MSX BASIC\nOk"
        return len(text.encode("ascii")) + 1

    def type_lines(self, lines):
        lines = tuple(lines)
        self.typed.append(("lines", lines))
        return sum(len(line.encode("ascii")) + 1 for line in lines)

    def put_file(self, source, target, **options):
        source = Path(source)
        self.transfers.append(("put", source, target, options))
        # BASIC uploads use a short-lived host file. Capture its bytes here so
        # assertions remain valid after the temporary directory is removed.
        self.put_payloads.append((target, source.read_bytes(), dict(options)))
        self.screen = "MSXAI PUT OK\nA:\\>"
        return {"direction": "put", "wire_bytes": source.stat().st_size,
                "target": target, "encoding": "raw"}

    def get_file(self, source, target, **options):
        self.transfers.append(("get", source, Path(target), options))
        self.screen = "MSXAI GET OK\nA:\\>"
        return {"direction": "get", "wire_bytes": 123,
                "target": str(target), "encoding": "raw"}

    def press(self, key):
        self.typed.append(("key", key))

    def screen_text(self, timeout=None):
        return self.screen


class MCPApplicationToolsTest(unittest.TestCase):
    def setUp(self):
        self.previous = (msx_mcp_server.SESSION.msx,
                         msx_mcp_server.SESSION.profile)
        self.backend = FakeRealMSX()
        msx_mcp_server.SESSION.msx = self.backend
        msx_mcp_server.SESSION.profile = "real"

    def tearDown(self):
        (msx_mcp_server.SESSION.msx,
         msx_mcp_server.SESSION.profile) = self.previous

    def test_app_load_uses_interface_neutral_loader_on_real_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.com"
            path.write_bytes(b"\x00\xC9")
            result = msx_mcp_server.t_app_load(
                str(path), execute="call", verify=True)

        self.assertEqual(self.backend.ram[0x100:0x102], b"\x00\xC9")
        self.assertEqual(self.backend.calls, [("call", 0x100)])
        self.assertEqual(self.backend.stops, 1)
        self.assertEqual(result["backend"], "real")
        self.assertEqual(result["format"], "com")
        self.assertEqual(result["bytes_loaded"], 2)
        self.assertTrue(result["segments"][0]["verified"])

    def test_format_and_execute_overrides_are_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "headerless.dat"
            path.write_bytes(b"\xC9")
            result = msx_mcp_server.t_app_load(
                str(path), format="com", execute="none")

        self.assertEqual(result["entry"], {"address": None, "mode": "none"})
        self.assertEqual(self.backend.calls, [])

    def test_hardware_control_handlers_validate_and_dispatch(self):
        self.assertEqual(msx_mcp_server.t_io_read(0x98),
                         {"port": 0x98, "value": 0xA5})
        self.assertTrue(
            msx_mcp_server.t_io_write(0x99, 0x34, verify=True)["verified"])
        self.assertEqual(msx_mcp_server.t_slot_select(1, 0x83),
                         {"page": 1, "slot_id": 0x83})
        self.assertEqual(msx_mcp_server.t_mapper_select(1, 7),
                         {"page": 1, "segment": 7})
        self.assertEqual(self.backend.hardware, [
            ("io_read", 0x98),
            ("io_write", 0x99, 0x34, True),
            ("slot", 1, 0x83),
            ("mapper", 1, 7),
        ])

        invalid_calls = (
            lambda: msx_mcp_server.t_io_read(True),
            lambda: msx_mcp_server.t_io_write(0x100, 0),
            lambda: msx_mcp_server.t_slot_select(2, 0),
            lambda: msx_mcp_server.t_mapper_select(0, -1),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises((TypeError, ValueError)):
                call()

    def test_memory_tools_use_direct_openmsx_debug_blocks_without_agent(self):
        machine = mock.Mock()
        machine.cmd.side_effect = (
            lambda command: "cafe" if "debug read_block memory" in command
            else "")
        msx_mcp_server.SESSION.msx = machine
        msx_mcp_server.SESSION.profile = "basic"

        read = msx_mcp_server.t_memory_read("ram", 0x100, 2)
        written = msx_mcp_server.t_memory_write(
            "ram", 0x100, "cafe", verify=True)

        self.assertEqual(read, "[ram 0x100+2]\ncafe")
        self.assertIn("verified", written)
        commands = [call.args[0] for call in machine.cmd.call_args_list]
        self.assertTrue(any(
            "debug read_block memory 256 2" in command
            for command in commands))
        self.assertTrue(any(
            "debug write_block memory 256" in command and "cafe" in command
            for command in commands))

    def test_agent_endpoints_are_literal_unicast_ipv4_and_bounded(self):
        with mock.patch.object(
                msx_mcp_server.SESSION, "listen_agent",
                return_value=("127.0.0.1", 12345)) as listen:
            msx_mcp_server.t_agent_listen()
        listen.assert_called_once_with(
            host="127.0.0.1", port=6603, timeout=60.0, cancelled=None)

        invalid = (
            lambda: msx_mcp_server.t_agent_listen(host="0.0.0.0"),
            lambda: msx_mcp_server.t_agent_listen(host="localhost"),
            lambda: msx_mcp_server.t_agent_listen(host="::1"),
            lambda: msx_mcp_server.t_agent_listen(port=0),
            lambda: msx_mcp_server.t_agent_connect("224.0.0.1"),
            lambda: msx_mcp_server.t_agent_connect(
                "127.0.0.1", timeout=float("nan")),
        )
        for call in invalid:
            with self.subTest(call=call), self.assertRaises(
                    (TypeError, ValueError)):
                call()

    def test_simulated_bench_is_loopback_only_and_bounded(self):
        cancelled = object()
        with mock.patch.object(msx_mcp_server.SESSION, "start_tcp_bench") as start, \
                mock.patch.object(
                    msx_mcp_server, "current_cancellation_callback",
                    return_value=cancelled):
            with mock.patch.object(
                    msx_mcp_server, "t_status", return_value="status"):
                self.assertEqual(msx_mcp_server.t_tcp_bench_start(), "status")
        start.assert_called_once_with(
            host="127.0.0.1", port=0, timeout=60.0, window=False,
            mode="resident", debug=False, cancelled=cancelled)

        invalid = (
            lambda: msx_mcp_server.t_tcp_bench_start(host="0.0.0.0"),
            lambda: msx_mcp_server.t_tcp_bench_start(host="192.168.1.2"),
            lambda: msx_mcp_server.t_tcp_bench_start(port=-1),
            lambda: msx_mcp_server.t_tcp_bench_start(port=65536),
            lambda: msx_mcp_server.t_tcp_bench_start(timeout=float("inf")),
            lambda: msx_mcp_server.t_tcp_bench_start(timeout=301),
        )
        for call in invalid:
            with self.subTest(call=call), self.assertRaises(
                    (TypeError, ValueError)):
                call()

    def test_dos_staging_names_reject_host_path_traversal(self):
        self.assertEqual(msx_mcp_server._dos_basename("demo.bas"), "DEMO.BAS")
        for name in ("../PWN", "A/BAS", "A\\BAS", "/tmp/PWN", "A:BAD",
                     "TOO-LONG9.BAS", "A..COM", "{BAD}.COM", "\x00.COM"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                msx_mcp_server._dos_basename(name)

        with self.assertRaises(ValueError):
            msx_mcp_server.t_disk_put_text("../../escape", "data")
        with self.assertRaises(ValueError):
            msx_mcp_server.t_dos_asm_run("ret", name="../../escape.com")

    def test_real_type_tools_dispatch_through_backend_keyboard_api(self):
        with (mock.patch.object(msx_mcp_server.time, "sleep"),
              mock.patch.object(
                  self.backend, "screen_text",
                  side_effect=AssertionError("unexpected VRAM read"))):
            typed = msx_mcp_server.t_type("PRINT 1")
            line = msx_mcp_server.t_type_line("RUN")
            lines = msx_mcp_server.t_type_lines(["10 PRINT 1", "RUN"])

        self.assertEqual(typed["bytes_consumed"], 7)
        self.assertEqual(line["bytes_consumed"], 4)
        self.assertEqual(lines["lines"], 2)
        self.assertFalse(typed["screen_capture_performed"])
        self.assertFalse(line["screen_capture_performed"])
        self.assertFalse(lines["screen_capture_performed"])
        self.assertEqual(self.backend.typed, [
            ("type", "PRINT 1"),
            ("line", "RUN"),
            ("lines", ("10 PRINT 1", "RUN")),
        ])

    def test_real_key_tool_dispatches_special_keys_through_agent_backend(self):
        with (mock.patch.object(msx_mcp_server.time, "sleep") as sleep,
              mock.patch.object(
                  self.backend, "screen_text",
                  side_effect=AssertionError("unexpected VRAM read"))):
            result = msx_mcp_server.t_key("CTRL+STOP")

        self.assertFalse(result["screen_capture_performed"])
        self.assertEqual(self.backend.typed, [("key", "CTRL+STOP")])
        sleep.assert_called_once_with(0.1)

    def test_real_run_basic_enters_from_dos_and_sends_one_batch(self):
        program = "10 PRINT \"MCP\"   \n\n20 END\n"
        with mock.patch.object(
                self.backend, "screen_text",
                side_effect=AssertionError("unexpected VRAM read")):
            result = msx_mcp_server.t_run_basic(
                program, clear=True, dos_prompt_confirmed=True)

        self.assertTrue(result["run_submitted"])
        self.assertFalse(result["screen_capture_performed"])
        self.assertEqual(self.backend.typed, [
            ("line", "BASIC"),
            ("line", "NEW"),
            ("lines", ("10 PRINT \"MCP\"", "20 END", "RUN")),
        ])

    def test_real_run_basic_clear_false_does_not_enter_basic_again(self):
        self.backend.screen = "Microsoft MSX BASIC\nOk"
        with mock.patch.object(msx_mcp_server.time, "sleep"):
            msx_mcp_server.t_run_basic(
                "10 PRINT 1", clear=False, allow_existing_basic=True)

        self.assertEqual(self.backend.typed, [
            ("lines", ("10 PRINT 1", "RUN")),
        ])

    def test_large_real_basic_program_uses_ascii_file_transfer(self):
        program = "\n".join(
            f"{10 + index * 10} REM " + ("X" * 40)
            for index in range(12))
        with (mock.patch.object(msx_mcp_server, "_temporary_basic_filename",
                                return_value="A:MX123456.BAS"),
              mock.patch.object(
                  self.backend, "screen_text",
                  side_effect=AssertionError("unexpected VRAM read"))):
            result = msx_mcp_server.t_run_basic(
                program, dos_prompt_confirmed=True)

        self.assertEqual(result["delivery"], "file-transfer-v2")
        self.assertFalse(result["screen_capture_performed"])
        self.assertEqual(len(self.backend.put_payloads), 1)
        name, data, options = self.backend.put_payloads[0]
        self.assertEqual(name, "A:MX123456.BAS")
        self.assertTrue(data.startswith(b"10 REM "))
        self.assertTrue(data.endswith(b"\r\n\x1a"))
        self.assertEqual(options["compression"], "raw")
        self.assertFalse(options["resume"])
        self.assertEqual(self.backend.typed, [
            ("line", "BASIC"),
            ("line", 'LOAD"A:MX123456.BAS"'),
            ("line", 'KILL"A:MX123456.BAS":RUN'),
        ])

    def test_forced_basic_file_transfer_requires_protocol_x(self):
        self.backend.feature_bits = 0
        with self.assertRaisesRegex(Exception, "file-transfer-v2"):
            msx_mcp_server.t_run_basic(
                "10 PRINT 1", transfer="file", clear=True,
                dos_prompt_confirmed=True)
        self.assertEqual(self.backend.put_payloads, [])

    def test_type_lines_never_captures_real_screen_implicitly(self):
        with mock.patch.object(
                self.backend, "screen_text",
                side_effect=AssertionError("unexpected VRAM read")) as capture:
            result = msx_mcp_server.t_type_lines(["10 PRINT 1", "RUN"])

        self.assertFalse(result["screen_capture_performed"])
        capture.assert_not_called()
        self.assertEqual(
            self.backend.typed, [("lines", ("10 PRINT 1", "RUN"))])

    def test_tokenized_basic_file_is_transferred_without_reencoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "DEMO.BAS"
            payload = b"\xff\x00\x80\x00\x00"
            path.write_bytes(payload)
            with (mock.patch.object(msx_mcp_server.time, "sleep"),
                  mock.patch.object(msx_mcp_server, "_temporary_basic_filename",
                                    return_value="A:MXABCDEF.BAS")):
                msx_mcp_server.t_run_basic_file(
                    str(path), dos_prompt_confirmed=True,
                    format="tokenized", drive="A")

        self.assertEqual(len(self.backend.put_payloads), 1)
        name, transferred, options = self.backend.put_payloads[0]
        self.assertEqual((name, transferred), ("A:MXABCDEF.BAS", payload))
        self.assertEqual(options["compression"], "raw")
        self.assertFalse(options["resume"])

    def test_basic_file_transfer_forwards_mcp_progress_and_cancellation(self):
        progress = mock.Mock()
        cancelled = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "DEMO.BAS"
            path.write_bytes(b"10 END\r\n\x1a")
            with (bind_execution_hooks(
                      progress=progress, cancelled=cancelled),
                  mock.patch.object(msx_mcp_server, "_temporary_basic_filename",
                                    return_value="A:MXABCDEF.BAS")):
                msx_mcp_server.t_run_basic_file(
                    str(path), dos_prompt_confirmed=True, format="ascii")

        _name, _transferred, options = self.backend.put_payloads[0]
        self.assertIs(options["progress"], progress)
        self.assertIs(options["cancelled"], cancelled)

    def test_basic_file_normalizer_preserves_msx_graphical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "GAME.BAS"
            path.write_bytes(b"10 PRINT \x80\n20 END\n")

            payload, selected, source = msx_mcp_server._read_basic_file(
                str(path), format="ascii")

        self.assertEqual(selected, "ascii")
        self.assertEqual(source, path)
        self.assertEqual(payload, b"10 PRINT \x80\r\n20 END\r\n\x1a")
        self.assertEqual(
            msx_mcp_server.normalize_msx_basic_text(payload), payload)

    def test_generic_put_leaves_basic_policy_to_shared_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "GAME.BAS"
            source.write_bytes(b"10 PRINT \x80\n20 END\n")

            msx_mcp_server.t_file_put(
                str(source), "A:\\GAME.BAS",
                dos_prompt_confirmed=True)

        name, payload, options = self.backend.put_payloads[0]
        self.assertEqual(name, "A:\\GAME.BAS")
        self.assertEqual(payload, b"10 PRINT \x80\n20 END\n")
        self.assertNotIn("caller_binding", options)

    def test_generic_put_accepts_canonical_and_tokenized_bas(self):
        with tempfile.TemporaryDirectory() as directory:
            canonical = Path(directory) / "TEXT.BAS"
            canonical.write_bytes(b"10 END\r\n\x1a")
            tokenized = Path(directory) / "TOKEN.BAS"
            tokenized.write_bytes(b"\xff\x00\x80\x00\x00")

            msx_mcp_server.t_file_put(
                str(canonical), "A:\\TEXT.BAS",
                dos_prompt_confirmed=True)
            msx_mcp_server.t_file_put(
                str(tokenized), "A:\\TOKEN.BAS",
                dos_prompt_confirmed=True)

        self.assertEqual(len(self.backend.transfers), 2)

    def test_real_run_basic_existing_prompt_requires_explicit_opt_in(self):
        self.backend.screen = "Microsoft MSX BASIC\nOk"
        with self.assertRaisesRegex(Exception, "allow_existing_basic=true"):
            msx_mcp_server.t_run_basic("10 PRINT 1", clear=False)
        self.assertEqual(self.backend.typed, [])

    def test_run_basic_validates_program_and_clear(self):
        with self.assertRaises(TypeError):
            msx_mcp_server.t_run_basic(123)
        with self.assertRaises(TypeError):
            msx_mcp_server.t_run_basic("10 END", clear="yes")
        with self.assertRaises(TypeError):
            msx_mcp_server.t_run_basic(
                "10 END", allow_existing_basic="yes")
        with self.assertRaises(TypeError):
            msx_mcp_server.t_run_basic(
                "10 END", dos_prompt_confirmed="yes")
        with self.assertRaisesRegex(ValueError, "transfer"):
            msx_mcp_server.t_run_basic("10 END", transfer="fastest")
        with self.assertRaisesRegex(ValueError, "drive"):
            msx_mcp_server.t_run_basic("10 END", dos_drive="AA")
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            msx_mcp_server.t_run_basic(
                "10 END", dos_prompt_confirmed=True,
                allow_existing_basic=True)
        with self.assertRaises(TypeError):
            msx_mcp_server.t_type_lines("10 END")
        with self.assertRaises(TypeError):
            msx_mcp_server.t_type_lines(["10 END", 20])

    def test_real_run_basic_refuses_arbitrary_application_screen(self):
        self.backend.screen = "GAME OVER\nPRESS FIRE"
        with (mock.patch.object(
                  self.backend, "screen_text",
                  side_effect=AssertionError("unexpected VRAM read")) as capture,
              self.assertRaisesRegex(Exception, "confirm the target state")):
            msx_mcp_server.t_run_basic("10 END")
        capture.assert_not_called()
        self.assertEqual(self.backend.typed, [])

    def test_tools_are_published_with_required_fields_and_ranges(self):
        for name in ("msx_app_load", "msx_io_read", "msx_io_write",
                     "msx_slot_select", "msx_mapper_select",
                     "msx_agent_listen", "msx_agent_connect",
                     "msx_type_lines", "msx_run_basic_file",
                     "msx_file_put", "msx_file_get"):
            self.assertIn(name, msx_mcp_server.TOOLS)
        app_schema = msx_mcp_server.TOOLS["msx_app_load"][2]
        self.assertEqual(app_schema["required"], ["path"])
        self.assertEqual(
            msx_mcp_server.TOOLS["msx_slot_select"][2]["properties"]["page"]["maximum"],
            1)
        for name in ("msx_file_put", "msx_file_get"):
            schema = msx_mcp_server.TOOLS[name][2]
            self.assertIn("dos_prompt_confirmed", schema["required"])
            self.assertEqual(
                schema["properties"]["dos_prompt_confirmed"]["type"],
                "boolean")
            self.assertNotIn("data_plane", schema["properties"])

    def test_generic_file_tools_delegate_without_loading_binary_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "archive.zip"
            source.write_bytes(b"PK\x03\x04binary")
            with mock.patch.object(
                    self.backend, "screen_text",
                    side_effect=AssertionError("unexpected VRAM read")) as capture:
                put = msx_mcp_server.t_file_put(
                    str(source), "B:\\ARCHIVE.ZIP",
                    dos_prompt_confirmed=True, compression="auto",
                    resume=True, timeout=45)
                destination = Path(directory) / "download.bin"
                get = msx_mcp_server.t_file_get(
                    "B:\\REMOTE.BIN", str(destination),
                    dos_prompt_confirmed=True, resume=False, timeout=90)

        capture.assert_not_called()
        self.assertEqual(put["direction"], "put")
        self.assertEqual(get["direction"], "get")
        self.assertEqual(put["completion"], "protocol-x-terminal-verified")
        self.assertEqual(get["prompt_check"], "not-performed")
        self.assertFalse(put["screen_capture_performed"])
        self.assertFalse(get["screen_capture_performed"])
        self.assertEqual(self.backend.transfers[0], (
            "put", source.resolve(), "B:\\ARCHIVE.ZIP",
            {"compression": "auto", "resume": True,
             "existing_only": False, "timeout": 45}))
        self.assertEqual(self.backend.transfers[1], (
            "get", "B:\\REMOTE.BIN", destination.resolve(strict=False),
            {"resume": False, "existing_only": False, "timeout": 90}))

    def test_generic_file_tools_expose_only_the_fast_data_plane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "payload.bin"
            source.write_bytes(b"fast transfer")
            destination = root / "download.bin"

            msx_mcp_server.t_file_put(
                str(source), "A:\\FAST.BIN", dos_prompt_confirmed=True)
            msx_mcp_server.t_file_get(
                "A:\\FAST.BIN", str(destination),
                dos_prompt_confirmed=True)

        self.assertNotIn("fast", self.backend.transfers[0][3])
        self.assertNotIn("fast", self.backend.transfers[1][3])

        with self.assertRaises(TypeError):
            msx_mcp_server.t_file_get(
                "A:\\FAST.BIN", "unused.bin", dos_prompt_confirmed=True,
                data_plane="legacy")

    def test_generic_file_tools_require_explicit_dos_confirmation_to_launch(self):
        self.backend.screen = "GAME OVER\nPRESS FIRE"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.bin"
            source.write_bytes(b"data")
            with self.assertRaisesRegex(Exception, "dos_prompt_confirmed=true"):
                msx_mcp_server.t_file_put(
                    str(source), "A:\\DATA.BIN",
                    dos_prompt_confirmed=False, resume=False)
        self.assertEqual(self.backend.transfers, [])

    def test_generic_file_tool_can_attach_active_resume_without_dos_prompt(self):
        self.backend.screen = "MSXAI PUT READY"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.bin"
            source.write_bytes(b"data")
            msx_mcp_server.t_file_put(
                str(source), "A:\\DATA.BIN",
                dos_prompt_confirmed=False, resume=True)

        self.assertTrue(self.backend.transfers[0][3]["existing_only"])

    def test_basic_file_uses_an_explicit_normalized_drive(self):
        self.assertEqual(msx_mcp_server._normalize_dos_drive("b"), "B")
        with self.assertRaisesRegex(ValueError, "drive"):
            msx_mcp_server._normalize_dos_drive("BB")

    def test_mcp_call_returns_structured_loader_summary_as_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.com"
            path.write_bytes(b"\xC9")
            response = msx_mcp_server.handle({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "msx_app_load", "arguments": {
                    "path": str(path), "execute": "none"}},
            })
        self.assertNotIn("isError", response["result"])
        result = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(result["backend"], "real")


if __name__ == "__main__":
    unittest.main()
