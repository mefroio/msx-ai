import contextlib
import io
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.build_memman_tsr import (  # noqa: E402
    BuildError,
    BuildSpec,
    Hook,
    _resolve_unique_cover,
    build_memman_tsr,
    infer_relocations,
    main,
)


ORIGIN = 0x4024
DELTA = 0x0100
IMAGE_A = bytes.fromhex("21 2a40 c3 2c40 c9 00 21 2b40 c9")
IMAGE_B = bytes.fromhex("21 2a41 c3 2c41 c9 00 21 2b41 c9")


def synthetic_spec(**changes):
    values = dict(
        name="SYNTH TEST",
        origin=ORIGIN,
        delta=DELTA,
        code_length=8,
        kill_offset=6,
        talk_offset=6,
        hooks=(Hook(0xFD9A, 0),),
        record_size=128,
        expected_relocations=(1, 4, 9),
    )
    values.update(changes)
    return BuildSpec(**values)


class MemManTsrBuilderTest(unittest.TestCase):
    def test_infers_only_the_three_origin_relative_words(self):
        self.assertEqual(
            infer_relocations(IMAGE_A, IMAGE_B, ORIGIN, DELTA),
            (1, 4, 9))

    def test_emits_exact_v3_layout_and_128_byte_record(self):
        result = build_memman_tsr(IMAGE_A, IMAGE_B, synthetic_spec())
        expected_prefix = (
            b"MST TSR\r\n"
            b"SYNTH TEST  "
            b"\x1a"
            b"\x03\x00"        # v3
            b"\x24\x40"        # resident code base
            b"\x2c\x40"        # init base
            b"\x2a\x40"        # kill
            b"\x2a\x40"        # talk
            b"\x08\x00"        # resident code length
            b"\x04\x00"        # init length
            b"\x08\x00"        # REL-table length, including this word
            b"\x25\x40\x28\x40\x2d\x40"
            + IMAGE_A
            + b"\x06\x00"      # hook-table length, including this word
            b"\x9a\xfd\x24\x40"
        )
        self.assertEqual(result.unpadded_size, len(expected_prefix))
        self.assertEqual(result.data, expected_prefix.ljust(128, b"\0"))
        self.assertEqual(result.padding_size, 128 - len(expected_prefix))

    def test_supports_observed_256_byte_padding_boundary(self):
        result = build_memman_tsr(
            IMAGE_A, IMAGE_B, synthetic_spec(record_size=256))
        self.assertEqual(len(result.data), 256)
        self.assertEqual(result.data[result.unpadded_size:],
                         bytes(256 - result.unpadded_size))

    def test_rejects_ambiguous_word_cover(self):
        # Changed bytes 1 and 2 can be represented by word 1, or by the two
        # non-overlapping words 0 and 2. The real inference path must stop in
        # the same situation rather than selecting one arbitrarily.
        with self.assertRaisesRegex(BuildError, "ambiguous relocation"):
            _resolve_unique_cover(
                4, frozenset((1, 2)), frozenset((0, 1, 2)))

    def test_rejects_unexplained_or_non_relative_changes(self):
        with self.assertRaisesRegex(BuildError, "cannot be represented"):
            infer_relocations(b"\0\0\0", b"\1\0\0", ORIGIN, DELTA)
        with self.assertRaisesRegex(BuildError, "cannot be represented"):
            infer_relocations(
                bytes.fromhex("005000"), bytes.fromhex("005100"),
                ORIGIN, DELTA)

    def test_pinned_relocation_set_detects_build_drift(self):
        with self.assertRaisesRegex(BuildError, "pinned set"):
            build_memman_tsr(
                IMAGE_A, IMAGE_B,
                synthetic_spec(expected_relocations=(1, 4)))

    def test_rejects_hook_handlers_in_discarded_init_code(self):
        with self.assertRaisesRegex(BuildError, "inside resident code"):
            build_memman_tsr(
                IMAGE_A, IMAGE_B,
                synthetic_spec(hooks=(Hook(0xFD9A, 9),)))

    def test_cli_is_deterministic_and_writes_only_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            image_a = root / "a.bin"
            image_b = root / "b.bin"
            output = root / "agent.tsr"
            image_a.write_bytes(IMAGE_A)
            image_b.write_bytes(IMAGE_B)
            argv = [
                "--image-a", str(image_a),
                "--image-b", str(image_b),
                "--origin", "0x4024",
                "--delta", "0x100",
                "--name", "SYNTH TEST",
                "--code-length", "8",
                "--kill-offset", "6",
                "--talk-offset", "6",
                "--hook", "0xFD9A:0",
                "--expect-relocation", "1",
                "--expect-relocation", "4",
                "--expect-relocation", "9",
                "--output", str(output),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
            first = output.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
            self.assertEqual(output.read_bytes(), first)

            output.write_bytes(b"do not replace on error")
            argv[argv.index("0x100")] = "0"
            with (contextlib.redirect_stdout(io.StringIO()),
                  contextlib.redirect_stderr(io.StringIO())):
                self.assertEqual(main(argv), 2)
            self.assertEqual(output.read_bytes(), b"do not replace on error")


if __name__ == "__main__":
    unittest.main()
