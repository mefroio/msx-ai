import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import msx_mcp_server  # noqa: E402
from msx_client import OpenMSXError  # noqa: E402


class CanonicalAgentBuildTest(unittest.TestCase):
    def test_build_uses_make_target_and_returns_final_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "work" / "agent" / "MSXAI.COM"

            def make_agent(*args, **kwargs):
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(b"universal-agent")
                return subprocess.CompletedProcess(args[0], 0, "built", "")

            with (mock.patch.object(msx_mcp_server, "AGENT_COM", artifact),
                  mock.patch.object(msx_mcp_server, "MAKE", "test-make"),
                  mock.patch.object(
                      msx_mcp_server.subprocess, "run",
                      side_effect=make_agent) as run):
                result = msx_mcp_server._build_agent_artifact()

            self.assertEqual(result, artifact)
            self.assertEqual(result.read_bytes(), b"universal-agent")
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["test-make", "agent"])
            self.assertEqual(kwargs["cwd"], msx_mcp_server.PROJ)
            self.assertEqual(kwargs["env"]["Z80ASM"], msx_mcp_server.Z80ASM)

    def test_success_without_final_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "work" / "agent" / "MSXAI.COM"
            completed = subprocess.CompletedProcess(
                ["test-make", "agent"], 0, "", "")
            with (mock.patch.object(msx_mcp_server, "AGENT_COM", artifact),
                  mock.patch.object(
                      msx_mcp_server.subprocess, "run",
                      return_value=completed)):
                with self.assertRaisesRegex(OpenMSXError, "did not produce"):
                    msx_mcp_server._build_agent_artifact()

    def test_dos_prompt_must_be_last_visible_row(self):
        self.assertTrue(msx_mcp_server._dos_prompt_visible(
            "MSX-DOS 2\nA:\\>\n\n"))
        self.assertTrue(msx_mcp_server._dos_prompt_visible(
            "C:\\GAMES>\n"))
        self.assertFalse(msx_mcp_server._dos_prompt_visible(
            "A:\\>\ninstalling resident agent"))


class _FakeMachine:
    def __init__(self, screen="MSX-DOS 2\nA:\\>"):
        self.screen = screen
        self.commands = []
        self.advances = []
        self.typed = []
        self.imported_agent = None
        self.closed = False

    def start(self, *, headless):
        self.headless = headless
        return self

    def power_on(self):
        pass

    def advance(self, seconds):
        self.advances.append(seconds)

    def cmd(self, command):
        if command.startswith("debug read memory"):
            raise AssertionError("bench must not inspect a fixed resident address")
        self.commands.append(command)
        if command.startswith("diskmanipulator import hda1"):
            path = re.search(r"\{(.+)\}", command).group(1)
            self.imported_agent = Path(path).read_bytes()
        return ""

    def type_line(self, command):
        self.typed.append(command)

    def screen_text(self):
        return self.screen

    def close(self):
        self.closed = True


class _FakeRealAgent:
    def __init__(self, host, port, runtime_mode="resident"):
        self.host = host
        self.port = 45678 if int(port) == 0 else int(port)
        self.runtime_mode = runtime_mode
        self.simulation = None
        self.accepted_timeout = None
        self.closed = False

    def listen(self):
        return self

    def accept(self, timeout):
        self.accepted_timeout = timeout
        return ("127.0.0.1", 65000)

    def status(self):
        return {"state": "monitor"}

    def close(self):
        self.closed = True


class TCPBenchHostFlowTest(unittest.TestCase):
    def test_resident_bench_imports_canonical_artifact_and_uses_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disk = root / "msxdos.dsk"
            artifact = root / "MSXAI.COM"
            disk.write_bytes(b"disk")
            artifact.write_bytes(b"canonical-universal-agent")
            machine = _FakeMachine()
            real = _FakeRealAgent("127.0.0.1", 0)

            def copy_home(_source, destination, **_kwargs):
                Path(destination).mkdir(parents=True)

            session = msx_mcp_server.Session()
            try:
                with (mock.patch.object(msx_mcp_server, "DOS_HDD", disk),
                      mock.patch.object(
                          msx_mcp_server, "_build_agent_artifact",
                          return_value=artifact) as build,
                      mock.patch.object(
                          msx_mcp_server.shutil, "copytree",
                          side_effect=copy_home),
                      mock.patch.object(
                          msx_mcp_server, "OpenMSX", return_value=machine),
                      mock.patch.object(
                          msx_mcp_server, "RealMSX", return_value=real)):
                    peer = session.start_tcp_bench(timeout=12)

                self.assertEqual(peer, ("127.0.0.1", 65000))
                build.assert_called_once_with()
                self.assertEqual(
                    machine.imported_agent, b"canonical-universal-agent")
                self.assertEqual(machine.typed, ["MSXAI /DRIVER:8251"])
                self.assertIn(
                    msx_mcp_server.RESIDENT_INSTALL_SECONDS,
                    machine.advances)
                self.assertEqual(real.accepted_timeout, 12.0)
                self.assertEqual(real.simulation, "openmsx-rs232-net")
                self.assertFalse(any(
                    command.startswith("debug read memory")
                    for command in machine.commands))
            finally:
                session.shutdown()


if __name__ == "__main__":
    unittest.main()
