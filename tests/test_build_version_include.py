import pathlib
import tempfile
import unittest

from tools.build_version_include import (
    VersionIncludeError,
    materialize_version_include,
    read_project_version,
    render_version_include,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


class VersionIncludeTest(unittest.TestCase):
    @staticmethod
    def make_repository(root: pathlib.Path, value: str) -> pathlib.Path:
        server = root / "server"
        server.mkdir()
        (server / "_version.py").write_text(value, encoding="utf-8")
        return root

    def test_materializes_banner_from_the_single_version_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(
                pathlib.Path(directory), '__version__ = "1.2.3"\n')
            output = materialize_version_include(root)
            self.assertEqual(read_project_version(root), "1.2.3")
            self.assertEqual(
                output.read_bytes(),
                b"; Generated from server/_version.py; do not edit.\n"
                b'db "MSX-AI MCP Agent 1.2.3",13,10\n')

    def test_tracked_include_matches_the_canonical_version(self):
        self.assertEqual(
            (ROOT / "agent" / "msx_version.inc").read_bytes(),
            render_version_include(read_project_version(ROOT)))

    def test_rejects_missing_duplicate_or_non_semver_values(self):
        for source in (
                "",
                '__version__ = "1.2.3"\n__version__ = "1.2.4"\n',
                '__version__ = "1.2"\n',
                '__version__ = "1.2.3.dev1"\n'):
            with self.subTest(source=source), \
                    tempfile.TemporaryDirectory() as directory:
                root = self.make_repository(pathlib.Path(directory), source)
                with self.assertRaises(VersionIncludeError):
                    read_project_version(root)


if __name__ == "__main__":
    unittest.main()
