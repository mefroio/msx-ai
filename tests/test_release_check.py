import pathlib
import tempfile
import sys
import tarfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import release_check


class ReleaseCheckPolicyTest(unittest.TestCase):
    @staticmethod
    def wheel_members():
        dist_info = "msx_ai-0.6.0.dist-info"
        return (
            {f"msx_ai/{name}" for name in release_check._RUNTIME_MODULES} |
            {"msx_ai/resources/openmsx/__init__.py"} |
            {f"msx_ai/resources/openmsx/{name}"
             for name in release_check._OPENMSX_WHEEL_RESOURCES} |
            {"msx_ai/resources/docs/__init__.py"} |
            {f"msx_ai/resources/docs/{name}"
             for name in release_check._DOC_WHEEL_RESOURCES} |
            {f"{dist_info}/licenses/{name}"
             for name in release_check._WHEEL_LICENSE_FILES} |
            {f"{dist_info}/METADATA"}
        )

    def test_forbidden_release_content_is_detected_case_insensitively(self):
        forbidden = (
            "project/work/agent/MSXAI.COM",
            "project/.openmsx-home/share/systemroms/MSX.ROM",
            "project/games/demo.DSK",
            "project/state/session.OMS",
            "project/savestates/session.xml",
        )
        for name in forbidden:
            with self.subTest(name=name):
                self.assertTrue(release_check._forbidden_reasons(name))

    def test_public_packaged_openmsx_templates_are_allowed(self):
        allowed = (
            "msx_ai/resources/openmsx/share/settings.xml",
            "msx_ai/resources/openmsx/share/extensions/rs232_proto.xml",
            "assets/msx-ai-robot.png",
        )
        for name in allowed:
            with self.subTest(name=name):
                self.assertEqual(release_check._forbidden_reasons(name), [])

    def test_archive_traversal_and_absolute_paths_are_rejected(self):
        for name in (
                "../secret", "root/../../secret", "/absolute/path",
                "C:/absolute/path", "root\\..\\secret", ".",
                "root//file"):
            with self.subTest(name=name), self.assertRaises(
                    release_check.ReleaseCheckError):
                release_check._safe_archive_path(name)

    def test_release_environment_cannot_enable_openmsx_integration(self):
        environment = release_check._release_environment()
        self.assertEqual(environment["MSX_RUN_INTEGRATION"], "0")

    def test_installed_probe_uses_default_private_state_location(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = pathlib.Path(directory)
            environment = release_check._isolated_environment({
                "MSX_AI_STATE_DIR": "/untrusted/state",
                "MSX_AI_SOURCE_ROOT": "/untrusted/source",
                "PYTHONPATH": "/untrusted/python",
            }, probe)
            self.assertNotIn("MSX_AI_STATE_DIR", environment)
            self.assertNotIn("MSX_AI_SOURCE_ROOT", environment)
            self.assertNotIn("PYTHONPATH", environment)
            self.assertEqual(environment["HOME"], str(probe / "home"))
            release_check._assert_no_state(probe, "test probe")

    def test_positive_wheel_content_policy_accepts_exact_public_sets(self):
        release_check._assert_wheel_contents(list(self.wheel_members()))

    def test_positive_wheel_content_policy_rejects_missing_or_extra_files(self):
        missing_notice = self.wheel_members() - {
            "msx_ai-0.6.0.dist-info/licenses/third_party/openmsx/NOTICE"}
        with self.assertRaisesRegex(
                release_check.ReleaseCheckError, "license/notice"):
            release_check._assert_wheel_contents(list(missing_notice))

        extra_openmsx = self.wheel_members() | {
            "msx_ai/resources/openmsx/share/systemroms/MSX.ROM"}
        with self.assertRaisesRegex(
                release_check.ReleaseCheckError, "openMSX resources"):
            release_check._assert_wheel_contents(list(extra_openmsx))

    def test_positive_sdist_content_policy_requires_release_sources(self):
        names = [f"msx_ai-0.6.0/{name}"
                 for name in release_check._SDIST_REQUIRED_FILES]
        release_check._assert_sdist_contents(names)
        without_asset = [name for name in names
                         if not name.endswith("assets/msx-ai-robot.png")]
        with self.assertRaisesRegex(
                release_check.ReleaseCheckError, "missing required source"):
            release_check._assert_sdist_contents(without_asset)

    def test_agent_suite_requires_exactly_seven_nonempty_files(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = pathlib.Path(directory)
            for name in release_check._AGENT_SUITE_FILES:
                (agent / name).write_bytes(b"artifact")
            (agent / "build").mkdir()
            release_check._assert_agent_suite(agent)

            (agent / "EXTRA.COM").write_bytes(b"unexpected")
            with self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "exact deployable set"):
                release_check._assert_agent_suite(agent)
            (agent / "EXTRA.COM").unlink()

            empty = agent / next(iter(release_check._AGENT_SUITE_FILES))
            empty.write_bytes(b"")
            with self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "empty artifacts"):
                release_check._assert_agent_suite(agent)

    def test_agent_suite_enforces_both_com_size_ceilings(self):
        for oversized_name, ceiling in (
                release_check._AGENT_COM_SIZE_CEILINGS.items()):
            with self.subTest(artifact=oversized_name), \
                    tempfile.TemporaryDirectory() as directory:
                agent = pathlib.Path(directory)
                for name in release_check._AGENT_SUITE_FILES:
                    (agent / name).write_bytes(b"artifact")
                (agent / oversized_name).write_bytes(b"x" * (ceiling + 1))
                with self.assertRaisesRegex(
                        release_check.ReleaseCheckError, "size ceiling"):
                    release_check._assert_agent_suite(agent)

    def test_sdist_rebuilt_agent_payload_must_match_staged_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            staged = root / "staged"
            rebuilt = root / "rebuilt"
            staged.mkdir()
            rebuilt.mkdir()
            for name in release_check._AGENT_SUITE_FILES:
                (staged / name).write_bytes(b"same payload")
                (rebuilt / name).write_bytes(b"same payload")
            release_check._assert_matching_agent_suites(staged, rebuilt)

            (rebuilt / "TL.COM").write_bytes(b"different payload")
            with self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "TL.COM"):
                release_check._assert_matching_agent_suites(staged, rebuilt)

    def test_z80asm_is_a_required_release_build_tool(self):
        def find_tool(name, *, path=None):
            del path
            return None if name == "z80asm" else "/usr/bin/make"

        with mock.patch.object(
                release_check.shutil, "which", side_effect=find_tool), \
                self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "z80asm"):
            release_check._resolve_build_tools({"PATH": "/custom/bin"})

    def test_make_and_z80asm_environment_overrides_are_honored(self):
        calls = []

        def find_tool(name, *, path=None):
            calls.append((name, path))
            return {
                "/custom/gmake": "/resolved/gmake",
                "custom-z80asm": "/resolved/z80asm",
            }.get(name)

        environment = {
            "PATH": "/custom/bin",
            "MAKE": "/custom/gmake",
            "Z80ASM": "custom-z80asm",
        }
        with mock.patch.object(
                release_check.shutil, "which", side_effect=find_tool):
            self.assertEqual(
                release_check._resolve_build_tools(environment),
                ("/resolved/gmake", "/resolved/z80asm"))
        self.assertEqual(calls, [
            ("/custom/gmake", "/custom/bin"),
            ("custom-z80asm", "/custom/bin"),
        ])

    def test_publish_status_requires_no_tracked_or_untracked_changes(self):
        release_check._assert_publish_status(b"")
        for status in (b" M tracked.py\x00", b"?? untracked.py\x00"):
            with self.subTest(status=status), self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "clean Git checkout"):
                release_check._assert_publish_status(status)

    def test_publish_staging_uses_mocked_git_archive_content(self):
        archive_buffer = release_check.io.BytesIO()
        content = b'__version__ = "0.6.0"\n'
        with tarfile.open(fileobj=archive_buffer, mode="w:") as archive:
            information = tarfile.TarInfo("server/_version.py")
            information.size = len(content)
            archive.addfile(information, release_check.io.BytesIO(content))

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(
                    release_check, "_require_clean_git_checkout") as clean, \
                mock.patch.object(
                    release_check, "_git_capture",
                    return_value=archive_buffer.getvalue()) as capture:
            source = release_check._stage_publish_source(
                pathlib.Path(directory), {"PATH": "/unused"})
            self.assertEqual(
                (source / "server/_version.py").read_bytes(), content)
            self.assertTrue((source / "work/release-secret.dsk").is_file())
            self.assertEqual(clean.call_count, 2)
            capture.assert_called_once_with(
                ["archive", "--format=tar", "HEAD"], {"PATH": "/unused"})

    def test_expected_z80asm_identity_is_enforced(self):
        output = (
            "Z80 assembler version 1.8\n"
            "Copyright (C) 2002-2007 Bas Wijnen <shevek@fmf.nl>.\n")
        self.assertEqual(
            release_check._assert_z80asm_version(output),
            release_check._Z80ASM_VERSION_LINE)
        for invalid in (
                "Z80 assembler version 1.9\nCopyright Bas Wijnen\n",
                "Z80 assembler version 1.8\nCopyright Someone Else\n"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "Bas Wijnen z80asm 1.8"):
                release_check._assert_z80asm_version(invalid)

    @staticmethod
    def create_agent_archive_fixture(root):
        source = root / "source"
        agent = source / "agent"
        server = source / "server"
        agent.mkdir(parents=True)
        server.mkdir()
        (source / "LICENSE").write_bytes(b"project MIT license\n")
        memman = source / "third_party" / "memman"
        memman.mkdir(parents=True)
        (memman / "NOTICE").write_bytes(b"MemMan Public Domain notice\n")
        (server / "_version.py").write_text(
            '__version__ = "0.6.0"\n', encoding="utf-8")
        (agent / "msx_agent_core.asm").write_text(
            'db "Version 2.0"\nFRAMED_VERSION: equ 3\n',
            encoding="utf-8")
        (agent / "msx_xfer_protocol.inc").write_text(
            "; fast-v1\nXFER_FAST_VERSION: equ 1\n", encoding="utf-8")
        suite = root / "suite"
        suite.mkdir()
        for name in release_check._AGENT_SUITE_FILES:
            (suite / name).write_bytes(("payload:" + name).encode("ascii"))
        return source, suite

    def test_agent_release_zip_is_deterministic_and_self_describing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, suite = self.create_agent_archive_fixture(root)
            first = release_check._build_agent_archive(
                source, suite, root / "msx-ai-agent-0.6.0.zip",
                release_check._Z80ASM_VERSION_LINE)
            second_directory = root / "second"
            second_directory.mkdir()
            second = release_check._build_agent_archive(
                source, suite,
                second_directory / "msx-ai-agent-0.6.0.zip",
                release_check._Z80ASM_VERSION_LINE)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with release_check.zipfile.ZipFile(first) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    release_check._AGENT_SUITE_FILES |
                    release_check._AGENT_ARCHIVE_METADATA)
                manifest = release_check.json.loads(
                    archive.read("COMPATIBILITY.json"))
                self.assertEqual(manifest["host"], "0.6.0")
                self.assertEqual(manifest["agent"], "2.0")
                self.assertEqual(manifest["wire"], "v3")
                self.assertEqual(manifest["transfer"], "fast-v1")
                self.assertEqual(
                    manifest["toolchain"]["id"], "bas-wijnen-z80asm")
                self.assertEqual(manifest["toolchain"]["version"], "1.8")
                self.assertEqual(
                    archive.read("LICENSE"), b"project MIT license\n")
                self.assertEqual(
                    archive.read("MEMMAN-NOTICE.txt"),
                    b"MemMan Public Domain notice\n")
                checksum_rows = {
                    row.split("  ", 1)[1]: row.split("  ", 1)[0]
                    for row in archive.read("SHA256SUMS").decode(
                        "ascii").splitlines()
                }
                hashed_names = (
                    release_check._AGENT_SUITE_FILES |
                    release_check._AGENT_ARCHIVE_LICENSES |
                    {"COMPATIBILITY.json"})
                self.assertEqual(set(checksum_rows), hashed_names)
                for name in hashed_names:
                    self.assertEqual(
                        checksum_rows[name],
                        release_check.hashlib.sha256(
                            archive.read(name)).hexdigest())

    @staticmethod
    def rewrite_zip(source, destination, *, drop=None, replacements=None):
        replacements = replacements or {}
        with release_check.zipfile.ZipFile(source) as original, \
                release_check.zipfile.ZipFile(destination, "x") as rewritten:
            for name in original.namelist():
                if name == drop:
                    continue
                data = replacements.get(name, original.read(name))
                rewritten.writestr(release_check._zip_info(name), data)

    def test_agent_release_zip_rejects_missing_and_tampered_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, suite = self.create_agent_archive_fixture(root)
            valid = release_check._build_agent_archive(
                source, suite, root / "msx-ai-agent-0.6.0.zip",
                release_check._Z80ASM_VERSION_LINE)

            missing = root / "missing.zip"
            self.rewrite_zip(valid, missing, drop="TL.COM")
            with self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "exactly seven"):
                release_check._assert_agent_archive(
                    missing, source, suite,
                    release_check._Z80ASM_VERSION_LINE)

            bad_checksums = root / "bad-checksums.zip"
            self.rewrite_zip(
                valid, bad_checksums,
                replacements={"SHA256SUMS": b"0" * 64 + b"  TL.COM\n"})
            with self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "SHA256SUMS"):
                release_check._assert_agent_archive(
                    bad_checksums, source, suite,
                    release_check._Z80ASM_VERSION_LINE)

            with release_check.zipfile.ZipFile(valid) as archive:
                entries = {name: archive.read(name)
                           for name in archive.namelist()}
            manifest = release_check.json.loads(entries["COMPATIBILITY.json"])
            manifest["host"] = "9.9.9"
            changed_manifest = release_check.json.dumps(
                manifest, indent=2, sort_keys=True,
                ensure_ascii=True).encode("utf-8") + b"\n"
            digest = release_check.hashlib.sha256(
                changed_manifest).hexdigest()
            checksum_lines = entries["SHA256SUMS"].decode("ascii").splitlines()
            checksum_lines = [
                f"{digest}  COMPATIBILITY.json"
                if line.endswith("  COMPATIBILITY.json") else line
                for line in checksum_lines]
            bad_manifest = root / "bad-manifest.zip"
            self.rewrite_zip(valid, bad_manifest, replacements={
                "COMPATIBILITY.json": changed_manifest,
                "SHA256SUMS": ("\n".join(checksum_lines) + "\n").encode(
                    "ascii"),
            })
            with self.assertRaisesRegex(
                    release_check.ReleaseCheckError,
                    "compatibility manifest"):
                release_check._assert_agent_archive(
                    bad_manifest, source, suite,
                    release_check._Z80ASM_VERSION_LINE)

    def test_persisted_release_assets_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.whl"
            source.write_bytes(b"wheel")
            output = root / "dist"
            published = release_check._persist_release_assets(
                output, [source])
            self.assertEqual(published, [(output / "source.whl").resolve()])
            self.assertEqual(published[0].read_bytes(), b"wheel")
            with self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "refusing to overwrite"):
                release_check._persist_release_assets(output, [source])

    def test_release_asset_publication_rolls_back_on_hardlink_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sources = []
            for index in range(3):
                source = root / f"artifact-{index}.bin"
                source.write_bytes(f"payload-{index}".encode("ascii"))
                sources.append(source)
            output = root / "dist"
            real_link = release_check.os.link
            calls = 0

            def fail_second_link(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(95, "hard links unsupported")
                return real_link(source, target)

            with mock.patch.object(
                    release_check.os, "link", side_effect=fail_second_link), \
                    self.assertRaisesRegex(
                        release_check.ReleaseCheckError,
                        "cannot atomically publish"):
                release_check._persist_release_assets(output, sources)
            self.assertEqual(list(output.iterdir()), [])

    def test_release_output_normalizes_macos_var_symlink_spelling(self):
        with tempfile.TemporaryDirectory() as directory:
            private_root = pathlib.Path(directory).resolve()
            private_text = str(private_root)
            if not private_text.startswith("/private/var/"):
                self.skipTest("macOS /var alias is not present")
            public_root = pathlib.Path(
                private_text.replace("/private/var/", "/var/", 1))
            source = private_root / "artifact.whl"
            source.write_bytes(b"wheel")
            published = release_check._persist_release_assets(
                public_root / "dist", [source])
            self.assertEqual(
                published,
                [private_root / "dist" / "artifact.whl"])


if __name__ == "__main__":
    unittest.main()
