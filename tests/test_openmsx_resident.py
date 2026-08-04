"""Opt-in E2E: one openMSX at a time, RS232-Net -> TCP -> ASM agent.

Run with:
    MSX_RUN_INTEGRATION=1 python3 -m unittest tests.test_openmsx_resident -v
"""
import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import msx_mcp_server  # noqa: E402


@unittest.skipUnless(os.environ.get("MSX_RUN_INTEGRATION") == "1",
                     "set MSX_RUN_INTEGRATION=1 to run openMSX E2E")
class OpenMSXResidentIntegrationTest(unittest.TestCase):
    def tearDown(self):
        # The session owns exactly one bench process and always closes it before
        # another test is allowed to start.
        msx_mcp_server.SESSION.shutdown()

    def tool_result(self, name, **arguments):
        response = msx_mcp_server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        self.assertIn("result", response)
        return response["result"]

    def call_tool(self, name, **arguments):
        result = self.tool_result(name, **arguments)
        if result.get("isError"):
            self.fail(result["content"][0]["text"])
        return result["content"]

    def status(self):
        try:
            return json.loads(self.call_tool("msx_status")[0]["text"])
        except Exception:
            machine = msx_mcp_server.SESSION.bench_machine
            if machine is not None:
                print(self.machine_diagnostics(machine), file=sys.stderr)
            raise

    def read_memory(self, space, address, length):
        try:
            content = self.call_tool(
                "msx_memory_read", space=space, address=address,
                length=length)
        except Exception:
            machine = msx_mcp_server.SESSION.bench_machine
            if machine is not None:
                print(self.machine_diagnostics(machine), file=sys.stderr)
            raise
        return bytes.fromhex(content[0]["text"].rsplit("\n", 1)[-1])

    def start_bench(self, *, mode, debug=False, preload_files=()):
        if preload_files:
            msx_mcp_server.SESSION.start_tcp_bench(
                mode=mode, debug=debug, window=False, timeout=30,
                preload_files=preload_files)
        else:
            self.call_tool(
                "msx_tcp_bench_start", mode=mode, debug=debug, window=False,
                timeout=30)
        machine = msx_mcp_server.SESSION.bench_machine
        self.assertIsNotNone(machine)
        self.assertIn(machine.cmd("set mute").strip().lower(),
                      ("1", "true", "on", "yes"))
        # Keep failures in this opt-in hardware-loopback test actionable. The
        # production default remains deliberately conservative for real links.
        real = msx_mcp_server.SESSION.msx
        real.socket_timeout = 3
        if real._v3 is not None:
            real._v3.timeout = 3
        return machine

    def machine_diagnostics(self, machine):
        """Snapshot a failed emulated target without starting another one."""
        try:
            machine.cmd("debug break")
            registers = machine.cmd("cpuregs")
            pc = int(machine.cmd("reg PC"))
            sp = int(machine.cmd("reg SP"))
            code = machine.cmd(
                f'set d [debug read_block memory {pc} 32]; '
                'binary scan $d H* h; set h')
            stack = machine.cmd(
                f'set d [debug read_block memory {sp} 32]; '
                'binary scan $d H* h; set h')
            hooks = machine.cmd(
                'set d [debug read_block memory 0xFD9A 10]; '
                'binary scan $d H* h; set h')
            commsk = int(machine.cmd("debug read memory 0xFB1D"))
            vdp_r1 = int(machine.cmd('debug read "VDP regs" 1'))
            uart_status = int(machine.cmd("debug read ioports 0x81"))
            return (f"\nSCREEN:\n{machine.screen_text()}\n"
                    f"REGISTERS:\n{registers}\n"
                    f"PC BYTES: {code.strip()}\n"
                    f"SP BYTES: {stack.strip()}\n"
                    f"H.KEYI/H.TIMI: {hooks.strip()}\n"
                    f"COMMSK=0x{commsk:02X} VDP.R1=0x{vdp_r1:02X} "
                    f"UART.STATUS=0x{uart_status:02X}")
        except Exception as exc:
            return f"\nDIAGNOSTIC SNAPSHOT FAILED: {exc}"

    def test_default_memman_resident_intervenes_in_dos_program(self):
        counter_source = """org 0100h
start:  ei
loop:   halt
        ld hl,(counter)
        inc hl
        ld (counter),hl
        jr loop
counter: dw 0
mailbox: dw 0
"""
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        source = pathlib.Path(fixture.name) / "counter.asm"
        binary = pathlib.Path(fixture.name) / "COUNT.COM"
        source.write_text(counter_source)
        subprocess.run(
            [msx_mcp_server.Z80ASM, str(source), "-o", str(binary)],
            check=True, capture_output=True, text=True)

        machine = self.start_bench(
            mode="resident", preload_files=(binary,))
        screen = machine.screen_text()
        self.assertIn("MSX-AI MCP resident agent installed", screen)
        self.assertIn("A:\\>", screen)

        status = self.status()
        self.assertEqual(status["protocol"], 3)
        self.assertEqual(status["runtime_mode"], "resident")
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["debug"])
        self.assertNotIn("run", status["capabilities"])
        self.assertNotIn("mapping", status["capabilities"])
        self.assertEqual(status["agent_transport"], "uart-8251")
        self.assertEqual(status["agent_transport_id"], 0)
        self.assertIn("snapshot-lease", status["features"])
        self.assertIn("frame-wake-ack", status["features"])

        mapping = self.tool_result(
            "msx_slot_select", page=0, slot_id=0)
        self.assertTrue(mapping.get("isError"))
        self.assertIn(
            "unavailable in resident mode", mapping["content"][0]["text"])

        machine.type_line("COUNT")
        # The command echo can appear before Nextor has finished loading the
        # COM file from the emulated disk. Give that I/O a deterministic margin
        # before asserting through the independent TCP/agent path.
        machine.advance(1.0)
        self.assertEqual(
            self.read_memory("ram", 0x0100, 11),
            bytes.fromhex("fb762a0b0123220b0118f6"),
            machine.screen_text())
        first = int.from_bytes(self.read_memory("ram", 0x010B, 2), "little")
        machine.advance(0.4)
        try:
            second = int.from_bytes(
                self.read_memory("ram", 0x010B, 2), "little")
        except Exception:
            print(self.machine_diagnostics(machine), file=sys.stderr)
            raise
        self.assertGreater(second, first)

        # Exercise the bounded S lease while the fixture is actively running.
        running_screenshot = self.call_tool("msx_screenshot", atomic=True)
        self.assertTrue(base64.b64decode(
            running_screenshot[1]["data"]).startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self.status()["state"], "running")
        machine.advance(0.4)
        after_snapshot = int.from_bytes(
            self.read_memory("ram", 0x010B, 2), "little")
        self.assertGreater(after_snapshot, second)

        self.call_tool("msx_pause")
        paused = int.from_bytes(self.read_memory("ram", 0x010B, 2), "little")
        machine.advance(0.5)
        self.assertEqual(
            int.from_bytes(self.read_memory("ram", 0x010B, 2), "little"),
            paused)
        # Use a mailbox the fixture never writes. Replacing the live counter
        # itself would race with an instruction interrupted between its load
        # and store, which is a property of the test program, not the agent.
        self.call_tool("msx_memory_write", space="ram", address=0x010D,
                       data_hex="3412", verify=True)
        self.assertEqual(self.read_memory("ram", 0x010D, 2), b"\x34\x12")

        screenshot = self.call_tool("msx_screenshot", atomic=True)
        self.assertIn("ASM agent/TCP", screenshot[0]["text"])
        self.assertTrue(base64.b64decode(screenshot[1]["data"]).startswith(
            b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self.status()["state"], "paused")

        self.call_tool("msx_resume")
        machine.advance(0.4)
        try:
            mailbox = self.read_memory("ram", 0x010D, 2)
        except Exception:
            print(self.machine_diagnostics(machine), file=sys.stderr)
            raise
        self.assertEqual(mailbox, b"\x34\x12")
        self.assertGreater(
            int.from_bytes(self.read_memory("ram", 0x010B, 2), "little"),
            paused)

        # Exercise repeated hook entry/unwind and UART re-arming in this same
        # process. This caught failures that a single happy-path pause missed.
        try:
            for _ in range(16):
                self.call_tool("msx_pause")
                self.assertEqual(self.status()["state"], "paused")
                self.call_tool("msx_resume")
                self.assertEqual(self.status()["state"], "running")
        except Exception:
            print(self.machine_diagnostics(machine), file=sys.stderr)
            raise

        stop = self.tool_result("msx_stop")
        self.assertTrue(stop.get("isError"))
        self.assertIn("unsafe in resident mode", stop["content"][0]["text"])
        self.assertEqual(self.status()["state"], "running")

    def test_resident_reconfigures_without_duplicate_then_uninstalls(self):
        machine = self.start_bench(mode="resident")
        initial_status = self.status()
        self.assertEqual(initial_status["runtime_mode"], "resident")
        self.assertEqual(initial_status["agent_transport"], "uart-8251")

        # Drop the simulated cable before invoking MSXAI again. Closing both
        # ends deliberately leaves no active host protocol session whose bytes
        # could be mistaken for evidence that the resident survived. From this
        # point onward the lifecycle is exercised only through MSX-DOS.
        real = msx_mcp_server.SESSION.msx
        self.assertIsNotNone(real)
        try:
            machine.cmd("unplug msx-rs232")
        finally:
            real.close()
        msx_mcp_server.SESSION.msx = None
        msx_mcp_server.SESSION.profile = None
        machine.cmd("set throttle off")

        machine.type_line("CLS")
        machine.advance(0.3)
        bench_agent = pathlib.Path(msx_mcp_server.BENCH_AGENT_NAME).stem
        machine.type_line(f"{bench_agent} /DRIVER:8251")
        machine.advance(3.0)
        reconfigured = machine.screen_text()
        reconfigured_compact = "".join(reconfigured.split())
        self.assertIn(
            "Residentagentalreadyactive;transportselected",
            reconfigured_compact,
            reconfigured)
        self.assertNotIn(
            "MSX-AIMCPresidentagentinstalled",
            reconfigured_compact,
            reconfigured)
        self.assertTrue(
            msx_mcp_server._dos_prompt_visible(reconfigured), reconfigured)

        # The first call overlays the embedded TsrKill utility directly. A second,
        # ordinary foreground invocation is an idempotency probe: its
        # "not installed" result comes from GetTsrID and therefore confirms
        # that the named TSR was removed rather than merely disconnected.
        machine.type_line("CLS")
        machine.advance(0.3)
        machine.type_line(f"{bench_agent} /UNINSTALL")
        machine.advance(msx_mcp_server.RESIDENT_INSTALL_SECONDS)
        removed = machine.screen_text()
        self.assertNotIn("resident loader failed", removed.lower(), removed)
        self.assertTrue(msx_mcp_server._dos_prompt_visible(removed), removed)

        machine.type_line("CLS")
        machine.advance(0.3)
        machine.type_line(f"{bench_agent} /UNINSTALL")
        machine.advance(1.0)
        absent = machine.screen_text()
        self.assertIn(
            "Residentagentisnotinstalled", "".join(absent.split()), absent)
        self.assertTrue(msx_mcp_server._dos_prompt_visible(absent), absent)

    def test_resident_types_and_runs_basic_only_through_agent_tcp(self):
        self.start_bench(mode="resident")
        status = self.status()
        self.assertEqual(status["runtime_mode"], "resident")
        self.assertIn("keybuf-input", status["features"])

        # The first source line exceeds the BIOS ring's 39-byte capacity.  The
        # host must split it without letting BASIC discard a following line at
        # the Return boundary.  msx_run_basic also enters BASIC from DOS here.
        program = (
            '10 A$="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n'
            '20 PRINT "MCP TYPE OK ";LEN(A$)')
        screen = self.call_tool(
            "msx_run_basic", program=program, clear=True)[0]["text"]
        self.assertIn("MCP TYPE OK", screen, screen)
        self.assertIn("36", screen, screen)

        # Exercise raw msx_type followed by msx_type_line on the same resident
        # connection; no openMSX input API participates in either operation.
        self.call_tool("msx_type", text="PRINT ")
        screen = self.call_tool(
            "msx_type_line", text='"RESIDENT TYPE OK"')[0]["text"]
        self.assertIn("RESIDENT TYPE OK", screen, screen)
        self.assertEqual(self.status()["state"], "running")

    def test_foreground_monitor_runs_code_and_debug_is_visible(self):
        machine = self.start_bench(mode="monitor", debug=True)
        status = self.status()
        self.assertEqual(status["runtime_mode"], "foreground-monitor")
        self.assertEqual(status["state"], "monitor")
        self.assertTrue(status["debug"])
        self.assertIn("run", status["capabilities"])
        self.assertIn("[71]", machine.screen_text())

        demo = """org 08000h
start:  ei
loop:   halt
        ld hl,(counter)
        inc hl
        ld (counter),hl
        jr loop
counter: dw 0
"""
        self.call_tool("msx_asm_load", source=demo, address=0x8000,
                       execute="run")
        machine.advance(0.3)
        self.assertEqual(self.status()["state"], "running")
        first = int.from_bytes(self.read_memory("ram", 0x800B, 2), "little")
        machine.advance(0.3)
        self.assertGreater(
            int.from_bytes(self.read_memory("ram", 0x800B, 2), "little"),
            first)

        self.call_tool("msx_pause")
        paused = int.from_bytes(self.read_memory("ram", 0x800B, 2), "little")
        machine.advance(0.4)
        self.assertEqual(
            int.from_bytes(self.read_memory("ram", 0x800B, 2), "little"),
            paused)

        bulk = bytes((index * 73 + 19) & 0xFF
                     for index in range(status["max_payload"] - 2))
        self.call_tool("msx_memory_write", space="ram", address=0x8200,
                       data_hex=bulk.hex(), verify=True)
        self.assertEqual(self.read_memory("ram", 0x8200, len(bulk)), bulk)
        self.call_tool("msx_memory_write", space="vram", address=0x4000,
                       data_hex="5aa5", verify=True)
        self.assertEqual(self.read_memory("vram", 0x4000, 2), b"\x5a\xa5")

        self.call_tool("msx_resume")
        machine.advance(0.3)
        self.assertGreater(
            int.from_bytes(self.read_memory("ram", 0x800B, 2), "little"),
            paused)
        self.call_tool("msx_stop")
        self.assertEqual(self.status()["state"], "monitor")


if __name__ == "__main__":
    unittest.main()
