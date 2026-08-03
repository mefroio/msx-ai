import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import msx_mcp_server  # noqa: E402


class FakeRealMSX:
    def __init__(self):
        self.ram = bytearray(0x10000)
        self.vram = bytearray(0x20000)
        self.calls = []
        self.stops = 0
        self.hardware = []

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
            result = json.loads(msx_mcp_server.t_app_load(
                str(path), execute="call", verify=True))

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
            result = json.loads(msx_mcp_server.t_app_load(
                str(path), format="com", execute="none"))

        self.assertEqual(result["entry"], {"address": None, "mode": "none"})
        self.assertEqual(self.backend.calls, [])

    def test_hardware_control_handlers_validate_and_dispatch(self):
        self.assertEqual(json.loads(msx_mcp_server.t_io_read(0x98)),
                         {"port": 0x98, "value": 0xA5})
        self.assertTrue(json.loads(
            msx_mcp_server.t_io_write(0x99, 0x34, verify=True))["verified"])
        self.assertEqual(json.loads(msx_mcp_server.t_slot_select(1, 0x83)),
                         {"page": 1, "slot_id": 0x83})
        self.assertEqual(json.loads(msx_mcp_server.t_mapper_select(1, 7)),
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

    def test_tools_are_published_with_required_fields_and_ranges(self):
        for name in ("msx_app_load", "msx_io_read", "msx_io_write",
                     "msx_slot_select", "msx_mapper_select",
                     "msx_agent_listen", "msx_agent_connect"):
            self.assertIn(name, msx_mcp_server.TOOLS)
        app_schema = msx_mcp_server.TOOLS["msx_app_load"][2]
        self.assertEqual(app_schema["required"], ["path"])
        self.assertEqual(
            msx_mcp_server.TOOLS["msx_slot_select"][2]["properties"]["page"]["maximum"],
            1)

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
