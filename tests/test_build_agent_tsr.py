import pathlib
import re
import shutil
import struct
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.build_agent_tsr import (  # noqa: E402
    BUILD_ORIGINS,
    H_CRUN,
    H_KEYI,
    H_TIMI,
    TRANSPORT_16C550,
    TRANSPORT_8251,
    TRANSPORT_UNAPI,
    TSR_NAME,
    AgentTsrBuildError,
    LinkedImage,
    _check_linked_images,
    build_agent_tsr,
    parse_labels,
)
from tools.build_memman_tsr import HEADER, HEADER_SIZE, MAGIC  # noqa: E402


class AgentTsrBuilderTest(unittest.TestCase):
    def test_parses_z80asm_label_stream_and_rejects_duplicates(self):
        self.assertEqual(
            parse_labels("noise\nresident_start:\tequ $4024\n"),
            {"resident_start": 0x4024},
        )
        with self.assertRaisesRegex(AgentTsrBuildError, "duplicate label"):
            parse_labels(
                "resident_start: equ $4024\n"
                "resident_start: equ $5135\n")

    def test_third_origin_label_drift_is_rejected(self):
        def linked(origin, talk_offset=5):
            return LinkedImage(
                origin,
                b"\xfe" + bytes(9),
                {
                    "H_KEYI": H_KEYI,
                    "H_TIMI": H_TIMI,
                    "H_CRUN": H_CRUN,
                    "resident_start": origin,
                    "active_transport_id": origin,
                    "resident_keyi_hook": origin + 1,
                    "resident_timi_hook": origin + 2,
                    "resident_basic_crunch_hook": origin + 3,
                    "tsr_kill": origin + 4,
                    "tsr_talk": origin + talk_offset,
                    "resident_end": origin + 8,
                    "tsr_init": origin + 8,
                    "tsr_init_end": origin + 10,
                },
            )

        images = (
            linked(BUILD_ORIGINS[0]),
            linked(BUILD_ORIGINS[1]),
            linked(BUILD_ORIGINS[2], talk_offset=6),
        )
        with self.assertRaisesRegex(AgentTsrBuildError, "origin-invariant"):
            _check_linked_images(images)

    @unittest.skipUnless(shutil.which("z80asm"), "z80asm is not installed")
    def test_real_core_build_is_cross_checked_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            output = temporary / "MSXAI.TSR"
            metadata = temporary / "MSXAI_TSR.INC"
            first = build_agent_tsr(ROOT, output, metadata)
            first_data = output.read_bytes()
            first_metadata = metadata.read_bytes()
            driver_8251 = first.driver_8251_path.read_bytes()
            driver_16c550 = first.driver_16c550_path.read_bytes()
            driver_unapi = first.driver_unapi_path.read_bytes()

            self.assertEqual(first.size, len(first_data))
            self.assertEqual(len(first_data) % 128, 0)
            self.assertGreater(len(first.relocation_offsets), 0)
            fields = HEADER.unpack_from(first_data)
            self.assertEqual(fields[0], MAGIC)
            self.assertEqual(fields[1], TSR_NAME.encode("ascii").ljust(12))
            self.assertEqual(fields[3], 3)
            self.assertEqual(fields[4], BUILD_ORIGINS[0])

            rel_length = struct.unpack_from("<H", first_data, HEADER_SIZE)[0]
            self.assertEqual(
                rel_length, 2 + 2 * len(first.relocation_offsets))
            hook_start = HEADER_SIZE + rel_length + fields[8] + fields[9]
            hook_length = struct.unpack_from("<H", first_data, hook_start)[0]
            self.assertEqual(hook_length, 14)
            hooks = tuple(
                struct.unpack_from("<HH", first_data, hook_start + offset)
                for offset in (2, 6, 10)
            )
            self.assertEqual(tuple(address for address, _ in hooks),
                             (H_KEYI, H_TIMI, H_CRUN))
            for _, hook_handler in hooks:
                self.assertGreaterEqual(hook_handler, fields[4])
                self.assertLess(hook_handler, fields[4] + fields[8])
            self.assertEqual(first_data[first.transport_file_offset], 0xFE)
            self.assertEqual(len(driver_8251), len(first_data))
            self.assertEqual(len(driver_16c550), len(first_data))
            self.assertEqual(len(driver_unapi), len(first_data))
            self.assertEqual(
                driver_8251[first.transport_file_offset], TRANSPORT_8251)
            self.assertEqual(
                driver_16c550[first.transport_file_offset], TRANSPORT_16C550)
            self.assertEqual(
                driver_unapi[first.transport_file_offset], TRANSPORT_UNAPI)
            self.assertEqual(
                [index for index, values in enumerate(zip(
                    driver_8251, driver_16c550, driver_unapi, strict=True))
                 if len(set(values)) != 1],
                [first.transport_file_offset])

            metadata_text = first_metadata.decode("ascii")
            self.assertRegex(
                metadata_text,
                rf"MSXAI_TSR_SIZE: equ 0{len(first_data):04X}h")
            self.assertRegex(
                metadata_text,
                rf"MSXAI_TSR_TRANSPORT_OFFSET: equ "
                rf"0{first.transport_file_offset:04X}h")
            self.assertEqual(
                len(re.findall(r"^MSXAI_TSR_", metadata_text, re.MULTILINE)),
                2,
            )

            second = build_agent_tsr(ROOT, output, metadata)
            self.assertEqual(second, first)
            self.assertEqual(output.read_bytes(), first_data)
            self.assertEqual(metadata.read_bytes(), first_metadata)
            self.assertEqual(first.driver_8251_path.read_bytes(), driver_8251)
            self.assertEqual(
                first.driver_16c550_path.read_bytes(), driver_16c550)
            self.assertEqual(
                first.driver_unapi_path.read_bytes(), driver_unapi)


if __name__ == "__main__":
    unittest.main()
