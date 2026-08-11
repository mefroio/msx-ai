import pathlib
from types import SimpleNamespace
import sys
import time
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

import anyio
import mcp.types as types
import mcp_runtime
from _version import __version__
from execution import current_cancellation_callback, current_progress_callback


class MCPRuntimeTest(unittest.TestCase):
    @staticmethod
    def run_async(function):
        return anyio.run(function)

    def fake_context(self):
        class Session:
            def __init__(self):
                self.progress = []

            async def report_progress(self, completed, total=None, message=None):
                self.progress.append((completed, total, message))

        return SimpleNamespace(
            session=Session(),
            lifespan_context=mcp_runtime.RuntimeState(anyio.Lock()))

    def test_server_exposes_tools_resources_prompts_and_dual_era_discovery(self):
        server = mcp_runtime.create_server()
        self.assertEqual(server.name, "msx-ai")
        self.assertEqual(server.version, __version__)
        methods = set(server._request_handlers)
        self.assertLessEqual({
            "server/discover", "tools/list", "tools/call",
            "resources/list", "resources/read", "prompts/list", "prompts/get",
        }, methods)

    def test_all_tools_have_output_schemas_and_explicit_annotations(self):
        async def invoke():
            return await mcp_runtime._list_tools(None, None)

        result = self.run_async(invoke)
        self.assertGreaterEqual(len(result.tools), 35)
        for tool in result.tools:
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.output_schema["type"], "object")
                self.assertIsNotNone(tool.annotations.read_only_hint)
                self.assertIsNotNone(tool.annotations.destructive_hint)
                self.assertIsNotNone(tool.annotations.idempotent_hint)
                self.assertIsNotNone(tool.annotations.open_world_hint)

    def test_disconnected_status_and_docs_search_return_structured_content(self):
        context = self.fake_context()

        async def invoke(name, arguments):
            return await mcp_runtime._call_tool(
                context, types.CallToolRequestParams(
                    name=name, arguments=arguments))

        status = self.run_async(lambda: invoke("msx_targets_status", {}))
        self.assertFalse(status.is_error)
        self.assertEqual(status.structured_content["state"], "disconnected")
        search = self.run_async(lambda: invoke(
            "msx_docs_search", {"query": "physical agent safety"}))
        self.assertFalse(search.is_error)
        self.assertGreater(search.structured_content["count"], 0)
        self.assertTrue(search.structured_content["results"][0]["uri"].startswith(
            "msx-ai://docs/"))

    def test_invalid_arguments_are_a_tool_error_without_invocation(self):
        context = self.fake_context()

        async def invoke():
            return await mcp_runtime._call_tool(
                context, types.CallToolRequestParams(
                    name="msx_local_memory_read",
                    arguments={"space": "rom", "address": 0, "length": 1}))

        result = self.run_async(invoke)
        self.assertTrue(result.is_error)
        self.assertIn("invalid arguments", result.content[0].text)

    def test_unknown_argument_is_rejected_by_the_declared_schema(self):
        context = self.fake_context()

        async def invoke():
            return await mcp_runtime._call_tool(
                context, types.CallToolRequestParams(
                    name="msx_targets_status", arguments={"unexpected": True}))

        result = self.run_async(invoke)
        self.assertTrue(result.is_error)
        self.assertIn("Additional properties are not allowed",
                      result.content[0].text)

    def test_screenshot_content_has_image_and_structured_metadata(self):
        result = mcp_runtime.normalize_tool_result("msx_local_screenshot", [
            {"type": "text", "text": "[SCREEN 2]"},
            {"type": "image", "data": "AA==", "mimeType": "image/png"},
        ])
        self.assertEqual(len(result.content), 2)
        self.assertEqual(result.structured_content, {
            "summary": "[SCREEN 2]",
            "media_type": "image/png",
            "image_in_content": True,
        })

    def test_json_shaped_plain_text_does_not_change_result_type(self):
        for tool in ("msx_local_cmd", "msx_local_type_line",
                     "msx_local_run_basic"):
            for raw in ("123", '{"a": 1}'):
                with self.subTest(tool=tool, raw=raw):
                    result = mcp_runtime.normalize_tool_result(tool, raw)
                    self.assertEqual(result.structured_content, {"result": raw})

    def test_native_dict_preserves_structured_dual_backend_result(self):
        raw = {
            "backend": "agent",
            "bytes_consumed": 4,
            "input": "line",
            "screen_capture_performed": False,
        }
        result = mcp_runtime.normalize_tool_result("msx_agent_type_line", raw)
        self.assertEqual(result.structured_content, raw)

    def test_bench_status_dict_matches_its_declared_object_schema(self):
        result = mcp_runtime.normalize_tool_result(
            "msx_tcp_bench_start",
            {"backend": "hybrid-bench", "bench_id": None,
             "state": "disconnected", "targets": {}})
        self.assertEqual(result.structured_content["state"], "disconnected")

    def test_real_status_normalizes_socket_endpoints_before_schema_validation(self):
        previous = mcp_runtime.core.SESSION
        backend = SimpleNamespace(
            status=lambda: {"state": "monitor", "state_code": 0,
                            "protocol": 3},
            peer=("192.0.2.20", 6603), capabilities=0,
            resident_base=0xC000, agent_transport=None,
            agent_transport_id=None, network_transport="tcp",
            network_role="connect", local_endpoint=("192.0.2.10", 41000),
            simulation=None, _v3=SimpleNamespace(max_payload=4096),
            control_level=0, debug=False, runtime_mode="foreground-monitor",
            runtime_mode_id=1, feature_bits=0, vdp_generation=2,
            vram_size=131072, vram_banks=8)
        try:
            session = mcp_runtime.core.Session()
            session._agent_msx = backend
            session.agent_id = "agent-test"
            mcp_runtime.core.SESSION = session
            raw = mcp_runtime.core.TOOLS["msx_agent_status"][0]()
            result = mcp_runtime.normalize_tool_result("msx_agent_status", raw)
        finally:
            mcp_runtime.core.SESSION = previous

        self.assertEqual(result.structured_content["peer"], ["192.0.2.20", 6603])
        self.assertEqual(result.structured_content["local_endpoint"],
                         ["192.0.2.10", 41000])

    def test_local_diagnostic_remains_live_while_agent_channel_is_blocked(self):
        async def exercise():
            state = mcp_runtime.RuntimeState(anyio.Lock())
            agent_entered = anyio.Event()
            release_agent = anyio.Event()
            local_entered = anyio.Event()
            second_agent_entered = anyio.Event()

            async def stalled_agent():
                async with mcp_runtime._tool_lock_scope(
                        state, "msx_agent_status"):
                    agent_entered.set()
                    await release_agent.wait()

            async def local_diagnostic():
                await agent_entered.wait()
                async with mcp_runtime._tool_lock_scope(
                        state, "msx_local_screenshot"):
                    local_entered.set()

            async def second_agent():
                await agent_entered.wait()
                async with mcp_runtime._tool_lock_scope(
                        state, "msx_agent_screen"):
                    second_agent_entered.set()

            async with anyio.create_task_group() as group:
                group.start_soon(stalled_agent)
                group.start_soon(local_diagnostic)
                group.start_soon(second_agent)
                with anyio.fail_after(0.5):
                    await local_entered.wait()
                await anyio.sleep(0.03)
                self.assertFalse(second_agent_entered.is_set())
                release_agent.set()
                with anyio.fail_after(0.5):
                    await second_agent_entered.wait()

        self.run_async(exercise)

    def test_resources_are_exact_hash_checked_corpus_entries(self):
        async def invoke():
            listed = await mcp_runtime._list_resources(None, None)
            first_doc = next(resource for resource in listed.resources
                             if str(resource.uri).endswith("/overview"))
            read = await mcp_runtime._read_resource(
                None, types.ReadResourceRequestParams(uri=str(first_doc.uri)))
            return listed, read

        listed, read = self.run_async(invoke)
        self.assertEqual(listed.cache_scope, "public")
        self.assertGreaterEqual(len(listed.resources), 8)
        self.assertIn("MSX-AI", read.contents[0].text)

    def test_prompts_generate_backend_specific_original_workflows(self):
        async def invoke():
            return await mcp_runtime._get_prompt(
                None, types.GetPromptRequestParams(
                    name="start_msx_session",
                    arguments={"backend": "agent-physical"}))

        result = self.run_async(invoke)
        text = result.messages[0].content.text
        self.assertIn("msx_agent_listen", text)
        self.assertIn("msx_agent_connect", text)
        self.assertIn("Do not assume BaDCaT-specific setup", text)

    def test_prompts_enforce_required_and_declared_arguments(self):
        async def missing_backend():
            return await mcp_runtime._get_prompt(
                None, types.GetPromptRequestParams(
                    name="diagnose_msx_connection", arguments={}))

        async def invalid_visible():
            return await mcp_runtime._get_prompt(
                None, types.GetPromptRequestParams(
                    name="start_msx_session",
                    arguments={"backend": "openmsx-direct",
                               "visible": "sometimes"}))

        async def unknown_argument():
            return await mcp_runtime._get_prompt(
                None, types.GetPromptRequestParams(
                    name="start_msx_session",
                    arguments={"backend": "openmsx-direct", "extra": "x"}))

        for invoke in (missing_backend, invalid_visible, unknown_argument):
            with self.subTest(invoke=invoke), self.assertRaises(
                    mcp_runtime.MCPError):
                self.run_async(invoke)

    def test_worker_cancellation_waits_for_synchronous_cleanup(self):
        context = self.fake_context()
        finished = []

        def handler():
            cancelled = current_cancellation_callback()
            progress = current_progress_callback()
            while not cancelled():
                progress(1, 2, "working")
                time.sleep(0.005)
            finished.append(True)
            return "cancelled safely"

        async def invoke():
            with anyio.move_on_after(0.05) as scope:
                await mcp_runtime._run_sync_handler(context, handler, {})
            return scope.cancelled_caught

        self.assertTrue(self.run_async(invoke))
        self.assertEqual(finished, [True])
        self.assertGreater(len(context.session.progress), 0)

    def test_http_cli_rejects_non_loopback_and_ipv6(self):
        parser = mcp_runtime._parser()
        for host in ("0.0.0.0", "::1", "localhost"):
            with self.subTest(host=host), self.assertRaises(SystemExit):
                args = parser.parse_args(["--transport", "http", "--host", host])
                mcp_runtime._validate_cli(parser, args)


if __name__ == "__main__":
    unittest.main()
