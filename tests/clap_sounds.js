// Synthetic sounds for the phone page's clap and snap detector test: 48 kHz,
// as Int16 chunks of 480 samples (10 ms), like the phone's mic.
const SR = 48000;
let seed = 7; const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648 * 2 - 1;
function biquadBP(f0, q) {           // band-pass filter (RBJ cookbook)
  const w = 2 * Math.PI * f0 / SR, a = Math.sin(w) / (2 * q), c = Math.cos(w), a0 = 1 + a;
  const b0 = a / a0, b2 = -a / a0, a1 = -2 * c / a0, a2 = (1 - a) / a0;
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  return x => { const y = b0 * x + b2 * x2 - a1 * y1 - a2 * y2; x2 = x1; x1 = x; y2 = y1; y1 = y; return y; };
}
const buf = secs => new Float64Array(Math.round(secs * SR));
const add = (b, at, sig) => { const s = Math.round(at * SR); for (let i = 0; i < sig.length && s + i < b.length; i++) b[s + i] += sig[i]; };
function burst(f0, q, amp, tau, len, attack = 0.0005, reverb = 0) {
  const n = Math.round(len * SR), out = new Float64Array(n), bp = biquadBP(f0, q), bp2 = biquadBP(f0, q);
  for (let i = 0; i < n; i++) {
    const t = i / SR, env = Math.min(1, t / attack) * Math.exp(-t / tau);
    out[i] = amp * 3 * bp(rnd()) * env + reverb * amp * bp2(rnd()) * Math.exp(-t / 0.15);
  }
  return out;
}
const clap = (amp = 0.5) => burst(1800, 0.7, amp, 0.008, 0.4, 0.0005, 0.06);
const snap = (amp = 0.4) => burst(3200, 1.5, amp, 0.003, 0.3, 0.0003, 0.04);
const thud = (amp = 0.25) => burst(90, 1, amp, 0.06, 0.5, 0.003);
function tone(freqs, amp, len, attack = 0.02) {
  const n = Math.round(len * SR), out = new Float64Array(n);
  for (let i = 0; i < n; i++) { const t = i / SR; let v = 0;
    for (const f of freqs) v += Math.sin(2 * Math.PI * f * t) / freqs.length;
    out[i] = amp * v * Math.min(1, t / attack) * Math.min(1, (len - t) / 0.05); }
  return out;
}
// Speech-like: a voiced vowel (150 Hz with formant-weighted harmonics) after a
// short "t" burst, syllables 4 per second.
function syllable(amp = 0.3) {
  const len = 0.22, n = Math.round(len * SR), out = new Float64Array(n);
  const tb = burst(4000, 1, amp * 0.5, 0.004, 0.02);
  for (let i = 0; i < tb.length; i++) out[i] += tb[i];
  const vot = Math.round(0.03 * SR);
  for (let i = vot; i < n; i++) { const t = (i - vot) / SR; let v = 0;
    for (let k = 1; k < 25; k++) { const f = 150 * k; const w = Math.exp(-((f - 700) ** 2) / 2e5) + 0.5 * Math.exp(-((f - 1200) ** 2) / 3e5);
      v += w * Math.sin(2 * Math.PI * f * t); }
    out[i] += amp * 0.4 * v * Math.min(1, t / 0.04) * Math.min(1, (len - (i / SR)) / 0.05); }
  return out;
}
const noise = (amp, len) => { const n = Math.round(len * SR), o = new Float64Array(n); for (let i = 0; i < n; i++) o[i] = amp * rnd(); return o; };
function chunks(b) {
  const out = [];
  for (let i = 0; i + 480 <= b.length; i += 480) {
    const c = new Int16Array(480);
    for (let k = 0; k < 480; k++) c[k] = Math.max(-32768, Math.min(32767, Math.round(b[i + k] * 32767)));
    out.push(c);
  }
  return out;
}
module.exports = {SR, buf, add, clap, snap, thud, tone, syllable, noise, burst, chunks};
