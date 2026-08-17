import pathlib
import shutil
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

from tools.build_port_helper import (  # noqa: E402
    ORIGIN,
    TSR_NAME,
    PortHelperBuildError,
    assemble_port_helper,
    build_port_helper,
    parse_labels,
    validate_port_helper_image,
)


Z_FLAG = 0x40
C_FLAG = 0x01


class _PortParserMachine:
    """Small deterministic Z80 subset for MP.COM's actual parser."""

    def __init__(self, image, command_tail):
        tail = command_tail.encode("ascii")
        if len(tail) > 127:
            raise ValueError("test command tail is too long")
        self.memory = bytearray(65536)
        self.memory[ORIGIN:ORIGIN + len(image.data)] = image.data
        self.memory[0x80] = len(tail)
        self.memory[0x81:0x81 + len(tail)] = tail
        self.labels = image.labels
        self.a = self.b = self.c = self.d = self.e = 0
        self.h = self.l = self.f = 0
        self.ix = 0
        self.pc = image.labels["parse_port_argument"]
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

    @property
    def bc(self):
        return self.b << 8 | self.c

    @bc.setter
    def bc(self, value):
        value &= 0xFFFF
        self.b, self.c = value >> 8, value & 0xFF

    def fetch(self):
        value = self.memory[self.pc]
        self.pc = self.pc + 1 & 0xFFFF
        return value

    def fetch_word(self):
        return self.fetch() | self.fetch() << 8

    def logic(self, value):
        value &= 0xFF
        self.a = value
        self.f = Z_FLAG if value == 0 else 0

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

    def step(self):
        address = self.pc
        opcode = self.fetch()
        if opcode == 0x3A:  # LD A,(nn)
            self.a = self.memory[self.fetch_word()]
        elif opcode == 0x32:  # LD (nn),A
            self.memory[self.fetch_word()] = self.a
        elif opcode == 0x21:  # LD HL,nn
            self.hl = self.fetch_word()
        elif opcode == 0x01:  # LD BC,nn
            self.bc = self.fetch_word()
        elif opcode == 0x11:  # LD DE,nn
            self.de = self.fetch_word()
        elif opcode == 0x16:  # LD D,n
            self.d = self.fetch()
        elif opcode == 0x3E:  # LD A,n
            self.a = self.fetch()
        elif opcode == 0x7E:  # LD A,(HL)
            self.a = self.memory[self.hl]
        elif opcode == 0x5F:  # LD E,A
            self.e = self.a
        elif opcode == 0x7B:  # LD A,E
            self.a = self.e
        elif opcode == 0x60:  # LD H,B
            self.h = self.b
        elif opcode == 0x69:  # LD L,C
            self.l = self.c
        elif opcode == 0x54:  # LD D,H
            self.d = self.h
        elif opcode == 0x5D:  # LD E,L
            self.e = self.l
        elif opcode == 0x44:  # LD B,H
            self.b = self.h
        elif opcode == 0x4D:  # LD C,L
            self.c = self.l
        elif opcode == 0x78:  # LD A,B
            self.a = self.b
        elif opcode == 0x79:  # LD A,C
            self.a = self.c
        elif opcode == 0x7D:  # LD A,L
            self.a = self.l
        elif opcode == 0x23:  # INC HL
            self.hl += 1
        elif opcode == 0x3C:  # INC A (carry preserved)
            carry = self.f & C_FLAG
            self.a = self.a + 1 & 0xFF
            self.f = carry | (Z_FLAG if self.a == 0 else 0)
        elif opcode == 0x3D:  # DEC A (carry preserved)
            carry = self.f & C_FLAG
            self.a = self.a - 1 & 0xFF
            self.f = carry | (Z_FLAG if self.a == 0 else 0)
        elif opcode == 0x29:  # ADD HL,HL
            result = self.hl * 2
            self.f = ((self.f & Z_FLAG)
                      | (C_FLAG if result > 0xFFFF else 0))
            self.hl = result
        elif opcode == 0x19:  # ADD HL,DE
            result = self.hl + self.de
            self.f = ((self.f & Z_FLAG)
                      | (C_FLAG if result > 0xFFFF else 0))
            self.hl = result
        elif opcode == 0xFE:  # CP n
            self.compare(self.fetch())
        elif opcode == 0xD6:  # SUB n
            value = self.fetch()
            result = self.a - value
            self.a = result & 0xFF
            self.f = ((Z_FLAG if self.a == 0 else 0)
                      | (C_FLAG if result < 0 else 0))
        elif opcode == 0xC6:  # ADD A,n
            result = self.a + self.fetch()
            self.a = result & 0xFF
            self.f = ((Z_FLAG if self.a == 0 else 0)
                      | (C_FLAG if result > 0xFF else 0))
        elif opcode == 0xE6:  # AND n
            self.logic(self.a & self.fetch())
        elif opcode == 0xA1:  # AND C
            self.logic(self.a & self.c)
        elif opcode == 0xB1:  # OR C
            self.logic(self.a | self.c)
        elif opcode == 0xB7:  # OR A
            self.logic(self.a)
        elif opcode == 0xAF:  # XOR A
            self.logic(0)
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
        elif opcode == 0xCA:  # JP Z,nn
            target = self.fetch_word()
            if self.f & Z_FLAG:
                self.pc = target
        elif opcode == 0xC2:  # JP NZ,nn
            target = self.fetch_word()
            if not self.f & Z_FLAG:
                self.pc = target
        elif opcode == 0xDA:  # JP C,nn
            target = self.fetch_word()
            if self.f & C_FLAG:
                self.pc = target
        elif opcode == 0xC3:  # JP nn
            self.pc = self.fetch_word()
        elif opcode == 0xCD:  # CALL nn
            target = self.fetch_word()
            self.push(self.pc)
            self.pc = target
        elif opcode == 0xE5:  # PUSH HL
            self.push(self.hl)
        elif opcode == 0xE1:  # POP HL
            self.hl = self.pop()
        elif opcode == 0xC5:  # PUSH BC
            self.push(self.bc)
        elif opcode == 0xC1:  # POP BC
            self.bc = self.pop()
        elif opcode == 0x4F:  # LD C,A
            self.c = self.a
        elif opcode == 0x37:  # SCF
            self.f |= C_FLAG
        elif opcode == 0xC8:  # RET Z
            if self.f & Z_FLAG:
                self.pc = self.pop()
        elif opcode == 0xC9:  # RET
            self.pc = self.pop()
        elif opcode == 0xDD:
            extension = self.fetch()
            if extension == 0x21:  # LD IX,nn
                self.ix = self.fetch_word()
            elif extension == 0x23:  # INC IX
                self.ix = self.ix + 1 & 0xFFFF
            elif extension == 0x77:  # LD (IX+d),A
                displacement = self.fetch()
                if displacement & 0x80:
                    displacement -= 0x100
                self.memory[self.ix + displacement & 0xFFFF] = self.a
            else:
                self.fail_opcode(address, opcode, extension)
        elif opcode == 0xED:
            extension = self.fetch()
            if extension == 0x43:  # LD (nn),BC
                destination = self.fetch_word()
                self.memory[destination] = self.c
                self.memory[destination + 1] = self.b
            elif extension == 0x52:  # SBC HL,DE
                result = self.hl - self.de - bool(self.f & C_FLAG)
                self.hl = result
                self.f = ((Z_FLAG if self.hl == 0 else 0)
                          | (C_FLAG if result < 0 else 0))
            else:
                self.fail_opcode(address, opcode, extension)
        else:
            self.fail_opcode(address, opcode)

    @staticmethod
    def fail_opcode(address, *opcodes):
        encoded = " ".join(f"{opcode:02X}" for opcode in opcodes)
        raise AssertionError(
            f"unsupported parser opcode at {address:04X}: {encoded}")

    def run(self):
        for _ in range(4096):
            if self.pc == self.sentinel:
                break
            self.step()
        else:
            raise AssertionError("port parser did not return")
        value_address = self.labels["port_value"]
        value = (self.memory[value_address]
                 | self.memory[value_address + 1] << 8)
        return bool(self.f & C_FLAG), value


class PortHelperTest(unittest.TestCase):
    def test_label_parser_rejects_duplicates(self):
        self.assertEqual(
            parse_labels("port_helper_start: equ $0100\n"),
            {"port_helper_start": ORIGIN},
        )
        with self.assertRaisesRegex(PortHelperBuildError, "duplicate label"):
            parse_labels(
                "port_helper_start: equ $0100\n"
                "port_helper_start: equ $0200\n")

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_real_build_is_minimal_deterministic_and_matches_loader_size(self):
        first = assemble_port_helper()
        second = assemble_port_helper()
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(len(first.data), 678)
        self.assertLess(first.labels["port_helper_end"], 0x4000)

        loader = (ROOT / "agent" / "msx_memman_loader.asm").read_text(
            encoding="utf-8")
        self.assertIn("MP_FILE_SIZE:            equ 002A6h", loader)

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "MP.COM"
            published = build_port_helper(output=output)
            self.assertEqual(output.read_bytes(), first.data)
            self.assertEqual(published.data, first.data)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_validator_pins_tsr_identifier(self):
        image = assemble_port_helper()
        self.assertEqual(
            image.data[
                image.labels["memman_tsr_name"] - ORIGIN:
                image.labels["memman_tsr_name_end"] - ORIGIN],
            TSR_NAME,
        )
        mutated = bytearray(image.data)
        mutated[image.labels["memman_tsr_name"] - ORIGIN] ^= 1
        with self.assertRaisesRegex(PortHelperBuildError, "MSXAI MCP1"):
            validate_port_helper_image(bytes(mutated), image.labels)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_z80_parser_accepts_public_range_and_private_hex(self):
        image = assemble_port_helper()
        cases = {
            "1": 1,
            "00001": 1,
            "6603": 6603,
            "/0001": 1,
            "/19CB": 6603,
            "/A873": 43123,
            "/a873": 43123,
            "/FFFE": 65534,
            "  12345\t ": 12345,
            "65534": 65534,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                carry, value = _PortParserMachine(
                    image, command).run()
                self.assertFalse(carry)
                self.assertEqual(value, expected)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_z80_parser_rejects_missing_invalid_or_extra_values(self):
        image = assemble_port_helper()
        for command in (
                "", "   \t", "0", "00000", "65535", "65536", "99999",
                "/", "//1", "/0000", "/FFFF", "/001", "/00001",
                "/GGGG", "-1", "+1", "abc",
                "6603 extra", "1 2", "000001"):
            with self.subTest(command=command):
                carry, _ = _PortParserMachine(image, command).run()
                self.assertTrue(carry)

    def test_helper_calls_exact_memman_selected_port_abi(self):
        source = (ROOT / "agent" / "msx_port_helper.asm").read_text(
            encoding="utf-8")
        entry = source.split("port_helper_start:", 1)[1].split(
            "port_helper_bad_version:", 1)[0]
        self.assertLess(entry.index("call find_memman_agent"),
                        entry.index("ld hl,(port_value)"))
        self.assertLess(entry.index("ld hl,(port_value)"),
                        entry.index("ld a,MSXAI_TALK_UNAPI_PORT"))
        self.assertIn("ld d,'M'", entry)
        self.assertIn("ld e,MEMMAN_TSR_CALL", entry)
        self.assertIn("cp MSXAI_TRANSPORT_UNAPI", entry)

        discovery = source.split("find_memman_agent:", 1)[1].split(
            "memman_tsr_name:", 1)[0]
        self.assertIn("ld e,MEMMAN_INICHK", discovery)
        self.assertIn("cp MEMMAN_MINIMUM_MAJOR", discovery)
        self.assertIn("cp MEMMAN_MINIMUM_MINOR", discovery)
        self.assertIn("ld e,MEMMAN_GET_TSR_ID", discovery)
        self.assertIn("ld c,DOS_TERM_ERROR", source)
        self.assertIn("MP: MSXAI UNAPI relisten failed.", source)

    def test_first_tsr_unapi_open_is_deferred_until_helper_tsr_call(self):
        transport = (ROOT / "agent" / "transports" /
                     "msx_transport_unapi.inc").read_text(encoding="utf-8")
        core = (ROOT / "agent" / "msx_agent_core.asm").read_text(
            encoding="utf-8")

        initialize = transport.split("unapi_init_inner:", 1)[1].split(
            "; Explicit-port and rollback helper", 1)[0]
        self.assertLess(initialize.index("call unapi_prepare_current_port"),
                        initialize.index("ld a,(unapi_defer_first_open)"))
        deferred = initialize.split(
            "ld a,(unapi_defer_first_open)", 1)[1].split(
                "unapi_init_open_now:", 1)[0]
        self.assertIn("ld (unapi_defer_first_open),a", deferred)
        self.assertIn("ret", deferred)
        self.assertIn("jp unapi_open_listener", initialize)

        explicit = transport.split(
            "unapi_init_current_port_inner:", 1)[1].split(
                "unapi_prepare_current_port:", 1)[0]
        self.assertIn("call unapi_prepare_current_port", explicit)
        self.assertIn("jp unapi_open_listener", explicit)
        self.assertNotIn("unapi_defer_first_open", explicit)
        preparation = transport.split(
            "unapi_prepare_current_port:", 1)[1].split(
                "unapi_restore:", 1)[0]
        self.assertIn("call unapi_discover", preparation)

        runtime = transport.split("unapi_runtime_start:", 1)[1].split(
            "unapi_runtime_end:", 1)[0]
        self.assertNotIn("unapi_defer_first_open:", runtime)
        after_runtime = transport.split("unapi_runtime_end:", 1)[1]
        self.assertIn("unapi_defer_first_open:\n    db 0", after_runtime)

        resident = core.split("resident_initialize:", 1)[1].split(
            "tsr_memman_entry:", 1)[0]
        self.assertLess(resident.index("call transport_init"),
                        resident.index("ld (unapi_defer_first_open),a"))

        tsr_init = core.split("tsr_init:", 1)[1].split(
            "tsr_init_failed:", 1)[0]
        self.assertLess(tsr_init.index("ld (unapi_defer_first_open),a"),
                        tsr_init.index("call resident_initialize"))


if __name__ == "__main__":
    unittest.main()
