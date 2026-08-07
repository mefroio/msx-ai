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
        hints = mcp_metadata.hints_for("msx_io_read")
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
                "msx_status", "msx_cpu_snapshot", "msx_file_put",
                "msx_file_get", "msx_app_load",
                "msx_type_line", "msx_type_lines", "msx_type", "msx_key",
                "msx_run_basic", "msx_run_basic_file"):
            with self.subTest(tool=name):
                schema = mcp_metadata.output_schema_for(name)
                jsonschema.Draft202012Validator.check_schema(schema)
                self.assertIsNot(schema, mcp_metadata.OBJECT_OUTPUT_SCHEMA)

    def test_status_schema_distinguishes_disconnected_openmsx_and_real(self):
        examples = [
            {"backend": "none", "state": "disconnected"},
            {
                "backend": "openmsx", "profile": "attach", "screen_mode": 5,
                "control_socket": "/tmp/openmsx-user/socket.1234",
            },
            {
                "backend": "real",
                "state": "running",
                "state_code": 1,
                "protocol": 3,
                "peer": ["127.0.0.1", 6603],
                "capabilities": ["ram-read", "ram-write"],
                "resident_base": 0xC000,
                "transport": "uart-16c550",
                "agent_transport": "uart-16c550",
                "agent_transport_id": 1,
                "network_transport": "tcp",
                "network_role": "client",
                "local_endpoint": ["127.0.0.1", 6603],
                "simulation": "openmsx-rs232-net",
                "max_payload": 16384,
                "control_level": 2,
                "debug": False,
                "runtime_mode": "resident",
                "runtime_mode_id": 0,
                "features": ["file-transfer-v2"],
                "feature_bits": 1,
                "vdp_generation": 2,
                "vram_size": 131072,
                "vram_banks": 8,
            },
        ]
        schema = mcp_metadata.output_schema_for("msx_status")
        for example in examples:
            with self.subTest(backend=example["backend"]):
                jsonschema.validate(example, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {"backend": "openmsx", "profile": "attach", "screen_mode": "5"},
                schema,
            )

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
        real = {
            "schema": "msx-ai-cpu-snapshot-v1",
            "backend": "real",
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
        schema = mcp_metadata.output_schema_for("msx_cpu_snapshot")
        jsonschema.validate(openmsx, schema)
        jsonschema.validate(real, schema)
        invalid = dict(openmsx)
        invalid["capture"] = dict(openmsx["capture"], source="bios-h-timi-hook-entry")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)

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
        jsonschema.validate(put, mcp_metadata.output_schema_for("msx_file_put"))
        jsonschema.validate(get, mcp_metadata.output_schema_for("msx_file_get"))
        invalid = dict(put, final_crc32="NOT-A-CRC")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                invalid, mcp_metadata.output_schema_for("msx_file_put"))

    def test_application_example_has_a_concrete_shape(self):
        application = {
            "backend": "real",
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
        }
        jsonschema.validate(
            application, mcp_metadata.output_schema_for("msx_app_load"))
        mapper_only = dict(
            application, format="msx-ai-app-v1", segments=[], bytes_loaded=0,
            mapper={"page": 1, "segment": 2})
        jsonschema.validate(
            mapper_only, mcp_metadata.output_schema_for("msx_app_load"))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                dict(application, bytes_loaded=-1),
                mcp_metadata.output_schema_for("msx_app_load"),
            )

    def test_dual_input_schema_accepts_screen_text_or_structured_agent_ack(self):
        schema = mcp_metadata.output_schema_for("msx_type_lines")
        jsonschema.validate({"result": "Ok\n"}, schema)
        jsonschema.validate({
            "backend": "real",
            "bytes_consumed": 24,
            "input": "lines",
            "lines": 2,
            "screen_capture_performed": False,
        }, schema)
        jsonschema.validate({
            "backend": "real",
            "input": "key",
            "key": "CTRL+STOP",
            "screen_capture_performed": False,
        }, mcp_metadata.output_schema_for("msx_key"))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({
                "backend": "real",
                "input": "key",
                "key": "CTRL+STOP",
                "screen_capture_performed": False,
            }, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({
                "backend": "real",
                "bytes_consumed": 24,
                "input": "lines",
                "screen_capture_performed": True,
            }, schema)


if __name__ == "__main__":
    unittest.main()
