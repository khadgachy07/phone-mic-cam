const TOKEN = encodeURIComponent(new URLSearchParams(location.search).get('t') || '');
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
  const heard = s.last_sound;
  document.getElementById('sound').textContent =
    !s.mic_connected ? 'Start the phone mic to detect claps and snaps'
    : heard ? `${heard.kind === 'snap' ? 'Snap' : 'Clap'} heard ${Math.round(heard.seconds_ago)} s ago`
    : 'Listening for claps and snaps';
  const fix = document.getElementById('camfix');
  fix.textContent = s.camera_problem || '';
  fix.style.display = s.camera_problem ? 'block' : 'none';
  const t = s.stats;
  document.getElementById('micstats').textContent =
    s.mic_connected && t.mic_kb_per_sec ? `${t.mic_kb_per_sec} KB/s · ${t.mic_buffer_ms} ms lag` : '';
  document.getElementById('camstats').textContent = s.cam_connected
    ? `${t.cam_codec === 'h264' ? 'H.264' : 'JPEG'} · ${t.cam_fps} fps · ` +
      `${t.cam_kb_per_sec.toLocaleString()} KB/s · ${t.cam_skipped} frames skipped`
    : '';
}
document.getElementById('stop').onclick = async () => {
  const btn = document.getElementById('stop');
  btn.disabled = true; btn.textContent = 'Stopping…';
  try { await fetch('/stop?t=' + TOKEN, {method: 'POST'}); } catch (e) {}
  stopped = true;
  btn.textContent = 'Stopped';
  show('mic', 'Stopped. Your previous microphone is back.'); show('cam', 'Stopped');
  document.getElementById('micstats').textContent = '';
  document.getElementById('camstats').textContent = '';
  document.getElementById('camfix').style.display = 'none';
};
refresh(); setInterval(refresh, 1000);
