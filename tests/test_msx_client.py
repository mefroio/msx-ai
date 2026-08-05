import pathlib
import importlib
import sys
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


class OpenMSXHeadlessAudioTests(unittest.TestCase):
    def _start_patches(self, process):
        return (
            mock.patch.object(msx_client.subprocess, "Popen", return_value=process),
            mock.patch.object(msx_client.threading.Thread, "start"),
            mock.patch.object(msx_client.time, "sleep"),
        )

    def test_adapter_supports_package_import_and_snapshot_transaction(self):
        packaged = importlib.import_module("server.msx_client")
        machine = packaged.OpenMSX(bin="/fake/openmsx")

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
            return ""

        popen_patch, thread_patch, sleep_patch = self._start_patches(process)
        machine = OpenMSX(bin="/fake/openmsx")
        with popen_patch as popen, thread_patch, sleep_patch, \
                mock.patch.object(machine, "cmd", side_effect=command):
            machine.start(headless=True)
            argv = popen.call_args.args[0]
            self.assertIn("-command", argv)
            self.assertIn("-setting", argv)
            runtime_settings = pathlib.Path(argv[argv.index("-setting") + 1])
            self.assertTrue(runtime_settings.is_file())
            startup = argv[argv.index("-command") + 1]
            self.assertEqual(startup, "set mute on")
            machine.close()

        self.assertNotIn("set mute off", calls)
        self.assertNotIn("set mute false", calls)
        self.assertIn("quit", calls)
        self.assertTrue(process.waited)

    def test_visible_spawn_does_not_change_host_mute(self):
        process = _Process()
        popen_patch, thread_patch, sleep_patch = self._start_patches(process)
        machine = OpenMSX(bin="/fake/openmsx")
        with popen_patch as popen, thread_patch, sleep_patch, \
                mock.patch.object(machine, "cmd") as command:
            machine.start(headless=False)
            argv = popen.call_args.args[0]
            self.assertNotIn("-command", argv)
            command.assert_not_called()
            machine.close()

    def test_headless_spawn_fails_closed_when_mute_is_not_active(self):
        process = _Process()
        calls = []

        def command(tcl, timeout=15):
            calls.append(tcl)
            if tcl == "set mute":
                return "false"
            return ""

        popen_patch, thread_patch, sleep_patch = self._start_patches(process)
        machine = OpenMSX(bin="/fake/openmsx")
        with popen_patch, thread_patch, sleep_patch, \
                mock.patch.object(machine, "cmd", side_effect=command):
            with self.assertRaisesRegex(OpenMSXError, "mandatory headless host mute"):
                machine.start(headless=True)

        self.assertIn("quit", calls)
        self.assertIsNone(machine.proc)


if __name__ == "__main__":
    unittest.main()
