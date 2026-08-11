import os
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from msx_client import OpenMSX
import msx_mcp_server


RUN_SMOKE = os.environ.get("MSX_RUN_OPENMSX_SMOKE") == "1"


@unittest.skipUnless(
    RUN_SMOKE,
    "set MSX_RUN_OPENMSX_SMOKE=1 to run the real openMSX/C-BIOS smoke test")
class OpenMSXCBiosIntegrationTests(unittest.TestCase):
    def _find_machine(self):
        explicit = os.environ.get("MSX_OPENMSX_SMOKE_MACHINE")
        candidates = ([explicit] if explicit else [
            "C-BIOS_MSX2", "C-BIOS_MSX2+", "C-BIOS_MSX1", "C-BIOS",
        ])
        reports = []
        for name in candidates:
            machine = OpenMSX(machine=name, extensions=())
            report = machine.preflight()
            reports.append(report)
            if report["executable_found"] and report["machine_config_found"]:
                return name, report["executable"]
        if not any(report["executable_found"] for report in reports):
            self.skipTest(
                "openMSX executable is unavailable; set OPENMSX_BIN to run "
                "the integration smoke test")
        searched = sorted({
            path for report in reports
            for path in report["machine_config_candidates"]
        })
        self.skipTest(
            "openMSX is installed but no C-BIOS machine configuration was "
            f"found (searched: {', '.join(searched)})")

    def test_owned_cbios_boot_command_and_cleanup(self):
        machine_name, executable = self._find_machine()
        config_mode = os.environ.get(
            "MSX_OPENMSX_SMOKE_CONFIG_MODE", "isolated")
        emulator = OpenMSX(
            machine=machine_name, extensions=(), bin=executable,
            config_mode=config_mode)
        process = None
        try:
            emulator.start(headless=True)
            process = emulator.proc
            version = emulator.cmd("openmsx_info version").strip()
            self.assertIn("openMSX", version)
            emulator.cmd("set power on")
            self.assertIn(
                emulator.cmd("set power").strip().lower(),
                {"1", "true", "on", "yes"})
            mode = int(emulator.cmd("get_screen_mode_number").strip())
            self.assertGreaterEqual(mode, 0)
        finally:
            emulator.close()

        self.assertIsNone(emulator.proc)
        self.assertIsNotNone(process)
        self.assertIsNotNone(process.poll(), "owned openMSX process was orphaned")

    def test_session_cbios_profile_status_readonly_command_and_shutdown(self):
        # Resolve availability through the same adapter preflight so a
        # deliberately enabled smoke job still skips clearly on machines where
        # openMSX/C-BIOS was not installed.
        self._find_machine()
        session = msx_mcp_server.Session()
        process = None
        with mock.patch.object(msx_mcp_server, "SESSION", session):
            try:
                screen = session.boot(
                    profile="cbios", boot_seconds=0.05, window=False,
                    config_mode="isolated")
                self.assertIsInstance(screen, str)
                machine, profile = session.backend("local")
                self.assertEqual(profile, "cbios")
                self.assertEqual(machine.config_mode, "isolated")
                process = machine.proc

                status = msx_mcp_server.t_status()
                self.assertEqual(status["state"], "connected")
                self.assertEqual(status["profile"], "cbios")
                self.assertEqual(status["config_mode"], "isolated")
                version = msx_mcp_server.t_cmd(
                    "openmsx_info version").strip()
                self.assertIn("openMSX", version)
            finally:
                session.shutdown_local()

            self.assertEqual(
                msx_mcp_server.t_status(),
                {"backend": "none", "state": "disconnected"})

        self.assertIsNotNone(process)
        self.assertIsNotNone(process.poll(), "Session left an openMSX orphan")


if __name__ == "__main__":
    unittest.main()
