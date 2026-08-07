import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import paths  # noqa: E402


class PackagingMetadataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        cls.manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    def test_distribution_uses_branded_package_and_mcp_entry_point(self):
        self.assertIn('name = "msx-ai"', self.pyproject)
        for dependency in (
                '"anyio>=4.5,<5"',
                '"jsonschema>=4.20,<5"',
                '"mcp>=2,<3"',
                '"uvicorn>=0.30,<1"'):
            self.assertIn(dependency, self.pyproject)
        self.assertIn(
            'msx-ai-mcp = "msx_ai.mcp_runtime:main"', self.pyproject)
        for package in (
                '"msx_ai"',
                '"msx_ai.resources"',
                '"msx_ai.resources.docs"',
                '"msx_ai.resources.openmsx"'):
            self.assertIn(package, self.pyproject)
        self.assertIn('package-dir = { msx_ai = "server" }', self.pyproject)
        self.assertIn(
            '"msx_ai.resources.docs" = ["*.md", "manifest.json"]',
            self.pyproject)

    def test_version_has_one_literal_source(self):
        namespace = {}
        source = (SERVER / "_version.py").read_text(encoding="utf-8")
        exec(compile(source, str(SERVER / "_version.py"), "exec"), namespace)
        self.assertEqual(namespace["__version__"], "0.6.0")
        self.assertIn(
            'version = { attr = "msx_ai._version.__version__" }',
            self.pyproject)

    def test_sdist_manifest_prunes_private_runtime_and_firmware(self):
        for entry in (
                "prune work",
                "prune .openmsx-home/persistent",
                "prune .openmsx-home/savestates",
                "prune .openmsx-home/share/systemroms"):
            self.assertIn(entry, self.manifest)
        self.assertIn("recursive-include agent *.asm *.inc *.md", self.manifest)
        self.assertIn(
            "recursive-include server/resources *.md *.json", self.manifest)
        self.assertIn(
            "recursive-include server/resources/openmsx *.xml README",
            self.manifest)
        self.assertNotIn("recursive-include .openmsx-home", self.manifest)
        self.assertNotIn("include .openmsx-home/share/settings.xml", self.manifest)
        self.assertNotIn("include open-msx.command", self.manifest)
        self.assertNotIn("include open-msx-mcp.command", self.manifest)
        self.assertIn("recursive-include third_party/memman", self.manifest)
        self.assertIn("recursive-include third_party/openmsx", self.manifest)
        self.assertIn('"third_party/openmsx/NOTICE"', self.pyproject)
        self.assertIn('"third_party/openmsx/GPL-2.0.txt"', self.pyproject)

    def test_openmsx_resource_tree_is_exactly_the_public_tracked_set(self):
        expected = {
            Path("share/extensions/README"):
                "95f627b9088d69e1029f8b6bf1d25980956b4cf81dd93acfc98770fe44da85ac",
            Path("share/extensions/rs232_proto.xml"):
                "bc5b49d0bf2ca8d0c62ebc74ee6452a5989af7281be6d5840eba2045e8fb30a9",
            Path("share/machines/Gradiente_Expert20.xml"):
                "55dc48233a366c3dd82be78d6bdbbc99dd0080e23e3c248c5c940c11f0e70827",
            Path("share/machines/Gradiente_Expert20_64K.xml"):
                "174d1846fac9307b36afb533837d32cce5c748df0a35a5926dbce27d7c4fdd00",
            Path("share/machines/Sony_HB-F1XDJ_128K_Lite.xml"):
                "c0e9dd564f902d67c5683b65722fae308b0ecd65da72e26d809c2c9fbbc9b3cd",
            Path("share/machines/Sony_HB-F1XDJ_64K_Lite.xml"):
                "4f29c5fea3426e0a78d63849d6fc0f97bfc1de1954d8ba9b160e900aff2a38eb",
            Path("share/settings.xml"):
                "73c0ccb17f705896b2bec49a64296e625848b098cbd1ceab30f7450016b33caf",
            Path("share/software/README"):
                "2be4799826517cc6bf391d713fa3d280669b41e0e19d3c231678615f161d7d07",
        }
        packaged_root = SERVER / "resources" / "openmsx"
        actual = {
            path.relative_to(packaged_root)
            for path in packaged_root.rglob("*")
            if (path.is_file() and path.name != "__init__.py" and
                "__pycache__" not in path.parts)
        }
        self.assertEqual(actual, set(expected))
        for relative, digest in expected.items():
            with self.subTest(relative=relative):
                source = ROOT / ".openmsx-home" / relative
                packaged = packaged_root / relative
                self.assertEqual(
                    hashlib.sha256(packaged.read_bytes()).hexdigest(), digest)
                if source.exists():
                    self.assertEqual(packaged.read_bytes(), source.read_bytes())
                self.assertNotIn(
                    packaged.suffix.lower(), {".rom", ".dsk", ".oms"})


class RuntimePathsTest(unittest.TestCase):
    def test_state_and_user_paths_are_independent_and_lazy(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            state = temporary / "state"
            user = temporary / "project"
            user.mkdir()
            with mock.patch.dict(os.environ, {
                    "MSX_AI_STATE_DIR": str(state),
                    "MSX_AI_USER_ROOT": str(user),
            }, clear=False):
                state = state.resolve()
                user = user.resolve()
                self.assertEqual(paths.state_root(), state)
                self.assertEqual(paths.work_root(), state / "work")
                self.assertEqual(
                    paths.transfer_state_directory(), state / "work" / "transfers")
                self.assertEqual(
                    paths.resolve_user_path("program.bin"), user / "program.bin")
                self.assertFalse(state.exists())

    def test_ensure_directory_is_the_explicit_creation_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state" / "work" / "disks"
            self.assertFalse(target.exists())
            self.assertEqual(paths.ensure_directory(target), target)
            self.assertTrue(target.is_dir())

    def test_source_override_and_missing_checkout_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "checkout"
            with mock.patch.dict(
                    os.environ, {"MSX_AI_SOURCE_ROOT": str(override)},
                    clear=False):
                self.assertEqual(paths.source_root(), override.resolve())
                self.assertEqual(paths.require_source_root(), override.resolve())

            with (mock.patch.dict(os.environ, {}, clear=False),
                  mock.patch.object(
                      paths, "_CHECKOUT_CANDIDATE", Path(directory) / "missing")):
                os.environ.pop("MSX_AI_SOURCE_ROOT", None)
                self.assertIsNone(paths.source_root())
                with self.assertRaisesRegex(RuntimeError, "source checkout"):
                    paths.require_source_root()

    def test_installed_openmsx_default_uses_state_not_package(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            state = temporary / "state"
            with (mock.patch.dict(
                      os.environ, {"MSX_AI_STATE_DIR": str(state)}, clear=False),
                  mock.patch.object(
                      paths, "_CHECKOUT_CANDIDATE", temporary / "installed")):
                os.environ.pop("MSX_AI_SOURCE_ROOT", None)
                os.environ.pop("MSX_AI_OPENMSX_HOME", None)
                self.assertEqual(
                    paths.openmsx_home(), state.resolve() / "openmsx-home")
                self.assertFalse(state.exists())

    def test_openmsx_materialization_is_lazy_idempotent_and_non_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            state = temporary / "state"
            missing_checkout = temporary / "installed"
            with (mock.patch.dict(
                      os.environ, {"MSX_AI_STATE_DIR": str(state)}, clear=False),
                  mock.patch.object(
                      paths, "_CHECKOUT_CANDIDATE", missing_checkout)):
                os.environ.pop("MSX_AI_SOURCE_ROOT", None)
                os.environ.pop("MSX_AI_OPENMSX_HOME", None)
                home = paths.openmsx_home()
                self.assertFalse(state.exists())

                self.assertEqual(paths.prepare_openmsx_home(home), home)
                actual = {
                    path.relative_to(home)
                    for path in home.rglob("*") if path.is_file()
                }
                self.assertEqual(actual, set(paths.OPENMSX_PUBLIC_FILES))

                protected = home / "share/settings.xml"
                protected.write_bytes(b"user-owned-settings")
                self.assertEqual(paths.prepare_openmsx_home(home), home)
                self.assertEqual(protected.read_bytes(), b"user-owned-settings")
                self.assertFalse(list(home.rglob("*.tmp")))

                raced = home / "share/extensions/rs232_proto.xml"
                raced.unlink()

                def publish_competing_file(_temporary, target):
                    Path(target).write_bytes(b"concurrent-user-settings")
                    raise FileExistsError

                with mock.patch.object(
                        paths.os, "link", side_effect=publish_competing_file):
                    self.assertEqual(paths.prepare_openmsx_home(home), home)
                self.assertEqual(
                    raced.read_bytes(), b"concurrent-user-settings")
                self.assertFalse(list(home.rglob("*.tmp")))

    def test_openmsx_override_is_never_materialized(self):
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "user-openmsx-home"
            with mock.patch.dict(
                    os.environ, {"MSX_AI_OPENMSX_HOME": str(override)},
                    clear=False):
                self.assertEqual(
                    paths.prepare_openmsx_home(), override.resolve())
                self.assertFalse(override.exists())

    def test_openmsx_materialization_fails_closed_without_hard_links(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with (mock.patch.dict(
                      os.environ, {"MSX_AI_STATE_DIR": str(state)}, clear=False),
                  mock.patch.object(
                      paths, "_CHECKOUT_CANDIDATE", state / "missing-checkout"),
                  mock.patch.object(
                      paths.os, "link", side_effect=OSError(
                          errno.EOPNOTSUPP, "hard links unsupported")),
                  self.assertRaisesRegex(RuntimeError, "support hard links")):
                os.environ.pop("MSX_AI_SOURCE_ROOT", None)
                os.environ.pop("MSX_AI_OPENMSX_HOME", None)
                paths.prepare_openmsx_home()

            self.assertFalse(list(state.rglob("*.tmp")))

    def test_legacy_and_package_imports_do_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            common = os.environ.copy()
            common["MSX_AI_STATE_DIR"] = str(state)
            common["MSX_AI_SOURCE_ROOT"] = str(ROOT)

            cases = (
                (str(SERVER), "import msx_client, msx_real, msx_mcp_server"),
                (str(ROOT),
                 "import server.msx_client, server.msx_real, "
                 "server.msx_mcp_server"),
            )
            for pythonpath, statement in cases:
                with self.subTest(pythonpath=pythonpath):
                    environment = dict(common, PYTHONPATH=pythonpath)
                    completed = subprocess.run(
                        [sys.executable, "-c", statement],
                        cwd=directory, env=environment,
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(
                        completed.returncode, 0, completed.stderr)
                    self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
