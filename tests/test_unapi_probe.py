import pathlib
import shutil
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

from tools.build_unapi_probe import (  # noqa: E402
    API_ID,
    DEFAULT_LISTENER_PORT,
    ORIGIN,
    TCP_OPEN_PARAMS,
    UnapiProbeBuildError,
    assemble_probe,
    build_probe,
    parse_labels,
    validate_probe_image,
)


Z_FLAG = 0x40
C_FLAG = 0x01


class _PortParserMachine:
    """Tiny deterministic Z80 subset for the probe's actual port routine."""

    def __init__(self, image, command_tail):
        tail = command_tail.encode("ascii")
        if len(tail) > 127:
            raise ValueError("test command tail is too long")
        self.memory = bytearray(65536)
        self.memory[ORIGIN:ORIGIN + len(image.data)] = image.data
        self.memory[0x80] = len(tail)
        self.memory[0x81:0x81 + len(tail)] = tail
        if 0x81 + len(tail) < ORIGIN:
            self.memory[0x81 + len(tail)] = 13
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
        return (self.h << 8) | self.l

    @hl.setter
    def hl(self, value):
        value &= 0xFFFF
        self.h, self.l = value >> 8, value & 0xFF

    @property
    def de(self):
        return (self.d << 8) | self.e

    @de.setter
    def de(self, value):
        value &= 0xFFFF
        self.d, self.e = value >> 8, value & 0xFF

    @property
    def bc(self):
        return (self.b << 8) | self.c

    @bc.setter
    def bc(self, value):
        value &= 0xFFFF
        self.b, self.c = value >> 8, value & 0xFF

    def fetch(self):
        value = self.memory[self.pc]
        self.pc = (self.pc + 1) & 0xFFFF
        return value

    def fetch_word(self):
        low = self.fetch()
        return low | (self.fetch() << 8)

    def set_logic_flags(self, value):
        self.f = Z_FLAG if (value & 0xFF) == 0 else 0

    def compare(self, value):
        result = self.a - value
        self.f = ((Z_FLAG if (result & 0xFF) == 0 else 0)
                  | (C_FLAG if result < 0 else 0))

    def push(self, value):
        self.sp = (self.sp - 1) & 0xFFFF
        self.memory[self.sp] = (value >> 8) & 0xFF
        self.sp = (self.sp - 1) & 0xFFFF
        self.memory[self.sp] = value & 0xFF

    def pop(self):
        low = self.memory[self.sp]
        self.sp = (self.sp + 1) & 0xFFFF
        high = self.memory[self.sp]
        self.sp = (self.sp + 1) & 0xFFFF
        return low | (high << 8)

    def relative(self, condition):
        displacement = self.fetch()
        if displacement & 0x80:
            displacement -= 0x100
        if condition:
            self.pc = (self.pc + displacement) & 0xFFFF

    def ret(self):
        self.pc = self.pop()

    def step(self):
        address = self.pc
        opcode = self.fetch()

        if opcode == 0x21:  # LD HL,nn
            self.hl = self.fetch_word()
        elif opcode == 0x22:  # LD (nn),HL
            destination = self.fetch_word()
            self.memory[destination] = self.l
            self.memory[(destination + 1) & 0xFFFF] = self.h
        elif opcode == 0x32:  # LD (nn),A
            self.memory[self.fetch_word()] = self.a
        elif opcode == 0x11:  # LD DE,nn
            self.de = self.fetch_word()
        elif opcode == 0x3A:  # LD A,(nn)
            self.a = self.memory[self.fetch_word()]
        elif opcode == 0x3E:  # LD A,n
            self.a = self.fetch()
        elif opcode == 0x06:  # LD B,n
            self.b = self.fetch()
        elif opcode == 0x26:  # LD H,n
            self.h = self.fetch()
        elif opcode == 0x6F:  # LD L,A
            self.l = self.a
        elif opcode == 0x4F:  # LD C,A
            self.c = self.a
        elif opcode == 0x54:  # LD D,H
            self.d = self.h
        elif opcode == 0x5D:  # LD E,L
            self.e = self.l
        elif opcode == 0x79:  # LD A,C
            self.a = self.c
        elif opcode == 0x78:  # LD A,B
            self.a = self.b
        elif opcode == 0x7C:  # LD A,H
            self.a = self.h
        elif opcode == 0x7D:  # LD A,L
            self.a = self.l
        elif opcode == 0x19:  # ADD HL,DE
            result = self.hl + self.de
            self.f = (self.f & Z_FLAG) | (C_FLAG if result > 0xFFFF else 0)
            self.hl = result
        elif opcode == 0x29:  # ADD HL,HL
            result = self.hl * 2
            self.f = (self.f & Z_FLAG) | (C_FLAG if result > 0xFFFF else 0)
            self.hl = result
        elif opcode == 0x81:  # ADD A,C
            result = self.a + self.c
            self.a = result & 0xFF
            self.f = ((Z_FLAG if self.a == 0 else 0)
                      | (C_FLAG if result > 0xFF else 0))
        elif opcode == 0x24:  # INC H (carry is preserved)
            carry = self.f & C_FLAG
            self.h = (self.h + 1) & 0xFF
            self.f = carry | (Z_FLAG if self.h == 0 else 0)
        elif opcode == 0x3D:  # DEC A (carry is preserved)
            carry = self.f & C_FLAG
            self.a = (self.a - 1) & 0xFF
            self.f = carry | (Z_FLAG if self.a == 0 else 0)
        elif opcode == 0xFE:  # CP n
            self.compare(self.fetch())
        elif opcode == 0xD6:  # SUB n
            value = self.fetch()
            result = self.a - value
            self.a = result & 0xFF
            self.f = ((Z_FLAG if self.a == 0 else 0)
                      | (C_FLAG if result < 0 else 0))
        elif opcode == 0xB7:  # OR A
            self.set_logic_flags(self.a)
        elif opcode == 0xB5:  # OR L
            self.a |= self.l
            self.set_logic_flags(self.a)
        elif opcode == 0xAF:  # XOR A
            self.a = 0
            self.set_logic_flags(self.a)
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
        elif opcode == 0xC8:  # RET Z
            if self.f & Z_FLAG:
                self.ret()
        elif opcode == 0xC9:  # RET
            self.ret()
        elif opcode == 0xC5:  # PUSH BC
            self.push(self.bc)
        elif opcode == 0xC1:  # POP BC
            self.bc = self.pop()
        elif opcode == 0xE5:  # PUSH HL
            self.push(self.hl)
        elif opcode == 0xE1:  # POP HL
            self.hl = self.pop()
        elif opcode == 0xDD:
            extension = self.fetch()
            if extension == 0x21:  # LD IX,nn
                self.ix = self.fetch_word()
            elif extension == 0x23:  # INC IX
                self.ix = (self.ix + 1) & 0xFFFF
            elif extension == 0x7E:  # LD A,(IX+d)
                displacement = self.fetch()
                if displacement & 0x80:
                    displacement -= 0x100
                self.a = self.memory[(self.ix + displacement) & 0xFFFF]
            else:
                self.fail_opcode(address, opcode, extension)
        elif opcode == 0xED:
            extension = self.fetch()
            if extension != 0x52:  # SBC HL,DE
                self.fail_opcode(address, opcode, extension)
            carry = 1 if self.f & C_FLAG else 0
            result = self.hl - self.de - carry
            self.hl = result
            self.f = ((Z_FLAG if self.hl == 0 else 0)
                      | (C_FLAG if result < 0 else 0))
        else:
            self.fail_opcode(address, opcode)

    @staticmethod
    def fail_opcode(address, *opcodes):
        encoded = " ".join(f"{opcode:02X}" for opcode in opcodes)
        raise AssertionError(f"unsupported parser opcode at {address:04X}: {encoded}")

    def run(self):
        for _ in range(4096):
            if self.pc == self.sentinel:
                break
            self.step()
        else:
            raise AssertionError("port parser did not return")

        listener = self.labels["listener_port"]
        params = self.labels["tcp_open_params"] + 6
        listener_value = self.memory[listener] | self.memory[listener + 1] << 8
        params_value = self.memory[params] | self.memory[params + 1] << 8
        return self.a, listener_value, params_value


class UnapiProbeBuilderTest(unittest.TestCase):
    def test_label_parser_rejects_duplicates(self):
        self.assertEqual(
            parse_labels("probe_start:\tequ $0100\n"),
            {"probe_start": 0x0100},
        )
        with self.assertRaisesRegex(UnapiProbeBuildError, "duplicate label"):
            parse_labels(
                "probe_start: equ $0100\n"
                "probe_start: equ $0200\n")

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_real_build_is_page_zero_and_deterministic(self):
        first = assemble_probe()
        second = assemble_probe()
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.labels["probe_start"], ORIGIN)
        self.assertEqual(first.labels["probe_end"], ORIGIN + len(first.data))
        self.assertLess(first.labels["probe_end"], 0x4000)
        self.assertEqual(
            first.data[
                first.labels["api_id"] - ORIGIN:
                first.labels["api_id_end"] - ORIGIN],
            API_ID,
        )
        self.assertEqual(
            first.data[
                first.labels["tcp_open_params"] - ORIGIN:
                first.labels["tcp_open_params_end"] - ORIGIN],
            TCP_OPEN_PARAMS,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "UNAPIPRB.COM"
            published = build_probe(output=output)
            self.assertEqual(output.read_bytes(), first.data)
            self.assertEqual(published.data, first.data)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_validator_rejects_changed_passive_open_flags(self):
        image = assemble_probe()
        mutated = bytearray(image.data)
        flags = image.labels["tcp_open_params"] - ORIGIN + 10
        mutated[flags] = 0x01
        with self.assertRaisesRegex(UnapiProbeBuildError, "TCP_OPEN block"):
            validate_probe_image(bytes(mutated), image.labels)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_z80_parser_accepts_default_and_full_port_range(self):
        image = assemble_probe()
        cases = {
            "": DEFAULT_LISTENER_PORT,
            "   \t": DEFAULT_LISTENER_PORT,
            "1": 1,
            "00001": 1,
            "6603": 6603,
            "  12345\t ": 12345,
            "65534": 65534,
            "1" + " " * 126: 1,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                error, listener, params = _PortParserMachine(
                    image, command).run()
                self.assertEqual(error, 0)
                self.assertEqual(listener, expected)
                self.assertEqual(params, expected)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_z80_parser_rejects_invalid_or_extra_arguments(self):
        image = assemble_probe()
        for command in (
            "0", "00000", "65535", "65536", "99999", "-1", "+1", "abc",
            "6603 extra", "1 2",
        ):
            with self.subTest(command=command):
                error, listener, params = _PortParserMachine(
                    image, command).run()
                self.assertEqual(error, 1)
                self.assertEqual(listener, DEFAULT_LISTENER_PORT)
                self.assertEqual(params, DEFAULT_LISTENER_PORT)

    def test_probe_remains_independent_from_agent_core(self):
        source = (ROOT / "agent" / "msx_unapi_probe.asm").read_text(
            encoding="ascii")
        self.assertNotIn("include", source.lower())
        self.assertIn('db "TCP/IP",0', source)
        self.assertIn("ld (tcp_open_params+6),hl", source)
        self.assertIn("ld a,TCPIP_TCP_OPEN", source)
        self.assertIn("ld a,TCPIP_TCP_STATE", source)
        self.assertIn("ld a,TCPIP_TCP_ABORT", source)

        selector = source.split(
            "select_compatible_implementation:", 1)[1].split(
                "execute_unapi:", 1)[0]
        self.assertIn("ld a,(implementation_index)", selector)
        self.assertIn("and PASSIVE_ANY_CAPABILITY", selector)
        self.assertIn("call implementation_is_callable", selector)
        self.assertIn("call copy_tcpip_api_id", selector)
        self.assertIn("jr nz,select_reject_get_info", selector)

        callable_check = source.split(
            "implementation_is_callable:", 1)[1].split(
                "execute_unapi:", 1)[0]
        self.assertIn("cp 0C0h", callable_check)
        self.assertNotIn("and 080h", callable_check)

        dispatcher = source.split("execute_unapi:", 1)[1].split(
            "read_implementation_byte:", 1)[0]
        self.assertIn("cp 0C0h", dispatcher)
        self.assertNotIn("and 080h", dispatcher)

        ram_call = source.split("execute_unapi_ram:", 1)[1].split(
            "read_implementation_byte:", 1)[0]
        self.assertIn("ld hl,(ram_helper)", ram_call)
        self.assertIn("ld a,ERR_NOT_IMP", ram_call)


if __name__ == "__main__":
    unittest.main()
