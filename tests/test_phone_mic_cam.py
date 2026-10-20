"""Tests for the phone_mic_cam package. Run from the repository root:

    python3 -m unittest discover -s tests -v

They need only the app's own dependencies (aiohttp, qrcode). They don't touch
PulseAudio, the webcam driver or the network beyond 127.0.0.1. Checks that need
node, ffmpeg or dpkg-deb are skipped when those aren't installed.
"""
import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "phone_mic_cam", "web")
sys.path.insert(0, ROOT)
import phone_mic_cam  # noqa: E402
from phone_mic_cam import app, audio, camera, control_page, phone_server, stats  # noqa: E402


def read_web(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def reset_state():
    camera.cam = None
    camera.camera_problem = None
    phone_server.clients.clear()
    audio.mic_clients.clear()
    phone_server.cam_clients.clear()
    audio.mic_wanted = True
    audio.mic_always_on = False
    stats.mic_meter = stats.Meter()
    stats.cam_meter = stats.Meter()
    with audio.buf_lock:
        audio.buf.clear()


def phone_app():
    a = web.Application()
    a.router.add_get("/", phone_server.index)
    a.router.add_get("/ws", phone_server.ws_handler)
    a.router.add_get("/video", phone_server.video_handler)
    a.router.add_post("/report", phone_server.report_handler)
    return a


class FakeCam:
    def __init__(self):
        self.frames = []
        self.skipped = 0

    def push(self, jpeg):
        self.frames.append(jpeg)


class PairingTest(unittest.TestCase):
    def test_token_is_required(self):
        self.assertTrue(phone_server.authorized(make_mocked_request("GET", f"/?t={phone_server.TOKEN}")))
        self.assertFalse(phone_server.authorized(make_mocked_request("GET", "/?t=wrong")))
        self.assertFalse(phone_server.authorized(make_mocked_request("GET", "/")))


class PhoneServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_state()
        # Never use a real webcam device, even on a machine that has Phone Cam.
        self.no_device = mock.patch.object(camera, "find_loopback_device", return_value=None)
        self.no_device.start()
        self.client = TestClient(TestServer(phone_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.no_device.stop()
        reset_state()

    async def test_page_needs_token(self):
        for query, status in [("", 403), ("?t=wrong", 403), (f"?t={phone_server.TOKEN}", 200)]:
            resp = await self.client.get("/" + query)
            self.assertEqual(resp.status, status, query)
        resp = await self.client.get(f"/?t={phone_server.TOKEN}")
        self.assertIn("Start mic", await resp.text())

    async def test_streams_need_token(self):
        for path in ("/ws?t=wrong", "/video"):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await self.client.ws_connect(path)
            self.assertEqual(cm.exception.status, 403)

    async def test_mic_gets_state_and_audio_reaches_buffer(self):
        audio.mic_wanted = False
        ws = await self.client.ws_connect(f"/ws?t={phone_server.TOKEN}")
        self.assertEqual((await ws.receive()).data, "off")
        await ws.send_bytes(bytes(960))          # 10 ms of 48 kHz mono s16
        for _ in range(50):
            if stats.mic_meter.bytes:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(stats.mic_meter.bytes, 960)
        self.assertEqual(audio.mic_buffer_ms(), 10)
        await ws.close()

    async def test_mic_wanted_changes_are_sent(self):
        audio.mic_wanted = False
        ws = await self.client.ws_connect(f"/ws?t={phone_server.TOKEN}")
        await ws.receive()
        with contextlib.redirect_stdout(io.StringIO()):
            await audio.set_mic_wanted(True)
        self.assertEqual((await ws.receive()).data, "on")
        await ws.close()

    async def test_video_without_camera_closes_with_4000(self):
        ws = await self.client.ws_connect(f"/video?t={phone_server.TOKEN}")
        await ws.receive()
        self.assertEqual(ws.close_code, 4000)

    async def test_camera_loaded_after_startup_is_picked_up(self):
        # Regression: the camera was only set up at startup, so loading the
        # driver as the control page suggests didn't help until a restart.
        camera.camera_problem = "The webcam driver isn't loaded."
        self.no_device.stop()
        with mock.patch.object(camera, "find_loopback_device", return_value="/dev/video99"), \
                mock.patch.object(shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(camera, "VirtualCam", side_effect=lambda dev: FakeCam()), \
                contextlib.redirect_stdout(io.StringIO()):
            ws = await self.client.ws_connect(f"/video?t={phone_server.TOKEN}")
            await ws.send_bytes(b"\xff\xd8 frame")
            for _ in range(50):
                if camera.cam and camera.cam.frames:
                    break
                await asyncio.sleep(0.01)
            await ws.close()
        self.no_device.start()
        self.assertEqual(camera.cam.frames, [b"\xff\xd8 frame"])
        self.assertIsNone(camera.camera_problem)

    async def test_starting_live_drops_audio_left_from_last_session(self):
        # Regression: audio left in the buffer when the mic went to Ready was
        # played at the start of the next recording.
        audio.mic_wanted = False
        audio.feed(bytes(9600))                      # 100 ms left over
        with contextlib.redirect_stdout(io.StringIO()):
            await audio.set_mic_wanted(True)
        self.assertEqual(audio.mic_buffer_ms(), 0)

    async def test_video_forwards_only_jpeg(self):
        camera.cam = FakeCam()
        ws = await self.client.ws_connect(f"/video?t={phone_server.TOKEN}")
        await ws.send_bytes(b"\xff\xd8 a jpeg")
        await ws.send_bytes(b"not a jpeg")
        for _ in range(50):
            if camera.cam.frames:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(camera.cam.frames, [b"\xff\xd8 a jpeg"])
        self.assertEqual(stats.cam_meter.count, 1)
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
        self.assertEqual((await self.post(f"?t={phone_server.TOKEN}", data=b"hello"))[0], 400)
        self.assertEqual((await self.post(f"?t={phone_server.TOKEN}", json=[1]))[0], 400)
        self.assertEqual((await self.post(f"?t={phone_server.TOKEN}", data=b"x" * 8192))[0], 413)

    async def test_logs_one_clean_line(self):
        report = {"what": "camera", "name": "NotAllowedError",
                  "message": "line1\nFAKE log line\x1b[31m" + "A" * 500,
                  "mic": "Ready", "page": "visible"}
        status, log = await self.post(f"?t={phone_server.TOKEN}", json=report)
        self.assertEqual(status, 204)
        self.assertEqual(log.count("\n"), 1)
        self.assertIn("Phone camera refused: NotAllowedError: line1?FAKE log line?[31m", log)
        self.assertIn("(mic Ready, page visible)", log)
        self.assertNotIn("A" * 201, log)

    async def test_logs_which_step_was_refused(self):
        # Safari gives the same NotAllowedError for the camera permission and
        # for playing the preview; the step tells them apart.
        report = {"what": "camera", "step": "preview", "name": "NotAllowedError",
                  "message": "not allowed", "mic": "off", "page": "visible"}
        _, log = await self.post(f"?t={phone_server.TOKEN}", json=report)
        self.assertIn("Phone camera refused at preview: NotAllowedError: not allowed", log)
        report["step"] = "permission\nFAKE" + "B" * 50
        _, log = await self.post(f"?t={phone_server.TOKEN}", json=report)
        self.assertIn("refused at permission?F: NotAllowedError", log)
        self.assertEqual(log.count("\n"), 1)


class LogFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "state", "phone-mic-cam", "last-run.log")

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_launch_leaves_running_copys_log_alone(self):
        # Regression: opening the app again (to reopen the control page)
        # wiped the running copy's log.
        os.makedirs(os.path.dirname(self.log))
        with open(self.log, "w") as f:
            f.write("12:00:00 Phone camera refused: NotAllowedError\n")
        with mock.patch.object(app, "port_free", return_value=False), \
                mock.patch.object(control_page, "running_copy_url", return_value="http://127.0.0.1:1/?t=x"), \
                mock.patch.object(app, "log_to_file") as log_to_file, \
                contextlib.redirect_stdout(io.StringIO()):
            app.main(["--log-file", self.log, "--no-browser"])
        log_to_file.assert_not_called()
        with open(self.log) as f:
            self.assertIn("Phone camera refused", f.read())

    def test_log_to_file_starts_fresh_keeps_previous_run_and_catches_child_output(self):
        os.makedirs(os.path.dirname(self.log))
        with open(self.log, "w") as f:
            f.write("previous run\n")
        os.chmod(self.log, 0o600)
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); from phone_mic_cam import app as a; "
                "a.log_to_file(sys.argv[2]); print('from python'); "
                "os.system('echo from a child process')")
        subprocess.run([sys.executable, "-c", code, ROOT, self.log], check=True)
        with open(self.log) as f:
            text = f.read()
        self.assertNotIn("previous run", text)
        self.assertIn("from python", text)
        self.assertIn("from a child process", text)
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600)
        # Regression: a restart wiped the log, losing the phone's refusal reports.
        previous = os.path.join(os.path.dirname(self.log), "last-run.previous.log")
        with open(previous) as f:
            self.assertEqual(f.read(), "previous run\n")
        self.assertEqual(os.stat(previous).st_mode & 0o777, 0o600)

    def test_launcher_passes_log_file_without_touching_it(self):
        fake_bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(fake_bin)
        args_file = os.path.join(self.tmp.name, "args")
        with open(os.path.join(fake_bin, "python3"), "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\n')
        os.chmod(os.path.join(fake_bin, "python3"), 0o755)
        os.makedirs(os.path.dirname(self.log))
        with open(self.log, "w") as f:
            f.write("running copy's log\n")
        env = dict(os.environ, PATH=fake_bin + os.pathsep + os.environ["PATH"],
                   XDG_STATE_HOME=os.path.join(self.tmp.name, "state"))
        subprocess.run(["sh", os.path.join(ROOT, "packaging", "phone-mic-cam"), "--no-browser"],
                       env=env, check=True, stdout=subprocess.PIPE)   # stdout is not a terminal
        with open(args_file) as f:
            args = f.read().split("\n")
        self.assertEqual(args[:6], ["-u", "-m", "phone_mic_cam",
                                    "--log-file", self.log, "--no-browser"])
        with open(self.log) as f:
            self.assertEqual(f.read(), "running copy's log\n")


class CleanTest(unittest.TestCase):
    def test_replaces_unprintable_and_truncates(self):
        self.assertEqual(phone_server.clean("a\nb\tc\x1bd", 100), "a?b?c?d")
        self.assertEqual(phone_server.clean("x" * 50, 10), "x" * 10)
        self.assertEqual(phone_server.clean(None, 10), "None")


class ControlPageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(control_page, "CONTROL_FILE", os.path.join(self.tmp.name, "url"))
        self.patch.start()
        self.start, self.stop = control_page.hooks("https://192.0.2.1:8443/?t=x",
                                                       open_browser=False)
        with contextlib.redirect_stdout(io.StringIO()):
            await self.start(None)
        with open(control_page.CONTROL_FILE) as f:
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
        self.assertEqual(os.stat(control_page.CONTROL_FILE).st_mode & 0o777, 0o600)

    async def test_needs_token_and_local_host(self):
        self.assertEqual(await self.status_of(self.url), 200)
        self.assertEqual(await self.status_of(self.base + "/"), 403)
        self.assertEqual(await self.status_of(self.base + "/?t=wrong"), 403)
        # DNS rebinding: right address and token, but another site's host name
        self.assertEqual(await self.status_of(self.url, headers={"Host": "evil.example"}), 403)

    async def test_status_reports_state_and_stats(self):
        camera.camera_problem = "no driver"
        async with self.session.get(f"{self.base}/status?t={phone_server.TOKEN}") as resp:
            s = await resp.json()
        self.assertEqual(s["camera_problem"], "no driver")
        self.assertFalse(s["mic_connected"])
        self.assertEqual(set(s["stats"]), {"cam_fps", "cam_kb_per_sec", "cam_skipped",
                                           "mic_kb_per_sec", "mic_buffer_ms"})

    async def test_stop_needs_token_and_post(self):
        async with self.session.post(f"{self.base}/stop") as resp:
            self.assertEqual(resp.status, 403)
        async with self.session.get(f"{self.base}/stop?t={phone_server.TOKEN}") as resp:
            self.assertEqual(resp.status, 405)
        with mock.patch.object(os, "kill") as kill, \
                contextlib.redirect_stdout(io.StringIO()):
            async with self.session.post(f"{self.base}/stop?t={phone_server.TOKEN}") as resp:
                self.assertEqual(resp.status, 200)
            await asyncio.sleep(0.4)
        kill.assert_called_once_with(os.getpid(), signal.SIGINT)

    async def test_running_copy_url(self):
        self.assertEqual(await asyncio.to_thread(control_page.running_copy_url), self.url)
        await self.stop(None)                     # removes the control file
        self.assertIsNone(await asyncio.to_thread(control_page.running_copy_url))
        with open(control_page.CONTROL_FILE, "w") as f:    # left over from a crash
            f.write(self.url)
        self.assertIsNone(await asyncio.to_thread(control_page.running_copy_url))


class CameraDiagnosisTest(unittest.TestCase):
    def run_with(self, ffmpeg=True, loaded=False, sb_state="", test_key=""):
        def which(tool):
            return None if tool == "ffmpeg" and not ffmpeg else f"/usr/bin/{tool}"

        def isdir(path):
            return loaded if path == "/sys/module/v4l2loopback" else os.path.isdir(path)

        def run(cmd, **kw):
            out = sb_state if "--sb-state" in cmd else test_key
            return types.SimpleNamespace(stdout=out, stderr="")

        with mock.patch.object(shutil, "which", which), \
                mock.patch.object(os.path, "isdir", isdir), \
                mock.patch.object(os.path, "exists", return_value=True), \
                mock.patch.object(subprocess, "run", run):
            return camera.diagnose_camera()

    def test_ffmpeg_missing(self):
        self.assertIn("ffmpeg isn't installed", self.run_with(ffmpeg=False))

    def test_loaded_under_another_name(self):
        self.assertIn("not as 'Phone Cam'", self.run_with(loaded=True))

    def test_secure_boot_blocks(self):
        msg = self.run_with(sb_state="SecureBoot enabled",
                            test_key=f"{camera.MOK_KEY} is not enrolled")
        self.assertIn("Secure Boot is blocking", msg)
        self.assertIn("mokutil --import", msg)

    def test_not_loaded(self):
        msg = self.run_with(sb_state="SecureBoot disabled")
        self.assertIn("isn't loaded", msg)
        self.assertIn('card_label="Phone Cam"', msg)


class MicInUseTest(unittest.IsolatedAsyncioTestCase):
    async def check(self, outputs):
        sources = [{"index": 7, "name": audio.SOURCE}, {"index": 3, "name": "other"}]

        async def fake(*args):
            return sources if "sources" in args else outputs

        with mock.patch.object(audio, "pactl_json", fake):
            return await audio.mic_in_use()

    async def test_counts_only_active_recordings_of_phone_mic(self):
        self.assertFalse(await self.check([]))
        self.assertTrue(await self.check([{"source": 7, "corked": False}]))
        self.assertFalse(await self.check([{"source": 7, "corked": True}]))
        self.assertFalse(await self.check([{"source": 3, "corked": False}]))


class VirtualMicTest(unittest.TestCase):
    def test_remembers_default_before_pulseaudio_switches_it(self):
        # Regression: PulseAudio can make a new source the default by itself;
        # asking afterwards gave "phone_mic", so the real mic wasn't restored.
        state = {"default": "real_mic"}

        def fake_pactl(*args, check=True):
            if args[0] == "get-default-source":
                return state["default"]
            if args[0] == "load-module":
                state["default"] = audio.SOURCE          # PulseAudio switches by itself
                return "42"
            return ""

        with mock.patch.object(audio, "pactl", fake_pactl), \
                mock.patch.object(audio, "unload_stale_modules"), \
                mock.patch.object(os.path, "exists", return_value=True):
            self.assertEqual(audio.create_virtual_mic(), ("42", "real_mic"))


class MeterTest(unittest.TestCase):
    def test_rates(self):
        with mock.patch.object(time, "monotonic", return_value=100.0):
            m = stats.Meter()
        for _ in range(30):
            m.add(1000)
        with mock.patch.object(time, "monotonic", return_value=102.0):
            m.tick()
        self.assertEqual(m.per_sec, 15)
        self.assertEqual(m.bytes_per_sec, 15000)


class VirtualCamTest(unittest.TestCase):
    def test_counts_frames_replaced_before_ffmpeg_took_them(self):
        cam = camera.VirtualCam.__new__(camera.VirtualCam)     # without its ffmpeg thread
        cam.frame, cam.skipped, cam.cond = None, 0, threading.Condition()
        cam.push(b"1")
        cam.push(b"2")
        cam.push(b"3")
        self.assertEqual((cam.frame, cam.skipped), (b"3", 2))

    def spawn_command(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            camera.VirtualCam._spawn(types.SimpleNamespace(device="/dev/video10"))
        return popen.call_args.args[0]

    def test_ffmpeg_passes_frames_through_when_not_probing(self):
        # Regression: with -probesize 32 ffmpeg guesses 1200k fps and drops
        # almost every frame unless frame timing is passed through.
        with mock.patch.object(camera, "ffmpeg_passthrough_args",
                               return_value=("-vsync", "passthrough")):
            cmd = self.spawn_command()
        self.assertIn("-probesize", cmd)
        i = cmd.index("-vsync")
        self.assertEqual(cmd[i + 1], "passthrough")

    def test_passthrough_option_follows_ffmpeg_version(self):
        helps = {
            # ffmpeg 8: -vsync deprecated, mentions -fps_mode, which has its own line
            "-vsync <>  set video sync method globally; deprecated, use -fps_mode\n"
            "-fps_mode[:<stream_spec>]  set framerate mode\n": "-fps_mode",
            # ffmpeg 4.4 (Ubuntu 22.04): no -fps_mode
            "-vsync  video sync method\n-r rate  set frame rate\n": "-vsync",
        }
        for help_text, expected in helps.items():
            camera.ffmpeg_passthrough_args.cache_clear()
            with mock.patch.object(subprocess, "run",
                                   return_value=types.SimpleNamespace(stdout=help_text)):
                self.assertEqual(camera.ffmpeg_passthrough_args(), (expected, "passthrough"))
        camera.ffmpeg_passthrough_args.cache_clear()

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_keeps_frames_with_our_options(self):
        # Runs the app's camera options through this system's ffmpeg: catches
        # options it doesn't know and the dropped-frames problem.
        camera.ffmpeg_passthrough_args.cache_clear()
        camera.ffmpeg_passthrough_args()          # real detection, before Popen is mocked
        jpeg = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=size=320x240", "-frames:v", "1", "-f", "mjpeg", "-"],
            capture_output=True, check=True).stdout
        cmd = self.spawn_command()
        cmd = cmd[:cmd.index("-pix_fmt")] + ["-f", "framemd5", "-"]
        r = subprocess.run(cmd, input=jpeg * 30, capture_output=True)
        frames = [l for l in r.stdout.decode().splitlines() if l and not l.startswith("#")]
        self.assertEqual(r.returncode, 0, r.stderr.decode()[-500:])
        self.assertGreaterEqual(len(frames), 25, r.stderr.decode()[-500:])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_keeps_phone_cam_size_when_the_phone_changes_size(self):
        # The phone sends 720p or 480p and can switch mid-call; apps using the
        # webcam must keep getting 1280x720.
        def jpeg(size):
            return subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", f"testsrc=size={size}", "-frames:v", "1", "-f", "mjpeg", "-"],
                capture_output=True, check=True).stdout
        camera.ffmpeg_passthrough_args.cache_clear()
        camera.ffmpeg_passthrough_args()
        cmd = self.spawn_command()
        cmd = cmd[:cmd.index("-f", cmd.index("-pix_fmt"))] + ["-f", "rawvideo", "-"]
        frames = jpeg("854x480") * 10 + jpeg("1280x720") * 10 + jpeg("854x480") * 10
        r = subprocess.run(cmd, input=frames, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode()[-500:])
        w, h = camera.CAM_SIZE
        frame_bytes = w * h * 3 // 2                  # yuv420p
        self.assertEqual(len(r.stdout) % frame_bytes, 0, "a frame isn't 1280x720")
        self.assertGreaterEqual(len(r.stdout) // frame_bytes, 25)


class PageScriptTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_page_javascript_parses(self):
        for name in ("phone.js", "control.js"):
            r = subprocess.run(["node", "--check", os.path.join(WEB, name)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")

    def test_pages_are_served_with_their_scripts_inlined(self):
        for name in ("phone", "control"):
            page = phone_server.load_page(name)
            self.assertNotIn("{{SCRIPT}}", page)
            self.assertIn(read_web(f"{name}.js").strip(), page)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PhoneReconnectTest(unittest.TestCase):
    """Runs the phone page's mic connect() in node with fake sockets and timers."""

    HARNESS = r"""
const sockets = []; let timers = [];
class WebSocket { constructor(url) { this.url = url; sockets.push(this); } }
const setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };
const clearTimeout = id => { if (id) timers[id - 1] = null; };
const flush = () => { const t = timers; timers = []; t.forEach(fn => fn && fn()); };
const location = {host: 'pc'}, TOKEN = 'x', RECONNECTING = 'reconnecting';
let ws, running = true; const states = [];
const setLive = on => states.push(on), say = () => {};
%s
const check = (label, cond) => { if (!cond) { console.log('FAIL: ' + label); process.exit(1); } };
connect();                                   // socket 0
sockets[0].onclose();                        // dropped: reconnect scheduled
connect();                                   // a new connection while it's pending: socket 1
flush();                                     // the pending reconnect must not fire
check('pending reconnect is cancelled by a new connection', sockets.length === 2);
sockets[0].onclose();                        // a late close of the old socket
flush();
check('old socket does not reconnect', sockets.length === 2);
sockets[0].onmessage({data: 'on'});
check('old socket does not change state', !states.includes(true));
sockets[1].onmessage({data: 'on'});
check('current socket changes state', states[states.length - 1] === true);
console.log('ok');
"""

    def test_one_connection_at_a_time(self):
        connect = re.search(r"let reconnect;\nfunction connect\(\) \{.*?\n\}\n",
                            read_web("phone.js"), re.S)
        self.assertIsNotNone(connect, "connect() not found in the phone page")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(self.HARNESS % connect.group(0))
        try:
            r = subprocess.run(["node", f.name], capture_output=True, text=True)
        finally:
            os.remove(f.name)
        self.assertEqual(r.stdout.strip(), "ok", r.stdout + r.stderr)

    def test_stop_cancels_pending_reconnects(self):
        for name, timer in (("stop", "reconnect"), ("stopCam", "camReconnect")):
            body = re.search(rf"function {name}\(\) \{{(.*?)\n\}}", read_web("phone.js"),
                             re.S).group(1)
            self.assertIn(f"clearTimeout({timer})", body, f"{name}() must cancel {timer}")

    def test_no_terminal_mentioned_to_phone_users(self):
        self.assertNotIn("terminal", phone_server.PAGE)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class CameraQualityTest(unittest.TestCase):
    """Runs the phone page's nextQuality() in node with a simulated send queue."""

    HARNESS = r"""
%s
const check = (label, cond) => { if (!cond) { console.log('FAIL: ' + label); process.exit(1); } };
let a = {q: 1, calm: 0, wait: 0};
a = nextQuality(a, 0.7, 0);
check('starts at the chosen quality', a.q === 0.7);
a = nextQuality(a, 0.7, BUSY + 1);
check('busy Wi-Fi lowers quality', Math.abs(a.q - 0.6) < 1e-9);
for (let i = 0; i < 5; i++) a = nextQuality(a, 0.7, BUSY + 1);
check('waits a few frames before lowering again', Math.abs(a.q - 0.6) < 1e-9);
for (let i = 0; i < 100; i++) a = nextQuality(a, 0.7, BUSY + 1);
check('never below the minimum', a.q === MIN_QUALITY);
for (let i = 0; i < 29; i++) a = nextQuality(a, 0.7, 0);
check('stays low until the queue has been empty for a while', a.q === MIN_QUALITY);
a = nextQuality(a, 0.7, 0);
check('then steps back up', a.q > MIN_QUALITY);
a = nextQuality(a, 0.7, 10);
check('a small queue resets the calm count', a.calm === 0);
for (let i = 0; i < 1000; i++) a = nextQuality(a, 0.7, 0);
check('recovers to the chosen quality and no higher', Math.abs(a.q - 0.7) < 1e-9);
a = nextQuality(a, 0.5, 0);
check('choosing a lower quality applies at once', a.q === 0.5);
console.log('ok');
"""

    def test_quality_follows_the_send_queue(self):
        js = read_web("phone.js")
        consts = re.search(r"const MIN_QUALITY = .*?;\n", js).group(0)
        fn = re.search(r"function nextQuality\(.*?\n\}\n", js, re.S)
        self.assertIsNotNone(fn, "nextQuality() not found in the phone page")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(self.HARNESS % (consts + fn.group(0)))
        try:
            r = subprocess.run(["node", f.name], capture_output=True, text=True)
        finally:
            os.remove(f.name)
        self.assertEqual(r.stdout.strip(), "ok", r.stdout + r.stderr)

    def test_frame_rate_setting_with_a_30_fps_camera(self):
        js = read_web("phone.js")
        due = re.search(r"const frameDue = .*?;\n", js)
        self.assertIsNotNone(due, "frameDue() not found in the phone page")
        harness = due.group(0) + r"""
const sent = fps => { let last = -1e9, n = 0;
  for (let i = 0; i < 300; i++) {                      // 10 s of a camera with timing jitter
    const now = i * 1000 / 30 + (i % 3) * 2;
    if (frameDue(now, last, fps)) { last = now; n++; } }
  return n / 10; };
console.log(JSON.stringify([sent(30), sent(15)]));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
        try:
            r = subprocess.run(["node", f.name], capture_output=True, text=True)
        finally:
            os.remove(f.name)
        self.assertEqual(json.loads(r.stdout), [30, 15], r.stderr)

    def test_every_setting_choice_is_known_to_the_script(self):
        js, html = read_web("phone.js"), read_web("phone.html")
        tables = {"size": "SIZES", "quality": "QUALITY"}
        for select, table in tables.items():
            known = re.search(rf"const {table} = \{{(.*?)\}};", js).group(1)
            options = re.search(rf'<select id="{select}".*?</select>', html, re.S).group(0)
            for value in re.findall(r'value="([^"]+)"', options):
                self.assertRegex(known, rf"'?{value}'?:", f"{select} option {value}")
        fps = re.search(r'<select id="fps".*?</select>', html, re.S).group(0)
        self.assertEqual(set(re.findall(r'value="(\d+)"', fps)), {"30", "15"})


class PackageTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb not installed")
    def test_build_deb(self):
        subprocess.run(["sh", "packaging/build-deb.sh"], cwd=ROOT, check=True,
                       capture_output=True)
        deb = os.path.join(ROOT, "dist", f"phone-mic-cam_{phone_mic_cam.__version__}_all.deb")
        info = subprocess.run(["dpkg-deb", "-I", deb], capture_output=True, text=True,
                              check=True).stdout
        self.assertIn(f"Version: {phone_mic_cam.__version__}", info)
        for dep in ("python3-aiohttp", "python3-qrcode", "ffmpeg", "v4l2loopback-dkms"):
            self.assertIn(dep, info)
        files = subprocess.run(["dpkg-deb", "-c", deb], capture_output=True, text=True,
                               check=True).stdout
        for path in ("./usr/bin/phone-mic-cam",
                     "./usr/share/phone-mic-cam/phone_mic_cam/__main__.py",
                     "./usr/share/phone-mic-cam/phone_mic_cam/app.py",
                     "./usr/share/phone-mic-cam/phone_mic_cam/web/phone.js",
                     "./usr/share/phone-mic-cam/phone_mic_cam/web/control.html",
                     "./usr/share/applications/phone-mic-cam.desktop",
                     "./usr/lib/modprobe.d/phone-mic-cam.conf"):
            self.assertIn(path, files)
        self.assertRegex(files, r"-rwxr-xr-x root/root .* \./usr/bin/phone-mic-cam")
        self.assertNotIn("__pycache__", files)


if __name__ == "__main__":
    unittest.main()
