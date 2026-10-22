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
// Capture at 1080p and scale down to what's sent: phone cameras give more
// detail at 1080p than at 720p, so the 720p picture comes out sharper.
const CAPTURE_W = 1920, CAPTURE_H = 1080;
// The PC always shows 1920x1080 (it scales smaller sizes up), so changing a
// setting never needs a new camera permission or breaks an app using Phone Cam.
const SIZES = {'1080': [1920, 1080], '720': [1280, 720], '480': [854, 480]};
const QUALITY = {high: 0.85, medium: 0.7, low: 0.5};
const settings = {size: '1080', fps: '30', quality: 'high'};
const camBtn = document.getElementById('cambtn'), flipBtn = document.getElementById('flip'),
      video = document.getElementById('preview'), camSt = document.getElementById('camstatus');
const canvas = document.createElement('canvas');
canvas.width = CAM_W; canvas.height = CAM_H;
const g = canvas.getContext('2d');
let camStream, camWs, camTimer, camRunning = false, encoding = false, facing = 'user';
let codec = 'jpeg', can1080 = false;      // 'h264' where the browser can encode it
// Safari can grant the camera but refuse to play the preview (frames are
// copied from it) outside a tap; then the next tap only starts the preview.
let previewBlocked = false;
let loopId = 0;                   // bumped to end the current frame loop
const camSay = t => camSt.textContent = t;

let camReconnect;
function camConnect() {
  clearTimeout(camReconnect);
  const sock = camWs = new WebSocket(`wss://${location.host}/video?t=${TOKEN}&codec=${codec}`);
  sock.onopen = () => { if (sock === camWs) { needKey = true; camSay(liveStatus()); } };
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
      facingMode: facing, width: {ideal: CAPTURE_W}, height: {ideal: CAPTURE_H}, frameRate: {ideal: CAM_FPS} }});
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
let adapt = {q: 1, calm: 0, wait: 0}, lastSent = 0, wifiBusy = false, upright = false;
// Phone Cam is landscape: an upright phone's picture fills only about a third
// of it, between black bars, so the page asks for the phone to be turned.
const liveStatus = () =>
  upright ? 'Camera live. Turn your phone sideways for a full, sharper picture.'
  : wifiBusy ? 'Camera live (lower quality: Wi-Fi is busy)'
  : 'Camera live: your PC is seeing you';
// Whether a camera frame arriving now should be sent at this fps; the slack
// absorbs jitter, so at 15 fps every other frame of a 30 fps camera goes.
const frameDue = (now, last, fps) => now - last >= 1000 / fps - 8;

// ------------------------------------------------------------ H.264
// The phone's hardware H.264 encoder (WebCodecs) sends only what changed
// between frames: a much better picture than a JPEG per frame for the same
// data, which makes 1080p practical. Browsers without it use the JPEG path.
const BITRATE = {'1080': 6e6, '720': 3.5e6, '480': 1.5e6};   // bits/s at High quality
const KEY_EVERY = 2000;                    // ms; a decoder can only join at a keyframe
const avcConfig = (w, h, bitrate, fps) => ({
  codec: w > 1280 ? 'avc1.42e028' : 'avc1.42e01f',      // Constrained Baseline 4.0 / 3.1
  width: w, height: h, bitrate, framerate: fps, latencyMode: 'realtime',
  avc: {format: 'avc'},                    // the format every browser supports
});
let enc = null, encCfg = null, avcc = null, needKey = true, lastKey = 0;

// Returns [codec, whether 1080p can be encoded].
async function pickCodec() {
  if (!('VideoEncoder' in window) || !('VideoFrame' in window)) return ['jpeg', false];
  const ok = async (w, h) => {
    try { return (await VideoEncoder.isConfigSupported(avcConfig(w, h, BITRATE['720'], 30))).supported; }
    catch (e) { return false; }
  };
  if (!await ok(1280, 720)) return ['jpeg', false];
  return ['h264', await ok(1920, 1080)];
}

// 1080p only with H.264: as JPEG it would need several times the data.
const sentSize = () =>
  settings.size === '1080' && !(codec === 'h264' && can1080) ? '720' : settings.size;

// Once the camera has started, make the size menu match what is sent: where
// 1080p can't be used it's shown as unavailable, and the size in use is
// selected. The saved choice stays 1080p, for a phone that can use it later.
function showSizes() {
  const select = document.getElementById('size');
  const option = select.querySelector('option[value="1080"]');
  const ok = codec === 'h264' && can1080;
  option.disabled = !ok;
  option.textContent = ok ? '1080p' : '1080p (not supported)';
  select.value = sentSize();
}

// The encoder's 'avc' output: NAL units with length prefixes, and the
// SPS/PPS parameter sets in a separate avcC record. ffmpeg reads the plain
// Annex B stream: start codes, with the parameter sets before each keyframe
// so the PC can start decoding at any keyframe.
function parseAvcC(desc) {
  const b = ArrayBuffer.isView(desc) ? new Uint8Array(desc.buffer, desc.byteOffset, desc.byteLength)
                                     : new Uint8Array(desc);
  const lenSize = (b[4] & 3) + 1, sets = [];
  let i = 5;
  for (let n = b[i++] & 31; n > 0; n--) { const l = b[i] << 8 | b[i + 1]; sets.push(b.slice(i + 2, i + 2 + l)); i += 2 + l; }
  for (let n = b[i++]; n > 0; n--) { const l = b[i] << 8 | b[i + 1]; sets.push(b.slice(i + 2, i + 2 + l)); i += 2 + l; }
  return {lenSize, sets};
}

// One WebSocket message: a byte saying keyframe (1) or not (0), then Annex B.
// The avcC's parameter sets go in front of a keyframe only when it doesn't
// carry its own: Firefox's avcC is malformed (it repeats each set's first
// byte), but its keyframes include correct sets.
function toAnnexB(data, avc, key) {
  const START = [0, 0, 0, 1], nals = [];
  for (let i = 0; i + avc.lenSize <= data.length;) {
    let len = 0;
    for (let k = 0; k < avc.lenSize; k++) len = len * 256 + data[i + k];
    i += avc.lenSize;
    nals.push(data.subarray(i, i + len));
    i += len;
  }
  const parts = [[key ? 1 : 0]];
  if (key && !nals.some(n => (n[0] & 31) === 7)) for (const set of avc.sets) parts.push(START, set);
  for (const n of nals) parts.push(START, n);
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

function closeEncoder() {
  if (enc && enc.state !== 'closed') enc.close();
  enc = encCfg = avcc = null;
}

// The encoder failed: carry on with JPEG on a new connection.
function useJpeg(e) {
  report('camera', {name: 'EncoderError', message: String(e && e.message || e), step: 'h264'});
  closeEncoder(); codec = 'jpeg';
  showSizes();
  if (camRunning) { const old = camWs; camConnect(); if (old) old.close(); }
}

function encodeFrame(now, w, h) {
  if (!enc) enc = new VideoEncoder({
    output: (chunk, meta) => {
      if (meta && meta.decoderConfig && meta.decoderConfig.description)
        avcc = parseAvcC(meta.decoderConfig.description);
      if (!avcc || !camWs || camWs.readyState !== 1) return;
      const data = new Uint8Array(chunk.byteLength);
      chunk.copyTo(data);
      camWs.send(toAnnexB(data, avcc, chunk.type === 'key'));
    },
    error: useJpeg,
  });
  const bitrate = Math.round(BITRATE[sentSize()] * adapt.q / QUALITY.high);
  const fps = Number(settings.fps);
  if (!encCfg || encCfg.width !== w || encCfg.bitrate !== bitrate || encCfg.framerate !== fps) {
    encCfg = avcConfig(w, h, bitrate, fps);
    enc.configure(encCfg);
    needKey = true;
  }
  if (enc.encodeQueueSize > 1) return;     // the encoder is behind: skip this frame
  const frame = new VideoFrame(canvas, {timestamp: Math.round(now * 1000)});
  const key = needKey || now - lastKey >= KEY_EVERY;
  enc.encode(frame, {keyFrame: key});
  frame.close();
  if (key) { needKey = false; lastKey = now; }
}

function sendFrame() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || encoding || !camWs || camWs.readyState !== 1) return;
  const now = performance.now();
  if (!frameDue(now, lastSent, Number(settings.fps))) return;
  const target = QUALITY[settings.quality];
  adapt = nextQuality(adapt, target, camWs.bufferedAmount);
  if (wifiBusy !== adapt.q < target || upright !== vh > vw) {
    wifiBusy = adapt.q < target; upright = vh > vw;
    camSay(liveStatus());
  }
  if (camWs.bufferedAmount > 256 * 1024) return;      // Wi-Fi is far behind: skip a frame
  const [cw, ch] = SIZES[sentSize()];
  if (canvas.width !== cw) { canvas.width = cw; canvas.height = ch; }
  g.imageSmoothingQuality = 'high';       // reset by a resize; sharper downscaling from 1080p
  const s = Math.min(cw / vw, ch / vh), w = vw * s, h = vh * s;
  g.fillStyle = '#000'; g.fillRect(0, 0, cw, ch);
  g.drawImage(video, (cw - w) / 2, (ch - h) / 2, w, h);
  lastSent = now;
  if (codec === 'h264') return encodeFrame(now, cw, ch);
  encoding = true;
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
  [codec, can1080] = await pickCodec();
  showSizes();
  camRunning = true; camConnect(); keepAwake();
  adapt = {q: 1, calm: 0, wait: 0}; wifiBusy = false; upright = false;
  startFrameLoop();
  camBtn.textContent = 'Stop camera'; camBtn.classList.add('on');
}

function stopCam() {
  camRunning = false; previewBlocked = false;
  closeEncoder();
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
  if (camRunning && camWs && camWs.readyState === 1) camSay(liveStatus());
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
