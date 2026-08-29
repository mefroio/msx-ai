import functools
import pathlib
import shutil
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

from tools.build_badcat_init import (  # noqa: E402
    COMMANDS,
    FORBIDDEN_COMMAND_FRAGMENTS,
    INITIAL_COMMANDS,
    LISTENER_COMMANDS,
    ORIGIN,
    PAGE_1_START,
    REQUIRED_CONSTANTS,
    BadcatInitBuildError,
    assemble_badcat_init,
    build_badcat_init,
    parse_labels,
    validate_badcat_init_image,
)


Z_FLAG = 0x40
C_FLAG = 0x01
EXTBIO = 0xFFCA


@functools.cache
def _real_image():
    return assemble_badcat_init()


def _read_c_string(image, name):
    start = image.labels[name] - ORIGIN
    end = image.data.index(0, start)
    return image.data[start:end].decode("ascii")


def _read_command_table(image, table_name):
    offset = image.labels[table_name] - ORIGIN
    addresses = []
    while True:
        address = image.data[offset] | image.data[offset + 1] << 8
        offset += 2
        if address == 0:
            return tuple(addresses)
        addresses.append(address)


class _ParserMachine:
    """Small Z80 executor for BADINIT.COM's assembled option parser."""

    def __init__(self, image, command_tail):
        tail = command_tail.encode("ascii")
        if len(tail) > 127:
            raise ValueError("MSX-DOS command tail is too long")
        self.memory = bytearray(65536)
        self.memory[ORIGIN:ORIGIN + len(image.data)] = image.data
        self.memory[0x80] = len(tail)
        self.memory[0x81:0x81 + len(tail)] = tail
        if 0x81 + len(tail) < ORIGIN:
            self.memory[0x81 + len(tail)] = 13
        self.labels = image.labels
        self.a = self.b = self.c = self.d = self.e = 0
        self.h = self.l = self.f = 0
        self.pc = image.labels["parse_command_line"]
        self.sp = 0xEFFE
        self.sentinel = 0xBEEF
        self.memory[self.sp] = self.sentinel & 0xFF
        self.memory[self.sp + 1] = self.sentinel >> 8

    @property
    def hl(self):
        return self.h << 8 | self.l

    @hl.setter
    def hl(self, value):
        value &= 0xFFFF
        self.h, self.l = value >> 8, value & 0xFF

    @property
    def de(self):
        return self.d << 8 | self.e

    @de.setter
    def de(self, value):
        value &= 0xFFFF
        self.d, self.e = value >> 8, value & 0xFF

    def fetch(self):
        value = self.memory[self.pc]
        self.pc = self.pc + 1 & 0xFFFF
        return value

    def fetch_word(self):
        return self.fetch() | self.fetch() << 8

    def push(self, value):
        self.sp = self.sp - 1 & 0xFFFF
        self.memory[self.sp] = value >> 8 & 0xFF
        self.sp = self.sp - 1 & 0xFFFF
        self.memory[self.sp] = value & 0xFF

    def pop(self):
        low = self.memory[self.sp]
        self.sp = self.sp + 1 & 0xFFFF
        high = self.memory[self.sp]
        self.sp = self.sp + 1 & 0xFFFF
        return low | high << 8

    def logic(self, value):
        self.a = value & 0xFF
        self.f = Z_FLAG if self.a == 0 else 0

    def compare(self, value):
        result = self.a - value
        self.f = ((Z_FLAG if result & 0xFF == 0 else 0)
                  | (C_FLAG if result < 0 else 0))

    def increment(self, value):
        carry = self.f & C_FLAG
        value = value + 1 & 0xFF
        self.f = carry | (Z_FLAG if value == 0 else 0)
        return value

    def decrement(self, value):
        carry = self.f & C_FLAG
        value = value - 1 & 0xFF
        self.f = carry | (Z_FLAG if value == 0 else 0)
        return value

    def relative(self, condition):
        displacement = self.fetch()
        if displacement & 0x80:
            displacement -= 0x100
        if condition:
            self.pc = self.pc + displacement & 0xFFFF

    def step(self):
        address = self.pc
        opcode = self.fetch()
        if opcode == 0x3A:  # LD A,(nn)
            self.a = self.memory[self.fetch_word()]
        elif opcode == 0x32:  # LD (nn),A
            self.memory[self.fetch_word()] = self.a
        elif opcode == 0x21:  # LD HL,nn
            self.hl = self.fetch_word()
        elif opcode == 0x11:  # LD DE,nn
            self.de = self.fetch_word()
        elif opcode == 0x3E:  # LD A,n
            self.a = self.fetch()
        elif opcode == 0x06:  # LD B,n
            self.b = self.fetch()
        elif opcode == 0x0E:  # LD C,n
            self.c = self.fetch()
        elif opcode == 0x47:  # LD B,A
            self.b = self.a
        elif opcode == 0x78:  # LD A,B
            self.a = self.b
        elif opcode == 0x79:  # LD A,C
            self.a = self.c
        elif opcode == 0x7E:  # LD A,(HL)
            self.a = self.memory[self.hl]
        elif opcode == 0x1A:  # LD A,(DE)
            self.a = self.memory[self.de]
        elif opcode == 0x12:  # LD (DE),A
            self.memory[self.de] = self.a
        elif opcode == 0x23:  # INC HL
            self.hl += 1
        elif opcode == 0x13:  # INC DE
            self.de += 1
        elif opcode == 0x0C:  # INC C
            self.c = self.increment(self.c)
        elif opcode == 0x05:  # DEC B
            self.b = self.decrement(self.b)
        elif opcode == 0xFE:  # CP n
            self.compare(self.fetch())
        elif opcode == 0xBE:  # CP (HL)
            self.compare(self.memory[self.hl])
        elif opcode == 0xE6:  # AND n
            self.logic(self.a & self.fetch())
        elif opcode == 0xB7:  # OR A
            self.logic(self.a)
        elif opcode == 0xAF:  # XOR A
            self.logic(0)
        elif opcode == 0x37:  # SCF
            self.f |= C_FLAG
        elif opcode == 0x18:  # JR
            self.relative(True)
        elif opcode == 0x20:  # JR NZ
            self.relative(not self.f & Z_FLAG)
        elif opcode == 0x28:  # JR Z
            self.relative(bool(self.f & Z_FLAG))
        elif opcode == 0x30:  # JR NC
            self.relative(not self.f & C_FLAG)
        elif opcode == 0x38:  # JR C
            self.relative(bool(self.f & C_FLAG))
        elif opcode == 0xCD:  # CALL nn
            target = self.fetch_word()
            self.push(self.pc)
            self.pc = target
        elif opcode == 0xC8:  # RET Z
            if self.f & Z_FLAG:
                self.pc = self.pop()
        elif opcode == 0xC0:  # RET NZ
            if not self.f & Z_FLAG:
                self.pc = self.pop()
        elif opcode == 0xC9:  # RET
            self.pc = self.pop()
        else:
            raise AssertionError(
                f"unsupported parser opcode at {address:04X}: {opcode:02X}")

    def run(self):
        for _ in range(4096):
            if self.pc == self.sentinel:
                selected = self.memory[self.labels["selected_divisor"]]
                return bool(self.f & C_FLAG), selected
            self.step()
        raise AssertionError("BADINIT option parser did not return")


class _ResidentProbeMachine:
    """Execute the real resident guard with a deterministic EXTBIO stub."""

    def __init__(self, image, memman_version=None, resident_id=None):
        self.memory = bytearray(65536)
        self.memory[ORIGIN:ORIGIN + len(image.data)] = image.data
        self.labels = image.labels
        self.memman_version = memman_version
        self.resident_id = resident_id
        self.calls = []
        self.a = self.d = self.e = self.f = 0
        self.h = self.l = 0
        self.pc = image.labels["find_resident_agent"]
        self.sp = 0xEFFE
        self.sentinel = 0xBEEF
        self.memory[self.sp] = self.sentinel & 0xFF
        self.memory[self.sp + 1] = self.sentinel >> 8

    @property
    def hl(self):
        return self.h << 8 | self.l

    @hl.setter
    def hl(self, value):
        value &= 0xFFFF
        self.h, self.l = value >> 8, value & 0xFF

    def fetch(self):
        value = self.memory[self.pc]
        self.pc = self.pc + 1 & 0xFFFF
        return value

    def fetch_word(self):
        return self.fetch() | self.fetch() << 8

    def pop(self):
        low = self.memory[self.sp]
        self.sp = self.sp + 1 & 0xFFFF
        high = self.memory[self.sp]
        self.sp = self.sp + 1 & 0xFFFF
        return low | high << 8

    def compare(self, value):
        result = self.a - value
        self.f = ((Z_FLAG if result & 0xFF == 0 else 0)
                  | (C_FLAG if result < 0 else 0))

    def relative(self, condition):
        displacement = self.fetch()
        if displacement & 0x80:
            displacement -= 0x100
        if condition:
            self.pc = self.pc + displacement & 0xFFFF

    def extbio(self):
        self.calls.append((self.d, self.e, self.hl))
        if self.e == 30:  # MemMan INICHK
            if self.memman_version is None:
                self.a = 0
            else:
                self.a = ord("M")
                self.d, self.e = self.memman_version
            return
        if self.e != 62:
            raise AssertionError(f"unexpected EXTBIO function {self.e}")
        name = bytes(self.memory[self.hl:self.hl + 12])
        if name != b"MSXAI MCP1  ":
            raise AssertionError(f"wrong MemMan TSR name {name!r}")
        if self.resident_id is None:
            self.a = 0
            self.f |= C_FLAG
        else:
            self.a = self.resident_id
            self.f &= ~C_FLAG

    def step(self):
        address = self.pc
        opcode = self.fetch()
        if opcode == 0xAF:  # XOR A
            self.a = 0
            self.f = Z_FLAG
        elif opcode == 0x16:  # LD D,n
            self.d = self.fetch()
        elif opcode == 0x1E:  # LD E,n
            self.e = self.fetch()
        elif opcode == 0x3E:  # LD A,n
            self.a = self.fetch()
        elif opcode == 0x7A:  # LD A,D
            self.a = self.d
        elif opcode == 0x7B:  # LD A,E
            self.a = self.e
        elif opcode == 0x21:  # LD HL,nn
            self.hl = self.fetch_word()
        elif opcode == 0xFE:  # CP n
            self.compare(self.fetch())
        elif opcode == 0x20:  # JR NZ
            self.relative(not self.f & Z_FLAG)
        elif opcode == 0x28:  # JR Z
            self.relative(bool(self.f & Z_FLAG))
        elif opcode == 0x30:  # JR NC
            self.relative(not self.f & C_FLAG)
        elif opcode == 0x38:  # JR C
            self.relative(bool(self.f & C_FLAG))
        elif opcode == 0x37:  # SCF
            self.f |= C_FLAG
        elif opcode == 0xCD:  # CALL nn
            target = self.fetch_word()
            if target != EXTBIO:
                raise AssertionError(
                    f"unexpected call at {address:04X}: {target:04X}")
            self.extbio()
        elif opcode == 0xC9:  # RET
            self.pc = self.pop()
        else:
            raise AssertionError(
                f"unsupported resident-probe opcode at {address:04X}: "
                f"{opcode:02X}")

    def run(self):
        for _ in range(128):
            if self.pc == self.sentinel:
                return bool(self.f & C_FLAG), self.a, tuple(self.calls)
            self.step()
        raise AssertionError("BADINIT resident probe did not return")


class BadcatInitBuilderTest(unittest.TestCase):
    def test_label_parser_rejects_duplicates(self):
        self.assertEqual(
            parse_labels("badinit_start: equ $0100\n"),
            {"badinit_start": ORIGIN},
        )
        with self.assertRaisesRegex(BadcatInitBuildError, "duplicate label"):
            parse_labels(
                "badinit_start: equ $0100\n"
                "badinit_start: equ $0200\n")

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_real_build_is_small_deterministic_validated_and_atomic(self):
        first = _real_image()
        second = assemble_badcat_init()
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.labels["badinit_start"], ORIGIN)
        self.assertEqual(first.labels["badinit_end"], ORIGIN + len(first.data))
        self.assertLess(first.labels["badinit_end"], PAGE_1_START)
        for name, expected in REQUIRED_CONSTANTS.items():
            self.assertEqual(first.labels[name], expected)
        self.assertEqual(
            first.labels["response_buffer_end"]
            - first.labels["response_buffer"],
            512,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "nested" / "BADINIT.COM"
            published = build_badcat_init(output=output)
            self.assertEqual(published.data, first.data)
            self.assertEqual(output.read_bytes(), first.data)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                tuple(output.parent.glob(f".{output.name}.*")), ())

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_validator_rejects_default_command_table_and_persistence_drift(self):
        image = _real_image()

        mutated = bytearray(image.data)
        mutated[image.labels["selected_divisor"] - ORIGIN] = 1
        with self.assertRaisesRegex(BadcatInitBuildError, "selected_divisor"):
            validate_badcat_init_image(bytes(mutated), image.labels)

        mutated = bytearray(image.data)
        table = image.labels["initial_command_table"] - ORIGIN
        mutated[table:table + 4] = (
            mutated[table + 2:table + 4] + mutated[table:table + 2])
        with self.assertRaisesRegex(
                BadcatInitBuildError, "initial_command_table"):
            validate_badcat_init_image(bytes(mutated), image.labels)

        for fragment in FORBIDDEN_COMMAND_FRAGMENTS:
            with self.subTest(fragment=fragment):
                mutated = bytearray(image.data)
                offset = image.labels["response_buffer"] - ORIGIN
                mutated[offset:offset + len(fragment)] = fragment
                with self.assertRaisesRegex(
                        BadcatInitBuildError, "forbidden persistent"):
                    validate_badcat_init_image(bytes(mutated), image.labels)

        wrong_constants = dict(image.labels)
        wrong_constants["UART_DIVISOR_57600"] = 1
        with self.assertRaisesRegex(
                BadcatInitBuildError, "UART_DIVISOR_57600"):
            validate_badcat_init_image(image.data, wrong_constants)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_binary_pins_nonpersistent_transcript_and_atomic_listener(self):
        image = _real_image()
        address_to_name = {
            image.labels[name]: name for name in COMMANDS
        }
        initial = tuple(
            address_to_name[address]
            for address in _read_command_table(image, "initial_command_table")
        )
        listener = tuple(
            address_to_name[address]
            for address in _read_command_table(image, "listener_command_table")
        )
        self.assertEqual(initial, INITIAL_COMMANDS)
        self.assertEqual(listener, LISTENER_COMMANDS)

        commands = {
            name: _read_c_string(image, name) for name in COMMANDS
        }
        self.assertEqual(
            commands["command_bootstrap"], "ATQ0V1E0R1F0")
        self.assertEqual(
            commands["command_b57600"], "ATQ1B57600")
        self.assertEqual(
            commands["command_b115200"], "ATQ1B115200")
        self.assertEqual(
            commands["command_listener_open"], "ATQ0S41=0A6603")
        self.assertEqual(
            commands["command_stream_commit"], "ATHS41=1Q1")
        self.assertNotIn(b"ATQ1\0", image.data)
        self.assertNotIn(b"ATA6603\0", image.data)
        for fragment in FORBIDDEN_COMMAND_FRAGMENTS:
            self.assertNotIn(fragment, image.data.upper())

        common = [
            "ATQ0V1E0R1F0",
            "ATN0",
            "ATS62=0",
            "ATS63=0",
            "ATS0=1",
        ]

        def cold_start_transcript(command_tail):
            failed, divisor = _ParserMachine(image, command_tail).run()
            self.assertFalse(failed)
            transcript = list(common)
            if divisor == 1:
                transcript.extend(("ATQ1B115200", "ATQ0V1E0R1F0"))
            transcript.extend(
                ("ATI2", "ATQ0S41=0A6603", "ATHS41=1Q1"))
            return transcript

        expected_57600 = common + [
            "ATI2", "ATQ0S41=0A6603", "ATHS41=1Q1"]
        self.assertEqual(cold_start_transcript(""), expected_57600)
        self.assertEqual(cold_start_transcript(" /57600 "), expected_57600)
        self.assertEqual(
            cold_start_transcript("/115200"),
            common
            + ["ATQ1B115200", "ATQ0V1E0R1F0", "ATI2",
               "ATQ0S41=0A6603", "ATHS41=1Q1"],
        )

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_option_parser_defaults_to_divisor_two_and_is_strict(self):
        image = _real_image()
        cases = {
            "": (False, 2),
            "   \t": (False, 2),
            "/57600": (False, 2),
            " \t/57600\t ": (False, 2),
            "/115200": (False, 1),
            "  /115200   ": (False, 1),
            "/57500": (True, 2),
            "/9600": (True, 2),
            "/115200 /57600": (True, 2),
            "/115200X": (True, 2),
            "115200": (True, 2),
            "1234567890123456": (True, 2),
        }
        for tail, expected in cases.items():
            with self.subTest(tail=tail):
                self.assertEqual(_ParserMachine(image, tail).run(), expected)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_resident_guard_requires_memman_24_and_exact_tsr(self):
        image = _real_image()
        name = image.labels["memman_tsr_name"]
        start = name - ORIGIN
        self.assertEqual(image.data[start:start + 12], b"MSXAI MCP1  ")

        absent = _ResidentProbeMachine(image).run()
        self.assertTrue(absent[0])
        self.assertEqual(len(absent[2]), 1)

        old = _ResidentProbeMachine(image, (2, 3), resident_id=7).run()
        self.assertTrue(old[0])
        self.assertEqual(len(old[2]), 1)

        missing = _ResidentProbeMachine(image, (2, 4)).run()
        self.assertTrue(missing[0])
        self.assertEqual(len(missing[2]), 2)
        self.assertEqual(missing[2][1], (ord("M"), 62, name))

        found = _ResidentProbeMachine(
            image, (2, 4), resident_id=7).run()
        self.assertFalse(found[0])
        self.assertEqual(found[1], 7)
        self.assertEqual(found[2][1], (ord("M"), 62, name))


class BadcatInitSourceSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "agent" / "msx_badcat_init.asm").read_text(
            encoding="utf-8")

    def section(self, start, end):
        return self.source.split(start, 1)[1].split(end, 1)[0]

    def test_resident_guard_runs_before_any_uart_initialization(self):
        entry = self.section("badinit_start:", "parse_command_line:")
        self.assertLess(
            entry.index("call find_resident_agent"),
            entry.index("call uart_init_57600"),
        )
        probe = self.section("find_resident_agent:", "uart_init_57600:")
        self.assertIn("ld e,MEMMAN_INICHK", probe)
        self.assertIn("ld e,MEMMAN_GET_TSR_ID", probe)
        self.assertIn("ld hl,memman_tsr_name", probe)

    def test_receive_fifo_is_drained_before_console_output_or_halt(self):
        fifo = self.section(
            "response_drain_fifo:", "response_no_byte_or_error:")
        self.assertLess(
            fifo.index("in a,(UART_LSR)"), fifo.index("in a,(UART_DATA)"))
        self.assertLess(
            fifo.index("in a,(UART_DATA)"),
            fifo.index("call response_store_character"),
        )
        self.assertNotIn("print_character", fifo)
        self.assertNotIn("print_response_buffer", fifo)
        self.assertNotIn("call BDOS", fifo)
        self.assertNotIn("halt", fifo)

        idle = self.section(
            "response_no_byte_or_error:", "response_succeeded:")
        self.assertIn("halt", idle)
        outcomes = self.section("response_succeeded:", "response_reset:")
        self.assertEqual(outcomes.count("call print_response_buffer"), 1)
        self.assertEqual(outcomes.count("call diagnostic_report_once"), 5)

    def test_baud_transition_waits_for_temt_before_switching_divisor(self):
        wait_empty = self.section("uart_wait_empty:", "drain_input:")
        self.assertIn("and 040h", wait_empty)

        transition = self.section(
            "change_runtime_baud:", "change_runtime_failed_pop:")
        operations = (
            "call send_command",
            "call uart_wait_empty",
            "call uart_set_baud",
            "call wait_ticks",
            "call drain_input",
            "jp run_visible_command",
        )
        positions = [transition.index(operation) for operation in operations]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("ld b,BAUD_SETTLE_TICKS", transition)
        self.assertIn("ld hl,command_bootstrap", transition)
        self.assertIn('db "ATQ1B115200",0', self.source)
        self.assertIn('db "ATQ1B57600",0', self.source)

        restore = self.section(
            "restore_visible_57600:", "; --------------------------------------------------------------- AT responses")
        self.assertLess(
            restore.index("call uart_set_baud"),
            restore.index("call wait_ticks"),
        )

    def test_uart_initialization_matches_badcat_reference_order(self):
        initialization = self.section("uart_init_57600:", "uart_set_baud:")
        operations = (
            "di",
            "ld a,00Dh",
            "out (UART_MCR),a",
            "ld a,087h",
            "out (UART_FCR),a",
            "call uart_set_baud_raw",
            "ld a,02Fh",
            "ei",
        )
        positions = [initialization.index(item) for item in operations]
        self.assertEqual(positions, sorted(positions))
        missing = initialization.split("uart_init_missing:", 1)[1]
        self.assertIn("ei", missing)

        runtime = self.section("uart_set_baud:", "uart_write:")
        runtime_operations = (
            "di",
            "call uart_set_baud_raw",
            "ei",
            "ret",
            "uart_set_baud_raw:",
        )
        positions = [runtime.index(item) for item in runtime_operations]
        self.assertEqual(positions, sorted(positions))

    def test_uart_errors_cannot_validate_a_response(self):
        fifo = self.section("response_drain_fifo:", "response_no_byte:")
        self.assertLess(
            fifo.index("cp 0FFh"), fifo.index("and LSR_ERROR_MASK"))
        self.assertIn("response_no_byte_or_error:", fifo)
        self.assertIn("jr nz,response_drain_fifo", fifo)
        self.assertIn("jp nc,response_line_failed", fifo)
        success_check = fifo.split("call response_store_character", 1)[1]
        self.assertLess(
            success_check.index("response_lsr_errors"),
            success_check.index("cp 1"),
        )
        self.assertLess(
            success_check.index("jr nz,response_drain_fifo"),
            success_check.index("ld (response_pending),a"),
        )
        quiet_refresh = success_check.split(
            "response_received_not_pending:", 1)[0]
        self.assertIn("ld a,(response_pending)", quiet_refresh)
        self.assertIn("ld a,(response_lsr_errors)", quiet_refresh)
        self.assertIn("ld (response_pending_start),a", quiet_refresh)
        receive = self.section(
            "response_drain_fifo:", "response_no_byte_or_error:")
        self.assertIn("ld de,RESPONSE_STREAM_LIMIT", receive)
        self.assertIn("jp response_stream_failed", receive)
        self.assertNotIn("response_timed_out", receive)
        lsr_transition = receive.split("and LSR_ERROR_MASK", 1)[1].split(
            "response_accumulate_lsr_errors:", 1)[0]
        self.assertIn("ld (response_pending_start),a", lsr_transition)
        self.assertIn("response_pending_wait:", fifo)
        line_settle = fifo.split("response_no_byte_or_error:", 1)[1]
        self.assertIn("call response_quiet_elapsed", line_settle)
        self.assertLess(
            line_settle.index("call response_quiet_elapsed"),
            line_settle.index("response_line_failed"),
        )
        quiet = self.section(
            "response_quiet_elapsed:", "response_no_byte:")
        self.assertIn("cp RESPONSE_QUIET_TICKS", quiet)
        self.assertLess(
            fifo.index("ld (response_pending),a"),
            fifo.index("response_succeeded"),
        )

        first_probe = self.section(
            "ld hl,stage_probe_57600", "badinit_retry_115200:")
        self.assertIn("cp RESPONSE_TIMEOUT_ERROR", first_probe)
        self.assertIn("cp RESPONSE_LINE_ERROR", first_probe)
        self.assertNotIn("cp RESPONSE_UART_ERROR", first_probe)

        deadline = self.section(
            "response_check_deadline:", "response_succeeded:")
        self.assertIn("cp RESPONSE_TIMEOUT", deadline)
        self.assertIn("ret", deadline)

    def test_failures_are_hex_diagnostic_not_raw_console_bytes(self):
        outcomes = self.section("response_succeeded:", "response_reset:")
        self.assertEqual(outcomes.count("call print_response_buffer"), 1)
        for label in (
                "response_timed_out:",
                "response_modem_failed:",
                "response_line_failed:",
                "response_uart_failed:",
                "response_stream_failed:"):
            failure = outcomes.split(label, 1)[1].split("ret", 1)[0]
            self.assertIn("call diagnostic_report_once", failure)
            self.assertNotIn("print_response_buffer", failure)

        diagnostic = self.section(
            "diagnostic_report_once:", "print_response_sample_hex:")
        self.assertIn("call print_hex_word", diagnostic)
        self.assertGreaterEqual(diagnostic.count("call print_hex_byte"), 2)
        self.assertIn("response_lsr_errors", diagnostic)
        self.assertIn("ld a,(current_divisor)", diagnostic)
        self.assertIn("message_diagnostic_baud_57600", diagnostic)
        self.assertIn("message_diagnostic_baud_115200", diagnostic)
        command = diagnostic.split("ld hl,(diagnostic_command)", 1)[1]
        self.assertLess(command.index("push hl"), command.index("call print_string"))
        self.assertLess(command.index("call print_string"), command.index("pop hl"))

        sample = self.section(
            "print_response_sample_hex:", "print_response_sample_start:")
        self.assertIn("ld de,message_none", sample)
        self.assertIn('message_none:\n    db " --$"', self.source)

    def test_listener_commit_failure_never_writes_uart(self):
        handler = self.section(
            "badinit_listener_commit_failed:", "badinit_usage:")
        self.assertIn("call diagnostic_report_once", handler)
        for forbidden in (
                "restore_visible_57600", "send_command", "uart_write",
                "uart_wait_empty", " in ", " out "):
            self.assertNotIn(forbidden, handler.lower())

        generic_failure = self.section(
            "badinit_failed:", "badinit_listener_commit_failed:")
        self.assertIn("cp RESPONSE_STREAM_ERROR", generic_failure)
        self.assertLess(
            generic_failure.index("cp RESPONSE_STREAM_ERROR"),
            generic_failure.index("call restore_visible_57600"),
        )

    def test_final_listener_is_atomic_send_only_and_last(self):
        opening = self.section("badinit_open_listener:", "badinit_quiet:")
        self.assertIn("ld hl,command_listener_open", opening)
        self.assertIn("call run_visible_command", opening)

        quiet = self.section("badinit_quiet:", "badinit_failed:")
        self.assertIn("ld hl,command_stream_commit", quiet)
        self.assertLess(
            quiet.index("call send_command"),
            quiet.index("call uart_wait_empty"),
        )
        self.assertLess(
            quiet.index("call uart_wait_empty"), quiet.index("call wait_ticks"))
        self.assertIn("ld b,LISTENER_SETTLE_TICKS", quiet)
        self.assertNotIn("run_visible_command", quiet)
        self.assertNotIn("drain_input", quiet)
        self.assertIn('db "ATQ0S41=0A6603",0', self.source)
        self.assertIn('db "ATHS41=1Q1",0', self.source)
        self.assertNotIn('db "ATQ1",0', self.source)
        self.assertNotIn('db "ATA6603",0', self.source)


if __name__ == "__main__":
    unittest.main()
