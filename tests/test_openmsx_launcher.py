import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "open-msx.command"
SHORTCUT = ROOT / "open-msx-mcp.command"
STARTUP = ROOT / "tools" / "openmsx_mcp_test.tcl"


class OpenMSXMCPLauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = LAUNCHER.read_text(encoding="utf-8")
        cls.shortcut = SHORTCUT.read_text(encoding="utf-8")
        cls.startup = STARTUP.read_text(encoding="utf-8")

    def test_shell_launchers_are_valid(self):
        subprocess.run(
            ["bash", "-n", str(LAUNCHER), str(SHORTCUT)], check=True)
        self.assertTrue(LAUNCHER.stat().st_mode & 0o111)
        self.assertTrue(SHORTCUT.stat().st_mode & 0o111)

    def test_mcp_profile_is_single_instance_ipv4_and_disposable(self):
        self.assertIn("basic|disk|dos|msx2plus|mcp", self.launcher)
        self.assertIn("pgrep -x openmsx", self.launcher)
        self.assertIn("validate_ipv4", self.launcher)
        self.assertIn("MSX_AI_MCP_IPV4:-127.0.0.1", self.launcher)
        self.assertIn("MSX_AI_MCP_PORT:-6603", self.launcher)
        self.assertIn(
            "MSX_AI_MCP_SLOT_EXPANDER:-slotexpander", self.launcher)
        self.assertIn('make -C "$PROJECT_DIR" agent', self.launcher)
        self.assertIn('cp "$DOS_DISK" "$MCP_DISK"', self.launcher)
        self.assertIn(
            'cp "$PROJECT_DIR/tools/openmsx_mcp_test.tcl" "$MCP_SCRIPT"',
            self.launcher)
        self.assertIn("MCP_PACKAGE_NAMES=(MSXAI.COM MSXAIXF.COM", self.launcher)
        self.assertIn("MCP8251.TSR MCP16550.TSR", self.launcher)
        self.assertIn("MEMMAN.COM TL.COM TK.COM", self.launcher)
        self.assertIn(
            'cp "$suite_source" "$suite_destination"', self.launcher)
        self.assertIn(
            'MSX_AI_MCP_SUITE_DIR="$MCP_RUNTIME_DIR"', self.launcher)
        slot_expander = self.launcher.index('-ext "$MCP_SLOT_EXPANDER"')
        dos_extension = self.launcher.index(
            '-ext "$DOS_EXTENSION"', slot_expander)
        rs232_extension = self.launcher.index(
            "-ext rs232_proto", dos_extension)
        self.assertLess(slot_expander, dos_extension)
        self.assertLess(dos_extension, rs232_extension)
        self.assertIn('-script "$MCP_SCRIPT"', self.launcher)

    def test_startup_keeps_agent_manual_and_retries_transport(self):
        self.assertNotIn('//type "MSXAI ', self.startup)
        self.assertIn("A:\\\\MSXAI is on PATH", self.startup)
        self.assertIn("MSXAI.COM MSXAIXF.COM MCP8251.TSR MCP16550.TSR",
                      self.startup)
        self.assertIn("MEMMAN.COM TL.COM TK.COM", self.startup)
        self.assertIn("diskmanipulator mkdir hda1 MSXAI", self.startup)
        self.assertIn("diskmanipulator chdir hda1 MSXAI", self.startup)
        self.assertIn("diskmanipulator chdir hda1 /", self.startup)
        self.assertIn("diskmanipulator delete hda1 $suite_name", self.startup)
        self.assertIn("diskmanipulator import hda1", self.startup)
        self.assertIn("MSX_AI_MCP_SUITE_DIR", self.startup)
        self.assertIn("SET MSXAI_HOME=A:\\\\MSXAI", self.startup)
        self.assertIn("PATH A:\\\\MSXAI;%PATH%", self.startup)
        self.assertIn('set rs232-net-address "$ipv4:$port"', self.startup)
        self.assertIn("set rs232-net-ip232 off", self.startup)
        self.assertIn("after realtime $retry_seconds", self.startup)
        self.assertIn('bind "keyb F11"', self.startup)
        self.assertIn("set renderer SDLGL-PP", self.startup)
        self.assertIn("set mute off", self.startup)
        self.assertIn("set throttle on", self.startup)


if __name__ == "__main__":
    unittest.main()
