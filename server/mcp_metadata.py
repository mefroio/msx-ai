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

LOCAL_ONLY_TOOLS = frozenset({"msx_docs_search"})


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
    "description": "State of the currently selected MSX backend.",
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
                "profile": {
                    "enum": ["basic", "disk", "dos", "msx2plus", "attach"],
                },
                "screen_mode": {"type": "integer", "minimum": 0, "maximum": 255},
                "control_socket": {"type": ["string", "null"]},
            },
            "required": ["backend", "profile", "screen_mode"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "backend": {"const": "real"},
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
                "backend", "state", "state_code", "protocol", "capabilities",
                "resident_base", "max_payload", "features", "feature_bits",
                "vram_size",
            ],
            # Handshake metadata can grow while retaining the stable fields above.
            "additionalProperties": True,
        },
    ],
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
        "backend": {"enum": ["real", "openmsx"]},
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
                "properties": {"backend": {"const": "real"}},
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
        "backend": {"enum": ["real", "openmsx"]},
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
                "backend": {"const": "real"},
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
                "backend": {"const": "real"},
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
                "backend": {"const": "real"},
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
                "backend": {"const": "real"},
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
        "backend": {"const": "real"},
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
        "backend": {"const": "real"},
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


def title_for(name: str) -> str:
    words = name.removeprefix("msx_").split("_")
    return "MSX " + " ".join(word.upper() if word in {"cpu", "io"}
                               else word.capitalize() for word in words)


def hints_for(name: str) -> ToolHints:
    return ToolHints(
        read_only=name in READ_ONLY_TOOLS,
        destructive=name in DESTRUCTIVE_TOOLS,
        idempotent=name in IDEMPOTENT_TOOLS,
        open_world=name not in LOCAL_ONLY_TOOLS,
    )


def output_schema_for(name: str) -> Mapping[str, Any]:
    if name == "msx_screenshot":
        return SCREENSHOT_OUTPUT_SCHEMA
    if name == "msx_docs_search":
        return DOCS_OUTPUT_SCHEMA
    if name in {"msx_status", "msx_tcp_bench_start"}:
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
