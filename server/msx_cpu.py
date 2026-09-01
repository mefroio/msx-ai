#!/usr/bin/env python3
"""Normalized Z80 debug snapshots for openMSX and the resident agent.

openMSX can stop the emulated CPU at an exact instruction boundary.  A real
MSX reached through the cooperative resident has a different contract: it can
report the register frame saved at the BIOS H.TIMI callback boundary, but it
cannot recover an arbitrary application's PC/SP from the opaque BIOS/MemMan
interrupt stack.  Keeping both capture sources in one schema makes that
distinction explicit instead of presenting service metadata as application
state.
"""
from __future__ import annotations


CPU_SNAPSHOT_SCHEMA = "msx-ai-cpu-snapshot-v1"
CPU_CONTEXT_VERSION = 1
CPU_CONTEXT_KIND_TIMI = 1
CPU_CONTEXT_SIZE = 40
CPU_CONTEXT_VALID_MAIN = 0x01
CPU_CONTEXT_VALID_ALT = 0x02
CPU_CONTEXT_VALID_INDEX = 0x04
CPU_CONTEXT_VALID_SERVICE_SP = 0x08
CPU_CONTEXT_VALID_CALLBACK_RETURN = 0x10
CPU_CONTEXT_VALID_SERVICE_META = 0x20
CPU_CONTEXT_REQUIRED_FLAGS = (
    CPU_CONTEXT_VALID_MAIN | CPU_CONTEXT_VALID_ALT |
    CPU_CONTEXT_VALID_INDEX | CPU_CONTEXT_VALID_SERVICE_SP |
    CPU_CONTEXT_VALID_CALLBACK_RETURN | CPU_CONTEXT_VALID_SERVICE_META
)

STATE_NAMES = {0: "monitor", 1: "running", 2: "paused"}
RUNTIME_NAMES = {0: "resident", 1: "foreground-monitor"}
TRANSPORT_NAMES = {
    0: "uart-8251",
    1: "uart-16c550",
    2: "tcpip-unapi",
    3: "uart-fossil",
}

REGISTER_WORDS = (
    "af", "bc", "de", "hl", "af_alt", "bc_alt", "de_alt", "hl_alt",
    "ix", "iy", "pc", "sp",
)
REGISTER_BYTES = ("i", "r", "im", "iff")


class CPUSnapshotError(ValueError):
    """The backend returned an invalid or unsupported CPU snapshot."""


def _hex8(value):
    return None if value is None else f"0x{value:02X}"


def _hex16(value):
    return None if value is None else f"0x{value:04X}"


def decode_z80_flags(value):
    """Decode the documented and undocumented bits of the Z80 F register."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Z80 flags must be an integer")
    if not 0 <= value <= 0xFF:
        raise ValueError("Z80 flags must be in range 0..255")
    return {
        "s": bool(value & 0x80),
        "z": bool(value & 0x40),
        "y": bool(value & 0x20),
        "h": bool(value & 0x10),
        "x": bool(value & 0x08),
        "pv": bool(value & 0x04),
        "n": bool(value & 0x02),
        "c": bool(value & 0x01),
    }


def _format_registers(values):
    formatted = {}
    for name in REGISTER_WORDS:
        formatted[name] = _hex16(values.get(name))
    for name in REGISTER_BYTES:
        formatted[name] = _hex8(values.get(name))
    return formatted


def parse_agent_cpu_context(payload):
    """Decode the resident opcode-D response into the public snapshot schema."""
    payload = bytes(payload)
    if len(payload) != CPU_CONTEXT_SIZE:
        raise CPUSnapshotError(
            f"CPU context must contain {CPU_CONTEXT_SIZE} bytes, got "
            f"{len(payload)}")
    version, kind, declared_size, validity = payload[:4]
    if version != CPU_CONTEXT_VERSION:
        raise CPUSnapshotError(
            f"unsupported CPU context version {version}")
    if kind != CPU_CONTEXT_KIND_TIMI:
        raise CPUSnapshotError(f"unsupported CPU context kind {kind}")
    if declared_size != len(payload):
        raise CPUSnapshotError(
            f"CPU context declares {declared_size} bytes, got {len(payload)}")
    if validity & CPU_CONTEXT_REQUIRED_FLAGS != CPU_CONTEXT_REQUIRED_FLAGS:
        raise CPUSnapshotError(
            f"CPU context is missing required fields (flags 0x{validity:02X})")
    if validity & ~CPU_CONTEXT_REQUIRED_FLAGS:
        raise CPUSnapshotError(
            f"CPU context uses unknown validity flags 0x{validity:02X}")
    if payload[39] != 0:
        raise CPUSnapshotError(
            f"CPU context reserved byte must be zero, got 0x{payload[39]:02X}")

    def word(offset):
        return int.from_bytes(payload[offset:offset + 2], "little")

    # The wire order mirrors the fixed Z80 PUSH frame at hook entry.
    values = {
        "hl_alt": word(8),
        "de_alt": word(10),
        "bc_alt": word(12),
        "af_alt": word(14),
        "iy": word(16),
        "ix": word(18),
        "hl": word(20),
        "de": word(22),
        "bc": word(24),
        "af": word(26),
        # These application-level values are not part of the hook ABI.
        "pc": None,
        "sp": None,
        "i": None,
        "r": None,
        "im": None,
        "iff": None,
    }
    state, hook_kind, runtime, transport = payload[4:8]
    if state not in (1, 2):
        raise CPUSnapshotError(
            f"CPU context has invalid active state {state}")
    if hook_kind != 1:
        raise CPUSnapshotError(
            f"CPU context did not originate in H.TIMI (hook {hook_kind})")
    if runtime not in RUNTIME_NAMES:
        raise CPUSnapshotError(
            f"CPU context has unknown runtime mode {runtime}")
    service_sp = word(28)
    callback_return = word(30)
    jiffy = int.from_bytes(payload[34:36], "little")
    service_flags = payload[38]
    if service_flags & ~0x03:
        raise CPUSnapshotError(
            f"CPU context uses unknown service flags 0x{service_flags:02X}")
    if service_flags & 0x02 and not service_flags & 0x01:
        raise CPUSnapshotError(
            "CPU context sets IFF2 without marking it valid")
    service_iff2_valid = bool(service_flags & 0x01)
    return {
        "schema": CPU_SNAPSHOT_SCHEMA,
        "backend": "real",
        "capture": {
            "source": "bios-h-timi-hook-entry",
            "context_version": version,
            "atomic": True,
            "exact_application_state": False,
            "application_context": False,
            "hook": "H.TIMI" if hook_kind == 1 else f"hook-{hook_kind}",
            "scope": "callback-entry",
        },
        "registers": _format_registers(values),
        "flags": decode_z80_flags(values["af"] & 0xFF),
        "debug": {
            "agent_state": STATE_NAMES.get(state, f"unknown-{state}"),
            "agent_state_code": state,
            "runtime_mode": RUNTIME_NAMES.get(runtime, f"unknown-{runtime}"),
            "runtime_mode_id": runtime,
            "transport": TRANSPORT_NAMES.get(
                transport, f"unknown-{transport}"),
            "transport_id": transport,
            "hook_entry_service_sp": _hex16(service_sp),
            "callback_return_address": _hex16(callback_return),
            "service_i": _hex8(payload[32]),
            "service_r": _hex8(payload[33]),
            "service_iff2": (
                bool(service_flags & 0x02) if service_iff2_valid else None),
            "service_iff2_valid": service_iff2_valid,
            "jiffy": jiffy,
            "jiffy_hex": _hex16(jiffy),
            "screen_mode": payload[36],
            "control_level": payload[37],
            "service_flags": _hex8(service_flags),
            "validity_flags": _hex8(validity),
        },
        "limitations": [
            "Registers are those visible at the BIOS H.TIMI callback entry "
            "after BIOS/MemMan interrupt dispatch, not an arbitrary "
            "application instruction boundary.",
            "Application PC, SP, I/R, IFF and interrupt mode are unavailable "
            "through this cooperative hook; service-only values are kept "
            "under debug metadata.",
        ],
    }


def _bounded_count(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be in range 0..{maximum}")
    return value


def _tcl_bool(value):
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "on", "yes"):
        return True
    if normalized in ("0", "false", "off", "no"):
        return False
    raise CPUSnapshotError(f"invalid openMSX boolean result {value!r}")


def _tcl_int_list(value, expected, description):
    tokens = str(value).split()
    if len(tokens) != expected:
        raise CPUSnapshotError(
            f"openMSX returned {len(tokens)} {description}, expected "
            f"{expected}")
    try:
        values = [int(token, 0) for token in tokens]
    except ValueError as exc:
        raise CPUSnapshotError(
            f"openMSX returned invalid {description}: {value!r}") from exc
    if any(not 0 <= item <= 0xFF for item in values):
        raise CPUSnapshotError(
            f"openMSX returned out-of-range {description}: {value!r}")
    return values


def _openmsx_bytes(msx, address, length, variable):
    if length == 0:
        return []
    script = (
        f"set {variable} {{}}; "
        f"for {{set i 0}} {{$i < {length}}} {{incr i}} {{"
        f"lappend {variable} [debug read memory "
        f"[expr {{({address} + $i) & 0xFFFF}}]]"
        f"}}; set {variable}")
    return _tcl_int_list(msx.cmd(script), length, "memory bytes")


def capture_openmsx_cpu(msx, *, stack_words=8, code_bytes=16):
    """Stop openMSX briefly and return one exact, internally consistent state."""
    stack_words = _bounded_count(stack_words, "stack_words", 32)
    code_bytes = _bounded_count(code_bytes, "code_bytes", 64)

    was_breaked = _tcl_bool(msx.cmd("debug breaked"))
    if not was_breaked:
        msx.cmd("debug break")
    try:
        register_script = (
            "set _msxai_cpu_regs {}; "
            "for {set i 0} {$i < 28} {incr i} {"
            "lappend _msxai_cpu_regs [debug read {CPU regs} $i]"
            "}; set _msxai_cpu_regs")
        raw = _tcl_int_list(
            msx.cmd(register_script), 28, "CPU register bytes")

        def pair(offset):
            return (raw[offset] << 8) | raw[offset + 1]

        values = {
            "af": pair(0), "bc": pair(2), "de": pair(4), "hl": pair(6),
            "af_alt": pair(8), "bc_alt": pair(10),
            "de_alt": pair(12), "hl_alt": pair(14),
            "ix": pair(16), "iy": pair(18), "pc": pair(20),
            "sp": pair(22), "i": raw[24], "r": raw[25],
            "im": raw[26], "iff": raw[27],
        }
        code = _openmsx_bytes(
            msx, values["pc"], code_bytes, "_msxai_cpu_code")
        stack = _openmsx_bytes(
            msx, values["sp"], stack_words * 2, "_msxai_cpu_stack")
        stack_entries = []
        for index in range(stack_words):
            offset = index * 2
            word = stack[offset] | (stack[offset + 1] << 8)
            stack_entries.append({
                "address": _hex16((values["sp"] + offset) & 0xFFFF),
                "value": _hex16(word),
            })
        instruction = str(
            msx.cmd(f"debug disasm {values['pc']}")).strip()
        try:
            cpu = str(msx.cmd("get_active_cpu")).strip()
        except Exception:
            cpu = "unknown"
        try:
            emulator_time = float(msx.cmd("machine_info time"))
        except (TypeError, ValueError, RuntimeError):
            emulator_time = None
        return {
            "schema": CPU_SNAPSHOT_SCHEMA,
            "backend": "openmsx",
            "capture": {
                "source": "openmsx-debugger",
                "atomic": True,
                "exact_application_state": True,
                "cpu": cpu,
                "was_already_breaked": was_breaked,
                "previous_run_state_restored": True,
            },
            "registers": _format_registers(values),
            "flags": decode_z80_flags(values["af"] & 0xFF),
            "debug": {
                "instruction": instruction,
                "code_address": _hex16(values["pc"]),
                "code_bytes": bytes(code).hex(),
                "stack_address": _hex16(values["sp"]),
                "stack_words": stack_entries,
                "emulator_time": emulator_time,
            },
            "limitations": [],
        }
    finally:
        if not was_breaked:
            msx.cmd("debug cont")


__all__ = [
    "CPU_CONTEXT_KIND_TIMI", "CPU_CONTEXT_SIZE", "CPU_CONTEXT_VERSION",
    "CPU_CONTEXT_VALID_ALT", "CPU_CONTEXT_VALID_CALLBACK_RETURN",
    "CPU_CONTEXT_VALID_INDEX", "CPU_CONTEXT_VALID_MAIN",
    "CPU_CONTEXT_VALID_SERVICE_META", "CPU_CONTEXT_VALID_SERVICE_SP",
    "CPU_SNAPSHOT_SCHEMA", "CPUSnapshotError", "capture_openmsx_cpu",
    "decode_z80_flags", "parse_agent_cpu_context",
]
