"""Tests for phone_mic_cam.py. Run from the repository root:

    python3 -m unittest discover -s tests -v

They need only the app's own dependencies (aiohttp, qrcode). They don't touch
PulseAudio, the webcam driver or the network beyond 127.0.0.1. Checks that need
node or dpkg-deb are skipped when those aren't installed.
"""
import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import phone_mic_cam as app  # noqa: E402


def reset_state():
    app.cam = None
    app.camera_problem = None
    app.clients.clear()
    app.mic_clients.clear()
    app.cam_clients.clear()
    app.mic_wanted = True
    app.mic_always_on = False
    app.mic_meter = app.Meter()
    app.cam_meter = app.Meter()
    with app.buf_lock:
        app.buf.clear()


def phone_app():
    a = web.Application()
    a.router.add_get("/", app.index)
    a.router.add_get("/ws", app.ws_handler)
    a.router.add_get("/video", app.video_handler)
    a.router.add_post("/report", app.report_handler)
    return a


class FakeCam:
    def __init__(self):
        self.frames = []
        self.skipped = 0

    def push(self, jpeg):
        self.frames.append(jpeg)


class PairingTest(unittest.TestCase):
    def test_token_is_required(self):
        self.assertTrue(app.authorized(make_mocked_request("GET", f"/?t={app.TOKEN}")))
        self.assertFalse(app.authorized(make_mocked_request("GET", "/?t=wrong")))
        self.assertFalse(app.authorized(make_mocked_request("GET", "/")))


class PhoneServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_state()
        self.client = TestClient(TestServer(phone_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        reset_state()

    async def test_page_needs_token(self):
        for query, status in [("", 403), ("?t=wrong", 403), (f"?t={app.TOKEN}", 200)]:
            resp = await self.client.get("/" + query)
            self.assertEqual(resp.status, status, query)
        resp = await self.client.get(f"/?t={app.TOKEN}")
        self.assertIn("Start mic", await resp.text())

    async def test_streams_need_token(self):
        for path in ("/ws?t=wrong", "/video"):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await self.client.ws_connect(path)
            self.assertEqual(cm.exception.status, 403)

    async def test_mic_gets_state_and_audio_reaches_buffer(self):
        app.mic_wanted = False
        ws = await self.client.ws_connect(f"/ws?t={app.TOKEN}")
        self.assertEqual((await ws.receive()).data, "off")
        await ws.send_bytes(bytes(960))          # 10 ms of 48 kHz mono s16
        for _ in range(50):
            if app.mic_meter.bytes:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(app.mic_meter.bytes, 960)
        self.assertEqual(app.mic_buffer_ms(), 10)
        await ws.close()

    async def test_mic_wanted_changes_are_sent(self):
        app.mic_wanted = False
        ws = await self.client.ws_connect(f"/ws?t={app.TOKEN}")
        await ws.receive()
        with contextlib.redirect_stdout(io.StringIO()):
            await app.set_mic_wanted(True)
        self.assertEqual((await ws.receive()).data, "on")
        await ws.close()

    async def test_video_without_camera_closes_with_4000(self):
        ws = await self.client.ws_connect(f"/video?t={app.TOKEN}")
        await ws.receive()
        self.assertEqual(ws.close_code, 4000)

    async def test_video_forwards_only_jpeg(self):
        app.cam = FakeCam()
        ws = await self.client.ws_connect(f"/video?t={app.TOKEN}")
        await ws.send_bytes(b"\xff\xd8 a jpeg")
        await ws.send_bytes(b"not a jpeg")
        for _ in range(50):
            if app.cam.frames:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(app.cam.frames, [b"\xff\xd8 a jpeg"])
        self.assertEqual(app.cam_meter.count, 1)
        await ws.close()


class ReportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(phone_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def post(self, query, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resp = await self.client.post("/report" + query, **kw)
        return resp.status, out.getvalue()

    async def test_rejects_bad_requests(self):
        ok = {"what": "camera"}
        self.assertEqual((await self.post("", json=ok))[0], 403)
        self.assertEqual((await self.post("?t=wrong", json=ok))[0], 403)
        self.assertEqual((await self.post(f"?t={app.TOKEN}", data=b"hello"))[0], 400)
        self.assertEqual((await self.post(f"?t={app.TOKEN}", json=[1]))[0], 400)
        self.assertEqual((await self.post(f"?t={app.TOKEN}", data=b"x" * 8192))[0], 413)

    async def test_logs_one_clean_line(self):
        report = {"what": "camera", "name": "NotAllowedError",
                  "message": "line1\nFAKE log line\x1b[31m" + "A" * 500,
                  "mic": "Ready", "page": "visible"}
        status, log = await self.post(f"?t={app.TOKEN}", json=report)
        self.assertEqual(status, 204)
        self.assertEqual(log.count("\n"), 1)
        self.assertIn("Phone camera refused: NotAllowedError: line1?FAKE log line?[31m", log)
        self.assertIn("(mic Ready, page visible)", log)
        self.assertNotIn("A" * 201, log)


class CleanTest(unittest.TestCase):
    def test_replaces_unprintable_and_truncates(self):
        self.assertEqual(app.clean("a\nb\tc\x1bd", 100), "a?b?c?d")
        self.assertEqual(app.clean("x" * 50, 10), "x" * 10)
        self.assertEqual(app.clean(None, 10), "None")


class ControlPageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(app, "CONTROL_FILE", os.path.join(self.tmp.name, "url"))
        self.patch.start()
        self.start, self.stop = app.control_page_hooks("https://192.0.2.1:8443/?t=x",
                                                       open_browser=False)
        with contextlib.redirect_stdout(io.StringIO()):
            await self.start(None)
        with open(app.CONTROL_FILE) as f:
            self.url = f.read()
        self.base = self.url.split("/?")[0]
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.stop(None)
        self.patch.stop()
        self.tmp.cleanup()
        reset_state()

    async def status_of(self, url, **kw):
        async with self.session.get(url, **kw) as resp:
            return resp.status

    async def test_control_file_is_private(self):
        self.assertEqual(os.stat(app.CONTROL_FILE).st_mode & 0o777, 0o600)

    async def test_needs_token_and_local_host(self):
        self.assertEqual(await self.status_of(self.url), 200)
        self.assertEqual(await self.status_of(self.base + "/"), 403)
        self.assertEqual(await self.status_of(self.base + "/?t=wrong"), 403)
        # DNS rebinding: right address and token, but another site's host name
        self.assertEqual(await self.status_of(self.url, headers={"Host": "evil.example"}), 403)

    async def test_status_reports_state_and_stats(self):
        app.camera_problem = "no driver"
        async with self.session.get(f"{self.base}/status?t={app.TOKEN}") as resp:
            s = await resp.json()
        self.assertEqual(s["camera_problem"], "no driver")
        self.assertFalse(s["mic_connected"])
        self.assertEqual(set(s["stats"]), {"cam_fps", "cam_kb_per_sec", "cam_skipped",
                                           "mic_kb_per_sec", "mic_buffer_ms"})

    async def test_stop_needs_token_and_post(self):
        async with self.session.post(f"{self.base}/stop") as resp:
            self.assertEqual(resp.status, 403)
        async with self.session.get(f"{self.base}/stop?t={app.TOKEN}") as resp:
            self.assertEqual(resp.status, 405)
        with mock.patch.object(app.os, "kill") as kill, \
                contextlib.redirect_stdout(io.StringIO()):
            async with self.session.post(f"{self.base}/stop?t={app.TOKEN}") as resp:
                self.assertEqual(resp.status, 200)
            await asyncio.sleep(0.4)
        kill.assert_called_once_with(os.getpid(), app.signal.SIGINT)

    async def test_running_copy_url(self):
        self.assertEqual(await asyncio.to_thread(app.running_copy_url), self.url)
        await self.stop(None)                     # removes the control file
        self.assertIsNone(await asyncio.to_thread(app.running_copy_url))
        with open(app.CONTROL_FILE, "w") as f:    # left over from a crash
            f.write(self.url)
        self.assertIsNone(await asyncio.to_thread(app.running_copy_url))


class CameraDiagnosisTest(unittest.TestCase):
    def run_with(self, ffmpeg=True, loaded=False, sb_state="", test_key=""):
        def which(tool):
            return None if tool == "ffmpeg" and not ffmpeg else f"/usr/bin/{tool}"

        def isdir(path):
            return loaded if path == "/sys/module/v4l2loopback" else os.path.isdir(path)

        def run(cmd, **kw):
            out = sb_state if "--sb-state" in cmd else test_key
            return types.SimpleNamespace(stdout=out, stderr="")

        with mock.patch.object(app.shutil, "which", which), \
                mock.patch.object(app.os.path, "isdir", isdir), \
                mock.patch.object(app.os.path, "exists", return_value=True), \
                mock.patch.object(app.subprocess, "run", run):
            return app.diagnose_camera()

    def test_ffmpeg_missing(self):
        self.assertIn("ffmpeg isn't installed", self.run_with(ffmpeg=False))

    def test_loaded_under_another_name(self):
        self.assertIn("not as 'Phone Cam'", self.run_with(loaded=True))

    def test_secure_boot_blocks(self):
        msg = self.run_with(sb_state="SecureBoot enabled",
                            test_key=f"{app.MOK_KEY} is not enrolled")
        self.assertIn("Secure Boot is blocking", msg)
        self.assertIn("mokutil --import", msg)

    def test_not_loaded(self):
        msg = self.run_with(sb_state="SecureBoot disabled")
        self.assertIn("isn't loaded", msg)
        self.assertIn('card_label="Phone Cam"', msg)


class MicInUseTest(unittest.IsolatedAsyncioTestCase):
    async def check(self, outputs):
        sources = [{"index": 7, "name": app.SOURCE}, {"index": 3, "name": "other"}]

        async def fake(*args):
            return sources if "sources" in args else outputs

        with mock.patch.object(app, "pactl_json", fake):
            return await app.mic_in_use()

    async def test_counts_only_active_recordings_of_phone_mic(self):
        self.assertFalse(await self.check([]))
        self.assertTrue(await self.check([{"source": 7, "corked": False}]))
        self.assertFalse(await self.check([{"source": 7, "corked": True}]))
        self.assertFalse(await self.check([{"source": 3, "corked": False}]))


class MeterTest(unittest.TestCase):
    def test_rates(self):
        with mock.patch.object(app.time, "monotonic", return_value=100.0):
            m = app.Meter()
        for _ in range(30):
            m.add(1000)
        with mock.patch.object(app.time, "monotonic", return_value=102.0):
            m.tick()
        self.assertEqual(m.per_sec, 15)
        self.assertEqual(m.bytes_per_sec, 15000)


class VirtualCamTest(unittest.TestCase):
    def test_counts_frames_replaced_before_ffmpeg_took_them(self):
        cam = app.VirtualCam.__new__(app.VirtualCam)     # without its ffmpeg thread
        cam.frame, cam.skipped, cam.cond = None, 0, threading.Condition()
        cam.push(b"1")
        cam.push(b"2")
        cam.push(b"3")
        self.assertEqual((cam.frame, cam.skipped), (b"3", 2))

    def test_ffmpeg_passes_frames_through_when_not_probing(self):
        # Regression: with -probesize 32 ffmpeg guesses 1200k fps and drops
        # almost every frame unless frame timing is passed through.
        with mock.patch.object(app.subprocess, "Popen") as popen:
            app.VirtualCam._spawn(types.SimpleNamespace(device="/dev/video10"))
        cmd = popen.call_args.args[0]
        self.assertIn("-probesize", cmd)
        i = cmd.index("-fps_mode")
        self.assertEqual(cmd[i + 1], "passthrough")


class PageScriptTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_page_javascript_parses(self):
        pages = {"phone": app.PAGE, "control": app.CONTROL_PAGE.replace("{{TOKEN}}", '"x"')}
        for name, page in pages.items():
            script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(script)
            try:
                r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name} page: {r.stderr}")
            finally:
                os.remove(f.name)


class PackageTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb not installed")
    def test_build_deb(self):
        subprocess.run(["sh", "packaging/build-deb.sh"], cwd=ROOT, check=True,
                       capture_output=True)
        deb = os.path.join(ROOT, "dist", f"phone-mic-cam_{app.__version__}_all.deb")
        info = subprocess.run(["dpkg-deb", "-I", deb], capture_output=True, text=True,
                              check=True).stdout
        self.assertIn(f"Version: {app.__version__}", info)
        for dep in ("python3-aiohttp", "python3-qrcode", "ffmpeg", "v4l2loopback-dkms"):
            self.assertIn(dep, info)
        files = subprocess.run(["dpkg-deb", "-c", deb], capture_output=True, text=True,
                               check=True).stdout
        for path in ("./usr/bin/phone-mic-cam", "./usr/share/phone-mic-cam/phone_mic_cam.py",
                     "./usr/share/applications/phone-mic-cam.desktop",
                     "./usr/lib/modprobe.d/phone-mic-cam.conf"):
            self.assertIn(path, files)
        self.assertRegex(files, r"-rwxr-xr-x root/root .* \./usr/bin/phone-mic-cam")


if __name__ == "__main__":
    unittest.main()
