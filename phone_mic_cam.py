#!/usr/bin/env python3
"""
Phone Mic Cam: use your phone as this Linux computer's microphone and webcam,
with no app on the phone.

  1. pip install -r requirements.txt     (needs openssl on the PATH)
  2. python3 phone_mic_cam.py
  3. Scan the QR code it shows (terminal and browser) with the phone on the
     same Wi-Fi, accept the certificate warning, tap "Start mic" and/or
     "Start camera". The link contains a secret code that changes every run.

While it runs, the computer has a microphone called "Phone Mic" set as the
default input, so every app (browser, YouTube, Claude, Discord...) uses it.
Ctrl+C restores your previous microphone.

Optional camera ("Phone Cam" webcam), one-time setup:
  sudo apt install ffmpeg v4l2loopback-dkms
  sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="Phone Cam" exclusive_caps=1
Without it the script runs mic-only.

SPDX-License-Identifier: MIT
"""
import argparse
import asyncio
import glob
import hmac
import html
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser

try:
    from aiohttp import web, WSCloseCode, WSMsgType
    import qrcode
    import qrcode.image.svg
except ImportError as e:
    sys.exit(f"Missing Python package '{e.name}'. Install with:  pip install -r requirements.txt")

RATE = 48000                      # samples/sec, mono, signed 16-bit
CHUNK_MS = 10
CHUNK_BYTES = RATE * CHUNK_MS // 1000 * 2
PREBUFFER = RATE * 40 // 1000 * 2     # wait for 40 ms before playing
MAX_BUFFER = RATE * 150 // 1000 * 2   # if lag exceeds 150 ms...
TRIM_TO = RATE * 60 // 1000 * 2       # ...drop back to 60 ms
PORT = 8443
FIFO = "/tmp/phone_mic_cam.fifo"
SOURCE = "phone_mic"
MIC_LABEL = "Phone Mic"
HERE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(HERE, "cert.pem")
KEY = os.path.join(HERE, "key.pem")
CAM_LABEL = "Phone Cam"           # must match v4l2loopback card_label
CAM_FPS = 30
MAX_FRAME = 1 << 21               # 2 MB per JPEG frame
TOKEN = secrets.token_urlsafe(16) # pairing code; only the QR link carries it

buf = bytearray()
buf_lock = threading.Lock()
stop_event = threading.Event()
cam = None                        # VirtualCam, or None when unavailable
clients = set()                   # open phone WebSockets, closed on shutdown
mic_clients = set()               # the subset streaming the mic
mic_wanted = True                 # an app is recording; stays True if we can't tell
MIC_OFF_DELAY = 3                 # seconds without a recording app before mic off


# ---------------------------------------------------------------- audio side
def pactl(*args, check=True):
    r = subprocess.run(["pactl", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def unload_stale_modules():
    for line in pactl("list", "short", "modules", check=False).splitlines():
        if "module-pipe-source" in line and FIFO in line:
            pactl("unload-module", line.split()[0], check=False)
    if os.path.exists(FIFO):
        os.remove(FIFO)


def create_virtual_mic():
    unload_stale_modules()
    module_id = pactl(
        "load-module", "module-pipe-source",
        f"source_name={SOURCE}", f"file={FIFO}",
        "format=s16le", f"rate={RATE}", "channels=1",
        f"source_properties='device.description=\"{MIC_LABEL}\"'",
    )
    for _ in range(50):              # wait for the FIFO to appear
        if os.path.exists(FIFO):
            break
        time.sleep(0.05)
    previous = pactl("get-default-source", check=False) or None
    pactl("set-default-source", SOURCE)
    return module_id, previous


def feed(data: bytes):
    if len(data) % 2:
        return
    with buf_lock:
        buf.extend(data)
        if len(buf) > MAX_BUFFER:            # keep latency low
            del buf[: len(buf) - TRIM_TO]


def writer_thread():
    """Write steady 10 ms chunks into the virtual mic; silence when idle."""
    fd = os.open(FIFO, os.O_RDWR)
    silence = bytes(CHUNK_BYTES)
    playing = False
    nxt = time.monotonic()
    while not stop_event.is_set():
        with buf_lock:
            if not playing and len(buf) >= PREBUFFER:
                playing = True
            if playing and len(buf) >= CHUNK_BYTES:
                chunk = bytes(buf[:CHUNK_BYTES])
                del buf[:CHUNK_BYTES]
            else:
                playing = False
                chunk = silence
        try:
            os.write(fd, chunk)
        except OSError:
            break
        nxt += CHUNK_MS / 1000
        delay = nxt - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.1:
            nxt = time.monotonic()
    os.close(fd)


# ---------------------------------------------------------------- video side
def find_loopback_device():
    """Return /dev/videoN of the v4l2loopback device labelled CAM_LABEL."""
    for name_file in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        with open(name_file) as f:
            if f.read().strip() == CAM_LABEL:
                return "/dev/" + name_file.split("/")[-2]
    return None


class VirtualCam:
    """Feed JPEG frames from the phone into the loopback device via ffmpeg."""

    def __init__(self, device):
        self.device = device
        self.proc = None
        self.frame = None
        self.cond = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def push(self, jpeg: bytes):
        with self.cond:
            self.frame = jpeg            # keep only the newest frame (low latency)
            self.cond.notify()

    def _spawn(self):
        return subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-fflags", "nobuffer", "-f", "mjpeg", "-framerate", str(CAM_FPS),
             "-i", "pipe:0", "-pix_fmt", "yuv420p", "-f", "v4l2", self.device],
            stdin=subprocess.PIPE,
        )

    def _run(self):
        while not stop_event.is_set():
            with self.cond:
                if self.frame is None:
                    self.cond.wait(0.5)
                frame, self.frame = self.frame, None
            if frame is None:
                continue
            if self.proc is None or self.proc.poll() is not None:
                self.proc = self._spawn()
            try:
                self.proc.stdin.write(frame)
                self.proc.stdin.flush()
            except OSError:
                print("ffmpeg stopped; restarting it on the next frame")
                self.proc = None
                time.sleep(1)

    def close(self):
        if self.proc and self.proc.poll() is None:
            # terminate rather than close stdin: the feeder thread may be
            # blocked mid-write holding stdin's lock
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def setup_camera():
    if not shutil.which("ffmpeg"):
        print("  Camera disabled: ffmpeg not installed (sudo apt install ffmpeg).")
        return None
    device = find_loopback_device()
    if device is None:
        print(f"  Camera disabled: no v4l2loopback device named '{CAM_LABEL}'. Run:")
        print("    sudo apt install v4l2loopback-dkms")
        print(f"    sudo modprobe v4l2loopback devices=1 video_nr=10 "
              f"card_label=\"{CAM_LABEL}\" exclusive_caps=1")
        return None
    print(f"  '{CAM_LABEL}' webcam available at {device}.")
    return VirtualCam(device)


# ---------------------------------------------------------------- web side
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Phone Mic Cam</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:24px;font-family:-apple-system,system-ui,sans-serif;
       background:#111;color:#eee}
  button{border:none;font-weight:600;background:#2a7;color:#fff}
  button.on{background:#c33}
  #btn{width:200px;height:200px;border-radius:50%;font-size:22px}
  .row{display:flex;gap:12px}
  .row button{padding:14px 22px;border-radius:12px;font-size:17px}
  #flip{background:#555}
  #preview{width:240px;max-height:240px;border-radius:12px;background:#000;display:none}
  #preview.mirror{transform:scaleX(-1)}
  .status{font-size:17px;opacity:.8;text-align:center;padding:0 16px}
  #bar{width:220px;height:10px;background:#333;border-radius:5px;overflow:hidden}
  #lvl{height:100%;width:0;background:#2c8;transition:width .05s}
</style></head><body>
<button id="btn">Start mic</button>
<div id="bar"><div id="lvl"></div></div>
<div id="status" class="status">Tap to start. Keep this page open.</div>
<video id="preview" playsinline muted autoplay></video>
<div class="row"><button id="cambtn">Start camera</button><button id="flip">Flip</button></div>
<div id="camstatus" class="status"></div>
<script>
const WORKLET = `
class Pcm extends AudioWorkletProcessor {
  constructor(){ super(); this.ratio = sampleRate/48000; this.pos = 0; this.last = 0;
                 this.out = new Int16Array(480); this.n = 0; }
  process(inputs){
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !ch.length) return true;
    const len = ch.length;
    while (this.pos < len - 1) {            // linear resample to 48 kHz
      const i = Math.floor(this.pos), f = this.pos - i;
      const a = i < 0 ? this.last : ch[i], b = ch[i + 1];
      let v = a + (b - a) * f;
      v = v > 1 ? 1 : v < -1 ? -1 : v;
      this.out[this.n++] = v < 0 ? v * 32768 : v * 32767;
      if (this.n === 480) { const c = this.out.slice(); this.port.postMessage(c.buffer, [c.buffer]); this.n = 0; }
      this.pos += this.ratio;
    }
    this.pos -= len; this.last = ch[len - 1];
    return true;
  }
}
registerProcessor('pcm', Pcm);`;

const TOKEN = encodeURIComponent(new URLSearchParams(location.search).get('t') || '');
const RECONNECTING = 'Reconnecting… (if this persists, scan the new QR code on your PC)';
const btn = document.getElementById('btn'), st = document.getElementById('status'),
      lvl = document.getElementById('lvl');
let ctx, stream, ws, wakeLock, running = false;
const say = t => st.textContent = t;

// The PC sends 'on' while an app there records from the mic, 'off' when none
// does; the mic only captures and sends audio while 'on'.
let live = false;
function setLive(on) {
  live = on;
  if (stream) stream.getAudioTracks().forEach(t => t.enabled = on);
  if (!on) lvl.style.width = '0';
  say(on ? 'Live: your PC is hearing you'
         : 'Ready: turns on when an app on your PC uses the mic');
}

function connect() {
  ws = new WebSocket(`wss://${location.host}/ws?t=${TOKEN}`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = e => { if (typeof e.data === 'string') setLive(e.data === 'on'); };
  ws.onclose = () => {
    if (!running) return;
    setLive(false); say(RECONNECTING); setTimeout(connect, 1000);
  };
}

async function keepAwake() {
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
}

async function start() {
  stream = await navigator.mediaDevices.getUserMedia({audio: {
    channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }});
  ctx = new AudioContext();
  const url = URL.createObjectURL(new Blob([WORKLET], {type: 'application/javascript'}));
  await ctx.audioWorklet.addModule(url);
  const node = new AudioWorkletNode(ctx, 'pcm');
  ctx.createMediaStreamSource(stream).connect(node).connect(ctx.destination);
  node.port.onmessage = e => {
    if (!live) return;
    if (ws && ws.readyState === 1) ws.send(e.data);
    const s = new Int16Array(e.data); let p = 0;
    for (let i = 0; i < s.length; i += 8) p = Math.max(p, Math.abs(s[i]));
    lvl.style.width = Math.min(100, p / 200) + '%';
  };
  setLive(false);
  running = true; connect(); keepAwake();
  btn.textContent = 'Stop'; btn.classList.add('on');
}

function stop() {
  running = false; live = false;
  if (ws) ws.close();
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (ctx) ctx.close();
  if (wakeLock) wakeLock.release();
  ctx = stream = ws = wakeLock = null;
  lvl.style.width = '0';
  btn.textContent = 'Start mic'; btn.classList.remove('on'); say('Stopped');
}

btn.onclick = async () => {
  if (running) return stop();
  try { await start(); } catch (e) { say('Error: ' + e.message); stop(); }
};
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && (running || camRunning)) keepAwake();
  if (document.visibilityState === 'visible' && running) ctx && ctx.resume();
});

// ------------------------------------------------------------ camera
const CAM_W = 1280, CAM_H = 720, CAM_FPS = 30, NO_CAMERA = 4000;
const camBtn = document.getElementById('cambtn'), flipBtn = document.getElementById('flip'),
      video = document.getElementById('preview'), camSt = document.getElementById('camstatus');
const canvas = document.createElement('canvas');
canvas.width = CAM_W; canvas.height = CAM_H;
const g = canvas.getContext('2d');
let camStream, camWs, camTimer, camRunning = false, encoding = false, facing = 'user';
const camSay = t => camSt.textContent = t;

function camConnect() {
  camWs = new WebSocket(`wss://${location.host}/video?t=${TOKEN}`);
  camWs.onopen = () => camSay('Camera live: your PC is seeing you');
  camWs.onclose = e => {
    if (e.code === NO_CAMERA) { stopCam(); camSay('Your PC has no virtual camera set up (see its terminal).'); }
    else if (camRunning) { camSay('Camera: ' + RECONNECTING); setTimeout(camConnect, 1000); }
  };
}

async function openCamera() {
  if (camStream) camStream.getTracks().forEach(t => t.stop());
  camStream = await navigator.mediaDevices.getUserMedia({video: {
    facingMode: facing, width: {ideal: CAM_W}, height: {ideal: CAM_H}, frameRate: {ideal: CAM_FPS} }});
  video.srcObject = camStream;
  video.classList.toggle('mirror', facing === 'user');
  await video.play();
}

function sendFrame() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || encoding || !camWs || camWs.readyState !== 1) return;
  if (camWs.bufferedAmount > 512 * 1024) return;      // Wi-Fi is behind: skip a frame
  const s = Math.min(CAM_W / vw, CAM_H / vh), w = vw * s, h = vh * s;
  g.fillStyle = '#000'; g.fillRect(0, 0, CAM_W, CAM_H);
  g.drawImage(video, (CAM_W - w) / 2, (CAM_H - h) / 2, w, h);
  encoding = true;
  canvas.toBlob(b => { encoding = false; if (b && camWs && camWs.readyState === 1) camWs.send(b); },
                'image/jpeg', 0.7);
}

async function startCam() {
  await openCamera();
  video.style.display = 'block';
  camRunning = true; camConnect(); keepAwake();
  camTimer = setInterval(sendFrame, 1000 / CAM_FPS);
  camBtn.textContent = 'Stop camera'; camBtn.classList.add('on');
}

function stopCam() {
  camRunning = false;
  clearInterval(camTimer);
  if (camWs) camWs.close();
  if (camStream) camStream.getTracks().forEach(t => t.stop());
  camStream = camWs = camTimer = null;
  video.srcObject = null; video.style.display = 'none';
  camBtn.textContent = 'Start camera'; camBtn.classList.remove('on'); camSay('Camera stopped');
}

camBtn.onclick = async () => {
  if (camRunning) return stopCam();
  try { await startCam(); } catch (e) { stopCam(); camSay('Camera error: ' + e.message); }
};
flipBtn.onclick = async () => {
  facing = facing === 'user' ? 'environment' : 'user';
  if (camRunning) try { await openCamera(); } catch (e) { camSay('Camera error: ' + e.message); }
};
</script></body></html>"""


DESKTOP_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone Mic &amp; Camera</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:20px;font-family:system-ui,sans-serif;background:#111;color:#eee}
  #qr{background:#fff;padding:16px;border-radius:12px;width:280px;height:280px}
  #qr svg{width:100%;height:100%}
  ol{font-size:17px;line-height:1.6;max-width:420px}
  code{font-size:13px;opacity:.7;word-break:break-all;max-width:420px;text-align:center}
</style></head><body>
<h1>Scan with your phone</h1>
<div id="qr">{{QR}}</div>
<ol>
  <li>Connect your phone to the same Wi-Fi as this computer.</li>
  <li>Point the phone's camera at the QR code and open the link.</li>
  <li>Accept the certificate warning (Show Details &rarr; visit this website).</li>
  <li>Tap <b>Start mic</b> and/or <b>Start camera</b>, and allow access.</li>
</ol>
<code>{{URL}}</code>
</body></html>"""


def authorized(request) -> bool:
    return hmac.compare_digest(request.query.get("t", ""), TOKEN)


async def index(request):
    if not authorized(request):
        raise web.HTTPForbidden(text="This link is out of date. Scan the QR code shown on your computer.")
    return web.Response(text=PAGE, content_type="text/html")


async def ws_handler(request):
    if not authorized(request):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(max_msg_size=1 << 20)
    await ws.prepare(request)
    clients.add(ws)
    mic_clients.add(ws)
    print(f"Phone mic connected from {request.remote}")
    try:
        await ws.send_str("on" if mic_wanted else "off")
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                feed(msg.data)
    finally:
        clients.discard(ws)
        mic_clients.discard(ws)
    print("Phone mic disconnected")
    return ws


async def video_handler(request):
    if not authorized(request):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(max_msg_size=MAX_FRAME)
    await ws.prepare(request)
    if cam is None:
        await ws.close(code=4000, message=b"no virtual camera")
        return ws
    clients.add(ws)
    print(f"Phone camera connected from {request.remote}")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and msg.data[:2] == b"\xff\xd8":   # JPEG
                cam.push(msg.data)
    finally:
        clients.discard(ws)
    print("Phone camera disconnected")
    return ws


async def pactl_json(*args):
    proc = await asyncio.create_subprocess_exec(
        "pactl", "-f", "json", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return json.loads(out or b"[]")


async def mic_in_use() -> bool:
    """True while any app on this computer is recording from the virtual mic."""
    sources = await pactl_json("list", "short", "sources")
    ids = {s["index"] for s in sources if s["name"] == SOURCE}
    outputs = await pactl_json("list", "source-outputs")
    return any(o["source"] in ids and not o["corked"] for o in outputs)


async def set_mic_wanted(wanted: bool, quiet=False):
    global mic_wanted
    if wanted == mic_wanted:
        return
    mic_wanted = wanted
    if not quiet:
        print("An app is using the mic: phone mic on" if wanted else
              "No app is using the mic: phone mic off")
    for ws in list(mic_clients):
        try:
            await ws.send_str("on" if wanted else "off")
        except ConnectionError:
            pass                     # closing; its handler cleans up


async def watch_mic_demand():
    """Follow PulseAudio events and switch the phone mic on/off to match."""
    proc = turn_off = None

    async def turn_off_later():
        await asyncio.sleep(MIC_OFF_DELAY)       # apps often reopen the mic
        if not await mic_in_use():
            await set_mic_wanted(False)

    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "subscribe", stdout=asyncio.subprocess.PIPE)
        await set_mic_wanted(await mic_in_use())
        while line := await proc.stdout.readline():
            if b"source-output" not in line:
                continue
            if turn_off:
                turn_off.cancel()
                turn_off = None
            if await mic_in_use():
                await set_mic_wanted(True)
            elif mic_wanted:
                turn_off = asyncio.create_task(turn_off_later())
        print("pactl subscribe exited; leaving the phone mic always on")
    except (OSError, ValueError, KeyError) as e:
        print(f"Can't watch mic usage ({e}); leaving the phone mic always on")
    finally:
        if turn_off:
            turn_off.cancel()
        if proc and proc.returncode is None:
            proc.terminate()
            await proc.wait()
    await set_mic_wanted(True, quiet=True)     # fallback already explained above


def mic_watch_hooks(enabled):
    task = None

    async def start(app):
        nonlocal task
        global mic_wanted
        if enabled:
            mic_wanted = False
            task = asyncio.create_task(watch_mic_demand())
        else:
            print("  Phone mic is always on (--mic-always-on).")

    async def stop(app):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return start, stop


async def close_clients(app):
    """Close open phone connections so Ctrl+C doesn't wait for them to time out."""
    for ws in list(clients):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")


# ---------------------------------------------------------------- helpers
def ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    print("Creating a self-signed HTTPS certificate (Safari needs HTTPS for the mic)...")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", KEY, "-out", CERT, "-days", "3650", "-subj", "/CN=phone-mic-cam"],
        check=True, capture_output=True,
    )


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def port_free(port):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # same as aiohttp
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def print_qr(url):
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.print_ascii(invert=True)


def desktop_page_hooks(phone_url, open_browser):
    """Serve the QR code to this computer's browser (localhost only, plain HTTP)."""
    runner = None
    svg = qrcode.make(phone_url, image_factory=qrcode.image.svg.SvgPathImage)
    page = (DESKTOP_PAGE.replace("{{QR}}", svg.to_string(encoding="unicode"))
            .replace("{{URL}}", html.escape(phone_url)))

    async def show(request):
        return web.Response(text=page, content_type="text/html")

    async def start(app):
        nonlocal runner
        desk = web.Application()
        desk.router.add_get("/", show)
        runner = web.AppRunner(desk, shutdown_timeout=1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()     # any free port
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/"
        print(f"  QR code page on this computer: {url}\n")
        if open_browser:
            webbrowser.open(url)

    async def stop(app):
        if runner:
            await runner.cleanup()

    return start, stop


def main():
    global cam
    parser = argparse.ArgumentParser(description="Use your phone as this computer's mic and camera.")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open the QR code page in this computer's browser")
    parser.add_argument("--mic-always-on", action="store_true",
                        help="stream the phone mic all the time, not only while an app records")
    args = parser.parse_args()
    # Check before touching audio: a second copy would remove the first one's mic.
    if not port_free(PORT):
        sys.exit(f"Port {PORT} is already in use. Is Phone Mic Cam already running?")
    ensure_cert()
    module_id, previous = create_virtual_mic()
    t = threading.Thread(target=writer_thread, daemon=True)
    t.start()
    cam = setup_camera()

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/video", video_handler)
    sslctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    sslctx.load_cert_chain(CERT, KEY)

    phone_url = f"https://{lan_ip()}:{PORT}/?t={TOKEN}"
    start_desktop, stop_desktop = desktop_page_hooks(phone_url, not args.no_browser)
    start_watch, stop_watch = mic_watch_hooks(not args.mic_always_on)
    app.on_startup.append(start_desktop)
    app.on_startup.append(start_watch)
    app.on_shutdown.append(close_clients)
    app.on_cleanup.append(stop_watch)
    app.on_cleanup.append(stop_desktop)

    print(f"\n  '{MIC_LABEL}' is now your default microphone.")
    print("  Scan this QR code with your phone (same Wi-Fi):\n")
    print_qr(phone_url)
    print(f"\n  or open: {phone_url}")
    print("  Accept the certificate warning, then tap Start mic / Start camera.  Ctrl+C to quit.")
    try:
        web.run_app(app, host="0.0.0.0", port=PORT, ssl_context=sslctx, print=None,
                    shutdown_timeout=5)
    finally:
        stop_event.set()
        if cam:
            cam.close()
        if previous and previous != SOURCE:
            pactl("set-default-source", previous, check=False)
        pactl("unload-module", module_id, check=False)
        print("Stopped. Previous microphone restored.")


if __name__ == "__main__":
    main()
