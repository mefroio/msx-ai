import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "msx_mcp_server.py"
sys.path.insert(0, str(SERVER.parent))

import msx_mcp_server  # noqa: E402
import msx_client  # noqa: E402


class _FakePhysicalBackend:
    def __init__(self):
        self.write_quarantined = True
        self.closed = False
        self.connect_timeout = None

    def connect(self, timeout):
        self.connect_timeout = timeout
        return ("192.0.2.20", 43123)

    def close(self):
        self.closed = True


class _FakeEmulatorBackend:
    def __init__(self):
        self.started_headless = None
        self.powered = False
        self.advanced = []
        self.closed = False

    def start(self, *, headless):
        self.started_headless = headless
        return self

    def power_on(self):
        self.powered = True

    def advance(self, seconds):
        self.advanced.append(seconds)

    def screen_text(self):
        return "MSX BASIC"

    def close(self):
        self.closed = True


class BackendOptionalityTest(unittest.TestCase):
    def test_server_initializes_and_lists_physical_tools_without_openmsx(self):
        missing_openmsx = ROOT / "does-not-exist" / "openmsx"
        environment = os.environ.copy()
        environment["OPENMSX_BIN"] = str(missing_openmsx)
        environment["MSX_AI_OPENMSX_HOME"] = str(
            ROOT / "does-not-exist" / "openmsx-home")
        requests = (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
             "params": {}},
        )
        wire = "".join(json.dumps(request) + "\n" for request in requests)

        completed = subprocess.run(
            [sys.executable, str(SERVER)], cwd=ROOT, env=environment,
            input=wire, capture_output=True, text=True, timeout=10)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "msx-ai")
        tools = {tool["name"] for tool in replies[1]["result"]["tools"]}
        self.assertIn("msx_agent_listen", tools)
        self.assertIn("msx_agent_connect", tools)
        self.assertIn("msx_agent_file_put", tools)
        self.assertIn("msx_local_boot", tools)
        self.assertNotIn("msx_file_put", tools)
        self.assertFalse(missing_openmsx.exists())

    def test_unselected_backend_never_autostarts_openmsx(self):
        session = msx_mcp_server.Session()
        with (mock.patch.object(msx_mcp_server, "OpenMSX") as openmsx,
              mock.patch.object(msx_client.shutil, "which") as which):
            with self.assertRaisesRegex(
                    msx_mcp_server.BackendNotSelectedError,
                    "msx_local_boot.*msx_local_attach.*msx_agent_listen"):
                session.require()
        openmsx.assert_not_called()
        which.assert_not_called()

    def test_physical_connect_constructs_only_the_real_backend(self):
        session = msx_mcp_server.Session()
        physical = _FakePhysicalBackend()
        with (mock.patch.object(msx_mcp_server, "OpenMSX") as openmsx,
              mock.patch.object(
                  msx_mcp_server, "RealMSX", return_value=physical) as real):
            peer = session.connect_agent("192.0.2.20", port=43123, timeout=7)

        self.assertEqual(peer, ("192.0.2.20", 43123))
        self.assertEqual(session.profile, "real")
        self.assertIs(session.msx, physical)
        self.assertEqual(physical.connect_timeout, 7.0)
        real.assert_called_once_with(host="192.0.2.20", port=43123)
        openmsx.assert_not_called()
        session.shutdown()
        self.assertTrue(physical.closed)

    def test_direct_openmsx_backend_never_constructs_real_tcp_backend(self):
        session = msx_mcp_server.Session()
        emulator = _FakeEmulatorBackend()
        with (mock.patch.object(
                  msx_mcp_server, "OpenMSX", return_value=emulator) as openmsx,
              mock.patch.object(msx_mcp_server, "RealMSX") as real):
            screen = session.boot("basic", boot_seconds=2, window=False)

        self.assertEqual(screen, "MSX BASIC")
        self.assertEqual(session.profile, "basic")
        self.assertTrue(emulator.started_headless)
        self.assertTrue(emulator.powered)
        self.assertEqual(emulator.advanced, [2])
        openmsx.assert_called_once()
        real.assert_not_called()

    def test_failed_openmsx_boot_closes_partial_backend(self):
        session = msx_mcp_server.Session()
        emulator = _FakeEmulatorBackend()
        emulator.power_on = mock.Mock(
            side_effect=msx_client.OpenMSXError("power failed"))
        with mock.patch.object(
                msx_mcp_server, "OpenMSX", return_value=emulator):
            with self.assertRaisesRegex(msx_client.OpenMSXError, "power failed"):
                session.boot("basic", boot_seconds=2, window=False)

        self.assertTrue(emulator.closed)
        self.assertIsNone(session.msx)
        self.assertIsNone(session.profile)

    def test_failed_openmsx_attach_screen_read_closes_without_publishing(self):
        session = msx_mcp_server.Session()
        emulator = mock.Mock()
        emulator.screen_text.side_effect = msx_client.OpenMSXError(
            "screen failed")
        with mock.patch.object(
                msx_mcp_server, "OpenMSX", return_value=emulator):
            with self.assertRaisesRegex(msx_client.OpenMSXError, "screen failed"):
                session.attach("/tmp/openmsx-user/socket.1")

        emulator.attach.assert_called_once_with(
            "/tmp/openmsx-user/socket.1")
        emulator.enable_keybuf.assert_called_once_with()
        emulator.close.assert_called_once_with()
        self.assertIsNone(session.msx)
        self.assertIsNone(session.profile)

    def test_physical_host_protocol_has_no_badcat_runtime_branch(self):
        for relative in (
                "server/msx_mcp_server.py", "server/msx_real.py",
                "server/msx_transfer.py", "server/msx_v3.py"):
            source = (ROOT / relative).read_text(encoding="utf-8").lower()
            self.assertNotIn("badcat", source, relative)


if __name__ == "__main__":
    unittest.main()
