#!/usr/bin/env python3
"""
Phone Mic Cam: use your phone as this Linux computer's microphone and webcam,
with no app on the phone.

Installed from the .deb package, start it from the app grid ("Phone Mic Cam")
or run `phone-mic-cam`. From source: pip install -r requirements.txt, then
python3 phone_mic_cam.py.

It opens a control page in this computer's browser with a QR code. Scan it
with the phone on the same Wi-Fi, accept the certificate warning, and tap
"Start mic" and/or "Start camera". The link contains a secret code that
changes every run.

While it runs, the computer has a microphone called "Phone Mic" set as the
default input, so every app (browser, YouTube, Claude, Discord...) uses it,
and, when the v4l2loopback driver is loaded, a webcam called "Phone Cam".
Stop it with the control page's Stop button or Ctrl+C; your previous
microphone is restored.

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
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

try:
    from aiohttp import web, WSCloseCode, WSMsgType
    import qrcode
    import qrcode.image.svg
except ImportError as e:
    sys.exit(f"Missing Python package '{e.name}'. Install with:  "
             "sudo apt install python3-aiohttp python3-qrcode  (or pip install -r requirements.txt)")

__version__ = "0.2.0"

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
# Per-user files: the installed script lives in read-only /usr/share.
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "phone-mic-cam")
CERT = os.path.join(DATA_DIR, "cert.pem")
KEY = os.path.join(DATA_DIR, "key.pem")
# URL of the running copy's control page, so a second launch can reopen it.
CONTROL_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
                            "phone-mic-cam.url")
CAM_LABEL = "Phone Cam"           # must match v4l2loopback card_label
MOK_KEY = "/var/lib/shim-signed/mok/MOK.der"   # Ubuntu's DKMS module-signing key
CAM_FPS = 30
MAX_FRAME = 1 << 21               # 2 MB per JPEG frame
TOKEN = secrets.token_urlsafe(16) # pairing code; only the QR link carries it

buf = bytearray()
buf_lock = threading.Lock()
stop_event = threading.Event()
cam = None                        # VirtualCam, or None when unavailable
camera_problem = None             # why the camera is unavailable, with the fix
clients = set()                   # open phone WebSockets, closed on shutdown
mic_clients = set()               # the subset streaming the mic
cam_clients = set()               # the subset streaming the camera
mic_wanted = True                 # an app is recording; stays True if we can't tell
mic_always_on = False             # --mic-always-on
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


def secure_boot_blocks_driver():
    """True when Secure Boot is on and the key DKMS signs drivers with isn't enrolled."""
    if not (shutil.which("mokutil") and os.path.exists(MOK_KEY)):
        return False
    state = subprocess.run(["mokutil", "--sb-state"], capture_output=True, text=True)
    if "SecureBoot enabled" not in state.stdout:
        return False
    test = subprocess.run(["mokutil", "--test-key", MOK_KEY], capture_output=True, text=True)
    return "is not enrolled" in test.stdout + test.stderr


def diagnose_camera():
    """Explain why there's no Phone Cam device, and how to fix it."""
    if not shutil.which("ffmpeg"):
        return "ffmpeg isn't installed. Install it with:  sudo apt install ffmpeg"
    if os.path.isdir("/sys/module/v4l2loopback"):
        return (f"The webcam driver is loaded, but not as '{CAM_LABEL}' (another app may "
                "have loaded it). Restart your computer to apply Phone Mic Cam's settings.")
    if secure_boot_blocks_driver():
        return ("Secure Boot is blocking the webcam driver. Run  "
                f"sudo mokutil --import {MOK_KEY}  and choose a one-time password, then "
                "restart. On the blue screen choose Enroll MOK, Continue, Yes, and enter "
                "that password.")
    return ("The webcam driver isn't loaded. Restart your computer, or run:  "
            "sudo modprobe v4l2loopback devices=1 video_nr=10 "
            f"card_label=\"{CAM_LABEL}\" exclusive_caps=1")


def setup_camera():
    global camera_problem
    device = find_loopback_device() if shutil.which("ffmpeg") else None
    if device is None:
        camera_problem = diagnose_camera()
        print(f"  Camera disabled: {camera_problem}")
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


CONTROL_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone Mic Cam</title>
<style>
  body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:20px;padding:24px 16px;box-sizing:border-box;
       font-family:system-ui,sans-serif;background:#111;color:#eee}
  h1{margin:0;font-size:26px}
  #qr{background:#fff;padding:16px;border-radius:12px;width:260px;height:260px}
  #qr svg{width:100%;height:100%}
  ol{font-size:16px;line-height:1.6;max-width:440px;margin:0;padding-left:22px}
  #status{display:grid;grid-template-columns:auto 1fr;gap:8px 14px;font-size:16px;
          background:#1c1c1c;border-radius:12px;padding:14px 18px;min-width:280px}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#555;
       margin-right:8px}
  .on .dot{background:#2c8}  .live .dot{background:#e44}
  #camfix{max-width:440px;font-size:14px;line-height:1.5;background:#3a2a10;color:#fc6;
          border-radius:10px;padding:12px 14px;display:none;word-break:break-word}
  button{border:none;border-radius:12px;padding:12px 28px;font-size:16px;font-weight:600;
         background:#c33;color:#fff;cursor:pointer}
  button:disabled{background:#444;cursor:default}
  code{font-size:12px;opacity:.6;word-break:break-all;max-width:440px;text-align:center}
</style></head><body>
<h1>Phone Mic Cam</h1>
<div id="qr">{{QR}}</div>
<ol>
  <li>Connect your phone to the same Wi-Fi as this computer.</li>
  <li>Point the phone's camera at the QR code and open the link.</li>
  <li>Accept the certificate warning (Show Details &rarr; visit this website).</li>
  <li>Tap <b>Start mic</b> and/or <b>Start camera</b>, and allow access.</li>
</ol>
<div id="status">
  <span>Microphone</span><span id="mic"><span class="dot"></span>…</span>
  <span>Camera</span><span id="cam"><span class="dot"></span>…</span>
</div>
<div id="camfix"></div>
<button id="stop">Stop Phone Mic Cam</button>
<code>{{URL}}</code>
<script>
const TOKEN = {{TOKEN}};
const show = (id, text, cls) => {
  const el = document.getElementById(id);
  el.className = cls || '';
  el.innerHTML = '<span class="dot"></span>';
  el.append(text);
};
let stopped = false;
async function refresh() {
  if (stopped) return;
  let s;
  try {
    s = await (await fetch('/status?t=' + TOKEN, {cache: 'no-store'})).json();
  } catch (e) {
    show('mic', 'Phone Mic Cam is not running'); show('cam', '—');
    document.getElementById('stop').disabled = true;
    return;
  }
  if (!s.mic_connected) show('mic', 'Waiting for your phone');
  else if (s.mic_always_on) show('mic', 'Live (always on)', 'live');
  else if (s.mic_live) show('mic', 'Live: an app is using it', 'live');
  else show('mic', 'Ready: turns on when an app uses it', 'on');
  if (s.camera_problem) show('cam', 'Not available');
  else if (s.cam_connected) show('cam', 'Live as "Phone Cam"', 'live');
  else show('cam', 'Ready: tap Start camera on your phone', 'on');
  const fix = document.getElementById('camfix');
  fix.textContent = s.camera_problem || '';
  fix.style.display = s.camera_problem ? 'block' : 'none';
}
document.getElementById('stop').onclick = async () => {
  const btn = document.getElementById('stop');
  btn.disabled = true; btn.textContent = 'Stopping…';
  try { await fetch('/stop?t=' + TOKEN, {method: 'POST'}); } catch (e) {}
  stopped = true;
  btn.textContent = 'Stopped';
  show('mic', 'Stopped. Your previous microphone is back.'); show('cam', 'Stopped');
  document.getElementById('camfix').style.display = 'none';
};
refresh(); setInterval(refresh, 1000);
</script>
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
    cam_clients.add(ws)
    print(f"Phone camera connected from {request.remote}")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and msg.data[:2] == b"\xff\xd8":   # JPEG
                cam.push(msg.data)
    finally:
        clients.discard(ws)
        cam_clients.discard(ws)
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
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
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


def control_page_hooks(phone_url, open_browser):
    """Serve the control page (QR code, status, Stop) to this computer's browser.

    Localhost only, plain HTTP. Every request needs the pairing token and the
    exact 127.0.0.1 Host, so other websites open in the browser can't read the
    page or press Stop (CSRF, DNS rebinding).
    """
    runner = None
    host = None                      # "127.0.0.1:<port>", known once listening
    svg = qrcode.make(phone_url, image_factory=qrcode.image.svg.SvgPathImage)
    page = (CONTROL_PAGE.replace("{{QR}}", svg.to_string(encoding="unicode"))
            .replace("{{URL}}", html.escape(phone_url))
            .replace("{{TOKEN}}", json.dumps(TOKEN)))

    @web.middleware
    async def local_only(request, handler):
        if request.host != host or not authorized(request):
            raise web.HTTPForbidden()
        return await handler(request)

    async def show(request):
        return web.Response(text=page, content_type="text/html")

    async def status(request):
        return web.json_response({
            "mic_connected": bool(mic_clients),
            "mic_live": mic_wanted,
            "mic_always_on": mic_always_on,
            "cam_connected": bool(cam_clients),
            "camera_problem": camera_problem,
        })

    async def stop_app(request):
        print("Stop pressed on the control page")
        # Same path as Ctrl+C, after this response has been sent.
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGINT)
        return web.json_response({"stopping": True})

    async def start(app):
        nonlocal runner, host
        desk = web.Application(middlewares=[local_only])
        desk.router.add_get("/", show)
        desk.router.add_get("/status", status)
        desk.router.add_post("/stop", stop_app)
        runner = web.AppRunner(desk, shutdown_timeout=1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()     # any free port
        host = f"127.0.0.1:{runner.addresses[0][1]}"
        url = f"http://{host}/?t={TOKEN}"
        fd = os.open(CONTROL_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(url)
        print(f"  Control page on this computer: {url}\n")
        if open_browser:
            webbrowser.open(url)

    async def stop(app):
        try:
            os.remove(CONTROL_FILE)
        except FileNotFoundError:
            pass
        if runner:
            await runner.cleanup()

    return start, stop


def running_copy_url():
    """Control page URL of an already running Phone Mic Cam, or None."""
    try:
        with open(CONTROL_FILE) as f:
            url = f.read().strip()
        port = int(url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except (OSError, ValueError, IndexError):
        return None                  # no file, or left over from a crash
    return url


def fatal(message):
    """Exit with an error the user can see, even when started from the app grid."""
    print(message, file=sys.stderr)
    if not sys.stderr.isatty() and shutil.which("notify-send"):
        subprocess.run(["notify-send", "--icon=phone-mic-cam", "Phone Mic Cam", message],
                       check=False)
    sys.exit(1)


def main():
    global cam, mic_always_on
    parser = argparse.ArgumentParser(description="Use your phone as this computer's mic and camera.")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open the control page in this computer's browser")
    parser.add_argument("--mic-always-on", action="store_true",
                        help="stream the phone mic all the time, not only while an app records")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()
    mic_always_on = args.mic_always_on
    # Check before touching audio: a second copy would remove the first one's mic.
    if not port_free(PORT):
        url = running_copy_url()
        if url is None:
            fatal(f"Port {PORT} is already in use by another program, so Phone Mic Cam can't start.")
        print(f"Phone Mic Cam is already running. Control page: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    for tool in ("pactl", "openssl"):
        if not shutil.which(tool):
            fatal(f"'{tool}' is missing. Install it with:  sudo apt install "
                  f"{'pulseaudio-utils' if tool == 'pactl' else 'openssl'}")
    try:
        ensure_cert()
        module_id, previous = create_virtual_mic()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as e:
        fatal(f"Couldn't set up the microphone: {e}")
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
    start_desktop, stop_desktop = control_page_hooks(phone_url, not args.no_browser)
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
    print("  Accept the certificate warning, then tap Start mic / Start camera.")
    print("  Stop with the control page's Stop button or Ctrl+C.")
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
