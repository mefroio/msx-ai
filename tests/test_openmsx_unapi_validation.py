"""Deterministic tests plus one explicitly opt-in openMSXnet E2E."""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import openmsx_unapi_validation as harness  # noqa: E402


class OpenMSXUNAPIHarnessUnitTests(unittest.TestCase):
    def test_port_default_is_custom_and_ffff_is_never_allowed(self):
        self.assertEqual(harness.DEFAULT_TEST_PORT, 43123)
        self.assertEqual(harness.validate_port("43123"), 43123)
        self.assertEqual(harness.validate_port(1), 1)
        self.assertEqual(harness.validate_port(65534), 65534)
        for invalid in (0, -1, 65535, 65536):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    harness.validate_port(invalid)
        with self.assertRaises(TypeError):
            harness.validate_port(True)
        with self.assertRaises(TypeError):
            harness.validate_port(1.5)
        with self.assertRaises(ValueError):
            harness.validate_port("not-a-port")

    def test_exact_platform_assets_are_pinned(self):
        self.assertEqual(
            harness.asset_name_for("darwin", "arm64"),
            "openmsx-macos-arm64.zip")
        self.assertEqual(
            harness.asset_name_for("linux", "x86_64"),
            "openmsx-linux-x86_64.zip")
        self.assertEqual(
            harness.asset_name_for("win32", "AMD64"),
            "openmsx-windows-x86_64.zip")
        with self.assertRaises(harness.PrerequisiteError):
            harness.asset_name_for("darwin", "x86_64")
        self.assertEqual(set(harness.ASSET_SHA256), {
            "openmsx-macos-arm64.zip",
            "openmsx-linux-x86_64.zip",
            "openmsx-windows-x86_64.zip",
        })
        self.assertEqual(harness.RELEASE, "v0.9.7")

    def test_verify_hash_accepts_exact_bytes_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "asset.bin"
            path.write_bytes(b"pinned bytes")
            digest = hashlib.sha256(b"pinned bytes").hexdigest()
            self.assertEqual(
                harness.verify_hash(path, digest, "fixture"), digest)
            path.write_bytes(b"changed bytes")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "SHA-256 mismatch"):
                harness.verify_hash(path, digest, "fixture")

    def test_safe_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escaped", b"bad")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "unsafe path"):
                harness.safe_extract_zip(archive, root / "out")
            self.assertFalse((root / "escaped").exists())

    def test_safe_extract_rejects_windows_separator_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "bad-windows.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(r"..\escaped", b"bad")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "unsafe path"):
                harness.safe_extract_zip(archive, root / "out")

    def test_safe_extract_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "link.zip"
            link = zipfile.ZipInfo("share/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(link, "../../outside")
            with self.assertRaisesRegex(
                    harness.PrerequisiteError, "Symbolic link|symbolic link"):
                harness.safe_extract_zip(archive, root / "out")

    def test_install_commands_use_unapi_custom_port(self):
        self.assertEqual(harness.AGENT_PACKAGE_NAMES, (
            "MSXAI.COM", "MSXAIXF.COM", "MCP8251.TSR", "MCP16550.TSR",
            "MCPUNAPI.TSR", "MP.COM", "MEMMAN.COM", "TL.COM", "TK.COM",
        ))
        self.assertEqual(harness.msx_install_commands(43123), (
            r"SET MSXAI_HOME=A:\MSXAI",
            r"PATH A:\MSXAI;%PATH%",
            "UNAPINET",
            "MSXAI /DRIVER:UNAPI /PORT:43123",
        ))
        with self.assertRaises(ValueError):
            harness.msx_install_commands(65535)

    def test_contract_names_explain_exact_coverage_and_limit(self):
        contract = "\n".join(harness.CONTRACT_PATH)
        for required in (
                "discovery", "GET_CAPAB", "TCP_OPEN", "TCP_STATE",
                "TCP_SEND", "TCP_RCV", "relisten", "public MCP"):
            self.assertIn(required.lower(), contract.lower())
        self.assertEqual(harness.MCP_TOOLS_EXERCISED, (
            "msx_agent_connect",
            "msx_agent_status",
            "msx_agent_memory_read",
            "msx_agent_disconnect",
        ))
        not_emulated = "\n".join(harness.NOT_EMULATED).lower()
        self.assertIn("firmware", not_emulated)
        self.assertIn("bus timing", not_emulated)
        self.assertTrue(
            harness.PINNED_GET_CAPAB_BLOCK1_HL &
            harness.PASSIVE_UNSPECIFIED_REMOTE_BIT)

    def test_e2e_uses_public_mcp_instead_of_direct_realmsx(self):
        source = pathlib.Path(harness.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from msx_real import RealMSX", source)
        self.assertIn('"-m", "server", "--transport", "stdio"', source)
        for tool_name in harness.MCP_TOOLS_EXERCISED:
            self.assertIn(tool_name, source)

    def test_screen_check_accepts_passive_banner_and_rejects_real_errors(self):
        harness._assert_command_screen(
            "MSXAI /DRIVER:UNAPI /PORT:43123",
            "Driver: TCP/IP UNAPI passive listener\n  A:\\>",
            prompt=True,
        )
        with self.assertRaisesRegex(
                harness.ValidationError, "Transport initialization failed"):
            harness._assert_command_screen(
                "MSXAI /DRIVER:UNAPI /PORT:43123",
                "Transport initialization failed\nA:\\>",
                prompt=True,
            )
        with self.assertRaisesRegex(
                harness.ValidationError, "UnapiNet extension not found"):
            harness._assert_command_screen(
                "UNAPINET",
                "ERROR: openMSX UnapiNet extension not found.\nA:\\>",
                prompt=True,
            )
        with self.assertRaisesRegex(
                harness.ValidationError, "transport initialization failed"):
            harness._assert_command_screen(
                "MSXAI /DRIVER:UNAPI /PORT:43123",
                "MSX-AI transport initialization faile\n"
                "d\n  A:\\>",
                prompt=True,
            )
        with self.assertRaisesRegex(
                harness.ValidationError, "msxai unapi relisten failed"):
            harness._assert_command_screen(
                "MSXAI /DRIVER:UNAPI /PORT:43123",
                "MP: MSXAI UNAPI relisten failed.\nA:\\>",
                prompt=True,
            )

    def _fake_inputs(self, root: pathlib.Path):
        archive = root / "openmsx-macos-arm64.zip"
        xml = b"<msxconfig><devices><UnapiNet/></devices></msxconfig>\n"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("openmsx", b"fake executable")
            output.writestr("share/extensions/unapinet.xml", xml)
            output.writestr(
                "share/extensions/SunriseIDE_Nextor.xml", b"<msxconfig/>\n")
            output.writestr(
                "share/extensions/slotexpander.xml", b"<msxconfig/>\n")
            output.writestr(
                "share/extensions/ram512k.xml", b"<msxconfig/>\n")
        unapinet_com = root / "UNAPINET.COM"
        unapinet_com.write_bytes(b"U" * harness.UNAPINET_COM_SIZE)
        disk = root / "msxdos.dsk"
        disk.write_bytes(b"disk")
        home = root / "home"
        (home / "share/machines").mkdir(parents=True)
        (home / "share/machines/Gradiente_Expert20.xml").write_text(
            "<msxconfig/>\n", encoding="utf-8")
        (home / "share/systemroms").mkdir(parents=True)
        nextor = home / "share/systemroms/Nextor.rom"
        nextor.write_bytes(b"licensed fixture ROM")
        settings = harness.Settings(
            archive=archive,
            unapinet_com=unapinet_com,
            dos_hdd=disk,
            openmsx_home=home,
            port=43123,
            root=ROOT,
        )
        hashes = {
            "archive": harness.sha256_file(archive),
            "com": harness.sha256_file(unapinet_com),
            "xml": hashlib.sha256(xml).hexdigest(),
            "nextor": harness.sha1_file(nextor),
        }
        return settings, hashes

    def test_preflight_is_reproducible_without_real_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, hashes = self._fake_inputs(pathlib.Path(directory))
            runner = mock.Mock(return_value=subprocess.CompletedProcess(
                args=["openmsx", "--version"], returncode=0,
                stdout="openMSX 21.0\nflavour: opt\n", stderr=""))
            with (mock.patch.dict(
                      harness.ASSET_SHA256,
                      {"openmsx-macos-arm64.zip": hashes["archive"]}),
                  mock.patch.object(
                      harness, "UNAPINET_COM_SHA256", hashes["com"]),
                  mock.patch.object(
                      harness, "UNAPINET_XML_SHA256", hashes["xml"]),
                  mock.patch.object(
                      harness, "NEXTOR_ROM_SHA1S", frozenset({hashes["nextor"]}))):
                report = harness.preflight(
                    settings,
                    platform_name="darwin",
                    architecture="arm64",
                    runner=runner,
                )

            self.assertTrue(report["ready"], report["problems"])
            self.assertEqual(report["custom_port"], 43123)
            self.assertFalse(report["pico_firmware_emulated"])
            runner.assert_called_once()
            call = runner.call_args
            self.assertEqual(call.args[0][1], "--version")
            self.assertEqual(call.kwargs["timeout"], 15)
            self.assertIn("OPENMSX_SYSTEM_DATA", call.kwargs["env"])
            self.assertIn("OPENMSX_HOME", call.kwargs["env"])
            self.assertIn(
                "openmsxnet-", call.kwargs["env"]["OPENMSX_SYSTEM_DATA"])

    def test_preflight_reports_missing_dynamic_library_actionably(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, hashes = self._fake_inputs(pathlib.Path(directory))
            runner = mock.Mock(return_value=subprocess.CompletedProcess(
                args=["openmsx", "--version"], returncode=1, stdout="",
                stderr=(
                    "dyld: Library not loaded: "
                    "/opt/homebrew/opt/libogg/lib/libogg.0.dylib")))
            with (mock.patch.dict(
                      harness.ASSET_SHA256,
                      {"openmsx-macos-arm64.zip": hashes["archive"]}),
                  mock.patch.object(
                      harness, "UNAPINET_COM_SHA256", hashes["com"]),
                  mock.patch.object(
                      harness, "UNAPINET_XML_SHA256", hashes["xml"]),
                  mock.patch.object(
                      harness, "NEXTOR_ROM_SHA1S", frozenset({hashes["nextor"]}))):
                report = harness.preflight(
                    settings,
                    platform_name="darwin",
                    architecture="arm64",
                    runner=runner,
                )
        self.assertFalse(report["ready"])
        problem = "\n".join(report["problems"])
        self.assertIn("libogg.0.dylib", problem)
        self.assertIn("runtime libraries", problem)
        self.assertIn("never installs", problem)


@unittest.skipUnless(
    os.environ.get("MSX_RUN_UNAPI_INTEGRATION") == "1",
    "set MSX_RUN_UNAPI_INTEGRATION=1 and supply pinned v0.9.7 assets",
)
class OpenMSXUNAPIIntegrationTest(unittest.TestCase):
    def test_discovery_passive_tcp_send_receive_and_foreground_relisten(self):
        parser = harness.build_parser()
        namespace = parser.parse_args(["run"])
        try:
            settings = harness.settings_from_namespace(namespace)
        except harness.PrerequisiteError as exc:
            self.skipTest(str(exc))
        report = harness.preflight(settings)
        if not report["ready"]:
            self.skipTest("UNAPI E2E prerequisites unavailable: " +
                          "; ".join(report["problems"]))
        result = harness.run_validation(settings)
        self.assertTrue(result["ok"])
        self.assertEqual(result["custom_port"], settings.port)
        self.assertEqual(
            result["host_control_path"], "public MCP tools over STDIO")
        self.assertEqual(
            result["mcp_tools_exercised"],
            list(harness.MCP_TOOLS_EXERCISED))
        self.assertEqual(
            result["first_connection"]["agent_transport"], "tcpip-unapi")
        self.assertEqual(
            result["second_connection"]["agent_transport"], "tcpip-unapi")
        self.assertTrue(result["foreground_relisten_after_host_close"])
        self.assertTrue(
            result["memory_compare"]["matched_openmsx_debugger"])
        self.assertFalse(result["pico_firmware_emulated"])


if __name__ == "__main__":
    unittest.main()
