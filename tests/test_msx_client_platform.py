import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import msx_client
from msx_client import OpenMSX, OpenMSXError


class _Pipe:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Process:
    def __init__(self, returncode=None):
        self.stdin = _Pipe()
        self.stdout = _Pipe()
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = -9


class _ControlSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(bytes(data))

    def close(self):
        self.closed = True


class OpenMSXPlatformTests(unittest.TestCase):
    def test_platform_detection_does_not_depend_on_af_unix(self):
        with mock.patch.object(msx_client.socket, "AF_UNIX", create=True):
            self.assertTrue(msx_client._is_windows("win32"))
            self.assertEqual(
                msx_client._control_transport("win32"), "tcp_sspi")
            self.assertEqual(msx_client._control_transport("linux"), "stdio")
            self.assertEqual(msx_client._control_transport("darwin"), "stdio")

    def test_windows_binary_discovery_prefers_env_then_path_then_registry(self):
        explicit = r"C:\Explicit\openmsx.exe"
        path_binary = r"C:\OnPath\openmsx.exe"
        registry_binary = r"C:\Registry\openmsx.exe"
        with (mock.patch.dict(
                  os.environ, {"OPENMSX_BIN": explicit,
                               "ProgramFiles": r"C:\Program Files"},
                  clear=False),
              mock.patch.object(msx_client.shutil, "which",
                                return_value=path_binary),
              mock.patch.object(msx_client, "_windows_registry_binary_candidates",
                                return_value=[registry_binary]),
              mock.patch.object(msx_client, "_executable_exists",
                                return_value=True)):
            self.assertEqual(msx_client._default_binary("win32"), explicit)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENMSX_BIN", None)
            with (mock.patch.object(msx_client.shutil, "which",
                                    return_value=path_binary),
                  mock.patch.object(
                      msx_client, "_windows_registry_binary_candidates",
                      return_value=[registry_binary]),
                  mock.patch.object(msx_client, "_executable_exists",
                                    return_value=True)):
                self.assertEqual(
                    msx_client._default_binary("win32"), path_binary)

            with (mock.patch.object(msx_client.shutil, "which",
                                    return_value=None),
                  mock.patch.object(
                      msx_client, "_windows_registry_binary_candidates",
                      return_value=[registry_binary]),
                  mock.patch.object(msx_client, "_executable_exists",
                                    return_value=True)):
                self.assertEqual(
                    msx_client._default_binary("win32"), registry_binary)

            os.environ["ProgramFiles"] = r"C:\Program Files"
            with (mock.patch.object(msx_client.shutil, "which",
                                    return_value=None),
                  mock.patch.object(
                      msx_client, "_windows_registry_binary_candidates",
                      return_value=[]),
                  mock.patch.object(msx_client, "_executable_exists",
                                    return_value=True)):
                self.assertEqual(
                    msx_client._default_binary("win32"),
                    r"C:\Program Files\openMSX\openmsx.exe")

    def test_macos_binary_discovery_falls_back_to_application_bundle(self):
        bundle = "/Applications/openMSX.app/Contents/MacOS/openmsx"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENMSX_BIN", None)
            with (mock.patch.object(msx_client.shutil, "which",
                                    return_value=None),
                  mock.patch.object(
                      msx_client, "_executable_exists",
                      side_effect=lambda candidate: candidate == bundle)):
                self.assertEqual(msx_client._default_binary("darwin"), bundle)

    def test_windows_user_home_uses_the_documents_known_folder(self):
        with (mock.patch.dict(os.environ, {}, clear=False),
              mock.patch.object(
                  msx_client, "_windows_documents_dir",
                  return_value=r"D:\Redirected Documents")):
            os.environ.pop("OPENMSX_HOME", None)
            home = msx_client._default_user_openmsx_home("win32")
        self.assertEqual(
            str(home), r"D:\Redirected Documents\openMSX")

    def test_windows_user_home_prefers_userprofile_without_path_home(self):
        with (mock.patch.dict(
                  os.environ,
                  {"USERPROFILE": r"C:\Users\MSX"},
                  clear=False),
              mock.patch.object(
                  msx_client, "_windows_documents_dir", return_value=None),
              mock.patch.object(
                  msx_client.pathlib.Path, "home",
                  side_effect=AssertionError("Path.home must not be called"))):
            os.environ.pop("OPENMSX_HOME", None)
            home = msx_client._default_user_openmsx_home("win32")
        self.assertEqual(str(home), r"C:\Users\MSX\Documents\openMSX")

    def test_windows_owned_start_uses_authenticated_xml_socket(self):
        process = _Process()
        control_socket = _ControlSocket()
        machine = OpenMSX(
            machine="C-BIOS_MSX2", extensions=(), bin=r"C:\openMSX.exe",
            platform="win32")

        with (mock.patch.object(msx_client.subprocess, "Popen",
                                return_value=process) as popen,
              mock.patch.object(
                  machine, "_connect_owned_windows_control",
                  return_value=("socket.123", control_socket)),
              mock.patch.object(msx_client.threading.Thread, "start"),
              mock.patch.object(msx_client.time, "sleep"),
              mock.patch.object(machine, "cmd", return_value="")):
            machine.start(headless=False, startup_timeout=2.5)
            argv = popen.call_args.args[0]
            self.assertNotIn("-control", argv)
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(control_socket.sent, [b"<openmsx-control>\n"])
            machine.close()

        self.assertTrue(control_socket.closed)
        self.assertIsNone(machine.proc)

    def test_posix_owned_start_preserves_stdio_control(self):
        process = _Process()
        machine = OpenMSX(
            machine="C-BIOS_MSX2", extensions=(), bin="/opt/openmsx",
            platform="linux")
        with (mock.patch.object(msx_client.subprocess, "Popen",
                                return_value=process) as popen,
              mock.patch.object(msx_client.threading.Thread, "start"),
              mock.patch.object(msx_client.time, "sleep"),
              mock.patch.object(machine, "cmd", return_value="")):
            machine.start(headless=False)
            argv = popen.call_args.args[0]
            self.assertEqual(argv[argv.index("-control") + 1], "stdio")
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
            machine.close()

    def test_windows_descriptor_accepts_openmsx_21_port_and_uses_sspi(self):
        marker = object()
        with tempfile.TemporaryDirectory() as directory:
            endpoint = pathlib.Path(directory) / "socket.4321"
            endpoint.write_text("9969\n", encoding="ascii")
            with (mock.patch.object(msx_client, "_uses_unix_control",
                                    return_value=False),
                  mock.patch.object(msx_client, "_windows_process_image",
                                    return_value=r"C:\Program Files\openMSX\openmsx.exe"),
                  mock.patch.object(msx_client, "_connect_windows_sspi",
                                    return_value=marker) as connect):
                self.assertIs(
                    msx_client._open_control_endpoint(endpoint), marker)
        connect.assert_called_once_with("127.0.0.1", 9969, timeout=5.0)

    def test_windows_descriptor_rejects_pid_reused_by_other_process(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = pathlib.Path(directory) / "socket.4321"
            endpoint.write_text("9969\n", encoding="ascii")
            with (mock.patch.object(msx_client, "_uses_unix_control",
                                    return_value=False),
                  mock.patch.object(msx_client, "_windows_process_image",
                                    return_value=r"C:\Tools\other.exe"),
                  self.assertRaisesRegex(OpenMSXError, "non-openMSX")):
                msx_client._open_control_endpoint(endpoint)

    def test_command_failure_reports_exit_code_and_openmsx_log(self):
        process = _Process(returncode=7)
        machine = OpenMSX(
            machine="C-BIOS_MSX2", extensions=(), bin="/opt/openmsx",
            platform="linux")
        machine.proc = process
        machine._output_tail = (
            '<openmsx-output><log level="error">Missing ROM foo.rom</log>')

        with self.assertRaises(OpenMSXError) as raised:
            machine.cmd("set power on", timeout=0.5)

        message = str(raised.exception)
        self.assertIn("exited with code 7", message)
        self.assertIn("Missing ROM foo.rom", message)
        self.assertIn("machine='C-BIOS_MSX2'", message)
        self.assertIn("transport=stdio", message)

    def test_user_mode_does_not_define_or_materialize_openmsx_home(self):
        process = _Process()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENMSX_HOME", None)
            machine = OpenMSX(
                machine="C-BIOS_MSX2", extensions=(), bin="/opt/openmsx",
                platform="linux", config_mode="user")
            with (mock.patch.object(msx_client, "prepare_openmsx_home")
                                    as prepare,
                  mock.patch.object(msx_client.subprocess, "Popen",
                                    return_value=process) as popen,
                  mock.patch.object(msx_client.threading.Thread, "start"),
                  mock.patch.object(msx_client.time, "sleep"),
                  mock.patch.object(machine, "cmd", return_value="")):
                machine.start(headless=False)
                self.assertNotIn("OPENMSX_HOME", popen.call_args.kwargs["env"])
                prepare.assert_not_called()
                machine.close()

    def test_overlay_combines_mcp_templates_and_user_file_pools_temporarily(self):
        process = _Process()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            project_home = root / "project-home"
            user_home = root / "user-home"
            project_machine = project_home / "share/machines/Custom.xml"
            user_rom = user_home / "share/systemroms/basic.rom"
            project_machine.parent.mkdir(parents=True)
            user_rom.parent.mkdir(parents=True)
            project_machine.write_text("<machine/>", encoding="utf-8")
            user_rom.write_bytes(b"rom")

            with mock.patch.dict(
                    os.environ, {"OPENMSX_HOME": str(user_home)}, clear=False):
                machine = OpenMSX(
                    machine="Custom", extensions=(), home=project_home,
                    bin="/opt/openmsx", platform="linux",
                    config_mode="overlay")
                with (mock.patch.object(msx_client.subprocess, "Popen",
                                        return_value=process) as popen,
                      mock.patch.object(msx_client.threading.Thread, "start"),
                      mock.patch.object(msx_client.time, "sleep"),
                      mock.patch.object(machine, "cmd", return_value="")):
                    machine.start(headless=False)
                    overlay = pathlib.Path(
                        popen.call_args.kwargs["env"]["OPENMSX_HOME"])
                    self.assertNotEqual(overlay, project_home)
                    self.assertTrue((overlay / "share/machines/Custom.xml").is_file())
                    self.assertTrue((overlay / "share/systemroms/basic.rom").is_file())
                    machine.close()
                    self.assertFalse(overlay.exists())

    def test_preflight_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            executable = root / "openmsx"
            machine_file = root / "home/share/machines/Custom.xml"
            executable.write_bytes(b"")
            machine_file.parent.mkdir(parents=True)
            machine_file.write_text("<machine/>", encoding="utf-8")
            machine = OpenMSX(
                machine="Custom", extensions=(), home=root / "home",
                bin=executable, platform="linux")
            with (mock.patch.object(msx_client, "prepare_openmsx_home") as prepare,
                  mock.patch.object(msx_client.tempfile, "TemporaryDirectory")
                                    as temporary):
                report = machine.preflight()
        self.assertTrue(report["ready"])
        self.assertEqual(report["control_transport"], "stdio")
        self.assertTrue(report["machine_config_found"])
        prepare.assert_not_called()
        temporary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
