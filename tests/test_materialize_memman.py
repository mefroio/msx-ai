import hashlib
import pathlib
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.materialize_memman import (  # noqa: E402
    MaterializeError,
    materialize_memman,
)


EXPECTED = {
    "MEMMAN.COM": (
        7680,
        "28c3a6193728d062533ae3ca3691f6b06a256a8d2a6efd457a05c8110cd984d5",
    ),
    "TL.COM": (
        2560,
        "910d4538d737dff21874011262a82616e6234c49019ff0191f39a40eb2fc01f1",
    ),
    "TK.COM": (
        1408,
        "b12d149e549c4197137d7701784390d63a8f9a6c1c310e2733f9652630d792e0",
    ),
}


class MaterializeMemmanTest(unittest.TestCase):
    def test_materializes_exact_verified_public_domain_binaries(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "vendor"
            assets = materialize_memman(
                ROOT / "third_party" / "memman", output)

            self.assertEqual({asset.name for asset in assets}, set(EXPECTED))
            for asset in assets:
                size, digest = EXPECTED[asset.name]
                self.assertEqual(asset.size, size)
                self.assertEqual(asset.sha256, digest)
                self.assertEqual(asset.path.stat().st_size, size)
                self.assertEqual(
                    hashlib.sha256(asset.path.read_bytes()).hexdigest(), digest)
            self.assertEqual(
                {path.name for path in output.iterdir()}, set(EXPECTED))

    def test_hash_failure_does_not_partially_replace_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            source = temporary / "source"
            output = temporary / "output"
            shutil.copytree(ROOT / "third_party" / "memman", source)
            output.mkdir()
            sentinel = output / "MEMMAN.COM"
            sentinel.write_bytes(b"existing verified output")

            encoded = source / "tl.com.b64"
            text = encoded.read_text(encoding="ascii")
            replacement = "A" if text[0] != "A" else "B"
            encoded.write_text(replacement + text[1:], encoding="ascii")

            with self.assertRaisesRegex(MaterializeError, "SHA-256 mismatch"):
                materialize_memman(source, output)
            self.assertEqual(sentinel.read_bytes(), b"existing verified output")
            self.assertFalse((output / "TL.COM").exists())
            self.assertFalse((output / "TK.COM").exists())

    def test_rejects_manifest_entries_outside_exact_asset_set(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            source = temporary / "source"
            shutil.copytree(ROOT / "third_party" / "memman", source)
            manifest = source / "SHA256SUMS"
            manifest.write_text(
                manifest.read_text(encoding="ascii")
                + "0" * 64 + "  unexpected.com\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                    MaterializeError, "checksum entries differ"):
                materialize_memman(source, temporary / "output")


if __name__ == "__main__":
    unittest.main()
