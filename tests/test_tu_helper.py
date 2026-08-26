import pathlib
import shutil
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

from tools.build_tu_helper import (  # noqa: E402
    API_ID,
    ORIGIN,
    OVERLAY_STUB,
    PICO_HOOK_HARDENER,
    PICO_HOOK_PREPARER,
    TuHelperBuildError,
    assemble_tu_helper,
    build_tu_helper,
    parse_labels,
    validate_tu_helper_image,
)


Z_FLAG = 0x40
H_TIMI = 0xFD9F


class _HardenerMachine:
    """Minimal Z80 executor for TU.COM's actual H.TIMI hardener bytes."""

    def __init__(self, image, hook, label="harden_pico_htimi"):
        if len(hook) != 5:
            raise ValueError("H.TIMI hook must contain exactly five bytes")
        self.memory = bytearray(65536)
        self.memory[ORIGIN:ORIGIN + len(image.data)] = image.data
        self.memory[H_TIMI:H_TIMI + 5] = hook
        self.pc = image.labels[label]
        self.sp = 0xEFFE
        self.sentinel = 0xBEEF
        self.memory[self.sp] = self.sentinel & 0xFF
        self.memory[self.sp + 1] = self.sentinel >> 8
        self.a = 0
        self.f = 0

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

    def step(self):
        address = self.pc
        opcode = self.fetch()
        if opcode == 0x3A:  # LD A,(nn)
            self.a = self.memory[self.fetch_word()]
        elif opcode == 0xFE:  # CP n
            self.f = Z_FLAG if self.a == self.fetch() else 0
        elif opcode == 0xC0:  # RET NZ
            if not self.f & Z_FLAG:
                self.pc = self.pop()
        elif opcode == 0x3E:  # LD A,n
            self.a = self.fetch()
        elif opcode == 0x32:  # LD (nn),A
            self.memory[self.fetch_word()] = self.a
        elif opcode == 0xC9:  # RET
            self.pc = self.pop()
        else:
            self.fail_opcode(address, opcode)

    @staticmethod
    def fail_opcode(address, opcode):
        raise AssertionError(
            f"unsupported hardener opcode at {address:04X}: {opcode:02X}")

    def run(self):
        for _ in range(32):
            if self.pc == self.sentinel:
                return bytes(self.memory[H_TIMI:H_TIMI + 5])
            self.step()
        raise AssertionError("Pico H.TIMI hardener did not return")


class TuHelperTest(unittest.TestCase):
    def test_label_parser_rejects_duplicates(self):
        self.assertEqual(
            parse_labels("tu_helper_start: equ $0100\n"),
            {"tu_helper_start": ORIGIN},
        )
        with self.assertRaisesRegex(TuHelperBuildError, "duplicate label"):
            parse_labels(
                "tu_helper_start: equ $0100\n"
                "tu_helper_start: equ $0200\n")

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_real_build_is_small_deterministic_and_atomic(self):
        first = assemble_tu_helper()
        second = assemble_tu_helper()
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(len(first.data), 749)
        self.assertLess(first.labels["tu_helper_end"], 0x4000)

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "TU.COM"
            published = build_tu_helper(output=output)
            self.assertEqual(output.read_bytes(), first.data)
            self.assertEqual(published.data, first.data)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_validator_pins_identifier_hardener_and_overlay_stub(self):
        image = assemble_tu_helper()
        api = image.labels["tcpip_api_id"] - ORIGIN
        self.assertEqual(image.data[api:api + len(API_ID)], API_ID)

        hardener = image.labels["harden_pico_htimi"] - ORIGIN
        self.assertEqual(
            image.data[hardener:hardener + len(PICO_HOOK_HARDENER)],
            PICO_HOOK_HARDENER,
        )
        mutated = bytearray(image.data)
        mutated[hardener + 3] ^= 1
        with self.assertRaisesRegex(TuHelperBuildError, "H.TIMI hardener"):
            validate_tu_helper_image(bytes(mutated), image.labels)

        preparer = image.labels["prepare_pico_htimi_tail"] - ORIGIN
        self.assertEqual(
            image.data[preparer:preparer + len(PICO_HOOK_PREPARER)],
            PICO_HOOK_PREPARER,
        )
        mutated = bytearray(image.data)
        mutated[preparer + 4] ^= 1
        with self.assertRaisesRegex(TuHelperBuildError, "H.TIMI preparer"):
            validate_tu_helper_image(bytes(mutated), image.labels)

        stub = image.labels["overlay_stub"] - ORIGIN
        self.assertEqual(
            image.data[stub:stub + len(OVERLAY_STUB)], OVERLAY_STUB)
        mutated = bytearray(image.data)
        mutated[stub] ^= 1
        with self.assertRaisesRegex(TuHelperBuildError, "overlay stub"):
            validate_tu_helper_image(bytes(mutated), image.labels)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_z80_hardener_accepts_any_slot_and_repairs_only_fifth_byte(self):
        image = assemble_tu_helper()
        for slot in (0x00, 0x89, 0xFF):
            with self.subTest(slot=slot):
                before = bytes((0xF7, slot, 0xB8, 0x4C, 0x54))
                after = _HardenerMachine(image, before).run()
                self.assertEqual(after, bytes((0xF7, slot, 0xB8, 0x4C, 0xC9)))

        clean = bytes((0xF7, 0x89, 0xB8, 0x4C, 0xC9))
        self.assertEqual(_HardenerMachine(image, clean).run(), clean)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_z80_hardener_rejects_every_near_signature(self):
        image = assemble_tu_helper()
        base = bytearray((0xF7, 0x89, 0xB8, 0x4C, 0x54))
        for index, replacement in ((0, 0xC3), (2, 0xB9), (3, 0x4D)):
            hook = bytearray(base)
            hook[index] = replacement
            with self.subTest(index=index):
                self.assertEqual(
                    _HardenerMachine(image, bytes(hook)).run(), bytes(hook))

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_actual_z80_preparer_closes_only_normalized_ret_hook_window(self):
        image = assemble_tu_helper()
        before = bytes((0xC9, 0x89, 0xB8, 0x4C, 0x54))
        after = _HardenerMachine(
            image, before, "prepare_pico_htimi_tail").run()
        self.assertEqual(after, bytes((0xC9, 0x89, 0xB8, 0x4C, 0xC9)))

        for first in (0xF7, 0xCD, 0xC3):
            hook = bytes((first, 0x89, 0xB8, 0x4C, 0x54))
            with self.subTest(first=first):
                self.assertEqual(
                    _HardenerMachine(
                        image, hook, "prepare_pico_htimi_tail").run(),
                    hook,
                )

    def test_source_stages_and_closes_tl_before_unapi_side_effects(self):
        source = (ROOT / "agent" / "msx_tu_helper.asm").read_text(
            encoding="utf-8")
        entry = source.split("tu_helper_start:", 1)[1].split(
            "; ---------------------------------------------------------------------------",
            1,
        )[0]
        self.assertLess(
            entry.index("call require_dos2"),
            entry.index("call resolve_tl_path"),
        )
        self.assertLess(
            entry.index("call resolve_tl_path"),
            entry.index("call plan_high_stage"),
        )
        self.assertLess(
            entry.index("call plan_high_stage"),
            entry.index("call stage_tl_exact"),
        )
        self.assertLess(
            entry.index("call stage_tl_exact"),
            entry.index("call enumerate_tcpip_unapi"),
        )
        self.assertLess(
            entry.index("call enumerate_tcpip_unapi"),
            entry.index("jp handoff_to_staged_tl"),
        )

        resolver = source.split("resolve_tl_path:", 1)[1].split(
            "plan_high_stage:", 1)[0]
        self.assertIn("ld c,DOS_GET_ENV", resolver)
        self.assertIn('db "MSXAI_HOME",0', source)
        self.assertIn('db "TL.COM",0', source)

        stage = source.split("stage_tl_exact:", 1)[1].split(
            "; ---------------------------------------------------------------------------", 1)[0]
        self.assertEqual(stage.count("ld c,DOS_OPEN"), 1)
        self.assertEqual(stage.count("ld c,DOS_READ"), 1)
        self.assertEqual(stage.count("ld c,DOS_CLOSE"), 1)
        self.assertEqual(stage.count("ld c,DOS_SEEK"), 2)
        self.assertIn("ld bc,TL_FILE_SIZE", stage)
        self.assertIn("ld hl,TL_FILE_SIZE", stage)
        self.assertIn("ld a,INVALID_HANDLE", stage)
        self.assertIn("ld (tl_handle),a", stage)

    def test_source_enumerates_only_and_hardens_after_each_candidate(self):
        source = (ROOT / "agent" / "msx_tu_helper.asm").read_text(
            encoding="utf-8")
        enumeration = source.split("enumerate_tcpip_unapi:", 1)[1].split(
            "enumerate_tcpip_unapi_end:", 1)[0]
        self.assertEqual(enumeration.count("call EXTBIO"), 2)
        self.assertEqual(enumeration.count("ei\n    call EXTBIO\n    di"), 2)
        self.assertNotIn("call BDOS", enumeration)
        self.assertNotIn("DOS_", enumeration)
        self.assertNotIn("execute_unapi", enumeration)
        self.assertIn("ld a,b\n    ld (implementation_remaining),a", enumeration)
        self.assertIn("ld a,1\n    ld (implementation_index),a", enumeration)
        candidate = enumeration.split("enumerate_candidate_extbio:", 1)[1]
        self.assertLess(
            candidate.index("call EXTBIO"),
            candidate.index("call harden_pico_htimi"),
        )
        before_candidate = enumeration.split(
            "enumerate_candidate_extbio:", 1)[0]
        self.assertIn("call prepare_pico_htimi_tail", before_candidate)

        preparer = source.split("prepare_pico_htimi_tail:", 1)[1].split(
            "prepare_pico_htimi_tail_end:", 1)[0]
        self.assertIn("ld a,(H_TIMI)", preparer)
        self.assertIn("cp RET_OPCODE", preparer)
        self.assertIn("ld (H_TIMI+4),a", preparer)
        self.assertNotIn("H_TIMI+1", preparer)
        self.assertNotIn("H_TIMI+2", preparer)
        self.assertNotIn("H_TIMI+3", preparer)

        hardener = source.split("harden_pico_htimi:", 1)[1].split(
            "harden_pico_htimi_end:", 1)[0]
        self.assertIn("ld a,(H_TIMI)", hardener)
        self.assertNotIn("H_TIMI+1", hardener)  # slot byte stays unconstrained
        self.assertIn("ld a,(H_TIMI+2)", hardener)
        self.assertIn("ld a,(H_TIMI+3)", hardener)
        self.assertIn("ld (H_TIMI+4),a", hardener)
        self.assertNotIn("0FD3Eh", source)
        self.assertNotIn("0FC4Ah", source)

    def test_source_overlay_preserves_tail_and_uses_exact_tl_length(self):
        source = (ROOT / "agent" / "msx_tu_helper.asm").read_text(
            encoding="utf-8")
        self.assertIn("COMMAND_TAIL:            equ 00080h", source)
        self.assertIn("TL_FILE_SIZE:            equ 00A00h", source)
        self.assertIn("TL_OVERLAY_END:          equ COM_ENTRY + TL_FILE_SIZE", source)

        plan = source.split("plan_high_stage:", 1)[1].split(
            "stage_tl_exact:", 1)[0]
        self.assertIn("ld hl,(TPA_TOP_POINTER)", plan)
        self.assertIn("ld de,(tu_entry_sp)", plan)
        self.assertIn("ld de,OVERLAY_STACK_HEADROOM + overlay_stub_size", plan)
        self.assertIn("ld de,tu_helper_end", plan)
        self.assertIn("ld de,TL_OVERLAY_END", plan)

        handoff = source.split("handoff_to_staged_tl:", 1)[1].split(
            "handoff_to_staged_tl_end:", 1)[0]
        self.assertIn("ld sp,hl", handoff)
        self.assertIn("ld de,COM_ENTRY", handoff)
        self.assertIn("ld bc,TL_FILE_SIZE", handoff)
        self.assertNotIn("COMMAND_TAIL", handoff)
        stub = source.split("overlay_stub:", 1)[1].split(
            "overlay_stub_end:", 1)[0]
        self.assertEqual(stub, "\n    ldir\n    jp COM_ENTRY\n")


if __name__ == "__main__":
    unittest.main()
