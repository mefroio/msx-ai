"""MCP presentation metadata derived independently from the tool registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolHints:
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


# These tools inspect a selected target without intentionally changing it.
READ_ONLY_TOOLS = frozenset({
    "msx_screen",
    "msx_status",
    "msx_cpu_snapshot",
    "msx_memory_read",
    "msx_screenshot",
    "msx_docs_search",
    "msx_doctor",
})

# Repeating these calls with the same target state and arguments is expected
# to produce the same observation.  Snapshot timing can change values, but the
# operation itself remains idempotent.
IDEMPOTENT_TOOLS = READ_ONLY_TOOLS | frozenset({
    "msx_pause",
    "msx_resume",
})

# Calls in this set can overwrite target state, launch/stop software, replace
# the selected session, or execute unrestricted emulator commands.  Explicit
# values are intentional: clients must not infer safety from a missing hint.
DESTRUCTIVE_TOOLS = frozenset({
    "msx_boot",
    "msx_attach",
    "msx_real_listen",
    "msx_agent_listen",
    "msx_agent_connect",
    "msx_tcp_bench_start",
    "msx_stop",
    # An I/O read can consume FIFO data or clear a device latch/flag. It is
    # intentionally classified as state-changing even though no value is
    # written to the port.
    "msx_io_read",
    "msx_io_write",
    "msx_slot_select",
    "msx_mapper_select",
    "msx_memory_write",
    "msx_type_line",
    "msx_type_lines",
    "msx_type",
    "msx_key",
    "msx_run_basic",
    "msx_run_basic_file",
    "msx_file_put",
    "msx_file_get",
    "msx_reset",
    "msx_app_load",
    "msx_asm_load",
    "msx_dos_asm_run",
    "msx_disk_put_text",
    "msx_cmd",
    "msx_shutdown",
})

LOCAL_ONLY_TOOLS = frozenset({"msx_docs_search", "msx_doctor"})


TEXT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "description": "Human-readable result returned by the backend.",
        },
    },
    "required": ["result"],
    "additionalProperties": True,
}

OBJECT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Structured result. Fields depend on the selected tool and backend.",
    "additionalProperties": True,
}

_ENDPOINT_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "array",
            "prefixItems": [
                {"type": "string", "format": "ipv4"},
                {"type": "integer", "minimum": 1, "maximum": 65535},
            ],
            "minItems": 2,
            "maxItems": 2,
        },
        {"type": "string"},
        {"type": "null"},
    ],
}

STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Explicit state of local, agent, or paired bench channels.",
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "backend": {"const": "none"},
                "state": {"const": "disconnected"},
            },
            "required": ["backend", "state"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "backend": {"const": "openmsx"},
                "target": {"const": "local"},
                "channel": {"const": "openmsx-control"},
                "target_id": {"type": ["string", "null"]},
                "bench_id": {"type": ["string", "null"]},
                "state": {"enum": ["connected", "disconnected"]},
                "profile": {
                    "enum": ["basic", "disk", "dos", "msx2plus", "cbios",
                             "attach", "bench"],
                },
                "requested_profile": {
                    "enum": ["basic", "disk", "dos", "msx2plus", "cbios",
                             "auto", "attach", "bench"],
                },
                "resolved_profile": {
                    "enum": ["basic", "disk", "dos", "msx2plus", "cbios",
                             "attach", "bench"],
                },
                "machine": {"type": ["string", "null"]},
                "config_mode": {
                    "type": ["string", "null"],
                    "enum": ["isolated", "user", "overlay", None],
                },
                "config_home": {"type": ["string", "null"]},
                "effective_config_home": {"type": ["string", "null"]},
                "user_config_home": {"type": ["string", "null"]},
                "control_transport": {"type": ["string", "null"]},
                "executable": {"type": ["string", "null"]},
                "platform": {"type": ["string", "null"]},
                "screen_mode": {"type": "integer", "minimum": 0, "maximum": 255},
                "control_socket": {"type": ["string", "null"]},
            },
            "required": ["backend", "target", "state"],
            "additionalProperties": True,
        },
        {
            "type": "object",
            "properties": {
                "backend": {"const": "agent"},
                "target": {"const": "agent"},
                "channel": {"const": "agent-protocol"},
                "target_id": {"type": ["string", "null"]},
                "bench_id": {"type": ["string", "null"]},
                "state": {"type": "string", "minLength": 1},
                "state_code": {"type": "integer", "minimum": 0, "maximum": 255},
                "protocol": {"type": "integer", "minimum": 1},
                "peer": _ENDPOINT_SCHEMA,
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "resident_base": {
                    "type": "integer", "minimum": 0, "maximum": 65535,
                },
                "transport": {"type": ["string", "null"]},
                "agent_transport": {"type": ["string", "null"]},
                "agent_transport_id": {
                    "type": ["integer", "null"], "minimum": 0, "maximum": 255,
                },
                "network_transport": {"type": ["string", "null"]},
                "network_role": {"type": ["string", "null"]},
                "local_endpoint": _ENDPOINT_SCHEMA,
                "simulation": {"type": ["string", "null"]},
                "max_payload": {"type": "integer", "minimum": 1},
                "control_level": {
                    "type": ["integer", "null"], "minimum": 0, "maximum": 255,
                },
                "debug": {"type": ["boolean", "null"]},
                "runtime_mode": {"type": ["string", "null"]},
                "runtime_mode_id": {
                    "type": ["integer", "null"], "minimum": 0, "maximum": 255,
                },
                "features": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "feature_bits": {"type": "integer", "minimum": 0},
                "vdp_generation": {
                    "type": ["integer", "null"], "minimum": 0,
                },
                "vram_size": {"type": "integer", "minimum": 1},
                "vram_banks": {
                    "type": ["integer", "null"], "minimum": 1,
                },
            },
            "required": [
                "backend", "target", "state",
            ],
            # Handshake metadata can grow while retaining the stable fields above.
            "additionalProperties": True,
        },
        {
            "type": "object",
            "properties": {
                "backend": {"enum": ["hybrid-bench", "multiple"]},
                "bench_id": {"type": ["string", "null"]},
                "state": {"enum": ["connected", "disconnected"]},
                "targets": {
                    "type": "object",
                    "properties": {
                        "local": {"type": "object"},
                        "agent": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["backend", "state", "targets"],
            "additionalProperties": False,
        },
    ],
}

_LOCAL_STATUS_PROPERTIES = dict(STATUS_OUTPUT_SCHEMA["oneOf"][1]["properties"])
_AGENT_STATUS_PROPERTIES = dict(STATUS_OUTPUT_SCHEMA["oneOf"][2]["properties"])

LOCAL_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _LOCAL_STATUS_PROPERTIES,
    "required": [
        "backend", "target", "channel", "target_id", "bench_id", "state",
    ],
    "additionalProperties": False,
    "allOf": [{
        "if": {
            "properties": {"state": {"const": "connected"}},
            "required": ["state"],
        },
        "then": {
            "properties": {"target_id": {"type": "string"}},
            "required": ["profile", "screen_mode"],
        },
    }],
}

AGENT_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _AGENT_STATUS_PROPERTIES,
    "required": [
        "backend", "target", "channel", "target_id", "bench_id", "state",
    ],
    # Negotiated protocol revisions may append handshake metadata while the
    # stable identity and capability fields below remain mandatory.
    "additionalProperties": True,
    "allOf": [{
        "if": {
            "properties": {"state": {"const": "disconnected"}},
            "required": ["state"],
        },
        "else": {
            "properties": {"target_id": {"type": "string"}},
            "required": [
                "state_code", "protocol", "peer", "capabilities",
                "resident_base", "transport", "agent_transport",
                "network_transport", "network_role", "local_endpoint",
                "max_payload", "features", "feature_bits", "vram_size",
            ],
        },
    }],
}

LOCAL_IDENTITY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        name: _LOCAL_STATUS_PROPERTIES[name]
        for name in (
            "backend", "target", "channel", "target_id", "bench_id", "state",
            "profile", "requested_profile", "resolved_profile", "machine",
            "config_mode", "config_home", "effective_config_home",
            "user_config_home",
            "control_transport", "executable", "platform")
    },
    "required": [
        "backend", "target", "channel", "target_id", "bench_id", "state",
    ],
    "additionalProperties": False,
}

AGENT_IDENTITY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        name: _AGENT_STATUS_PROPERTIES[name]
        for name in (
            "backend", "target", "channel", "target_id", "bench_id", "state",
            "peer", "local_endpoint", "runtime_mode", "agent_transport")
    },
    "required": [
        "backend", "target", "channel", "target_id", "bench_id", "state",
    ],
    "additionalProperties": False,
}

TARGETS_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "oneOf": [
        STATUS_OUTPUT_SCHEMA["oneOf"][0],
        LOCAL_IDENTITY_OUTPUT_SCHEMA,
        AGENT_IDENTITY_OUTPUT_SCHEMA,
        {
            "type": "object",
            "properties": {
                "backend": {"enum": ["hybrid-bench", "multiple"]},
                "state": {"enum": ["connected", "degraded"]},
                "bench_id": {"type": ["string", "null"]},
                "targets": {
                    "type": "object",
                    "properties": {
                        "local": LOCAL_IDENTITY_OUTPUT_SCHEMA,
                        "agent": AGENT_IDENTITY_OUTPUT_SCHEMA,
                    },
                    "required": ["local", "agent"],
                    "additionalProperties": False,
                },
            },
            "required": ["backend", "state", "bench_id", "targets"],
            "additionalProperties": False,
        },
    ],
}

BENCH_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "backend": {"const": "hybrid-bench"},
        "bench_id": {"type": ["string", "null"]},
        "state": {"enum": ["connected", "degraded", "disconnected"]},
        "targets": {
            "type": "object",
            "properties": {
                "local": LOCAL_STATUS_OUTPUT_SCHEMA,
                "agent": AGENT_STATUS_OUTPUT_SCHEMA,
            },
            "additionalProperties": False,
        },
    },
    "required": ["backend", "bench_id", "state", "targets"],
    "additionalProperties": False,
    "allOf": [{
        "if": {
            "properties": {"state": {"enum": ["connected", "degraded"]}},
            "required": ["state"],
        },
        "then": {
            "properties": {
                "bench_id": {"type": "string"},
                "targets": {"required": ["local", "agent"]},
            },
        },
    }],
}

CPU_SNAPSHOT_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "description": (
        "Normalized Z80 snapshot. openMSX captures an exact instruction "
        "boundary; the physical agent captures the BIOS H.TIMI hook entry."
    ),
    "$defs": {
        "hex8": {"type": "string", "pattern": "^0x[0-9A-F]{2}$"},
        "hex16": {"type": "string", "pattern": "^0x[0-9A-F]{4}$"},
        "nullableHex8": {
            "oneOf": [{"$ref": "#/$defs/hex8"}, {"type": "null"}],
        },
        "nullableHex16": {
            "oneOf": [{"$ref": "#/$defs/hex16"}, {"type": "null"}],
        },
    },
    "properties": {
        "schema": {"const": "msx-ai-cpu-snapshot-v1"},
        "backend": {"enum": ["agent", "openmsx"]},
        "capture": {"type": "object"},
        "registers": {
            "type": "object",
            "properties": {
                **{
                    name: {"$ref": "#/$defs/nullableHex16"}
                    for name in (
                        "af", "bc", "de", "hl", "af_alt", "bc_alt",
                        "de_alt", "hl_alt", "ix", "iy", "pc", "sp")
                },
                **{
                    name: {"$ref": "#/$defs/nullableHex8"}
                    for name in ("i", "r", "im", "iff")
                },
            },
            "required": [
                "af", "bc", "de", "hl", "af_alt", "bc_alt", "de_alt",
                "hl_alt", "ix", "iy", "pc", "sp", "i", "r", "im", "iff",
            ],
            "additionalProperties": False,
        },
        "flags": {
            "type": "object",
            "properties": {
                name: {"type": "boolean"}
                for name in ("s", "z", "y", "h", "x", "pv", "n", "c")
            },
            "required": ["s", "z", "y", "h", "x", "pv", "n", "c"],
            "additionalProperties": False,
        },
        "debug": {"type": "object"},
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "schema", "backend", "capture", "registers", "flags", "debug",
        "limitations",
    ],
    "additionalProperties": False,
    "allOf": [
        {
            "if": {
                "properties": {"backend": {"const": "agent"}},
                "required": ["backend"],
            },
            "then": {
                "properties": {
                    "capture": {
                        "type": "object",
                        "properties": {
                            "source": {"const": "bios-h-timi-hook-entry"},
                            "context_version": {"type": "integer", "minimum": 1},
                            "atomic": {"const": True},
                            "exact_application_state": {"const": False},
                            "application_context": {"const": False},
                            "hook": {"const": "H.TIMI"},
                            "scope": {"const": "callback-entry"},
                        },
                        "required": [
                            "source", "context_version", "atomic",
                            "exact_application_state", "application_context",
                            "hook", "scope",
                        ],
                        "additionalProperties": False,
                    },
                    "debug": {
                        "type": "object",
                        "properties": {
                            "agent_state": {"type": "string"},
                            "agent_state_code": {
                                "type": "integer", "minimum": 0, "maximum": 255,
                            },
                            "runtime_mode": {"type": "string"},
                            "runtime_mode_id": {
                                "type": "integer", "minimum": 0, "maximum": 255,
                            },
                            "transport": {"type": "string"},
                            "transport_id": {
                                "type": "integer", "minimum": 0, "maximum": 255,
                            },
                            "hook_entry_service_sp": {"$ref": "#/$defs/hex16"},
                            "callback_return_address": {"$ref": "#/$defs/hex16"},
                            "service_i": {"$ref": "#/$defs/hex8"},
                            "service_r": {"$ref": "#/$defs/hex8"},
                            "service_iff2": {"type": ["boolean", "null"]},
                            "service_iff2_valid": {"type": "boolean"},
                            "jiffy": {
                                "type": "integer", "minimum": 0, "maximum": 65535,
                            },
                            "jiffy_hex": {"$ref": "#/$defs/hex16"},
                            "screen_mode": {
                                "type": "integer", "minimum": 0, "maximum": 255,
                            },
                            "control_level": {
                                "type": "integer", "minimum": 0, "maximum": 255,
                            },
                            "service_flags": {"$ref": "#/$defs/hex8"},
                            "validity_flags": {"$ref": "#/$defs/hex8"},
                        },
                        "required": [
                            "agent_state", "agent_state_code", "runtime_mode",
                            "runtime_mode_id", "transport", "transport_id",
                            "hook_entry_service_sp", "callback_return_address",
                            "service_i", "service_r", "service_iff2",
                            "service_iff2_valid", "jiffy", "jiffy_hex",
                            "screen_mode", "control_level", "service_flags",
                            "validity_flags",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        },
        {
            "if": {
                "properties": {"backend": {"const": "openmsx"}},
                "required": ["backend"],
            },
            "then": {
                "properties": {
                    "capture": {
                        "type": "object",
                        "properties": {
                            "source": {"const": "openmsx-debugger"},
                            "atomic": {"const": True},
                            "exact_application_state": {"const": True},
                            "cpu": {"type": "string"},
                            "was_already_breaked": {"type": "boolean"},
                            "previous_run_state_restored": {"const": True},
                        },
                        "required": [
                            "source", "atomic", "exact_application_state", "cpu",
                            "was_already_breaked", "previous_run_state_restored",
                        ],
                        "additionalProperties": False,
                    },
                    "debug": {
                        "type": "object",
                        "properties": {
                            "instruction": {"type": "string"},
                            "code_address": {"$ref": "#/$defs/hex16"},
                            "code_bytes": {
                                "type": "string", "pattern": "^(?:[0-9a-f]{2})*$",
                            },
                            "stack_address": {"$ref": "#/$defs/hex16"},
                            "stack_words": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "address": {"$ref": "#/$defs/hex16"},
                                        "value": {"$ref": "#/$defs/hex16"},
                                    },
                                    "required": ["address", "value"],
                                    "additionalProperties": False,
                                },
                            },
                            "emulator_time": {"type": ["number", "null"]},
                        },
                        "required": [
                            "instruction", "code_address", "code_bytes",
                            "stack_address", "stack_words", "emulator_time",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        },
    ],
}

_FILE_TRANSFER_COMMON_PROPERTIES: dict[str, Any] = {
    "transfer_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "source": {"type": "string", "minLength": 1},
    "target": {"type": "string", "minLength": 1},
    "wire_bytes": {"type": "integer", "minimum": 0},
    "final_bytes": {"type": "integer", "minimum": 0},
    "wire_crc32": {"type": "string", "pattern": "^[0-9a-f]{8}$"},
    "final_crc32": {"type": "string", "pattern": "^[0-9a-f]{8}$"},
    "resumed_from": {"type": "integer", "minimum": 0},
    "data_plane": {"const": "fast-v1"},
    "stream_bytes": {"type": "integer", "minimum": 0},
    "stream_seconds": {"type": "number", "minimum": 0},
    "stream_rate_bps": {"type": "number", "minimum": 0},
    "completion": {
        "enum": [
            "protocol-x-terminal-verified",
            "fast-v1-terminal-verified",
        ],
    },
    "prompt_check": {"const": "not-performed"},
    "screen_capture_performed": {"const": False},
}

_FILE_TRANSFER_REQUIRED = [
    "direction", "transfer_id", "source", "target", "encoding", "wire_bytes",
    "final_bytes", "wire_crc32", "final_crc32", "resumed_from", "stream_bytes",
    "data_plane", "stream_seconds", "stream_rate_bps", "completion", "prompt_check",
    "screen_capture_performed",
]

FILE_PUT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Completed, end-to-end CRC-32 verified host-to-MSX file transfer.",
    "properties": {
        **_FILE_TRANSFER_COMMON_PROPERTIES,
        "direction": {"const": "put"},
        "encoding": {"enum": ["raw", "packbits"]},
        "compression_reason": {"type": "string"},
        "source_bytes": {"type": "integer", "minimum": 0},
        "basic_format": {"enum": ["ascii-msx-dos", "tokenized"]},
        "basic_normalization": {"type": ["string", "null"]},
    },
    "required": _FILE_TRANSFER_REQUIRED + ["compression_reason"],
    "additionalProperties": False,
}

FILE_GET_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Completed, end-to-end CRC-32 verified MSX-to-host file transfer.",
    "properties": {
        **_FILE_TRANSFER_COMMON_PROPERTIES,
        "direction": {"const": "get"},
        "encoding": {"const": "raw"},
    },
    "required": _FILE_TRANSFER_REQUIRED,
    "additionalProperties": False,
}

APP_LOAD_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Application segments loaded into the selected MSX backend.",
    "properties": {
        "backend": {"enum": ["agent", "openmsx"]},
        "name": {"type": "string", "minLength": 1},
        "format": {"enum": ["msx-ai-app-v1", "com", "bload", "flat-rom"]},
        "origin": {"type": ["string", "null"]},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "space": {"enum": ["ram", "vram"]},
                    "address": {
                        "type": "integer", "minimum": 0, "maximum": 0x1FFFF,
                    },
                    "length": {"type": "integer", "minimum": 0},
                    "sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "verified": {"type": "boolean"},
                },
                "required": ["space", "address", "length", "sha256", "verified"],
                "additionalProperties": False,
            },
        },
        "bytes_loaded": {"type": "integer", "minimum": 0},
        "entry": {
            "type": "object",
            "oneOf": [
                {
                    "properties": {
                        "mode": {"const": "none"},
                        "address": {"type": "null"},
                    },
                    "required": ["mode", "address"],
                    "additionalProperties": False,
                },
                {
                    "properties": {
                        "mode": {"enum": ["call", "run"]},
                        "address": {
                            "type": "integer", "minimum": 0, "maximum": 65535,
                        },
                    },
                    "required": ["mode", "address"],
                    "additionalProperties": False,
                },
            ],
        },
        "mapper": {"type": ["object", "null"]},
        "required_capabilities": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
    },
    "required": [
        "backend", "name", "format", "origin", "segments", "bytes_loaded",
        "entry", "mapper", "required_capabilities",
    ],
    "additionalProperties": False,
}

INPUT_ACKNOWLEDGEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Input accepted by the physical resident without a VRAM read.",
    "oneOf": [
        {
            "properties": {
                "backend": {"const": "agent"},
                "bytes_consumed": {"type": "integer", "minimum": 0},
                "input": {"const": "line"},
                "screen_capture_performed": {"const": False},
            },
            "required": [
                "backend", "bytes_consumed", "input", "screen_capture_performed",
            ],
            "additionalProperties": False,
        },
        {
            "properties": {
                "backend": {"const": "agent"},
                "bytes_consumed": {"type": "integer", "minimum": 0},
                "input": {"const": "lines"},
                "lines": {"type": "integer", "minimum": 0},
                "screen_capture_performed": {"const": False},
            },
            "required": [
                "backend", "bytes_consumed", "input", "lines",
                "screen_capture_performed",
            ],
            "additionalProperties": False,
        },
        {
            "properties": {
                "backend": {"const": "agent"},
                "bytes_consumed": {"type": "integer", "minimum": 0},
                "input": {"const": "text"},
                "screen_capture_performed": {"const": False},
            },
            "required": [
                "backend", "bytes_consumed", "input", "screen_capture_performed",
            ],
            "additionalProperties": False,
        },
        {
            "properties": {
                "backend": {"const": "agent"},
                "input": {"const": "key"},
                "key": {"type": "string", "minLength": 1},
                "screen_capture_performed": {"const": False},
            },
            "required": ["backend", "input", "key", "screen_capture_performed"],
            "additionalProperties": False,
        },
    ],
}

DUAL_INPUT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "openMSX returns captured screen text; the physical agent returns an "
        "input acknowledgement without performing a screen capture."
    ),
    "oneOf": [TEXT_OUTPUT_SCHEMA, INPUT_ACKNOWLEDGEMENT_SCHEMA],
}

# Each public tool accepts the common openMSX text form but only its own
# physical-agent acknowledgement, so clients cannot confuse (for example) a
# key acknowledgement with a completed multi-line input operation.
DUAL_INPUT_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    name: {
        "type": "object",
        "description": DUAL_INPUT_OUTPUT_SCHEMA["description"],
        "oneOf": [TEXT_OUTPUT_SCHEMA, INPUT_ACKNOWLEDGEMENT_SCHEMA["oneOf"][index]],
    }
    for index, name in enumerate(
        ("msx_type_line", "msx_type_lines", "msx_type", "msx_key"))
}

RUN_BASIC_FILE_ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "backend": {"const": "agent"},
        "bytes_transferred": {"type": "integer", "minimum": 1},
        "delivery": {"const": "file-transfer-v2"},
        "operation": {"const": "run-basic"},
        "run_submitted": {"const": True},
        "screen_capture_performed": {"const": False},
        "transfer_id": {
            "oneOf": [
                {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                {"type": "null"},
            ],
        },
    },
    "required": [
        "backend", "bytes_transferred", "delivery", "operation",
        "run_submitted", "screen_capture_performed", "transfer_id",
    ],
    "additionalProperties": False,
}

RUN_BASIC_KEYBOARD_ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "backend": {"const": "agent"},
        "bytes_consumed": {"type": "integer", "minimum": 0},
        "delivery": {"const": "keyboard-spool"},
        "lines": {"type": "integer", "minimum": 0},
        "operation": {"const": "run-basic"},
        "run_submitted": {"const": True},
        "screen_capture_performed": {"const": False},
    },
    "required": [
        "backend", "bytes_consumed", "delivery", "lines", "operation",
        "run_submitted", "screen_capture_performed",
    ],
    "additionalProperties": False,
}

RUN_BASIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "oneOf": [
        TEXT_OUTPUT_SCHEMA,
        RUN_BASIC_FILE_ACK_SCHEMA,
        RUN_BASIC_KEYBOARD_ACK_SCHEMA,
    ],
}

SCREENSHOT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "media_type": {"const": "image/png"},
        "image_in_content": {"const": True},
    },
    "required": ["summary", "media_type", "image_in_content"],
    "additionalProperties": False,
}

DOCS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "count": {"type": "integer", "minimum": 0},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "uri": {"type": "string"},
                    "score": {"type": "number"},
                    "snippet": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "uri", "score", "snippet", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["query", "count", "results"],
    "additionalProperties": False,
}

_LOCAL_PROFILE_NAMES = [
    "basic", "disk", "dos", "msx2plus", "cbios", "auto",
]
_RESOLVED_PROFILE_NAMES = [
    "basic", "disk", "dos", "msx2plus", "cbios",
]
_CONFIG_MODE_SCHEMA: dict[str, Any] = {
    "type": "string", "enum": ["isolated", "user", "overlay"],
}
_DOCTOR_COMPONENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["machine", "extension"]},
        "name": {"type": "string"},
        "config_found": {"type": "boolean"},
        "config_path": {"type": ["string", "null"]},
        "config_candidates": {
            "type": "array", "items": {"type": "string"},
        },
    },
    "required": [
        "kind", "name", "config_found", "config_path", "config_candidates",
    ],
    "additionalProperties": False,
}
_DOCTOR_CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "profile": {"enum": _RESOLVED_PROFILE_NAMES},
        "machine": {"type": ["string", "null"]},
        "ready": {"type": "boolean"},
        "platform": {"type": ["string", "null"]},
        "control_transport": {"type": ["string", "null"]},
        "control_transport_supported": {"type": "boolean"},
        "boot_supported": {"type": "boolean"},
        "attach_transport": {"type": ["string", "null"]},
        "attach_supported": {"type": "boolean"},
        "config_mode": _CONFIG_MODE_SCHEMA,
        "executable": {"type": ["string", "null"]},
        "executable_found": {"type": "boolean"},
        "home": {"type": ["string", "null"]},
        "user_home": {"type": ["string", "null"]},
        "home_exists": {"type": "boolean"},
        "machine_config_found": {"type": ["boolean", "null"]},
        "machine_config_candidates": {
            "type": "array", "items": {"type": "string"},
        },
        "config_components": {
            "type": "array", "items": _DOCTOR_COMPONENT_SCHEMA,
        },
        "required_roms": {
            "type": "array", "items": {"type": "string"},
        },
        "missing_roms": {
            "type": "array", "items": {"type": "string"},
        },
        "rom_readiness": {
            "enum": ["ready", "unverified", "not-required"],
        },
        "problems": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "profile", "machine", "ready", "platform", "control_transport",
        "control_transport_supported", "boot_supported", "attach_transport",
        "attach_supported", "config_mode", "executable",
        "executable_found", "home", "user_home", "home_exists",
        "machine_config_found", "machine_config_candidates", "problems",
    ],
    "additionalProperties": False,
}
LOCAL_DOCTOR_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "platform": {"type": ["string", "null"]},
        "executable": {"type": ["string", "null"]},
        "executable_found": {"type": "boolean"},
        "control_transport": {"type": ["string", "null"]},
        "control_transport_supported": {"type": "boolean"},
        "transport_ready": {"type": "boolean"},
        "boot_supported": {"type": "boolean"},
        "attach_transport": {"type": ["string", "null"]},
        "attach_supported": {"type": "boolean"},
        "config_mode": _CONFIG_MODE_SCHEMA,
        "config_home": {"type": ["string", "null"]},
        "user_config_home": {"type": ["string", "null"]},
        "config_home_exists": {"type": "boolean"},
        "requested_profile": {"enum": _LOCAL_PROFILE_NAMES},
        "resolved_profile": {
            "type": ["string", "null"],
            "enum": [*_RESOLVED_PROFILE_NAMES, None],
        },
        "machine": {"type": ["string", "null"]},
        "machine_config_found": {"type": ["boolean", "null"]},
        "profile_ready": {"type": "boolean"},
        "ready": {"type": "boolean"},
        "candidates": {
            "type": "array", "minItems": 1,
            "items": _DOCTOR_CANDIDATE_SCHEMA,
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"enum": ["warning", "error"]},
                    "code": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                    "action": {"type": "string", "minLength": 1},
                },
                "required": ["severity", "code", "message", "action"],
                "additionalProperties": False,
            },
        },
        "persistent_process_started": {"const": False},
    },
    "required": [
        "platform", "executable", "executable_found", "control_transport",
        "control_transport_supported", "transport_ready", "boot_supported",
        "attach_transport",
        "attach_supported",
        "config_mode", "config_home", "user_config_home",
        "config_home_exists", "requested_profile", "resolved_profile",
        "machine", "machine_config_found", "profile_ready", "ready",
        "candidates", "issues", "persistent_process_started",
    ],
    "additionalProperties": False,
}

# Existing functions returning JSON objects.  All other legacy handlers are
# normalized into {"result": "..."} by the modern runtime.
OBJECT_RESULT_TOOLS = frozenset({
    "msx_docs_search",
    "msx_tcp_bench_start",
    "msx_status",
    "msx_cpu_snapshot",
    "msx_io_read",
    "msx_io_write",
    "msx_slot_select",
    "msx_mapper_select",
    "msx_app_load",
    "msx_type_line",
    "msx_type_lines",
    "msx_type",
    "msx_key",
    "msx_run_basic",
    "msx_run_basic_file",
    "msx_file_put",
    "msx_file_get",
})


def canonical_tool_name(name: str) -> str:
    """Map fixed-route public names to their shared implementation contract."""
    if name in {"msx_targets_status", "msx_tcp_bench_status"}:
        return "msx_status"
    if name == "msx_tcp_bench_shutdown":
        return "msx_shutdown"
    if name.startswith("msx_local_"):
        return "msx_" + name.removeprefix("msx_local_")
    if name.startswith("msx_agent_"):
        suffix = name.removeprefix("msx_agent_")
        if suffix == "disconnect":
            return "msx_shutdown"
        # Connection setup tools are already canonical and have no neutral
        # operational counterpart.
        if suffix in {"listen", "connect"}:
            return name
        return "msx_" + suffix
    return name


def title_for(name: str) -> str:
    words = name.removeprefix("msx_").split("_")
    return "MSX " + " ".join(word.upper() if word in {"cpu", "io"}
                               else word.capitalize() for word in words)


def hints_for(name: str) -> ToolHints:
    canonical = canonical_tool_name(name)
    return ToolHints(
        read_only=canonical in READ_ONLY_TOOLS,
        destructive=canonical in DESTRUCTIVE_TOOLS,
        idempotent=canonical in IDEMPOTENT_TOOLS,
        open_world=canonical not in LOCAL_ONLY_TOOLS,
    )


def _backend_specific_schema(schema: Mapping[str, Any], backend: str):
    """Constrain a structured dual-backend schema to one public route."""
    return {
        **schema,
        "properties": {
            **schema["properties"],
            "backend": {"const": backend},
        },
    }


def output_schema_for(name: str) -> Mapping[str, Any]:
    public_name = name
    if public_name == "msx_local_doctor":
        return LOCAL_DOCTOR_OUTPUT_SCHEMA
    if public_name == "msx_targets_status":
        return TARGETS_STATUS_OUTPUT_SCHEMA
    if public_name in {"msx_tcp_bench_start", "msx_tcp_bench_status"}:
        return BENCH_STATUS_OUTPUT_SCHEMA
    if public_name == "msx_local_status":
        return LOCAL_STATUS_OUTPUT_SCHEMA
    if public_name == "msx_agent_status":
        return AGENT_STATUS_OUTPUT_SCHEMA
    if public_name == "msx_local_cpu_snapshot":
        return _backend_specific_schema(
            CPU_SNAPSHOT_OUTPUT_SCHEMA, "openmsx")
    if public_name == "msx_agent_cpu_snapshot":
        return _backend_specific_schema(CPU_SNAPSHOT_OUTPUT_SCHEMA, "agent")
    if public_name == "msx_local_app_load":
        return _backend_specific_schema(APP_LOAD_OUTPUT_SCHEMA, "openmsx")
    if public_name == "msx_agent_app_load":
        return _backend_specific_schema(APP_LOAD_OUTPUT_SCHEMA, "agent")
    if public_name in {
            "msx_local_type_line", "msx_local_type_lines", "msx_local_type",
            "msx_local_key", "msx_local_run_basic"}:
        return TEXT_OUTPUT_SCHEMA
    agent_input_names = {
        "msx_agent_type_line": 0,
        "msx_agent_type_lines": 1,
        "msx_agent_type": 2,
        "msx_agent_key": 3,
    }
    if public_name in agent_input_names:
        return {
            "type": "object",
            **INPUT_ACKNOWLEDGEMENT_SCHEMA["oneOf"][
                agent_input_names[public_name]],
        }
    if public_name == "msx_agent_run_basic":
        return {
            "type": "object",
            "oneOf": [
                RUN_BASIC_FILE_ACK_SCHEMA,
                RUN_BASIC_KEYBOARD_ACK_SCHEMA,
            ],
        }

    name = canonical_tool_name(public_name)
    if name == "msx_screenshot":
        return SCREENSHOT_OUTPUT_SCHEMA
    if name == "msx_docs_search":
        return DOCS_OUTPUT_SCHEMA
    if name == "msx_status":
        return STATUS_OUTPUT_SCHEMA
    if name == "msx_cpu_snapshot":
        return CPU_SNAPSHOT_OUTPUT_SCHEMA
    if name == "msx_app_load":
        return APP_LOAD_OUTPUT_SCHEMA
    if name in DUAL_INPUT_OUTPUT_SCHEMAS:
        return DUAL_INPUT_OUTPUT_SCHEMAS[name]
    if name == "msx_run_basic":
        return RUN_BASIC_OUTPUT_SCHEMA
    if name == "msx_run_basic_file":
        return RUN_BASIC_FILE_ACK_SCHEMA
    if name == "msx_file_put":
        return FILE_PUT_OUTPUT_SCHEMA
    if name == "msx_file_get":
        return FILE_GET_OUTPUT_SCHEMA
    if name in OBJECT_RESULT_TOOLS:
        return OBJECT_OUTPUT_SCHEMA
    return TEXT_OUTPUT_SCHEMA
