import base64
import hashlib
import pathlib
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.materialize_memman import (  # noqa: E402
    MEMMAN_CONFIGURED_HEAP_SIZE,
    MEMMAN_CONFIG_FILE_OFFSET,
    MEMMAN_HEAP_WORD_OFFSET,
    MEMMAN_UPSTREAM_CONFIG_SIGNATURE,
    MaterializeError,
    materialize_memman,
)


EXPECTED = {
    "MEMMAN.COM": (
        7680,
        "beba69a5351925c8f7147d5bb4b80e54a8e6702effc7b6ce7d37c48ba9519875",
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

            encoded = (
                ROOT / "third_party" / "memman" / "memman.com.b64"
            ).read_bytes()
            upstream = base64.b64decode(encoded)
            start = MEMMAN_CONFIG_FILE_OFFSET
            end = start + len(MEMMAN_UPSTREAM_CONFIG_SIGNATURE)
            self.assertEqual(
                upstream[start:end], MEMMAN_UPSTREAM_CONFIG_SIGNATURE)
            self.assertEqual(
                upstream.count(MEMMAN_UPSTREAM_CONFIG_SIGNATURE), 1)
            heap_start = start + MEMMAN_HEAP_WORD_OFFSET
            self.assertEqual(upstream[heap_start:heap_start + 2], b"\x80\x00")
            configured = (output / "MEMMAN.COM").read_bytes()
            self.assertEqual(
                int.from_bytes(configured[heap_start:heap_start + 2], "little"),
                MEMMAN_CONFIGURED_HEAP_SIZE,
            )
            self.assertEqual(
                configured[:heap_start] + configured[heap_start + 2:],
                upstream[:heap_start] + upstream[heap_start + 2:],
            )

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

    def test_rejects_memman_with_rehashed_configuration_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            source = temporary / "source"
            output = temporary / "output"
            shutil.copytree(ROOT / "third_party" / "memman", source)
            output.mkdir()
            sentinel = output / "MEMMAN.COM"
            sentinel.write_bytes(b"existing verified output")

            encoded = source / "memman.com.b64"
            data = bytearray(base64.b64decode(encoded.read_bytes()))
            data[MEMMAN_CONFIG_FILE_OFFSET] ^= 0x01
            encoded.write_bytes(base64.encodebytes(data))
            digest = hashlib.sha256(data).hexdigest()
            manifest = source / "SHA256SUMS"
            lines = manifest.read_text(encoding="ascii").splitlines()
            lines[0] = f"{digest}  memman.com"
            manifest.write_text("\n".join(lines) + "\n", encoding="ascii")

            with self.assertRaisesRegex(
                    MaterializeError, "configuration signature mismatch"):
                materialize_memman(source, output)
            self.assertEqual(sentinel.read_bytes(), b"existing verified output")
            self.assertFalse((output / "TL.COM").exists())
            self.assertFalse((output / "TK.COM").exists())


if __name__ == "__main__":
    unittest.main()
