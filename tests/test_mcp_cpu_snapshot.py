import json
import pathlib
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

import msx_client  # noqa: E402
import msx_mcp_server  # noqa: E402


class FakeSnapshotBackend:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def cpu_snapshot(self):
        self.calls += 1
        return self.snapshot


class MCPUSnapshotToolTests(unittest.TestCase):
    def setUp(self):
        self.old_msx = msx_mcp_server.SESSION.msx
        self.old_profile = msx_mcp_server.SESSION.profile

    def tearDown(self):
        msx_mcp_server.SESSION.msx = self.old_msx
        msx_mcp_server.SESSION.profile = self.old_profile

    def test_backend_neutral_tool_delegates_and_returns_stable_json(self):
        expected = {
            "schema": "msx-ai-cpu-snapshot-v1",
            "backend": "agent",
            "registers": {"af": "0x1234"},
        }
        backend = FakeSnapshotBackend(expected)
        msx_mcp_server.SESSION.msx = backend
        msx_mcp_server.SESSION.profile = "real"

        result = msx_mcp_server.t_cpu_snapshot()

        self.assertEqual(result, expected)
        self.assertEqual(backend.calls, 1)

    def test_tool_is_published_without_backend_specific_arguments(self):
        implementation, description, schema = (
            msx_mcp_server.TOOLS["msx_agent_cpu_snapshot"])
        self.assertTrue(callable(implementation))
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"], {})
        self.assertIn("ASM-agent protocol", description)
        self.assertIn("H.TIMI", description)
        self.assertIn("does not claim", description)

    def test_openmsx_adapter_uses_shared_capture_implementation(self):
        machine = msx_client.OpenMSX(bin="unused")
        expected = {"backend": "openmsx", "schema": "snapshot"}
        with mock.patch.object(
                msx_client, "capture_openmsx_cpu",
                return_value=expected) as capture:
            self.assertEqual(machine.cpu_snapshot(), expected)
        capture.assert_called_once_with(machine)

    def test_disconnected_tool_fails_before_any_capture(self):
        msx_mcp_server.SESSION.msx = None
        msx_mcp_server.SESSION.profile = None
        with self.assertRaises(msx_mcp_server.BackendNotSelectedError):
            msx_mcp_server.t_cpu_snapshot()


if __name__ == "__main__":
    unittest.main()
