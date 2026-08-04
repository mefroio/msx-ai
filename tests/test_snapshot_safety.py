import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import msx_mcp_server  # noqa: E402
import msx_real  # noqa: E402
import msx_screenshot  # noqa: E402
from msx_client import OpenMSXError  # noqa: E402
from msx_real import (  # noqa: E402
    FEATURE_SNAPSHOT_LEASE,
    RECONNECT_ESCAPE,
    SNAPSHOT_LEASE_TIMEOUTS,
    UART8251_FRAME_WAKE_DELAY,
    RealMSX,
    RealMSXError,
    RealMSXProtocolError,
    RealMSXTimeoutError,
)


class SnapshotLeaseTest(unittest.TestCase):
    def setUp(self):
        self.msx = RealMSX(socket_timeout=0.01)
        self.msx._v3 = SimpleNamespace(timeout=15.0)
        self.msx.feature_bits = FEATURE_SNAPSHOT_LEASE

    def test_lost_pause_ack_is_cleaned_up_by_direct_resume(self):
        events = []

        def request(opcode, payload=b"", **_kwargs):
            events.append((opcode, payload, self.msx._snapshot_pause_owned))
            if opcode == "S":
                raise RealMSXProtocolError("snapshot ACK lost")
            if opcode == "g":
                return b""
            self.fail(f"unexpected opcode {opcode!r}")

        with (mock.patch.object(self.msx, "status",
                                return_value={"state": "running"}),
              mock.patch.object(self.msx, "_request_v3",
                                side_effect=request)):
            with self.assertRaisesRegex(
                    RealMSXProtocolError, "snapshot ACK lost"):
                with self.msx.snapshot_lease():
                    self.fail("a lost ACK must not enter the acquisition")

        self.assertEqual(events, [
            ("S", bytes([SNAPSHOT_LEASE_TIMEOUTS]), True),
            ("g", b"", True),
        ])
        self.assertFalse(self.msx._snapshot_pause_owned)

    def test_failed_direct_resume_rebootstraps_and_guarantees_running(self):
        statuses = iter((
            "running", "paused", "paused", "paused", "running"))
        events = []
        direct_resume = True

        def status():
            state = next(statuses)
            events.append(("status", state))
            return {"state": state}

        def request(opcode, payload=b"", **_kwargs):
            nonlocal direct_resume
            events.append((opcode, payload))
            if opcode == "S":
                return b""
            if opcode == "g" and direct_resume:
                direct_resume = False
                raise RealMSXProtocolError("resume ACK lost")
            if opcode == "g":
                return b""
            self.fail(f"unexpected opcode {opcode!r}")

        def rebootstrap():
            events.append(("rebootstrap", b"\x1b" * 8))

        with (mock.patch.object(self.msx, "status", side_effect=status),
              mock.patch.object(self.msx, "_request_v3",
                                side_effect=request),
              mock.patch.object(self.msx, "_rebootstrap_v3",
                                side_effect=rebootstrap)):
            with self.msx.snapshot_lease() as owned:
                self.assertTrue(owned)

        self.assertIn(("rebootstrap", b"\x1b" * 8), events)
        self.assertEqual(events.count(("g", b"")), 2)
        self.assertFalse(self.msx._snapshot_pause_owned)

    def test_cached_pause_ack_after_expiry_uses_a_new_pause_sequence(self):
        statuses = iter(("running", "running", "paused", "paused"))
        requests = []

        def request(opcode, payload=b"", **_kwargs):
            requests.append((opcode, payload))
            return b""

        with (mock.patch.object(
                  self.msx, "status",
                  side_effect=lambda: {"state": next(statuses)}),
              mock.patch.object(self.msx, "_request_v3",
                                side_effect=request)):
            with self.msx.snapshot_lease():
                pass

        self.assertEqual(requests[:2], [
            ("S", bytes([SNAPSHOT_LEASE_TIMEOUTS])),
            ("S", bytes([SNAPSHOT_LEASE_TIMEOUTS])),
        ])
        self.assertEqual(requests[-1], ("g", b""))

    def test_lease_request_timeout_is_capped_and_restored(self):
        statuses = iter(("running", "paused", "paused"))
        observed = []

        def request(opcode, payload=b"", **_kwargs):
            observed.append((opcode, self.msx._v3.timeout))
            return b""

        with (mock.patch.object(
                  self.msx, "status",
                  side_effect=lambda: {"state": next(statuses)}),
              mock.patch.object(self.msx, "_request_v3",
                                side_effect=request)):
            with self.msx.snapshot_lease():
                self.assertEqual(self.msx._v3.timeout, 1.0)

        self.assertEqual(observed, [("S", 1.0), ("g", 1.0)])
        self.assertEqual(self.msx._v3.timeout, 15.0)

    def test_expired_lease_discards_completed_acquisition(self):
        statuses = iter(("running", "paused", "running"))
        requests = []

        with (mock.patch.object(
                  self.msx, "status",
                  side_effect=lambda: {"state": next(statuses)}),
              mock.patch.object(
                  self.msx, "_request_v3",
                  side_effect=lambda opcode, payload=b"", **_kwargs:
                  (requests.append((opcode, payload)) or b""))):
            with self.assertRaisesRegex(RealMSXError, "not atomic"):
                with self.msx.snapshot_lease():
                    pass

        self.assertEqual(requests, [
            ("S", bytes([SNAPSHOT_LEASE_TIMEOUTS]))])
        self.assertFalse(self.msx._snapshot_pause_owned)
        self.assertEqual(self.msx._v3.timeout, 15.0)

    def test_old_agent_refuses_running_atomic_capture(self):
        self.msx.feature_bits = 0
        with (mock.patch.object(self.msx, "status",
                                return_value={"state": "running"}),
              mock.patch.object(self.msx, "_request_v3") as request):
            with self.assertRaisesRegex(RealMSXError, "snapshot-lease"):
                with self.msx.snapshot_lease():
                    pass
        request.assert_not_called()

    def test_idle_foreground_monitor_needs_no_snapshot_feature(self):
        self.msx.feature_bits = 0
        self.msx.runtime_mode = "foreground-monitor"
        with (mock.patch.object(self.msx, "status",
                                return_value={"state": "monitor"}),
              mock.patch.object(self.msx, "_request_v3") as request):
            with self.msx.snapshot_lease() as owned:
                self.assertFalse(owned)
        request.assert_not_called()

    def test_manual_pause_is_not_resumed(self):
        with (mock.patch.object(self.msx, "status",
                                return_value={"state": "paused"}),
              mock.patch.object(self.msx, "_request_v3") as request):
            with self.msx.snapshot_lease() as owned:
                self.assertFalse(owned)
        request.assert_not_called()

    def test_acquisition_error_remains_primary_when_resume_also_fails(self):
        class MidReadError(RuntimeError):
            pass

        statuses = iter(("running", "paused"))

        def request(opcode, payload=b"", **_kwargs):
            if opcode == "S":
                return b""
            raise RealMSXProtocolError("direct resume failed")

        with (mock.patch.object(
                  self.msx, "status",
                  side_effect=lambda: {"state": next(statuses)}),
              mock.patch.object(self.msx, "_request_v3",
                                side_effect=request),
              mock.patch.object(self.msx, "_rebootstrap_v3",
                                side_effect=RealMSXProtocolError(
                                    "rebootstrap failed"))):
            with self.assertRaises(MidReadError) as caught:
                with self.msx.snapshot_lease():
                    raise MidReadError("VRAM read failed midway")

        self.assertEqual(str(caught.exception), "VRAM read failed midway")
        self.assertIsInstance(caught.exception.__cause__, RealMSXError)
        self.assertIn("could not guarantee", str(caught.exception.__cause__))
        self.assertEqual(self.msx._v3.timeout, 15.0)

    def test_rebootstrap_queries_raw_mode_when_automatic_hello_is_lost(self):
        class TimeoutStream:
            timeout = 0.01

            def gettimeout(self):
                return self.timeout

            def settimeout(self, value):
                self.timeout = value

        self.msx.conn = TimeoutStream()
        sent = []
        scan_framing = []

        def scan(*_args, **_kwargs):
            scan_framing.append(self.msx._v3)
            if len(scan_framing) == 1:
                raise RealMSXTimeoutError("automatic HELLO lost")
            return b"M\x02\x20\xc8"

        def activate(reply, **kwargs):
            self.assertEqual(reply, b"M\x02\x20\xc8")
            self.assertEqual(kwargs, {"v3_timeout": 1.0})
            self.msx._v3 = object()
            return {"protocol": 3}

        with (mock.patch.object(self.msx, "_drain_recovery_noise",
                                return_value=0) as drain,
              mock.patch.object(self.msx, "_send",
                                side_effect=sent.append),
              mock.patch.object(self.msx, "_scan_bootstrap_hello",
                                side_effect=scan),
              mock.patch.object(self.msx, "_activate_bootstrap_hello",
                                side_effect=activate),
              mock.patch.object(msx_real.time, "sleep") as sleep):
            result = self.msx._rebootstrap_v3()

        self.assertEqual(result, {"protocol": 3})
        self.assertEqual(sent, [b"\x1b", b"\x1b" * 7, b"?"])
        sleep.assert_called_once_with(UART8251_FRAME_WAKE_DELAY)
        self.assertEqual(drain.call_count, 2)
        self.assertEqual(scan_framing, [None, None])

    def test_reconnect_marker_is_split_only_for_unknown_or_8251(self):
        for transport_id in (None, 0):
            sent = []
            self.msx.agent_transport_id = transport_id
            with (mock.patch.object(self.msx, "_send",
                                    side_effect=sent.append),
                  mock.patch.object(msx_real.time, "sleep") as sleep):
                self.msx._send_reconnect_escape()
            self.assertEqual(sent, [RECONNECT_ESCAPE[:1],
                                    RECONNECT_ESCAPE[1:]])
            sleep.assert_called_once_with(UART8251_FRAME_WAKE_DELAY)

        sent = []
        self.msx.agent_transport_id = 1
        with (mock.patch.object(self.msx, "_send",
                                side_effect=sent.append),
              mock.patch.object(msx_real.time, "sleep") as sleep):
            self.msx._send_reconnect_escape()
        self.assertEqual(sent, [RECONNECT_ESCAPE])
        sleep.assert_not_called()


class ScreenshotFlowTest(unittest.TestCase):
    def setUp(self):
        self.previous = (msx_mcp_server.SESSION.msx,
                         msx_mcp_server.SESSION.profile)

    def tearDown(self):
        (msx_mcp_server.SESSION.msx,
         msx_mcp_server.SESSION.profile) = self.previous

    @staticmethod
    def plan(size=64, mode=5):
        return msx_screenshot.RealMSXCapturePlan(
            mode=mode, regs=(0,) * 28, text_width=None, height=1,
            sprites=False, page=0, ranges=((0, size),))

    @staticmethod
    def capture(plan):
        return msx_screenshot.RealMSXCapture(
            plan=plan, vram=bytes(msx_screenshot.VRAM_SIZE))

    def fake_msx(self, events, *, transport_id=1):
        instance = SimpleNamespace(
            agent_transport_id=transport_id,
            agent_transport=("uart-8251" if transport_id == 0
                             else "uart-16c550"),
            _v3=SimpleNamespace(max_payload=320),
        )

        @contextlib.contextmanager
        def lease(*, atomic=True):
            events.append(("lease", atomic))
            if not atomic:
                yield False
                return
            try:
                yield atomic
            finally:
                events.append("resumed")

        instance.snapshot_lease = lease
        return instance

    def test_resume_happens_before_render_and_base64(self):
        events = []
        plan = self.plan()
        capture = self.capture(plan)
        msx_mcp_server.SESSION.msx = self.fake_msx(events)
        msx_mcp_server.SESSION.profile = "real"

        def acquire(*_args, **_kwargs):
            events.append("last-target-byte")
            return capture

        def render(_capture, path, **_kwargs):
            self.assertEqual(events[-1], "resumed")
            events.append("render")
            Path(path).write_bytes(b"png-after-resume")
            return path, plan.mode

        with (mock.patch.object(msx_screenshot, "plan_realmsx_capture",
                                return_value=plan),
              mock.patch.object(msx_screenshot, "acquire_realmsx_capture",
                                side_effect=acquire),
              mock.patch.object(msx_screenshot, "render_realmsx_capture",
                                side_effect=render)):
            result = msx_mcp_server.t_screenshot()

        self.assertEqual(events, [
            ("lease", True), "last-target-byte", "resumed", "render"])
        self.assertEqual(result[1]["type"], "image")

    def test_midway_read_exception_still_resumes(self):
        events = []
        plan = self.plan()
        msx_mcp_server.SESSION.msx = self.fake_msx(events)
        msx_mcp_server.SESSION.profile = "real"

        with (mock.patch.object(msx_screenshot, "plan_realmsx_capture",
                                return_value=plan),
              mock.patch.object(
                  msx_screenshot, "acquire_realmsx_capture",
                  side_effect=RuntimeError("VRAM failed midway")),
              mock.patch.object(msx_screenshot,
                                "render_realmsx_capture") as render):
            with self.assertRaisesRegex(RuntimeError, "failed midway"):
                msx_mcp_server.t_screenshot()

        self.assertEqual(events, [("lease", True), "resumed"])
        render.assert_not_called()

    def test_old_agent_is_rejected_before_display_metadata_reads(self):
        legacy = RealMSX(socket_timeout=0.01)
        legacy._v3 = object()
        legacy.feature_bits = 0
        msx_mcp_server.SESSION.msx = legacy
        msx_mcp_server.SESSION.profile = "real"

        with mock.patch.object(
                msx_screenshot, "plan_realmsx_capture") as plan:
            with self.assertRaisesRegex(RealMSXError, "snapshot-lease"):
                msx_mcp_server.t_screenshot()

        plan.assert_not_called()

    def test_expired_atomic_lease_never_renders_the_capture(self):
        plan = self.plan()
        capture = self.capture(plan)
        target = RealMSX(socket_timeout=0.01)
        target._v3 = SimpleNamespace(timeout=15.0, max_payload=320)
        target.feature_bits = FEATURE_SNAPSHOT_LEASE
        target.agent_transport_id = 1
        target.agent_transport = "uart-16c550"
        msx_mcp_server.SESSION.msx = target
        msx_mcp_server.SESSION.profile = "real"
        statuses = iter(("running", "paused", "running"))

        with (mock.patch.object(
                  target, "status",
                  side_effect=lambda: {"state": next(statuses)}),
              mock.patch.object(target, "_request_v3", return_value=b""),
              mock.patch.object(msx_screenshot, "plan_realmsx_capture",
                                return_value=plan),
              mock.patch.object(msx_screenshot, "acquire_realmsx_capture",
                                return_value=capture),
              mock.patch.object(msx_screenshot,
                                "render_realmsx_capture") as render):
            with self.assertRaisesRegex(RealMSXError, "not atomic"):
                msx_mcp_server.t_screenshot()

        render.assert_not_called()

    def test_slow_8251_guard_and_explicit_opt_in(self):
        events = []
        plan = self.plan(size=60000, mode=8)
        capture = self.capture(plan)
        msx_mcp_server.SESSION.msx = self.fake_msx(
            events, transport_id=0)
        msx_mcp_server.SESSION.profile = "real"

        def render(_capture, path, **_kwargs):
            Path(path).write_bytes(b"slow-png")
            return path, plan.mode

        with (mock.patch.object(msx_screenshot, "plan_realmsx_capture",
                                return_value=plan),
              mock.patch.object(msx_screenshot, "acquire_realmsx_capture",
                                return_value=capture),
              mock.patch.object(msx_screenshot, "render_realmsx_capture",
                                side_effect=render)):
            with self.assertRaisesRegex(
                    OpenMSXError, "allow_slow=true"):
                msx_mcp_server.t_screenshot()
            self.assertEqual(events, [("lease", True), "resumed"])

            result = msx_mcp_server.t_screenshot(
                allow_slow=True)

        self.assertEqual(events, [
            ("lease", True), "resumed", ("lease", True), "resumed"])
        self.assertEqual(result[1]["type"], "image")
        schema = msx_mcp_server.TOOLS["msx_screenshot"][2]
        self.assertFalse(schema["properties"]["allow_slow"]["default"])


if __name__ == "__main__":
    unittest.main()
