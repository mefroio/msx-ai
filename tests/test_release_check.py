import asyncio
import pathlib
import tempfile
import sys
import tarfile
import types
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
            "project/.MCP.JSON",
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
            self.assertEqual(environment["USERPROFILE"], str(probe / "home"))
            release_check._assert_no_state(probe, "test probe")

    def test_runtime_smoke_exercises_both_stdio_eras_and_http(self):
        entrypoint = pathlib.Path("/installed/msx-ai-mcp")
        with (mock.patch.object(
                  release_check, "_runtime_entrypoint",
                  return_value=entrypoint),
              mock.patch.object(release_check, "_run") as run,
              mock.patch.object(
                  release_check, "_run_http_smoke") as http,
              mock.patch.object(release_check, "_assert_no_state") as clean):
            self.assertEqual(release_check.run_runtime_smoke(), 0)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertEqual(
            [(command[-3], command[-1]) for command in commands],
            [("_mcp-stdio", "auto"), ("_mcp-stdio", "legacy")])
        self.assertEqual(
            [pathlib.Path(command[-2]) for command in commands],
            [entrypoint, entrypoint])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(clean.call_count, 2)
        environment = run.call_args_list[0].kwargs["env"]
        self.assertEqual(environment["HOME"], environment["USERPROFILE"])
        self.assertEqual(environment["MSX_RUN_INTEGRATION"], "0")

    def test_runtime_smoke_uses_unambiguous_target_inventory(self):
        client = mock.MagicMock()
        client.protocol_version = "test"
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        explicit_tools = [
            types.SimpleNamespace(name=name)
            for name in sorted(release_check._RUNTIME_REQUIRED_TOOLS)
        ]
        explicit_tools.extend(
            types.SimpleNamespace(name=f"extra_{index}")
            for index in range(35 - len(explicit_tools)))
        client.list_tools = mock.AsyncMock(return_value=types.SimpleNamespace(
            tools=explicit_tools))
        client.call_tool = mock.AsyncMock(return_value=types.SimpleNamespace(
            is_error=False,
            structured_content={"backend": "none", "state": "disconnected"}))
        client.list_resources = mock.AsyncMock(return_value=types.SimpleNamespace(
            resources=[types.SimpleNamespace(uri="docs://msx-ai/index")] * 8))
        client.read_resource = mock.AsyncMock(return_value=types.SimpleNamespace(
            contents=[types.SimpleNamespace(text="MSX-AI documentation")]))
        client.list_prompts = mock.AsyncMock(return_value=types.SimpleNamespace(
            prompts=[object(), object()]))

        protocol = asyncio.run(release_check._mcp_assertions(client, "test")())

        self.assertEqual(protocol, "test")
        client.call_tool.assert_awaited_once_with("msx_targets_status", {})

    def test_runtime_smoke_cli_is_explicit_and_bypasses_release_build(self):
        with (mock.patch.object(
                  release_check, "run_runtime_smoke", return_value=0) as smoke,
              mock.patch.object(release_check, "run_release_check") as release):
            self.assertEqual(
                release_check.main(["--runtime-smoke"]), 0)
        smoke.assert_called_once_with()
        release.assert_not_called()

    def test_runtime_smoke_requires_installed_entrypoint(self):
        with mock.patch.object(release_check.shutil, "which", return_value=None), \
                self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "installed msx-ai-mcp"):
            release_check._runtime_entrypoint({"PATH": "/missing"})

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
                (str(pathlib.Path("/resolved/gmake").resolve()),
                 str(pathlib.Path("/resolved/z80asm").resolve())))
        self.assertEqual(calls, [
            ("/custom/gmake", "/custom/bin"),
            ("custom-z80asm", "/custom/bin"),
        ])

    def test_windows_without_make_override_selects_portable_builder(self):
        calls = []

        def find_tool(name, *, path=None):
            calls.append((name, path))
            return {
                "make": "/incidental/make",
                "z80asm": "/resolved/z80asm",
            }.get(name)

        environment = {"PATH": "/custom/bin"}
        with mock.patch.object(
                release_check.shutil, "which", side_effect=find_tool):
            self.assertEqual(
                release_check._resolve_build_tools(
                    environment, platform="nt"),
                (None, str(pathlib.Path("/resolved/z80asm").resolve())))
        self.assertEqual(calls, [("z80asm", "/custom/bin")])

    def test_posix_without_make_keeps_existing_failure(self):
        with mock.patch.object(
                release_check.shutil, "which", return_value=None), \
                self.assertRaisesRegex(
                    release_check.ReleaseCheckError, "MAKE='make'"):
            release_check._resolve_build_tools(
                {"PATH": "/custom/bin"}, platform="posix")

    def test_windows_does_not_hide_invalid_explicit_make_override(self):
        with mock.patch.object(
                release_check.shutil, "which", return_value=None) as find, \
                self.assertRaisesRegex(
                    release_check.ReleaseCheckError,
                    "MAKE='/missing/gmake'"):
            release_check._resolve_build_tools({
                "PATH": "/custom/bin",
                "MAKE": "/missing/gmake",
            }, platform="nt")
        find.assert_called_once_with("/missing/gmake", path="/custom/bin")

    def test_agent_builder_preserves_make_path_when_available(self):
        environment = {"PATH": "/custom/bin"}
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory)
            agent = source / "work" / "agent"
            with (mock.patch.object(
                      release_check, "_resolve_build_tools",
                      return_value=("make-tool", "assembler-tool")),
                  mock.patch.object(
                      release_check, "_z80asm_version_line"),
                  mock.patch.object(release_check, "_run") as run,
                  mock.patch.object(
                      release_check, "_build_agent_suite_portable")
                  as portable,
                  mock.patch.object(
                      release_check, "_assert_agent_suite") as check):
                self.assertEqual(
                    release_check._build_agent_suite(source, environment),
                    agent)

        run.assert_called_once_with([
            "make-tool", "agent", f"PYTHON={sys.executable}",
            "Z80ASM=assembler-tool",
        ], cwd=source, env=environment)
        portable.assert_not_called()
        check.assert_called_once_with(agent)

    def test_windows_portable_builder_matches_make_recipe(self):
        environment = {"PATH": "/custom/bin"}
        assembler = "C:/Program Files/z80asm/z80asm.exe"
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory)
            agent = source / "work" / "agent"
            build = agent / "build"
            tools = source / "tools"
            with (mock.patch.object(
                      release_check, "_resolve_build_tools",
                      return_value=(None, assembler)),
                  mock.patch.object(
                      release_check, "_z80asm_version_line") as version,
                  mock.patch.object(release_check, "_run") as run,
                  mock.patch.object(
                      release_check, "_assert_agent_suite") as check):
                self.assertEqual(
                    release_check._build_agent_suite(source, environment),
                    agent)

            expected = [
                [
                    sys.executable, str(tools / "materialize_memman.py"),
                    "--source-dir", str(source / "third_party" / "memman"),
                    "--output-dir", str(agent),
                ],
                [
                    sys.executable, str(tools / "build_agent_tsr.py"),
                    "--repository", str(source), "--assembler", assembler,
                    "--output", str(build / "MSXAI.TSR"),
                    "--metadata-output", str(build / "MSXAI_TSR.INC"),
                    "--8251-output", str(agent / "MCP8251.TSR"),
                    "--16c550-output", str(agent / "MCP16550.TSR"),
                ],
                [
                    assembler, str(source / "agent" / "msx_agent.asm"),
                    "-o", str(agent / "MSXAI.COM"),
                ],
                [
                    sys.executable, str(tools / "check_msx_com_size.py"),
                    str(agent / "MSXAI.COM"), "36760",
                ],
                [
                    assembler, str(source / "agent" / "msx_xfer.asm"),
                    "-o", str(agent / "MSXAIXF.COM"),
                ],
                [
                    sys.executable, str(tools / "check_msx_com_size.py"),
                    str(agent / "MSXAIXF.COM"), "16128",
                ],
            ]

        self.assertEqual([call.args[0] for call in run.call_args_list], expected)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs, {"cwd": source, "env": environment})
        version.assert_called_once_with(assembler, environment)
        check.assert_called_once_with(agent)

    def test_make_failure_is_not_retried_with_portable_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory)
            with (mock.patch.object(
                      release_check, "_resolve_build_tools",
                      return_value=("make-tool", "assembler-tool")),
                  mock.patch.object(
                      release_check, "_z80asm_version_line"),
                  mock.patch.object(
                      release_check, "_run",
                      side_effect=release_check.ReleaseCheckError(
                          "make failed")) as run,
                  mock.patch.object(
                      release_check, "_build_agent_suite_portable")
                  as portable,
                  self.assertRaisesRegex(
                      release_check.ReleaseCheckError, "make failed")):
                release_check._build_agent_suite(source, {})

        self.assertEqual(run.call_count, 1)
        portable.assert_not_called()

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
            self.assertTrue((source / ".mcp.json").is_file())
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
        (source / "LICENSE").write_bytes(b"project GPL license\n")
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
                self.assertEqual(
                    manifest["creator"], "Rodrigo Galhardi M. Garcia")
                self.assertEqual(manifest["agent"], "2.0")
                self.assertEqual(manifest["wire"], "v3")
                self.assertEqual(manifest["transfer"], "fast-v1")
                self.assertEqual(
                    manifest["toolchain"]["id"], "bas-wijnen-z80asm")
                self.assertEqual(manifest["toolchain"]["version"], "1.8")
                self.assertEqual(
                    archive.read("LICENSE"), b"project GPL license\n")
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
