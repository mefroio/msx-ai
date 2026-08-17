import pathlib
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import msx_client
import paths
from msx_client import OpenMSX, OpenMSXError


class _Pipe:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Process:
    def __init__(self):
        self.stdin = _Pipe()
        self.stdout = _Pipe()
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _ControlSocket:
    def __init__(self, *, connect_error=None):
        self.connect_error = connect_error
        self.connected_path = None
        self.sent = []
        self.closed = False

    def connect(self, path):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected_path = path

    def sendall(self, data):
        self.sent.append(bytes(data))

    def close(self):
        self.closed = True


class OpenMSXHeadlessAudioTests(unittest.TestCase):
    def _start_patches(self, process):
        return (
            mock.patch.object(msx_client.subprocess, "Popen", return_value=process),
            mock.patch.object(msx_client.threading.Thread, "start"),
            mock.patch.object(msx_client.time, "sleep"),
        )

    def test_ctrl_stop_uses_a_real_keyboard_matrix_chord(self):
        machine = OpenMSX(bin="/fake/openmsx", platform="linux")
        machine.cmd = mock.Mock(return_value="")

        machine.press("ctrl+stop")

        machine.cmd.assert_called_once_with(
            "keymatrixdown 6 2; keymatrixdown 7 16; "
            "after time 0.10 {keymatrixup 7 16; keymatrixup 6 2}")

    def test_named_single_keys_emit_one_matrix_down_up_pair(self):
        expected = {
            "1": (0, 2),
            "2": (0, 4),
            "3": (0, 8),
            "4": (0, 16),
            "5": (0, 32),
            "F1": (6, 32),
            "F2": (6, 64),
            "F3": (6, 128),
            "F4": (7, 1),
            "F5": (7, 2),
            "ESC": (7, 4),
            "TAB": (7, 8),
            "STOP": (7, 16),
            "SELECT": (7, 64),
            "RET": (7, 128),
            "SPACE": (8, 1),
            "UP": (8, 32),
            "DOWN": (8, 64),
            "LEFT": (8, 16),
            "RIGHT": (8, 128),
        }
        self.assertEqual(OpenMSX.KEYS, expected)
        machine = OpenMSX(bin="/fake/openmsx", platform="linux")
        machine.cmd = mock.Mock(return_value="")

        for key, (row, mask) in expected.items():
            with self.subTest(key=key):
                machine.cmd.reset_mock()
                machine.press(key.lower())
                command = (
                    f"keymatrixdown {row} {mask}; "
                    f"after time 0.06 {{keymatrixup {row} {mask}}}"
                )
                machine.cmd.assert_called_once_with(command)
                self.assertEqual(command.count("keymatrixdown"), 1)
                self.assertEqual(command.count("keymatrixup"), 1)

    def test_adapter_supports_package_import_and_snapshot_transaction(self):
        packaged = importlib.import_module("server.msx_client")
        machine = packaged.OpenMSX(bin="/fake/openmsx", platform="linux")

        # The debugger snapshot is a multi-command transaction, so its lock
        # must be re-entrant when capture_openmsx_cpu calls machine.cmd().
        self.assertTrue(machine._lock.acquire(blocking=False))
        try:
            self.assertTrue(machine._lock.acquire(blocking=False))
            machine._lock.release()
        finally:
            machine._lock.release()

        with mock.patch.object(
                packaged, "capture_openmsx_cpu", return_value={"ok": True}) \
                as capture:
            self.assertEqual(machine.cpu_snapshot(), {"ok": True})
        capture.assert_called_once_with(machine)

    def test_headless_starts_and_exits_with_host_mixer_muted(self):
        process = _Process()
        calls = []

        def command(tcl, timeout=15):
            calls.append(tcl)
            if tcl == "set mute":
                return "true"
            if tcl == "set renderer":
                return "none"
            return ""

        popen_patch, thread_patch, sleep_patch = self._start_patches(process)
        machine = OpenMSX(bin="/fake/openmsx", platform="linux")
        with popen_patch as popen, thread_patch, sleep_patch, \
                mock.patch.object(machine, "cmd", side_effect=command):
            machine.start(headless=True)
            argv = popen.call_args.args[0]
            self.assertIn("-command", argv)
            self.assertIn("-setting", argv)
            runtime_settings = pathlib.Path(argv[argv.index("-setting") + 1])
            self.assertTrue(runtime_settings.is_file())
            startup = argv[argv.index("-command") + 1]
            self.assertEqual(startup, "set mute on; set renderer none")
            machine.close()

        self.assertNotIn("set mute off", calls)
        self.assertNotIn("set mute false", calls)
        self.assertIn("quit", calls)
        self.assertTrue(process.waited)

    def test_visible_spawn_explicitly_initializes_the_renderer(self):
        process = _Process()
        popen_patch, thread_patch, sleep_patch = self._start_patches(process)
        machine = OpenMSX(bin="/fake/openmsx", platform="linux")
        with popen_patch as popen, thread_patch, sleep_patch, \
                mock.patch.object(machine, "cmd") as command:
            machine.start(headless=False)
            argv = popen.call_args.args[0]
            self.assertIn("-command", argv)
            startup = argv[argv.index("-command") + 1]
            self.assertEqual(startup, "set renderer SDLGL-PP")
            command.assert_not_called()
            machine.close()

    def test_control_channel_failure_after_spawn_terminates_process(self):
        process = _Process()
        popen_patch, thread_patch, sleep_patch = self._start_patches(process)
        machine = OpenMSX(bin="/fake/openmsx", platform="linux")
        with popen_patch, thread_patch, sleep_patch, \
                mock.patch.object(
                    machine, "_write", side_effect=BrokenPipeError("closed")):
            with self.assertRaises(BrokenPipeError):
                machine.start(headless=False)

        self.assertTrue(process.waited)
        self.assertIsNone(machine.proc)
        self.assertIsNone(machine._runtime_settings_dir)

    def test_installed_start_materializes_public_configs_before_popen(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            state = temporary / "state"
            missing_checkout = temporary / "installed"
            process = _Process()

            def spawn(*_args, **_kwargs):
                home = state.resolve() / "openmsx-home"
                actual = {
                    path.relative_to(home)
                    for path in home.rglob("*") if path.is_file()
                }
                self.assertEqual(actual, set(paths.OPENMSX_PUBLIC_FILES))
                return process

            with (mock.patch.dict(
                      os.environ, {"MSX_AI_STATE_DIR": str(state)}, clear=False),
                  mock.patch.object(
                      paths, "_CHECKOUT_CANDIDATE", missing_checkout)):
                os.environ.pop("MSX_AI_SOURCE_ROOT", None)
                os.environ.pop("MSX_AI_OPENMSX_HOME", None)
                home = paths.openmsx_home()
                machine = OpenMSX(
                    home=home, bin="/fake/openmsx", platform="linux")
                with (mock.patch.object(
                          msx_client.subprocess, "Popen", side_effect=spawn) as popen,
                      mock.patch.object(msx_client.threading.Thread, "start"),
                      mock.patch.object(msx_client.time, "sleep"),
                      mock.patch.object(machine, "cmd", return_value="")):
                    machine.start(headless=False)
                    popen.assert_called_once()
                    machine.close()

    def test_headless_spawn_fails_closed_when_mute_is_not_active(self):
        process = _Process()
        calls = []

        def command(tcl, timeout=15):
            calls.append(tcl)
            if tcl == "set mute":
                return "false"
            if tcl == "set renderer":
                return "none"
            return ""

        popen_patch, thread_patch, sleep_patch = self._start_patches(process)
        machine = OpenMSX(bin="/fake/openmsx", platform="linux")
        with popen_patch, thread_patch, sleep_patch, \
                mock.patch.object(machine, "cmd", side_effect=command):
            with self.assertRaisesRegex(OpenMSXError, "mandatory headless host mute"):
                machine.start(headless=True)

        self.assertIn("quit", calls)
        self.assertIsNone(machine.proc)

    def test_attach_refuses_to_choose_between_multiple_live_instances(self):
        first = _ControlSocket()
        second = _ControlSocket()
        machine = OpenMSX(bin="/fake/openmsx")
        with (mock.patch.object(
                  msx_client, "list_sockets",
                  return_value=["/tmp/openmsx-user/socket.1",
                                "/tmp/openmsx-user/socket.2"]),
              mock.patch.object(
                  msx_client, "_open_control_endpoint",
                  side_effect=[first, second])):
            with self.assertRaisesRegex(
                    OpenMSXError, "multiple running openMSX instances"):
                machine.attach()

        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertIsNone(machine.sock)
        self.assertIsNone(machine.socket_path)
        self.assertFalse(machine.attached)

    def test_attach_can_select_one_exact_discovered_socket(self):
        selected = _ControlSocket()
        machine = OpenMSX(bin="/fake/openmsx")
        requested = "/tmp/openmsx-user/socket.2"

        def connect(path):
            selected.connect(path)
            return selected

        with (mock.patch.object(
                  msx_client, "list_sockets",
                  return_value=["/tmp/openmsx-user/socket.1", requested]),
              mock.patch.object(
                  msx_client, "_open_control_endpoint", side_effect=connect),
              mock.patch.object(msx_client.threading.Thread, "start"),
              mock.patch.object(msx_client.time, "sleep")):
            self.assertIs(machine.attach(requested), machine)

        self.assertEqual(selected.connected_path, requested)
        self.assertEqual(selected.sent, [b"<openmsx-control>\n"])
        self.assertEqual(machine.socket_path, requested)
        self.assertTrue(machine.attached)
        machine.close()
        self.assertTrue(selected.closed)
        self.assertIsNone(machine.socket_path)

    def test_attach_rejects_an_undiscovered_unix_socket(self):
        machine = OpenMSX(bin="/fake/openmsx")
        with (mock.patch.object(
                  msx_client, "list_sockets",
                  return_value=["/tmp/openmsx-user/socket.1"]),
              mock.patch.object(msx_client.socket, "socket") as socket_factory,
              self.assertRaisesRegex(OpenMSXError, "not among the discovered")):
            machine.attach("/tmp/unrelated-service.sock")
        socket_factory.assert_not_called()

    def test_windows_control_endpoint_uses_published_loopback_port(self):
        control = _ControlSocket()
        with tempfile.TemporaryDirectory() as directory:
            endpoint = pathlib.Path(directory) / "socket.123"
            endpoint.write_text("9947\n", encoding="ascii")
            with (mock.patch.object(msx_client, "_uses_unix_control",
                                    return_value=False),
                  mock.patch.object(msx_client, "_connect_windows_sspi",
                                    return_value=control)):
                self.assertIs(msx_client._open_control_endpoint(endpoint), control)

    def test_windows_control_endpoint_rejects_untrusted_port(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = pathlib.Path(directory) / "socket.123"
            endpoint.write_text("8080\n", encoding="ascii")
            with mock.patch.object(msx_client, "_uses_unix_control",
                                   return_value=False):
                with self.assertRaisesRegex(OpenMSXError, "outside 9938..10001"):
                    msx_client._open_control_endpoint(endpoint)


if __name__ == "__main__":
    unittest.main()
