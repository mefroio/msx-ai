import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

from msx_cpu import (  # noqa: E402
    CPU_CONTEXT_SIZE,
    CPUSnapshotError,
    capture_openmsx_cpu,
    decode_z80_flags,
    parse_agent_cpu_context,
)


def agent_context_payload():
    payload = bytearray(CPU_CONTEXT_SIZE)
    payload[:8] = bytes([1, 1, CPU_CONTEXT_SIZE, 0x3F, 1, 1, 0, 0])
    words = {
        8: 0x7788,    # HL'
        10: 0x5566,   # DE'
        12: 0x3344,   # BC'
        14: 0x11A5,   # AF'
        16: 0xBBCC,   # IY
        18: 0x99AA,   # IX
        20: 0x789A,   # HL
        22: 0x5678,   # DE
        24: 0x3456,   # BC
        26: 0x12A5,   # AF
        28: 0xF120,   # service SP
        30: 0x4ABC,   # internal callback return
    }
    for offset, value in words.items():
        payload[offset:offset + 2] = value.to_bytes(2, "little")
    payload[32] = 0xD1
    payload[33] = 0x42
    payload[34:36] = (0xBEEF).to_bytes(2, "little")
    payload[36] = 5
    payload[37] = 2
    payload[38] = 0x03
    payload[39] = 0
    return bytes(payload)


class FakeOpenMSX:
    def __init__(self, *, breaked=False, invalid_registers=False):
        self.breaked = bool(breaked)
        self.invalid_registers = bool(invalid_registers)
        self.commands = []
        self.register_bytes = list(range(1, 29))
        self.code = list(range(0x80, 0x90))
        self.stack = list(range(0x10, 0x20))

    def cmd(self, command):
        self.commands.append(command)
        if command == "debug breaked":
            return "true" if self.breaked else "false"
        if command == "debug break":
            self.breaked = True
            return ""
        if command == "debug cont":
            self.breaked = False
            return ""
        if command.startswith("set _msxai_cpu_regs"):
            if self.invalid_registers:
                return "1 2 broken"
            return " ".join(str(value) for value in self.register_bytes)
        if command.startswith("set _msxai_cpu_code"):
            return " ".join(str(value) for value in self.code)
        if command.startswith("set _msxai_cpu_stack"):
            return " ".join(str(value) for value in self.stack)
        if command.startswith("debug disasm"):
            return "62 02 LD A,2"
        if command == "get_active_cpu":
            return "z80"
        if command == "machine_info time":
            return "12.5"
        raise AssertionError(f"unexpected openMSX command: {command}")


class AgentCPUContextTests(unittest.TestCase):
    def test_agent_context_is_decoded_without_claiming_application_pc_sp(self):
        snapshot = parse_agent_cpu_context(agent_context_payload())

        self.assertEqual(snapshot["schema"], "msx-ai-cpu-snapshot-v1")
        self.assertEqual(snapshot["backend"], "real")
        self.assertEqual(
            snapshot["capture"]["source"], "bios-h-timi-hook-entry")
        self.assertFalse(snapshot["capture"]["exact_application_state"])
        self.assertFalse(snapshot["capture"]["application_context"])
        self.assertEqual(snapshot["registers"]["af"], "0x12A5")
        self.assertEqual(snapshot["registers"]["bc"], "0x3456")
        self.assertEqual(snapshot["registers"]["hl_alt"], "0x7788")
        self.assertEqual(snapshot["registers"]["ix"], "0x99AA")
        self.assertEqual(snapshot["registers"]["iy"], "0xBBCC")
        self.assertIsNone(snapshot["registers"]["pc"])
        self.assertIsNone(snapshot["registers"]["sp"])
        self.assertTrue(snapshot["flags"]["s"])
        self.assertFalse(snapshot["flags"]["z"])
        self.assertTrue(snapshot["flags"]["pv"])
        self.assertTrue(snapshot["flags"]["c"])
        debug = snapshot["debug"]
        self.assertEqual(debug["hook_entry_service_sp"], "0xF120")
        self.assertEqual(debug["callback_return_address"], "0x4ABC")
        self.assertEqual(debug["service_i"], "0xD1")
        self.assertEqual(debug["service_r"], "0x42")
        self.assertTrue(debug["service_iff2_valid"])
        self.assertTrue(debug["service_iff2"])
        self.assertEqual(debug["jiffy"], 0xBEEF)
        self.assertEqual(debug["jiffy_hex"], "0xBEEF")
        self.assertEqual(debug["screen_mode"], 5)
        self.assertEqual(debug["control_level"], 2)
        self.assertTrue(snapshot["limitations"])

    def test_agent_context_names_unapi_transport(self):
        payload = bytearray(agent_context_payload())
        payload[7] = 2

        snapshot = parse_agent_cpu_context(bytes(payload))

        self.assertEqual(snapshot["debug"]["transport"], "tcpip-unapi")
        self.assertEqual(snapshot["debug"]["transport_id"], 2)

    def test_agent_context_names_fossil_transport(self):
        payload = bytearray(agent_context_payload())
        payload[7] = 3

        snapshot = parse_agent_cpu_context(bytes(payload))

        self.assertEqual(snapshot["debug"]["transport"], "uart-fossil")
        self.assertEqual(snapshot["debug"]["transport_id"], 3)

    def test_agent_context_rejects_incompatible_or_reserved_records(self):
        cases = []
        cases.append(agent_context_payload()[:-1])
        bad_version = bytearray(agent_context_payload())
        bad_version[0] = 2
        cases.append(bytes(bad_version))
        bad_size = bytearray(agent_context_payload())
        bad_size[2] = 39
        cases.append(bytes(bad_size))
        missing_validity = bytearray(agent_context_payload())
        missing_validity[3] = 0x1F
        cases.append(bytes(missing_validity))
        unknown_validity = bytearray(agent_context_payload())
        unknown_validity[3] = 0x7F
        cases.append(bytes(unknown_validity))
        bad_service = bytearray(agent_context_payload())
        bad_service[38] = 0x80
        cases.append(bytes(bad_service))
        noncanonical_iff2 = bytearray(agent_context_payload())
        noncanonical_iff2[38] = 0x02
        cases.append(bytes(noncanonical_iff2))
        bad_reserved = bytearray(agent_context_payload())
        bad_reserved[39] = 1
        cases.append(bytes(bad_reserved))
        idle_state = bytearray(agent_context_payload())
        idle_state[4] = 0
        cases.append(bytes(idle_state))
        wrong_hook = bytearray(agent_context_payload())
        wrong_hook[5] = 0
        cases.append(bytes(wrong_hook))
        unknown_runtime = bytearray(agent_context_payload())
        unknown_runtime[6] = 2
        cases.append(bytes(unknown_runtime))

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(CPUSnapshotError):
                    parse_agent_cpu_context(payload)

    def test_flag_decoder_includes_documented_and_undocumented_bits(self):
        flags = decode_z80_flags(0xFF)
        self.assertEqual(
            set(flags), {"s", "z", "y", "h", "x", "pv", "n", "c"})
        self.assertTrue(all(flags.values()))


class OpenMSXCPUSnapshotTests(unittest.TestCase):
    def test_exact_snapshot_breaks_reads_and_restores_running_cpu(self):
        msx = FakeOpenMSX()
        snapshot = capture_openmsx_cpu(msx)

        self.assertEqual(snapshot["backend"], "openmsx")
        self.assertTrue(snapshot["capture"]["exact_application_state"])
        self.assertFalse(snapshot["capture"]["was_already_breaked"])
        self.assertEqual(snapshot["capture"]["cpu"], "z80")
        self.assertEqual(snapshot["registers"]["af"], "0x0102")
        self.assertEqual(snapshot["registers"]["af_alt"], "0x090A")
        self.assertEqual(snapshot["registers"]["ix"], "0x1112")
        self.assertEqual(snapshot["registers"]["pc"], "0x1516")
        self.assertEqual(snapshot["registers"]["sp"], "0x1718")
        self.assertEqual(snapshot["registers"]["iff"], "0x1C")
        self.assertEqual(snapshot["debug"]["code_bytes"], bytes(msx.code).hex())
        self.assertEqual(snapshot["debug"]["stack_words"][0], {
            "address": "0x1718", "value": "0x1110"})
        self.assertEqual(snapshot["debug"]["emulator_time"], 12.5)
        self.assertEqual(msx.commands[0], "debug breaked")
        self.assertEqual(msx.commands[1], "debug break")
        self.assertEqual(msx.commands[-1], "debug cont")
        self.assertFalse(msx.breaked)

    def test_preexisting_break_is_preserved(self):
        msx = FakeOpenMSX(breaked=True)
        snapshot = capture_openmsx_cpu(msx)

        self.assertTrue(snapshot["capture"]["was_already_breaked"])
        self.assertNotIn("debug break", msx.commands)
        self.assertNotIn("debug cont", msx.commands)
        self.assertTrue(msx.breaked)

    def test_error_does_not_leave_a_running_emulator_breaked(self):
        msx = FakeOpenMSX(invalid_registers=True)
        with self.assertRaises(CPUSnapshotError):
            capture_openmsx_cpu(msx)
        self.assertEqual(msx.commands[-1], "debug cont")
        self.assertFalse(msx.breaked)

    def test_window_size_arguments_are_bounded(self):
        msx = FakeOpenMSX()
        for kwargs in (
                {"stack_words": -1}, {"stack_words": 33},
                {"code_bytes": -1}, {"code_bytes": 65}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    capture_openmsx_cpu(msx, **kwargs)


if __name__ == "__main__":
    unittest.main()
