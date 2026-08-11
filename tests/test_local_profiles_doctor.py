import pathlib
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

import msx_mcp_server  # noqa: E402
import mcp_metadata  # noqa: E402


class _FakeOpenMSX:
    instances = []
    root = None
    fail_machine = None
    transport_supported = True

    def __init__(self, machine, extensions=(), harddisk=None, home=None,
                 bin=None, *, config_mode="isolated", platform=None):
        self.machine = machine
        self.extensions = list(extensions)
        self.harddisk = harddisk
        self.config_mode = config_mode
        self.platform = platform or "win32"
        self.home = str(home or self.root)
        self.effective_home = self.home if config_mode != "user" else None
        self.user_home = str(pathlib.Path(self.root) / "user")
        self.bin = str(bin or pathlib.Path(self.root) / "openmsx.exe")
        self.control_transport = "tcp_sspi"
        self.transport = self.control_transport
        self.attach_supported = False
        self.started = False
        self.closed = False
        self.powered = False
        self.advanced = []
        self.__class__.instances.append(self)

    def preflight(self):
        machine_config = (
            pathlib.Path(self.home) / "share" / "machines" /
            f"{self.machine}.xml")
        executable = pathlib.Path(self.bin)
        problems = []
        if not executable.is_file():
            problems.append("openMSX executable not found")
        if not machine_config.is_file():
            problems.append(
                f"machine configuration {self.machine}.xml was not found")
        if self.transport_supported:
            problems.append(
                "Windows attach is unavailable in this test adapter; "
                "authenticated tcp_sspi owned boot remains available")
        else:
            problems.append(
                "Windows SSPI control helper is unavailable for owned boot "
                "and attach")
        return {
            "ready": (executable.is_file() and machine_config.is_file() and
                      self.transport_supported),
            "platform": "windows",
            "control_transport": "tcp_sspi",
            "control_transport_supported": self.transport_supported,
            "boot_supported": self.transport_supported,
            "attach_transport": "tcp_sspi",
            "attach_supported": False,
            "config_mode": self.config_mode,
            "machine": self.machine,
            "executable": str(executable),
            "executable_found": executable.is_file(),
            "home": self.home,
            "user_home": self.user_home,
            "home_exists": pathlib.Path(self.home).is_dir(),
            "machine_config_found": machine_config.is_file(),
            "machine_config_candidates": [str(machine_config)],
            "problems": problems,
        }

    def start(self, *, headless=True):
        self.started = True
        self.headless = headless
        return self

    def power_on(self):
        if self.machine == self.fail_machine:
            raise RuntimeError("configured firmware is unavailable")
        self.powered = True

    def advance(self, seconds):
        self.advanced.append(seconds)

    def cmd(self, _command):
        return ""

    def screen_text(self):
        return f"SCREEN {self.machine}"

    def screen_mode(self):
        return 0

    def close(self):
        self.closed = True


def _machine_xml(filename, sha1):
    return (
        "<msxconfig><devices><primary><ROM><rom>"
        f"<filename>{filename}</filename><sha1>{sha1}</sha1>"
        "</rom></ROM></primary></devices></msxconfig>")


class LocalProfilesDoctorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        (self.root / "share" / "machines").mkdir(parents=True)
        (self.root / "share" / "systemroms").mkdir(parents=True)
        (self.root / "openmsx.exe").write_bytes(b"exe")
        _FakeOpenMSX.root = self.root
        _FakeOpenMSX.instances = []
        _FakeOpenMSX.fail_machine = None
        _FakeOpenMSX.transport_supported = True
        self.previous = msx_mcp_server.SESSION
        msx_mcp_server.SESSION = msx_mcp_server.Session()

    def tearDown(self):
        try:
            msx_mcp_server.SESSION.shutdown_all()
        finally:
            msx_mcp_server.SESSION = self.previous
            self.temporary.cleanup()

    def add_machine(self, machine, rom):
        sha1 = "1" * 40 if machine == msx_mcp_server.BASIC_MACHINE else "2" * 40
        config = self.root / "share" / "machines" / f"{machine}.xml"
        config.write_text(_machine_xml(rom, sha1), encoding="utf-8")
        return sha1

    def add_rom(self, filename):
        (self.root / "share" / "systemroms" / filename).write_bytes(b"rom")

    def test_boot_schema_advertises_profiles_and_configuration_modes(self):
        schema = msx_mcp_server.TOOLS["msx_local_boot"][2]
        self.assertEqual(schema["properties"]["profile"]["enum"], [
            "basic", "disk", "dos", "msx2plus", "cbios", "auto",
        ])
        self.assertEqual(schema["properties"]["config_mode"]["enum"], [
            "isolated", "user", "overlay",
        ])
        self.assertIn("msx_local_doctor", msx_mcp_server.TOOLS)

    def test_existing_profile_constructor_semantics_are_unchanged(self):
        dos_image = self.root / "msxdos.dsk"
        dos_image.write_bytes(b"disk")
        with mock.patch.object(msx_mcp_server, "DOS_HDD", dos_image):
            basic = msx_mcp_server._profile_arguments("basic")
            disk = msx_mcp_server._profile_arguments("disk")
            dos = msx_mcp_server._profile_arguments("dos")
            msx2plus = msx_mcp_server._profile_arguments("msx2plus")

        self.assertEqual(basic, {
            "machine": msx_mcp_server.BASIC_MACHINE, "extensions": [],
        })
        self.assertEqual(disk, {
            "machine": msx_mcp_server.BASIC_MACHINE,
            "extensions": [msx_mcp_server.DISK_EXTENSION],
        })
        self.assertEqual(dos, {
            "machine": msx_mcp_server.BASIC_MACHINE,
            "extensions": [msx_mcp_server.DOS_EXTENSION],
            "harddisk": str(dos_image),
        })
        self.assertEqual(msx2plus, {
            "machine": msx_mcp_server.MSX2PLUS_MACHINE, "extensions": [],
        })

    def test_doctor_has_specific_read_only_local_output_contract(self):
        hints = mcp_metadata.hints_for("msx_local_doctor")
        self.assertTrue(hints.read_only)
        self.assertTrue(hints.idempotent)
        self.assertFalse(hints.destructive)
        self.assertFalse(hints.open_world)
        schema = mcp_metadata.output_schema_for("msx_local_doctor")
        self.assertIs(schema, mcp_metadata.LOCAL_DOCTOR_OUTPUT_SCHEMA)
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("persistent_process_started", schema["properties"])

    def test_cbios_boot_propagates_config_and_machine_status(self):
        with mock.patch.object(msx_mcp_server, "OpenMSX", _FakeOpenMSX):
            screen = msx_mcp_server.SESSION.boot(
                "cbios", boot_seconds=2, window=True, config_mode="user")

        self.assertEqual(screen, f"SCREEN {msx_mcp_server.CBIOS_MACHINE}")
        machine = _FakeOpenMSX.instances[0]
        self.assertEqual(machine.machine, msx_mcp_server.CBIOS_MACHINE)
        self.assertEqual(machine.config_mode, "user")
        self.assertFalse(machine.headless)
        status = msx_mcp_server._status_for("local")
        self.assertEqual(status["profile"], "cbios")
        self.assertEqual(status["requested_profile"], "cbios")
        self.assertEqual(status["resolved_profile"], "cbios")
        self.assertEqual(status["machine"], msx_mcp_server.CBIOS_MACHINE)
        self.assertEqual(status["config_mode"], "user")
        self.assertEqual(status["control_transport"], "tcp_sspi")

    def test_auto_falls_back_only_after_basic_boot_failure(self):
        _FakeOpenMSX.fail_machine = msx_mcp_server.BASIC_MACHINE
        with mock.patch.object(msx_mcp_server, "OpenMSX", _FakeOpenMSX):
            screen = msx_mcp_server.SESSION.boot(
                "auto", boot_seconds=1, config_mode="overlay")

        first, second = _FakeOpenMSX.instances
        self.assertEqual(first.machine, msx_mcp_server.BASIC_MACHINE)
        self.assertTrue(first.closed)
        self.assertEqual(second.machine, msx_mcp_server.CBIOS_MACHINE)
        self.assertIn(msx_mcp_server.CBIOS_MACHINE, screen)
        status = msx_mcp_server._status_for("local")
        self.assertEqual(status["profile"], "cbios")
        self.assertEqual(status["requested_profile"], "auto")
        self.assertEqual(status["config_mode"], "overlay")
        self.assertEqual(status["effective_config_home"], str(self.root))

    def test_doctor_is_read_only_and_reports_windows_transport_split(self):
        self.add_machine(msx_mcp_server.CBIOS_MACHINE, "cbios.rom")
        self.add_rom("cbios.rom")
        with mock.patch.object(msx_mcp_server, "OpenMSX", _FakeOpenMSX):
            result = msx_mcp_server.t_local_doctor(
                profile="cbios", config_mode="isolated")

        self.assertTrue(result["ready"])
        self.assertEqual(result["resolved_profile"], "cbios")
        self.assertEqual(result["control_transport"], "tcp_sspi")
        self.assertTrue(result["control_transport_supported"])
        self.assertTrue(result["transport_ready"])
        self.assertTrue(result["boot_supported"])
        self.assertEqual(result["attach_transport"], "tcp_sspi")
        self.assertFalse(result["attach_supported"])
        self.assertFalse(result["persistent_process_started"])
        self.assertFalse(_FakeOpenMSX.instances[0].started)
        self.assertIn(
            "windows-sspi-unavailable",
            {issue["code"] for issue in result["issues"]})
        schema = mcp_metadata.LOCAL_DOCTOR_OUTPUT_SCHEMA
        self.assertEqual(set(result), set(schema["required"]))
        candidate_schema = schema["properties"]["candidates"]["items"]
        for candidate in result["candidates"]:
            self.assertLessEqual(set(candidate),
                                 set(candidate_schema["properties"]))
            self.assertLessEqual(set(candidate_schema["required"]),
                                 set(candidate))

    def test_doctor_blocks_windows_boot_when_sspi_is_unavailable(self):
        self.add_machine(msx_mcp_server.CBIOS_MACHINE, "cbios.rom")
        self.add_rom("cbios.rom")
        _FakeOpenMSX.transport_supported = False
        with mock.patch.object(msx_mcp_server, "OpenMSX", _FakeOpenMSX):
            result = msx_mcp_server.t_local_doctor(profile="cbios")

        self.assertFalse(result["ready"])
        self.assertFalse(result["boot_supported"])
        self.assertFalse(result["transport_ready"])
        issues = [issue for issue in result["issues"]
                  if issue["code"] == "windows-sspi-unavailable"]
        self.assertTrue(issues)
        self.assertEqual(issues[0]["severity"], "error")

    def test_auto_doctor_resolves_unverified_basic_to_ready_cbios(self):
        self.add_machine(msx_mcp_server.BASIC_MACHINE, "proprietary.rom")
        self.add_machine(msx_mcp_server.CBIOS_MACHINE, "cbios.rom")
        self.add_rom("cbios.rom")
        with mock.patch.object(msx_mcp_server, "OpenMSX", _FakeOpenMSX):
            result = msx_mcp_server.t_local_doctor(
                profile="auto", config_mode="overlay")

        self.assertTrue(result["ready"])
        self.assertEqual(result["resolved_profile"], "cbios")
        self.assertEqual(result["candidates"][0]["rom_readiness"], "unverified")
        self.assertEqual(result["candidates"][1]["rom_readiness"], "ready")
        self.assertIn(
            "auto-profile-fallback",
            {issue["code"] for issue in result["issues"]})

    def test_isolated_hides_user_rom_pool_while_overlay_includes_it(self):
        self.add_machine(msx_mcp_server.BASIC_MACHINE, "user-only.rom")
        user_pool = self.root / "user" / "share" / "systemroms"
        user_pool.mkdir(parents=True)
        (user_pool / "user-only.rom").write_bytes(b"rom")
        with mock.patch.object(msx_mcp_server, "OpenMSX", _FakeOpenMSX):
            isolated = msx_mcp_server.t_local_doctor(
                profile="basic", config_mode="isolated")
            overlay = msx_mcp_server.t_local_doctor(
                profile="basic", config_mode="overlay")

        self.assertFalse(isolated["ready"])
        self.assertEqual(
            isolated["candidates"][0]["rom_readiness"], "unverified")
        self.assertTrue(overlay["ready"])
        self.assertEqual(overlay["candidates"][0]["rom_readiness"], "ready")


if __name__ == "__main__":
    unittest.main()
