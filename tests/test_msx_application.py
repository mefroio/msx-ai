import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from msx_application import (  # noqa: E402
    ApplicationFormatError,
    ApplicationIntegrityError,
    ApplicationPathError,
    ApplicationRangeError,
    BackendCapabilityError,
    BackendError,
    UnsupportedMapperError,
    load_application,
    parse_application,
    parse_bload,
    parse_com,
    parse_flat_rom,
    parse_manifest,
)


class FakeBackend:
    def __init__(self):
        self.ram = bytearray(0x10000)
        self.vram = bytearray(0x20000)
        self.calls = []
        self.stops = 0

    def poke(self, address, data):
        self.ram[address:address + len(data)] = data
        return len(data)

    def peek(self, address, length):
        return self.ram[address:address + length]

    def vpoke(self, address, data):
        self.vram[address:address + len(data)] = data
        return len(data)

    def vpeek(self, address, length):
        return self.vram[address:address + length]

    def call(self, address):
        self.calls.append(("call", address))

    def run(self, address):
        self.calls.append(("run", address))

    def stop(self):
        self.stops += 1


class CanonicalBackend:
    """Backend using interface-neutral names instead of RealMSX aliases."""

    def __init__(self):
        self.ram = bytearray(0x10000)
        self.executed = []

    def write_ram(self, address, data):
        self.ram[address:address + len(data)] = data

    def read_ram(self, address, length):
        return bytes(self.ram[address:address + length])

    def execute_run(self, address):
        self.executed.append(address)


class ApplicationManifestTest(unittest.TestCase):
    def test_all_segment_encodings_hashes_and_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = b"asset-data"
            (root / "asset.bin").write_bytes(asset)
            manifest = {
                "format": "msx-ai-app-v1",
                "name": "mixed",
                "segments": [
                    {"space": "ram", "address": "0x8000", "hex": "00 C9"},
                    {"space": "ram", "address": 0x8100,
                     "base64": base64.b64encode(b"abc").decode()},
                    {"space": "vram", "address": 0x100,
                     "file": "asset.bin", "sha256": hashlib.sha256(asset).hexdigest()},
                    {"space": "vram", "address": 0x200,
                     "fill": {"value": 0xA5, "length": 4}},
                ],
                "entry": {"mode": "run", "address": "0x8000"},
            }
            app = parse_manifest(manifest, base_dir=root)
            self.assertEqual([segment.data for segment in app.segments],
                             [b"\x00\xC9", b"abc", asset, b"\xA5" * 4])

            backend = FakeBackend()
            result = load_application(backend, app, verify=True, stop_before_load=True)
            self.assertEqual(backend.ram[0x8000:0x8002], b"\x00\xC9")
            self.assertEqual(backend.vram[0x100:0x100 + len(asset)], asset)
            self.assertEqual(backend.calls, [("run", 0x8000)])
            self.assertEqual(backend.stops, 1)
            self.assertEqual(result["bytes_loaded"], 2 + 3 + len(asset) + 4)
            self.assertTrue(all(item["verified"] for item in result["segments"]))

    def test_json_manifest_resolves_files_relative_to_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data.bin").write_bytes(b"xyz")
            path = root / "demo.msxapp"
            path.write_text(json.dumps({
                "format": "msx-ai-app-v1",
                "segments": [{"space": "ram", "address": 0x9000,
                              "file": "data.bin"}],
            }))
            app = parse_application(path)
            self.assertEqual(app.segments[0].data, b"xyz")
            self.assertEqual(app.origin, str(path.resolve()))

    def test_checksum_mismatch_is_rejected_before_loading(self):
        with self.assertRaises(ApplicationIntegrityError):
            parse_manifest({
                "format": "msx-ai-app-v1",
                "segments": [{"space": "ram", "address": 0,
                              "hex": "00", "sha256": "0" * 64}],
            })

    def test_path_traversal_absolute_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / (root.name + "-outside.bin")
            outside.write_bytes(b"secret")
            try:
                base = {"format": "msx-ai-app-v1", "segments": []}
                for invalid in ("../" + outside.name, str(outside), "..\\secret.bin"):
                    document = dict(base)
                    document["segments"] = [
                        {"space": "ram", "address": 0, "file": invalid}]
                    with self.subTest(invalid=invalid), self.assertRaises(ApplicationPathError):
                        parse_manifest(document, base_dir=root)
                try:
                    (root / "link.bin").symlink_to(outside)
                except OSError:
                    pass
                else:
                    document = dict(base)
                    document["segments"] = [
                        {"space": "ram", "address": 0, "file": "link.bin"}]
                    with self.assertRaises(ApplicationPathError):
                        parse_manifest(document, base_dir=root)
            finally:
                outside.unlink(missing_ok=True)

    def test_ranges_invalid_payloads_and_entry_are_rejected(self):
        cases = [
            {"space": "ram", "address": 0xFFFF, "hex": "0001"},
            {"space": "vram", "address": 0x20000, "fill": 0, "length": 1},
        ]
        for segment in cases:
            with self.subTest(segment=segment), self.assertRaises(ApplicationRangeError):
                parse_manifest({"format": "msx-ai-app-v1", "segments": [segment]})
        with self.assertRaises(ApplicationFormatError):
            parse_manifest({
                "format": "msx-ai-app-v1",
                "segments": [{"space": "ram", "address": 0,
                              "hex": "00", "base64": "AA=="}],
            })
        with self.assertRaises(ApplicationRangeError):
            parse_manifest({
                "format": "msx-ai-app-v1", "segments": [],
                "entry": {"mode": "run", "address": 0x10000},
            })


class LegacyFormatTest(unittest.TestCase):
    def test_com_load_address_and_default_entry(self):
        app = parse_com(b"\x00\xC9", name="TEST.COM")
        self.assertEqual((app.segments[0].address, app.segments[0].data),
                         (0x100, b"\x00\xC9"))
        self.assertEqual((app.entry.mode, app.entry.address), ("run", 0x100))
        with self.assertRaises(ApplicationRangeError):
            parse_com(bytes(0xFF01))

    def test_bload_header_and_payload(self):
        image = b"\xFE\x00\x80\x02\x80\x01\x80" + b"ABC"
        app = parse_bload(image)
        self.assertEqual(app.segments[0].address, 0x8000)
        self.assertEqual(app.segments[0].data, b"ABC")
        self.assertEqual((app.entry.mode, app.entry.address), ("run", 0x8001))
        with self.assertRaises(ApplicationFormatError):
            parse_bload(image[:-1])

    def test_flat_rom_header_16k_32k_and_page2_detection(self):
        rom16 = bytearray(0x4000)
        rom16[:4] = b"AB\x10\x40"
        app16 = parse_flat_rom(rom16)
        self.assertEqual(app16.segments[0].address, 0x4000)
        self.assertEqual(app16.entry.mode, "call")
        self.assertEqual(app16.mapper,
                         {"type": "flat", "base": 0x4000, "size": 0x4000})

        rom_page2 = bytearray(0x4000)
        rom_page2[:4] = b"AB\x10\x80"
        app_page2 = parse_flat_rom(rom_page2)
        self.assertEqual(app_page2.segments[0].address, 0x8000)

        rom32 = bytearray(0x8000)
        rom32[:2] = b"AB"
        app32 = parse_flat_rom(rom32)
        self.assertEqual(app32.segments[0].address, 0x4000)
        self.assertEqual(app32.entry.mode, "none")
        with self.assertRaises(ApplicationFormatError):
            parse_flat_rom(b"AB" + bytes(100))

    def test_detection_and_com_needs_explicit_format_for_bytes(self):
        bload = b"\xFE\x00\x80\x00\x80\x00\x00X"
        self.assertEqual(parse_application(bload).source_format, "bload")
        with self.assertRaises(ApplicationFormatError):
            parse_application(b"\x00\xC9")
        self.assertEqual(parse_application(b"\x00\xC9", format="com").source_format,
                         "com")


class LoaderContractTest(unittest.TestCase):
    def test_canonical_backend_names_and_execution_override(self):
        backend = CanonicalBackend()
        result = load_application(
            backend, b"\x00\xC9", format="com", execute="run",
            entry_address=0x100, verify=True)
        self.assertEqual(backend.ram[0x100:0x102], b"\x00\xC9")
        self.assertEqual(backend.executed, [0x100])
        self.assertEqual(result["entry"], {"mode": "run", "address": 0x100})
        self.assertIn("execute:run", result["required_capabilities"])

    def test_execution_override_controls_effective_capabilities(self):
        image = b"\xFE\x00\x80\x00\x80\x00\x80\xC9"

        disabled = load_application(
            FakeBackend(), image, execute="none", verify=True)
        self.assertEqual(
            disabled["entry"], {"mode": "none", "address": None})
        self.assertIn("write:ram", disabled["required_capabilities"])
        self.assertNotIn("execute:run", disabled["required_capabilities"])
        self.assertFalse(any(
            capability.startswith("execute:")
            for capability in disabled["required_capabilities"]))

        backend = FakeBackend()
        called = load_application(
            backend, image, execute="call", verify=False)
        self.assertEqual(
            called["entry"], {"mode": "call", "address": 0x8000})
        self.assertEqual(backend.calls, [("call", 0x8000)])
        self.assertIn("execute:call", called["required_capabilities"])
        self.assertNotIn("execute:run", called["required_capabilities"])

    def test_missing_backend_operation_fails_before_any_write(self):
        class RamOnly:
            def __init__(self):
                self.writes = 0

            def poke(self, address, data):
                self.writes += 1

        backend = RamOnly()
        manifest = {
            "format": "msx-ai-app-v1",
            "segments": [{"space": "ram", "address": 0, "hex": "01"},
                         {"space": "vram", "address": 0, "hex": "02"}],
        }
        with self.assertRaises(BackendError):
            load_application(backend, manifest)
        self.assertEqual(backend.writes, 0)

    def test_backend_preflight_runs_before_stop_or_write(self):
        class RejectingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.events = []

            def preflight_application(self, application):
                self.events.append("prepare")
                raise BackendCapabilityError("incompatible target")

            def stop(self):
                self.events.append("stop")
                super().stop()

            def poke(self, address, data):
                self.events.append("write")
                return super().poke(address, data)

        backend = RejectingBackend()
        with self.assertRaisesRegex(BackendCapabilityError, "incompatible"):
            load_application(
                backend, b"\xC9", format="com", execute="none",
                stop_before_load=True)
        self.assertEqual(backend.events, ["prepare"])
        self.assertEqual(backend.stops, 0)
        self.assertEqual(backend.ram[0x100], 0)

    def test_declared_capabilities_are_enforced(self):
        backend = FakeBackend()
        backend.capabilities = {"vdp:v9958"}
        manifest = {
            "format": "msx-ai-app-v1", "segments": [],
            "requires": ["vdp:v9958", "ram:mapper"],
        }
        with self.assertRaises(BackendCapabilityError):
            load_application(backend, manifest)

    def test_nonflat_mapper_requires_explicit_backend_support(self):
        manifest = {
            "format": "msx-ai-app-v1", "segments": [],
            "mapper": {"type": "ascii8", "banks": 8},
        }
        with self.assertRaises(UnsupportedMapperError):
            load_application(FakeBackend(), manifest)

        class MapperBackend(FakeBackend):
            def supports_mapper(self, mapper_type):
                return mapper_type == "ascii8"

            def configure_mapper(self, spec):
                self.mapper = spec

        backend = MapperBackend()
        result = load_application(backend, manifest)
        self.assertEqual(backend.mapper["type"], "ascii8")
        self.assertIn("mapper:ascii8", result["required_capabilities"])


if __name__ == "__main__":
    unittest.main()
