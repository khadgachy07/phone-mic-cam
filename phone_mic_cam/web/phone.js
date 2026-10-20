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
// Audio still waiting to be sent after a Wi-Fi stall would only arrive late;
// past 150 ms of backlog new audio is dropped so the mic stays current.
const MIC_BACKLOG = 48000 * 2 * 0.15;
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
    if (ws && ws.readyState === 1 && ws.bufferedAmount < MIC_BACKLOG) ws.send(e.data);
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
    body: JSON.stringify({what, step: e.step || '', name: e.name, message: e.message,
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
// The PC always shows 1280x720 (it scales 480p up), so changing a setting
// never needs a new camera permission or breaks an app using Phone Cam.
const SIZES = {'720': [1280, 720], '480': [854, 480]};
const QUALITY = {high: 0.85, medium: 0.7, low: 0.5};
const settings = {size: '720', fps: '30', quality: 'medium'};
const camBtn = document.getElementById('cambtn'), flipBtn = document.getElementById('flip'),
      video = document.getElementById('preview'), camSt = document.getElementById('camstatus');
const canvas = document.createElement('canvas');
canvas.width = CAM_W; canvas.height = CAM_H;
const g = canvas.getContext('2d');
let camStream, camWs, camTimer, camRunning = false, encoding = false, facing = 'user';
// Safari can grant the camera but refuse to play the preview (frames are
// copied from it) outside a tap; then the next tap only starts the preview.
let previewBlocked = false;
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

// Both steps fail with the same NotAllowedError, so mark which one did.
async function openCamera() {
  if (camStream) camStream.getTracks().forEach(t => t.stop());
  let step = 'permission';
  try {
    camStream = await navigator.mediaDevices.getUserMedia({video: {
      facingMode: facing, width: {ideal: CAM_W}, height: {ideal: CAM_H}, frameRate: {ideal: CAM_FPS} }});
    step = 'preview';
    video.srcObject = camStream;
    video.classList.toggle('mirror', facing === 'user');
    video.style.display = 'block';        // Safari is stricter about playing a hidden video
    await video.play();
  } catch (e) { e.step = step; throw e; }
}

const previewRefused = e => e.name === 'NotAllowedError' && e.step === 'preview';

// Busy Wi-Fi: camera frames queue up on the phone and delay the mic, which
// shares the link. Lower the JPEG quality while frames wait to be sent, and
// raise it again once the queue has stayed empty for about a second.
const MIN_QUALITY = 0.3, BUSY = 64 * 1024;
function nextQuality(a, target, buffered) {
  if (buffered > BUSY) {
    if (a.wait > 0) return {q: Math.min(a.q, target), calm: 0, wait: a.wait - 1};
    return {q: Math.max(MIN_QUALITY, Math.min(a.q, target) - 0.1), calm: 0, wait: 5};
  }
  if (buffered > 0) return {q: Math.min(a.q, target), calm: 0, wait: a.wait};
  if (a.q < target && a.calm + 1 >= 30) return {q: Math.min(target, a.q + 0.1), calm: 0, wait: 0};
  return {q: Math.min(a.q, target), calm: a.calm + 1, wait: 0};
}
let adapt = {q: 1, calm: 0, wait: 0}, lastSent = 0, wifiBusy = false;
// Whether a camera frame arriving now should be sent at this fps; the slack
// absorbs jitter, so at 15 fps every other frame of a 30 fps camera goes.
const frameDue = (now, last, fps) => now - last >= 1000 / fps - 8;

function sendFrame() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || encoding || !camWs || camWs.readyState !== 1) return;
  const now = performance.now();
  if (!frameDue(now, lastSent, Number(settings.fps))) return;
  const target = QUALITY[settings.quality];
  adapt = nextQuality(adapt, target, camWs.bufferedAmount);
  if (wifiBusy !== adapt.q < target) {
    wifiBusy = adapt.q < target;
    camSay(wifiBusy ? 'Camera live (lower quality: Wi-Fi is busy)' : 'Camera live: your PC is seeing you');
  }
  if (camWs.bufferedAmount > 256 * 1024) return;      // Wi-Fi is far behind: skip a frame
  const [cw, ch] = SIZES[settings.size];
  if (canvas.width !== cw) { canvas.width = cw; canvas.height = ch; }
  const s = Math.min(cw / vw, ch / vh), w = vw * s, h = vh * s;
  g.fillStyle = '#000'; g.fillRect(0, 0, cw, ch);
  g.drawImage(video, (cw - w) / 2, (ch - h) / 2, w, h);
  encoding = true; lastSent = now;
  canvas.toBlob(b => { encoding = false; if (b && camWs && camWs.readyState === 1) camWs.send(b); },
                'image/jpeg', adapt.q);
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
  if (previewBlocked) { previewBlocked = false; await video.play(); }   // inside the tap
  else await openCamera();
  camRunning = true; camConnect(); keepAwake();
  adapt = {q: 1, calm: 0, wait: 0}; wifiBusy = false;
  startFrameLoop();
  camBtn.textContent = 'Stop camera'; camBtn.classList.add('on');
}

function stopCam() {
  camRunning = false; previewBlocked = false;
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
    report('camera', e);
    if (previewRefused(e)) {
      previewBlocked = true;
      camSay('Safari paused the preview. Tap Start camera once more.');
      return;
    }
    stopCam(); camSay(explain('camera', e));
    camBtn.textContent = 'Try camera again';
  }
};
flipBtn.onclick = async () => {
  facing = facing === 'user' ? 'environment' : 'user';
  if (previewBlocked) stopCam();          // the held stream faces the old way; Start reopens
  if (camRunning) try { await openCamera(); startFrameLoop(); }
  catch (e) {
    report('camera', e);
    if (previewRefused(e)) { startFrameLoop(); camSay('Safari paused the preview. Tap the preview to resume.'); }
    else camSay(explain('camera', e));
  }
};
// A tap on a paused preview resumes it; the frame loop picks up the new frames.
video.onclick = () => {
  if (camRunning && video.paused) video.play().catch(e => report('camera', e));
};
// However the preview restarts (that tap, or autoplay), clear the paused message.
video.addEventListener('playing', () => {
  if (camRunning && camWs && camWs.readyState === 1) camSay('Camera live: your PC is seeing you');
});

// ------------------------------------------------------------ settings
// Saved on the phone, so they're the same next time. They apply at once.
try { Object.assign(settings, JSON.parse(localStorage.getItem('camSettings')) || {}); } catch (e) {}
for (const name of Object.keys(settings)) {
  const select = document.getElementById(name);
  if (![...select.options].some(o => o.value === settings[name])) settings[name] = select.value;
  select.value = settings[name];
  select.onchange = () => {
    settings[name] = select.value;
    adapt = {q: 1, calm: 0, wait: 0};      // start at the new choice, not at a lowered quality
    try { localStorage.setItem('camSettings', JSON.stringify(settings)); } catch (e) {}
  };
}

// ------------------------------------------------------------ dim screen
// A black screen while streaming; the page must stay open and awake.
const dimmer = document.getElementById('dimmer');
document.getElementById('dim').onclick = () => dimmer.style.display = 'flex';
dimmer.onclick = () => dimmer.style.display = 'none';

// ------------------------------------------------------------ resume
// iOS ends the mic and camera when Safari goes to the background or the phone
// locks. Coming back, one tap restarts whatever ended: the browser only grants
// the mic and camera from a tap, and both requests start inside this one.
const resumeBox = document.getElementById('resume');
function endedByPhone() {
  const ended = s => !!s && s.getTracks().some(t => t.readyState === 'ended');
  return {mic: running && (ended(stream) || (!!ctx && ctx.state === 'closed')),
          cam: camRunning && ended(camStream)};
}
function checkResume() {
  const e = endedByPhone();
  resumeBox.style.display = e.mic || e.cam ? 'flex' : 'none';
}
document.getElementById('resumebtn').onclick = () => {
  const e = endedByPhone();
  resumeBox.style.display = 'none';
  if (e.mic) { stop(); btn.onclick(); }
  if (e.cam) { stopCam(); camBtn.onclick(); }
};
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') setTimeout(checkResume, 500);   // let tracks settle
});
