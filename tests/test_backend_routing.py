import pathlib
import sys
import types
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

import msx_mcp_server  # noqa: E402


class _Local:
    attached = False
    socket_path = None

    def __init__(self, screen="LOCAL"):
        self.screen = screen
        self.closed = False
        self.started = False

    def start(self, *, headless):
        self.started = True
        return self

    def power_on(self):
        pass

    def advance(self, _seconds):
        pass

    def screen_text(self):
        return self.screen

    def screen_mode(self):
        return 0

    def close(self):
        self.closed = True


class _Agent:
    write_quarantined = True
    capabilities = 0
    resident_base = 0xC000
    peer = ("127.0.0.1", 43123)
    local_endpoint = ("127.0.0.1", 49152)
    vram_size = 0x20000
    _v3 = None

    def __init__(self, screen="AGENT"):
        self.screen = screen
        self.closed = False
        self.connected = None

    def connect(self, timeout):
        self.connected = timeout
        return self.peer

    def screen_text(self):
        return self.screen

    def snapshot_lease(self, *, atomic):
        return mock.MagicMock(
            __enter__=mock.Mock(return_value=None),
            __exit__=mock.Mock(return_value=False))

    def status(self):
        return {"state": "running", "state_code": 1, "protocol": 3}

    def close(self):
        self.closed = True


class ExplicitBackendRoutingTest(unittest.TestCase):
    def setUp(self):
        self.previous = msx_mcp_server.SESSION
        msx_mcp_server.SESSION = msx_mcp_server.Session()

    def tearDown(self):
        try:
            msx_mcp_server.SESSION.shutdown_all()
        finally:
            msx_mcp_server.SESSION = self.previous

    def test_public_tools_have_fixed_families_and_no_ambiguous_names(self):
        tools = set(msx_mcp_server.TOOLS)
        self.assertIn("msx_local_screenshot", tools)
        self.assertIn("msx_agent_screenshot", tools)
        self.assertIn("msx_targets_status", tools)
        self.assertIn("msx_tcp_bench_status", tools)
        self.assertNotIn("msx_screenshot", tools)
        self.assertNotIn("msx_status", tools)
        self.assertNotIn("msx_shutdown", tools)
        self.assertNotIn("msx_real_listen", tools)
        local_shot = msx_mcp_server.TOOLS["msx_local_screenshot"][2]
        agent_shot = msx_mcp_server.TOOLS["msx_agent_screenshot"][2]
        self.assertNotIn("atomic", local_shot["properties"])
        self.assertNotIn("allow_slow", local_shot["properties"])
        self.assertIn("atomic", agent_shot["properties"])
        self.assertIn("allow_slow", agent_shot["properties"])
        local_memory_description = msx_mcp_server.TOOLS[
            "msx_local_memory_read"][1]
        local_basic_description = msx_mcp_server.TOOLS[
            "msx_local_run_basic"][1]
        agent_cpu_description = msx_mcp_server.TOOLS[
            "msx_agent_cpu_snapshot"][1]
        self.assertNotIn("atomic=true", local_memory_description)
        self.assertNotIn("dos_prompt_confirmed", local_basic_description)
        self.assertNotIn("openMSX", agent_cpu_description)

        local_key = msx_mcp_server.TOOLS["msx_local_key"]
        for name in ("1-5", "F1-F5", "UP", "DOWN", "LEFT", "RIGHT",
                     "CTRL+STOP"):
            with self.subTest(local_key_name=name):
                self.assertIn(name, local_key[1])
                self.assertIn(
                    name, local_key[2]["properties"]["key"]["description"])

    def test_fixed_screen_routes_can_alternate_without_selection_state(self):
        local = _Local()
        agent = _Agent()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "bench"
        session._agent_msx = agent

        local_screen = msx_mcp_server.TOOLS["msx_local_screen"][0]
        agent_screen = msx_mcp_server.TOOLS["msx_agent_screen"][0]
        self.assertEqual(local_screen(), "LOCAL")
        self.assertEqual(agent_screen(), "AGENT")
        self.assertEqual(local_screen(), "LOCAL")
        self.assertFalse(local.closed)
        self.assertFalse(agent.closed)

    def test_screenshot_names_cannot_cross_backend_implementations(self):
        local = _Local()
        agent = _Agent()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "bench"
        session._agent_msx = agent
        plan = types.SimpleNamespace(mode=0, ranges=(), target_bytes=1)

        def write_local(_machine, path, **_options):
            pathlib.Path(path).write_bytes(b"local-png")
            return path, 0

        def write_agent(_capture, path, **_options):
            pathlib.Path(path).write_bytes(b"agent-png")
            return path, 0

        with (mock.patch.object(
                msx_mcp_server.msx_screenshot, "capture_openmsx",
                side_effect=write_local) as local_capture,
              mock.patch.object(
                  msx_mcp_server.msx_screenshot, "plan_realmsx_capture",
                  return_value=plan) as agent_plan,
              mock.patch.object(
                  msx_mcp_server.msx_screenshot, "acquire_realmsx_capture",
                  return_value=object()) as agent_capture,
              mock.patch.object(
                  msx_mcp_server.msx_screenshot, "render_realmsx_capture",
                  side_effect=write_agent) as agent_render):
            local_result = msx_mcp_server.TOOLS[
                "msx_local_screenshot"][0]()
            self.assertIn("openMSX control", local_result[0]["text"])
            local_capture.assert_called_once()
            agent_plan.assert_not_called()

            agent_result = msx_mcp_server.TOOLS[
                "msx_agent_screenshot"][0]()
            self.assertIn("ASM agent/TCP", agent_result[0]["text"])
            agent_plan.assert_called_once()
            agent_capture.assert_called_once()
            agent_render.assert_called_once()
            self.assertEqual(local_capture.call_count, 1)

    def test_local_boot_preserves_an_existing_agent(self):
        agent = _Agent()
        local = _Local("MSX BASIC")
        session = msx_mcp_server.SESSION
        session._agent_msx = agent
        session.agent_id = "agent-existing"
        with mock.patch.object(
                msx_mcp_server, "OpenMSX", return_value=local):
            self.assertEqual(session.boot("basic", boot_seconds=1), "MSX BASIC")

        self.assertIs(session.backend("agent")[0], agent)
        self.assertIs(session.backend("local")[0], local)
        self.assertFalse(agent.closed)

    def test_local_boot_does_not_forget_legacy_agent_assignment(self):
        agent = _Agent()
        local = _Local("MSX BASIC")
        session = msx_mcp_server.SESSION
        session.msx = agent
        session.profile = "real"
        with mock.patch.object(
                msx_mcp_server, "OpenMSX", return_value=local):
            session.boot("basic", boot_seconds=1)

        self.assertIs(session.backend("agent")[0], agent)
        self.assertIs(session.backend("local")[0], local)
        self.assertFalse(agent.closed)

    def test_agent_connect_preserves_an_existing_local_target(self):
        local = _Local()
        agent = _Agent()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "basic"
        with mock.patch.object(
                msx_mcp_server, "RealMSX", return_value=agent):
            session.connect_agent("127.0.0.1", timeout=7)

        self.assertIs(session.backend("local")[0], local)
        self.assertIs(session.backend("agent")[0], agent)
        self.assertEqual(agent.connected, 7.0)
        self.assertFalse(local.closed)

    def test_occupied_slots_refuse_silent_replacement(self):
        local = _Local()
        agent = _Agent()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "basic"
        session._agent_msx = agent

        with self.assertRaisesRegex(Exception, "already connected"):
            session.boot("basic")
        with self.assertRaisesRegex(Exception, "already connected"):
            session.connect_agent("127.0.0.1")
        self.assertFalse(local.closed)
        self.assertFalse(agent.closed)

    def test_agent_disconnect_leaves_local_bench_diagnostics_available(self):
        local = _Local()
        agent = _Agent()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "bench"
        session._agent_msx = agent
        session.bench_machine = local
        session.bench_runtime = mock.Mock()
        session.bench_id = "bench-test"

        self.assertTrue(session.disconnect_agent())
        self.assertTrue(agent.closed)
        self.assertFalse(local.closed)
        inventory = msx_mcp_server.t_status()
        self.assertEqual(inventory["backend"], "hybrid-bench")
        self.assertEqual(inventory["state"], "degraded")
        self.assertEqual(
            inventory["targets"]["agent"]["state"], "disconnected")
        self.assertEqual(msx_mcp_server.t_tcp_bench_status()["state"],
                         "degraded")
        self.assertEqual(
            msx_mcp_server.TOOLS["msx_local_screen"][0](), "LOCAL")
        with self.assertRaisesRegex(Exception, "no agent target"):
            msx_mcp_server.TOOLS["msx_agent_screen"][0]()
        with (mock.patch.object(msx_mcp_server, "RealMSX") as constructor,
              self.assertRaisesRegex(Exception, "bench still owns")):
            session.connect_agent("127.0.0.1")
        constructor.assert_not_called()

    def test_agent_reboot_detaches_only_agent_and_degrades_hybrid_bench(self):
        local = _Local("POST-REBOOT DIAGNOSTICS")
        agent = _Agent()
        agent.runtime_mode = "resident"
        agent.write_quarantined = False
        agent.reboot = mock.Mock(return_value="accepted")
        agent.status = mock.Mock(
            side_effect=AssertionError("terminal detach must not probe status"))
        agent.close = mock.Mock()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "bench"
        session._agent_msx = agent
        session.bench_machine = local
        session.bench_runtime = mock.Mock()
        session.bench_id = "bench-reboot"
        session.local_id = "bench-reboot:local"
        session.agent_id = "bench-reboot:agent"

        result = msx_mcp_server.TOOLS["msx_agent_reboot"][0]()

        self.assertTrue(result["acknowledged"])
        agent.reboot.assert_called_once_with()
        agent.status.assert_not_called()
        agent.close.assert_called_once_with(recover_snapshot=False)
        self.assertIsNone(session.backend("agent")[0])
        self.assertIs(session.backend("local")[0], local)
        self.assertEqual(msx_mcp_server.t_tcp_bench_status()["state"],
                         "degraded")
        self.assertEqual(
            msx_mcp_server.TOOLS["msx_local_screen"][0](),
            "POST-REBOOT DIAGNOSTICS")
        self.assertFalse(local.closed)

    def test_target_inventory_never_probes_a_stalled_agent(self):
        local = _Local()
        agent = _Agent()
        agent.status = mock.Mock(side_effect=TimeoutError("stalled"))
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "basic"
        session.local_id = "local-test"
        session._agent_msx = agent
        session.agent_id = "agent-test"

        inventory = msx_mcp_server.t_status()

        self.assertEqual(inventory["backend"], "multiple")
        self.assertEqual(inventory["targets"]["local"]["state"], "connected")
        self.assertEqual(inventory["targets"]["agent"]["state"], "connected")
        agent.status.assert_not_called()

    def test_local_shutdown_preserves_an_independent_agent(self):
        local = _Local()
        agent = _Agent()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "basic"
        session._agent_msx = agent

        result = msx_mcp_server.TOOLS["msx_local_shutdown"][0]()

        self.assertEqual(result, "[local openMSX stopped]")
        self.assertTrue(local.closed)
        self.assertFalse(agent.closed)
        self.assertIs(session.backend("agent")[0], agent)

    def test_local_shutdown_reports_detach_for_external_openmsx(self):
        local = _Local()
        local.attached = True
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "attach"

        result = msx_mcp_server.TOOLS["msx_local_shutdown"][0]()

        self.assertEqual(result, "[local openMSX detached]")

    def test_agent_failure_never_blocks_the_local_channel(self):
        local = _Local("DIAGNOSTIC SCREEN")
        agent = _Agent()
        agent.screen_text = mock.Mock(side_effect=TimeoutError("agent timeout"))
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "bench"
        session._agent_msx = agent

        with self.assertRaisesRegex(TimeoutError, "agent timeout"):
            msx_mcp_server.TOOLS["msx_agent_screen"][0]()
        self.assertEqual(
            msx_mcp_server.TOOLS["msx_local_screen"][0](),
            "DIAGNOSTIC SCREEN")
        self.assertFalse(local.closed)

    def test_bench_shutdown_is_the_only_combined_lifecycle_operation(self):
        events = []
        local = _Local()
        agent = _Agent()
        local.close = lambda: events.append("local")
        agent.close = lambda: events.append("agent")
        runtime = mock.Mock()
        runtime.cleanup.side_effect = lambda: events.append("runtime")
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "bench"
        session._agent_msx = agent
        session.bench_machine = local
        session.bench_runtime = runtime
        session.bench_id = "bench-test"

        with self.assertRaisesRegex(Exception, "msx_tcp_bench_shutdown"):
            session.shutdown_local()
        with self.assertRaisesRegex(Exception, "paired TCP bench"):
            msx_mcp_server.TOOLS["msx_local_reset"][0]()
        with self.assertRaisesRegex(Exception, "raw Tcl is refused"):
            msx_mcp_server.TOOLS["msx_local_cmd"][0](tcl="set harmless 1")
        self.assertTrue(session.shutdown_bench())
        self.assertEqual(events, ["agent", "local", "runtime"])
        self.assertEqual(session.connected_targets(), ())

    def test_statuses_identify_both_channels_and_shared_bench(self):
        local = _Local()
        agent = _Agent()
        session = msx_mcp_server.SESSION
        session._local_msx = local
        session._local_profile = "bench"
        session._agent_msx = agent
        session.bench_machine = local
        session.bench_id = "bench-123"
        session.local_id = "bench-123:local"
        session.agent_id = "bench-123:agent"

        status = msx_mcp_server.t_tcp_bench_status()
        self.assertEqual(status["backend"], "hybrid-bench")
        self.assertEqual(status["bench_id"], "bench-123")
        self.assertEqual(
            status["targets"]["local"]["channel"], "openmsx-control")
        self.assertEqual(
            status["targets"]["agent"]["channel"], "agent-protocol")
        self.assertEqual(
            status["targets"]["local"]["bench_id"], "bench-123")
        self.assertEqual(
            status["targets"]["agent"]["bench_id"], "bench-123")


if __name__ == "__main__":
    unittest.main()
