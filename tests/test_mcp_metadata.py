import pathlib
import sys
import unittest

import jsonschema


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

import mcp_metadata  # noqa: E402
import msx_docs  # noqa: E402
import msx_mcp_server  # noqa: E402


class MCPMetadataTest(unittest.TestCase):
    @staticmethod
    def _registers(*, exact=True):
        words = (
            "af", "bc", "de", "hl", "af_alt", "bc_alt", "de_alt",
            "hl_alt", "ix", "iy", "pc", "sp",
        )
        byte_registers = ("i", "r", "im", "iff")
        registers = {name: "0x1234" for name in words}
        registers.update({name: "0x12" for name in byte_registers})
        if not exact:
            for name in ("pc", "sp", "i", "r", "im", "iff"):
                registers[name] = None
        return registers

    @staticmethod
    def _flags():
        return {
            "s": False, "z": True, "y": False, "h": True,
            "x": False, "pv": True, "n": False, "c": True,
        }

    def test_every_registered_tool_has_explicit_hints_and_output_schema(self):
        for name in msx_mcp_server.TOOLS:
            with self.subTest(tool=name):
                hints = mcp_metadata.hints_for(name)
                self.assertIs(type(hints.read_only), bool)
                self.assertIs(type(hints.destructive), bool)
                self.assertIs(type(hints.idempotent), bool)
                self.assertIs(type(hints.open_world), bool)
                schema = mcp_metadata.output_schema_for(name)
                self.assertEqual(schema["type"], "object")
                self.assertFalse(
                    msx_mcp_server.TOOLS[name][2]["additionalProperties"])

    def test_read_only_tools_are_not_marked_destructive(self):
        self.assertFalse(
            mcp_metadata.READ_ONLY_TOOLS & mcp_metadata.DESTRUCTIVE_TOOLS)

    def test_unrestricted_and_hardware_writes_are_destructive(self):
        expected = {
            "msx_cmd", "msx_memory_write", "msx_io_write", "msx_reset",
            "msx_app_load", "msx_asm_load", "msx_file_put", "msx_file_get",
        }
        self.assertLessEqual(expected, mcp_metadata.DESTRUCTIVE_TOOLS)

    def test_io_reads_are_state_changing_and_not_idempotent(self):
        hints = mcp_metadata.hints_for("msx_agent_io_read")
        self.assertFalse(hints.read_only)
        self.assertTrue(hints.destructive)
        self.assertFalse(hints.idempotent)

    def test_documentation_search_is_local_read_only_and_structured(self):
        hints = mcp_metadata.hints_for("msx_docs_search")
        self.assertTrue(hints.read_only)
        self.assertTrue(hints.idempotent)
        self.assertFalse(hints.destructive)
        self.assertFalse(hints.open_world)
        self.assertIn("results", mcp_metadata.DOCS_OUTPUT_SCHEMA["properties"])
        filtered = msx_docs.search(
            "physical agent", audience="user", backend="agent-physical")
        self.assertGreater(filtered["count"], 0)

    def test_specific_output_schemas_are_valid_draft_2020_12_schemas(self):
        for name in (
                "msx_targets_status", "msx_local_cpu_snapshot",
                "msx_agent_cpu_snapshot", "msx_agent_file_put",
                "msx_agent_file_get", "msx_local_app_load",
                "msx_agent_app_load",
                "msx_agent_type_line", "msx_agent_type_lines",
                "msx_agent_type", "msx_agent_key",
                "msx_agent_run_basic", "msx_agent_run_basic_file"):
            with self.subTest(tool=name):
                schema = mcp_metadata.output_schema_for(name)
                jsonschema.Draft202012Validator.check_schema(schema)
                self.assertIsNot(schema, mcp_metadata.OBJECT_OUTPUT_SCHEMA)

    def test_status_schema_distinguishes_local_agent_and_hybrid_channels(self):
        local_identity = {
            "backend": "openmsx", "target": "local",
            "channel": "openmsx-control", "target_id": "local-1",
            "bench_id": "bench-123", "state": "connected",
            "profile": "bench",
        }
        agent_identity = {
            "backend": "agent", "target": "agent",
            "channel": "agent-protocol", "target_id": "agent-1",
            "bench_id": "bench-123", "state": "connected",
            "peer": ["127.0.0.1", 43123],
            "local_endpoint": ["127.0.0.1", 41000],
            "runtime_mode": "resident", "agent_transport": "uart-8251",
        }
        targets_schema = mcp_metadata.output_schema_for("msx_targets_status")
        for example in (
                {"backend": "none", "state": "disconnected"},
                local_identity,
                agent_identity,
                {"backend": "hybrid-bench", "bench_id": "bench-123",
                 "state": "connected",
                 "targets": {"local": local_identity,
                             "agent": agent_identity}}):
            with self.subTest(backend=example["backend"]):
                jsonschema.validate(example, targets_schema)

        local_status = dict(
            local_identity, screen_mode=5,
            control_socket="/tmp/openmsx-user/socket.1234")
        agent_status = dict(
            agent_identity, state="running", state_code=1, protocol=3,
            capabilities=["ram-read", "ram-write"], resident_base=0xC000,
            transport="uart-16c550", agent_transport_id=1,
            network_transport="tcp", network_role="client",
            simulation="openmsx-rs232-net", max_payload=16384,
            control_level=2, debug=False, runtime_mode_id=0,
            features=["file-transfer-v2"], feature_bits=1,
            vdp_generation=2, vram_size=131072, vram_banks=8)
        jsonschema.validate(
            local_status, mcp_metadata.output_schema_for("msx_local_status"))
        jsonschema.validate(
            agent_status, mcp_metadata.output_schema_for("msx_agent_status"))
        jsonschema.validate({
            "backend": "hybrid-bench", "bench_id": "bench-123",
            "state": "connected",
            "targets": {"local": local_status, "agent": agent_status},
        }, mcp_metadata.output_schema_for("msx_tcp_bench_status"))
        disconnected_agent = {
            "backend": "agent", "target": "agent",
            "channel": "agent-protocol", "target_id": None,
            "bench_id": "bench-123", "state": "disconnected",
        }
        jsonschema.validate({
            "backend": "hybrid-bench", "bench_id": "bench-123",
            "state": "degraded",
            "targets": {"local": local_status,
                        "agent": disconnected_agent},
        }, mcp_metadata.output_schema_for("msx_tcp_bench_status"))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(agent_status,
                                mcp_metadata.output_schema_for(
                                    "msx_local_status"))

    def test_cpu_snapshot_schema_models_both_capture_contracts(self):
        openmsx = {
            "schema": "msx-ai-cpu-snapshot-v1",
            "backend": "openmsx",
            "capture": {
                "source": "openmsx-debugger",
                "atomic": True,
                "exact_application_state": True,
                "cpu": "Z80",
                "was_already_breaked": False,
                "previous_run_state_restored": True,
            },
            "registers": self._registers(),
            "flags": self._flags(),
            "debug": {
                "instruction": "LD A,(HL)",
                "code_address": "0x8000",
                "code_bytes": "7e23",
                "stack_address": "0xF380",
                "stack_words": [
                    {"address": "0xF380", "value": "0x4010"},
                ],
                "emulator_time": 12.5,
            },
            "limitations": [],
        }
        agent = {
            "schema": "msx-ai-cpu-snapshot-v1",
            "backend": "agent",
            "capture": {
                "source": "bios-h-timi-hook-entry",
                "context_version": 1,
                "atomic": True,
                "exact_application_state": False,
                "application_context": False,
                "hook": "H.TIMI",
                "scope": "callback-entry",
            },
            "registers": self._registers(exact=False),
            "flags": self._flags(),
            "debug": {
                "agent_state": "running",
                "agent_state_code": 1,
                "runtime_mode": "resident",
                "runtime_mode_id": 0,
                "transport": "uart-8251",
                "transport_id": 0,
                "hook_entry_service_sp": "0xF200",
                "callback_return_address": "0x0038",
                "service_i": "0xF3",
                "service_r": "0x20",
                "service_iff2": True,
                "service_iff2_valid": True,
                "jiffy": 1234,
                "jiffy_hex": "0x04D2",
                "screen_mode": 2,
                "control_level": 1,
                "service_flags": "0x03",
                "validity_flags": "0x3F",
            },
            "limitations": ["Cooperative H.TIMI boundary."],
        }
        local_schema = mcp_metadata.output_schema_for("msx_local_cpu_snapshot")
        agent_schema = mcp_metadata.output_schema_for("msx_agent_cpu_snapshot")
        jsonschema.validate(openmsx, local_schema)
        jsonschema.validate(agent, agent_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(openmsx, agent_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(agent, local_schema)
        invalid = dict(openmsx)
        invalid["capture"] = dict(openmsx["capture"], source="bios-h-timi-hook-entry")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid, local_schema)

    def test_file_transfer_schemas_require_identity_crc_and_completion(self):
        common = {
            "transfer_id": "0123456789abcdef0123456789abcdef",
            "source": "/tmp/PAYLOAD.BIN",
            "target": "A:\\PAYLOAD.BIN",
            "wire_bytes": 4096,
            "final_bytes": 4096,
            "wire_crc32": "89abcdef",
            "final_crc32": "89abcdef",
            "resumed_from": 0,
            "data_plane": "fast-v1",
            "stream_bytes": 4096,
            "stream_seconds": 1.25,
            "stream_rate_bps": 3276.8,
            "completion": "fast-v1-terminal-verified",
            "prompt_check": "not-performed",
            "screen_capture_performed": False,
        }
        put = dict(common, direction="put", encoding="packbits",
                   compression_reason="smaller wire payload")
        get = dict(common, direction="get", encoding="raw")
        get["source"], get["target"] = "A:\\PAYLOAD.BIN", "/tmp/PAYLOAD.BIN"
        jsonschema.validate(
            put, mcp_metadata.output_schema_for("msx_agent_file_put"))
        jsonschema.validate(
            get, mcp_metadata.output_schema_for("msx_agent_file_get"))
        invalid = dict(put, final_crc32="NOT-A-CRC")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                invalid, mcp_metadata.output_schema_for("msx_agent_file_put"))

    def test_application_example_has_a_concrete_shape(self):
        application = {
            "backend": "agent",
            "name": "demo.com",
            "format": "com",
            "origin": "/tmp/demo.com",
            "segments": [{
                "space": "ram",
                "address": 0x100,
                "length": 2,
                "sha256": "a" * 64,
                "verified": True,
            }],
            "bytes_loaded": 2,
            "entry": {"mode": "call", "address": 0x100},
            "mapper": None,
            "required_capabilities": ["write:ram", "execute:call"],
            "execution_environment": "direct",
            "environment_auto_selected": False,
            "target_transition": "none",
            "execution_submission": "agent-call",
            "screen_probe_performed": False,
        }
        jsonschema.validate(
            application, mcp_metadata.output_schema_for("msx_agent_app_load"))
        local_application = dict(
            application, backend="openmsx",
            execution_submission="openmsx-call")
        jsonschema.validate(
            local_application, mcp_metadata.output_schema_for(
                "msx_local_app_load"))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                local_application,
                mcp_metadata.output_schema_for("msx_agent_app_load"))
        mapper_only = dict(
            application, format="msx-ai-app-v1", segments=[], bytes_loaded=0,
            mapper={"page": 1, "segment": 2})
        jsonschema.validate(
            mapper_only, mcp_metadata.output_schema_for("msx_agent_app_load"))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                dict(application, bytes_loaded=-1),
                mcp_metadata.output_schema_for("msx_agent_app_load"),
            )

    def test_application_environment_fields_are_required(self):
        application = {
            "backend": "agent",
            "name": "demo.bin",
            "format": "bload",
            "origin": "/tmp/demo.bin",
            "segments": [{
                "space": "ram",
                "address": 0x8000,
                "length": 1,
                "sha256": "b" * 64,
                "verified": True,
            }],
            "bytes_loaded": 1,
            "entry": {"mode": "run", "address": 0x8000},
            "mapper": None,
            "required_capabilities": ["write:ram", "input:basic-usr"],
            "execution_environment": "msx-basic",
            "environment_auto_selected": True,
            "target_transition": "dos-to-basic",
            "execution_submission": "basic-usr",
            "screen_probe_performed": True,
        }
        schema = mcp_metadata.output_schema_for("msx_agent_app_load")
        jsonschema.validate(application, schema)

        environment_fields = {
            "execution_environment", "environment_auto_selected",
            "target_transition", "execution_submission",
            "screen_probe_performed",
        }
        self.assertLessEqual(
            environment_fields,
            set(mcp_metadata.APP_LOAD_OUTPUT_SCHEMA["required"]))
        for field in sorted(environment_fields):
            with self.subTest(field=field), self.assertRaises(
                    jsonschema.ValidationError):
                invalid = dict(application)
                invalid.pop(field)
                jsonschema.validate(invalid, schema)

    def test_application_execution_metadata_rejects_cross_route_values(self):
        common = {
            "name": "demo.com",
            "format": "com",
            "origin": "/tmp/demo.com",
            "segments": [{
                "space": "ram",
                "address": 0x100,
                "length": 1,
                "sha256": "c" * 64,
                "verified": False,
            }],
            "bytes_loaded": 1,
            "entry": {"mode": "run", "address": 0x100},
            "mapper": None,
            "required_capabilities": ["write:ram", "execute:run"],
            "execution_environment": "direct",
            "environment_auto_selected": False,
            "target_transition": "none",
            "screen_probe_performed": False,
        }
        agent_schema = mcp_metadata.output_schema_for("msx_agent_app_load")
        local_schema = mcp_metadata.output_schema_for("msx_local_app_load")

        agent = dict(
            common, backend="agent", execution_submission="agent-run")
        local = dict(
            common, backend="openmsx",
            execution_submission="openmsx-run")
        jsonschema.validate(agent, agent_schema)
        jsonschema.validate(local, local_schema)

        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                dict(agent, execution_submission="openmsx-run"),
                agent_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                dict(local, execution_submission="agent-run"),
                local_schema)

        basic = dict(
            agent, format="bload", execution_environment="msx-basic",
            environment_auto_selected=True,
            target_transition="already-basic",
            execution_submission="basic-usr",
            screen_probe_performed=True)
        jsonschema.validate(basic, agent_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                dict(basic, backend="openmsx"), local_schema)

    def test_input_schemas_are_fixed_to_local_text_or_agent_ack(self):
        local_schema = mcp_metadata.output_schema_for("msx_local_type_lines")
        agent_schema = mcp_metadata.output_schema_for("msx_agent_type_lines")
        jsonschema.validate({"result": "Ok\n"}, local_schema)
        acknowledgement = {
            "backend": "agent",
            "bytes_consumed": 24,
            "input": "lines",
            "lines": 2,
            "screen_capture_performed": False,
        }
        jsonschema.validate(acknowledgement, agent_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"result": "Ok\n"}, agent_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(acknowledgement, local_schema)
        jsonschema.validate({
            "backend": "agent",
            "input": "key",
            "key": "CTRL+STOP",
            "screen_capture_performed": False,
        }, mcp_metadata.output_schema_for("msx_agent_key"))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({
                "backend": "agent",
                "input": "key",
                "key": "CTRL+STOP",
                "screen_capture_performed": False,
            }, agent_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({
                "backend": "agent",
                "bytes_consumed": 24,
                "input": "lines",
                "screen_capture_performed": True,
            }, agent_schema)

        local_basic_schema = mcp_metadata.output_schema_for(
            "msx_local_run_basic")
        agent_basic_schema = mcp_metadata.output_schema_for(
            "msx_agent_run_basic")
        basic_ack = {
            "backend": "agent", "bytes_consumed": 32,
            "delivery": "keyboard-spool", "lines": 3,
            "operation": "run-basic", "run_submitted": True,
            "screen_capture_performed": False,
        }
        jsonschema.validate({"result": "Ok\n"}, local_basic_schema)
        jsonschema.validate(basic_ack, agent_basic_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"result": "Ok\n"}, agent_basic_schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(basic_ack, local_basic_schema)


if __name__ == "__main__":
    unittest.main()
