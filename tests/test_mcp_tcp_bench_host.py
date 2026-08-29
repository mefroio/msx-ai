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
    def test_package_names_include_complete_twelve_file_suite(self):
        self.assertEqual(
            msx_mcp_server.AGENT_PACKAGE_NAMES,
            (
                "MSXAI.COM", "MSXAIXF.COM", "MCP8251.TSR",
                "MCP16550.TSR", "MCP115K.TSR", "MCPUNAPI.TSR", "TU.COM",
                "MP.COM", "BADINIT.COM", "MEMMAN.COM", "TL.COM", "TK.COM",
            ),
        )

    def test_build_uses_make_target_and_returns_agent_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "work" / "agent"
            paths = tuple(root / name for name in msx_mcp_server.AGENT_PACKAGE_NAMES)

            def make_agent(*args, **kwargs):
                root.mkdir(parents=True)
                for path in paths:
                    path.write_bytes(("artifact:" + path.name).encode())
                return subprocess.CompletedProcess(args[0], 0, "built", "")

            constants = (
                "AGENT_COM", "AGENT_XFER_COM", "AGENT_TSR_8251",
                "AGENT_TSR_16C550", "AGENT_TSR_16C550_115200",
                "AGENT_TSR_UNAPI", "AGENT_TU_COM", "AGENT_PORT_COM",
                "AGENT_BADINIT_COM", "AGENT_MEMMAN_COM", "AGENT_TL_COM",
                "AGENT_TK_COM")
            replacements = dict(zip(constants, paths, strict=True))
            with (mock.patch.multiple(msx_mcp_server, **replacements),
                  mock.patch.object(msx_mcp_server, "MAKE", "test-make"),
                  mock.patch.object(
                      msx_mcp_server.subprocess, "run",
                      side_effect=make_agent) as run):
                result = msx_mcp_server._build_agent_artifacts()

            self.assertEqual(result, paths)
            self.assertTrue(all(path.stat().st_size for path in paths))
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["test-make", "agent"])
            self.assertEqual(kwargs["cwd"], msx_mcp_server.PROJ)
            self.assertEqual(kwargs["env"]["Z80ASM"], msx_mcp_server.Z80ASM)

    def test_success_without_final_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "work" / "agent"
            paths = tuple(root / name for name in msx_mcp_server.AGENT_PACKAGE_NAMES)
            completed = subprocess.CompletedProcess(
                ["test-make", "agent"], 0, "", "")
            constants = (
                "AGENT_COM", "AGENT_XFER_COM", "AGENT_TSR_8251",
                "AGENT_TSR_16C550", "AGENT_TSR_16C550_115200",
                "AGENT_TSR_UNAPI", "AGENT_TU_COM", "AGENT_PORT_COM",
                "AGENT_BADINIT_COM", "AGENT_MEMMAN_COM", "AGENT_TL_COM",
                "AGENT_TK_COM")
            replacements = dict(zip(constants, paths, strict=True))
            with (mock.patch.multiple(msx_mcp_server, **replacements),
                  mock.patch.object(
                      msx_mcp_server.subprocess, "run",
                      return_value=completed)):
                with self.assertRaisesRegex(OpenMSXError, "did not produce"):
                    msx_mcp_server._build_agent_artifacts()

    def test_dos_prompt_must_be_last_visible_row(self):
        self.assertTrue(msx_mcp_server._dos_prompt_visible(
            "MSX-DOS 2\nA:\\>\n\n"))
        self.assertTrue(msx_mcp_server._dos_prompt_visible(
            "C:\\GAMES>\n"))
        self.assertFalse(msx_mcp_server._dos_prompt_visible(
            "A:\\>\ninstalling resident agent"))

    def test_basic_prompt_must_be_last_visible_row(self):
        self.assertTrue(msx_mcp_server._basic_prompt_visible(
            "Microsoft MSX BASIC\nOk\n"))
        self.assertTrue(msx_mcp_server._basic_prompt_visible(
            "Microsoft MSX BASIC\nOk\n\nCopy   Files  Bload\" List   Run"))
        self.assertFalse(msx_mcp_server._basic_prompt_visible(
            "Ok\nprogram still running"))


class _FakeMachine:
    def __init__(self, screen="MSX-DOS 2\nA:\\>"):
        self.screen = screen
        self.commands = []
        self.advances = []
        self.typed = []
        self.imported_agent = None
        self.imported_files = {}
        self.imported_locations = {}
        self.disk_cwd = ""
        self.disk_dirs = {
            "": {"MSXAI.COM": b"stale-agent"},
        }
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
        if command == "diskmanipulator dir hda1":
            entries = []
            if self.disk_cwd == "":
                entries.extend(
                    f"{name:<12} <DIR>"
                    for name in self.disk_dirs if name)
            entries.extend(
                f"{name.lower():<12} -----  {len(data)}"
                for name, data in self.disk_dirs[self.disk_cwd].items())
            return "\n".join(entries)
        if command == "diskmanipulator chdir hda1 /":
            self.disk_cwd = ""
        elif command.startswith("diskmanipulator chdir hda1 "):
            target = command.rsplit(" ", 1)[-1].upper()
            if target not in self.disk_dirs:
                raise AssertionError(f"missing fake disk directory: {target}")
            self.disk_cwd = target
        elif command.startswith("diskmanipulator mkdir hda1 "):
            target = command.rsplit(" ", 1)[-1].upper()
            self.disk_dirs.setdefault(target, {})
        if command.startswith("diskmanipulator delete hda1"):
            self.disk_dirs[self.disk_cwd].pop(
                command.rsplit(" ", 1)[-1].upper(), None)
        if command.startswith("diskmanipulator import hda1"):
            path = re.search(r"\{(.+)\}", command).group(1)
            self.imported_agent = Path(path).read_bytes()
            name = Path(path).name.upper()
            self.disk_dirs[self.disk_cwd][name] = self.imported_agent
            self.imported_files[name] = self.imported_agent
            self.imported_locations[name] = self.disk_cwd
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
        self.accepted_cancelled = None
        self.closed = False

    def listen(self):
        return self

    def accept(self, timeout, cancelled=None):
        self.accepted_timeout = timeout
        self.accepted_cancelled = cancelled
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
            artifacts = tuple(
                root / ("source-" + name)
                for name in msx_mcp_server.AGENT_PACKAGE_NAMES)
            disk.write_bytes(b"disk")
            expected_files = {}
            for name, artifact in zip(
                    msx_mcp_server.AGENT_PACKAGE_NAMES, artifacts, strict=True):
                data = ("canonical:" + name).encode()
                artifact.write_bytes(data)
                expected_files[name] = data
            machine = _FakeMachine()
            real = _FakeRealAgent("127.0.0.1", 0)

            def copy_home(_source, destination, **_kwargs):
                Path(destination).mkdir(parents=True)

            session = msx_mcp_server.Session()
            try:
                with (mock.patch.object(msx_mcp_server, "DOS_HDD", disk),
                      mock.patch.object(
                          msx_mcp_server, "_build_agent_artifacts",
                          return_value=artifacts) as build,
                      mock.patch.object(
                          msx_mcp_server.shutil, "copytree",
                          side_effect=copy_home) as copytree,
                      mock.patch.object(
                          msx_mcp_server, "prepare_openmsx_home",
                          return_value=root / "prepared-openmsx-home") as prepare,
                      mock.patch.object(
                          msx_mcp_server, "OpenMSX",
                          return_value=machine) as openmsx,
                      mock.patch.object(
                          msx_mcp_server, "RealMSX",
                          return_value=real) as real_cls):
                    peer = session.start_tcp_bench(timeout=12, window=True)

                self.assertEqual(peer, ("127.0.0.1", 65000))
                build.assert_called_once_with()
                prepare.assert_called_once_with(msx_mcp_server.OPENMSX_HOME)
                ignore = copytree.call_args.kwargs["ignore"]
                self.assertIn("software", ignore("unused", ["software"]))
                self.assertEqual(
                    openmsx.call_args.kwargs["extensions"],
                    [msx_mcp_server.MCP_SLOT_EXPANDER,
                     msx_mcp_server.DOS_EXTENSION, "ram512k",
                     "rs232_proto"])
                self.assertEqual(machine.imported_files, expected_files)
                self.assertEqual(
                    machine.imported_locations,
                    {name: msx_mcp_server.BENCH_SUITE_DIR
                     for name in expected_files})
                self.assertNotIn("MSXAI.COM", machine.disk_dirs[""])
                self.assertEqual(machine.typed, [
                    "SET MSXAI_HOME=A:\\MSXAI",
                    "PATH A:\\MSXAI;%PATH%",
                    "MSXAI /DRIVER:8251",
                ])
                delete_index = machine.commands.index(
                    "diskmanipulator delete hda1 MSXAI.COM")
                mkdir_index = machine.commands.index(
                    "diskmanipulator mkdir hda1 MSXAI")
                chdir_index = machine.commands.index(
                    "diskmanipulator chdir hda1 MSXAI")
                import_index = next(
                    index for index, command in enumerate(machine.commands)
                    if command.startswith("diskmanipulator import hda1") and
                    "MSXAI.COM" in command)
                self.assertLess(delete_index, import_index)
                self.assertLess(mkdir_index, chdir_index)
                self.assertLess(chdir_index, import_index)
                self.assertLess(
                    machine.commands.index("set power off"),
                    import_index)
                self.assertIn(
                    msx_mcp_server.RESIDENT_INSTALL_SECONDS,
                    machine.advances)
                self.assertEqual(real.accepted_timeout, 12.0)
                self.assertIsNone(real.accepted_cancelled)
                self.assertEqual(real.simulation, "openmsx-rs232-net")
                transfer_state = Path(
                    real_cls.call_args.kwargs[
                        "file_transfer_state_directory"])
                self.assertEqual(transfer_state.name, "transfers")
                self.assertTrue(
                    transfer_state.parent.name.startswith("msx-ai-tcp-bench-"))
                self.assertFalse(machine.headless)
                self.assertLess(
                    machine.commands.index("plug msx-rs232 rs232-net"),
                    machine.commands.index("set renderer SDLGL-PP"))
                self.assertFalse(any(
                    command.startswith("debug read memory")
                    for command in machine.commands))
            finally:
                session.shutdown()


if __name__ == "__main__":
    unittest.main()
