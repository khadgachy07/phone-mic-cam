# Phone Mic Cam

Use your phone as your computer's **microphone** and **webcam** over Wi-Fi.
There's no app to install on the phone: scan a QR code, and the phone's
browser streams its mic and camera to the computer.

While it runs, the computer gets:

- a microphone called **Phone Mic**, set as the default input, so every app
  (browser, Zoom, Discord, OBS…) uses it;
- optionally, a webcam called **Phone Cam** that apps can choose like any
  other camera.

Press Ctrl+C to stop. Your previous default microphone is restored.

> **Platform support:** the computer side currently runs on **Linux** with
> PulseAudio or PipeWire. Windows and macOS aren't supported yet. Testing so
> far has used an iPhone with Safari. Android with Chrome should work, since it
> uses the same browser features, but hasn't been tested.

## How it works

```
 Phone browser                              Computer (phone_mic_cam.py)
 ─────────────                              ───────────────────────────
 mic  ──► 48 kHz PCM ───── wss /ws ──────►  PulseAudio pipe source "Phone Mic"
 cam  ──► JPEG frames ──── wss /video ───►  ffmpeg ──► v4l2loopback "Phone Cam"
```

The script runs a small HTTPS server on port **8443**. Browsers only allow
mic and camera access over HTTPS, so the script creates a self-signed
certificate on first run. The phone page records audio and video and sends
them back over WebSockets.

## Requirements

- Linux with PulseAudio, or PipeWire with `pipewire-pulse` (the `pactl`
  command must work)
- Python 3 and `openssl`
- The phone and the computer on the **same Wi-Fi network**
- For the camera only: `ffmpeg` and the `v4l2loopback` kernel module (see
  below)

## Installation

```bash
git clone <repository-url> phone-mic-cam
cd phone-mic-cam
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Optional: camera support

Install the virtual-webcam driver and `ffmpeg`, then load the driver:

```bash
sudo apt install ffmpeg v4l2loopback-dkms
sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="Phone Cam" exclusive_caps=1
```

- `card_label` must be exactly `Phone Cam`. That's how the script finds the
  device.
- `exclusive_caps=1` is needed for Chrome and other browsers to list the
  camera.
- Run the `modprobe` command again after every reboot.

**Secure Boot:** if `modprobe` fails with `Key was rejected by service`,
Secure Boot is rejecting the driver because its signing key isn't trusted
yet. Enroll the key that Ubuntu signed it with. You only need to do this once:

```bash
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
```

`mokutil` asks you to choose a one-time password. Reboot, and a blue **MOK
management** screen appears. Press a key quickly, because it times out after
about 10 seconds. Choose **Enroll MOK → Continue → Yes**, enter the password,
and reboot. Then run the `modprobe` command again.

Without camera support, the script still runs and works as a mic only.

## Usage

```bash
.venv/bin/python phone_mic_cam.py
```

1. A QR code appears in the terminal, and a page showing it opens in your
   browser. Pass `--no-browser` to skip opening the page.
2. Scan the QR code with your phone's camera and open the link.
3. Accept the certificate warning. In Safari, tap **Show Details → visit this
   website**. The warning is expected, because the certificate is
   self-signed.
4. Tap **Start mic** and/or **Start camera**, and allow access. **Flip**
   switches between the front and back cameras.
5. In your apps, choose **Phone Mic** and **Phone Cam** if they don't use
   them automatically.

Keep the page open and the phone's screen on. The page tries to stop the
screen from locking.

## Security

- The link in the QR code contains a **random pairing code that changes every
  time the script starts**. Requests without it, including the page and both
  audio and video streams, are refused. This stops other devices on the
  network from sending audio or video into your mic or camera. After a
  restart, scan the new QR code.
- The QR code page on the computer is served only to the computer itself
  (`127.0.0.1`).
- `key.pem` is the certificate's private key. It's generated for each install
  and is excluded by `.gitignore`. **Never commit it or share it.**

## Troubleshooting

| Problem | Fix |
|---|---|
| `Port 8443 is already in use` | Another copy is already running. Stop it first with Ctrl+C. |
| Phone can't open the page | Check that both devices are on the same Wi-Fi. If you use `ufw`, run `sudo ufw allow 8443/tcp`. |
| "Reconnecting… scan the new QR code" | The script was restarted, so the old link's pairing code is no longer valid. Scan the new QR code. |
| "Your PC has no virtual camera set up" | Set up the camera (see above), then restart the script. |
| `Camera disabled: no v4l2loopback device named 'Phone Cam'` | The driver was loaded with a different `card_label`. Reload it: `sudo modprobe -r v4l2loopback`, then run the `modprobe` command above. |
| Camera not listed in Chrome | Load `v4l2loopback` with `exclusive_caps=1`. |
| Mic is still "Phone Mic" after a crash | Choose your normal microphone in Sound settings. |

## Limitations

- Linux only for now.
- iOS stops the mic and camera when Safari goes into the background or the
  screen locks.
- Video is sent at 1280×720 landscape. A phone held upright appears with
  black bars at the sides.
- Audio is mono, 48 kHz, with about 40–150 ms of buffering.

## Contributing

Issues and pull requests are welcome. Windows and macOS support are the most
wanted features. Please test changes on a real phone, and describe what you
tested in the pull request.

## License

[MIT](LICENSE)
