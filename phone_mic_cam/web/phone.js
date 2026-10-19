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

// Only the current socket may change state or reconnect; a pending reconnect
// is cancelled by Stop and Start, so a quick Stop+Start can't open two.
let reconnect;
function connect() {
  clearTimeout(reconnect);
  const sock = ws = new WebSocket(`wss://${location.host}/ws?t=${TOKEN}`);
  sock.binaryType = 'arraybuffer';
  sock.onmessage = e => {
    if (sock === ws && typeof e.data === 'string') setLive(e.data === 'on');
  };
  sock.onclose = () => {
    if (!running || sock !== ws) return;
    setLive(false); say(RECONNECTING); reconnect = setTimeout(connect, 1000);
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
  clearTimeout(reconnect);
  if (ws) ws.close();
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (ctx) ctx.close();
  if (wakeLock) wakeLock.release();
  ctx = stream = ws = wakeLock = null;
  lvl.style.width = '0';
  btn.textContent = 'Start mic'; btn.classList.remove('on'); say('Stopped');
}

// When the browser refuses the mic or camera, tell the PC so its log shows
// why and in what state, and tell the user what to do.
function report(what, e) {
  fetch(`/report?t=${TOKEN}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({what, name: e.name, message: e.message,
                          mic: running ? (live ? 'Live' : 'Ready') : 'off',
                          page: document.visibilityState}),
  }).catch(() => {});
}

function explain(what, e) {
  if (e.name === 'NotAllowedError')
    return `Safari refused the ${what}. Tap the button to try again. If it keeps happening: ` +
           `tap aA in the address bar → Website Settings → ` +
           `${what === 'camera' ? 'Camera' : 'Microphone'} → Allow.`;
  if (e.name === 'NotReadableError')
    return `The ${what} is busy, probably in another app (FaceTime, Camera…). ` +
           `Close it and try again.`;
  return `${what === 'camera' ? 'Camera' : 'Microphone'} error: ${e.message}`;
}

btn.onclick = async () => {
  if (running) return stop();
  try { await start(); }
  catch (e) { report('microphone', e); stop(); say(explain('microphone', e)); }
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
let loopId = 0;                   // bumped to end the current frame loop
const camSay = t => camSt.textContent = t;

let camReconnect;
function camConnect() {
  clearTimeout(camReconnect);
  const sock = camWs = new WebSocket(`wss://${location.host}/video?t=${TOKEN}`);
  sock.onopen = () => { if (sock === camWs) camSay('Camera live: your PC is seeing you'); };
  sock.onclose = e => {
    if (sock !== camWs) return;
    if (e.code === NO_CAMERA) {
      stopCam(); camSay('Your PC has no virtual camera set up. The control page on your PC shows how to fix it.');
    }
    else if (camRunning) { camSay('Camera: ' + RECONNECTING); camReconnect = setTimeout(camConnect, 1000); }
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

// Send each new camera frame once. requestVideoFrameCallback fires when the
// video element shows a new frame; a fixed timer could send one frame twice
// or skip one. Fall back to the timer where it's missing.
function startFrameLoop() {
  const id = ++loopId;
  clearInterval(camTimer);
  if (!('requestVideoFrameCallback' in HTMLVideoElement.prototype)) {
    camTimer = setInterval(sendFrame, 1000 / CAM_FPS);
    return;
  }
  const step = () => {
    if (!camRunning || id !== loopId) return;
    sendFrame();
    video.requestVideoFrameCallback(step);
  };
  video.requestVideoFrameCallback(step);
}

async function startCam() {
  await openCamera();
  video.style.display = 'block';
  camRunning = true; camConnect(); keepAwake();
  startFrameLoop();
  camBtn.textContent = 'Stop camera'; camBtn.classList.add('on');
}

function stopCam() {
  camRunning = false;
  loopId++;
  clearTimeout(camReconnect);
  clearInterval(camTimer);
  if (camWs) camWs.close();
  if (camStream) camStream.getTracks().forEach(t => t.stop());
  camStream = camWs = camTimer = null;
  video.srcObject = null; video.style.display = 'none';
  camBtn.textContent = 'Start camera'; camBtn.classList.remove('on'); camSay('Camera stopped');
}

camBtn.onclick = async () => {
  if (camRunning) return stopCam();
  try { await startCam(); }
  catch (e) {
    report('camera', e); stopCam(); camSay(explain('camera', e));
    camBtn.textContent = 'Try camera again';
  }
};
flipBtn.onclick = async () => {
  facing = facing === 'user' ? 'environment' : 'user';
  if (camRunning) try { await openCamera(); startFrameLoop(); }
  catch (e) { report('camera', e); camSay(explain('camera', e)); }
};
