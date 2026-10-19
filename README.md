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
- For the camera only: `ffmpeg` and the `v4l2loopback` webcam driver (see
  [Camera setup](#camera-setup-the-webcam-driver))

## Installation

```bash
git clone <repository-url> phone-mic-cam
cd phone-mic-cam
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Camera setup: the webcam driver

The camera feature needs **v4l2loopback**, a free Linux kernel driver that
creates a *virtual webcam*: a camera device that apps can select like a real
one but that has no hardware behind it. Phone Mic Cam receives video from
your phone and writes it into this virtual webcam with `ffmpeg`, so apps see
it as a camera called **Phone Cam**.

Linux has no built-in way for a program to create a webcam, so the driver is
needed. **You only need it for the camera. Without it, the script still works
as a microphone.**

### 1. Install the driver and ffmpeg

```bash
sudo apt install ffmpeg v4l2loopback-dkms linux-headers-$(uname -r)
```

The driver is compiled for your kernel during installation, which needs your
kernel's headers, so the command installs them too.

### 2. Load the driver

```bash
sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="Phone Cam" exclusive_caps=1
```

| Option | Meaning |
|---|---|
| `devices=1` | Create one virtual webcam. |
| `video_nr=10` | Use `/dev/video10`, which keeps it clear of real cameras. |
| `card_label="Phone Cam"` | The name apps show. It must be exactly this, because that's how the script finds the device. |
| `exclusive_caps=1` | Needed for Chrome and other browsers to list the camera. |

### 3. Check that it worked

```bash
cat /sys/class/video4linux/video10/name
```

This should print `Phone Cam`. When you start the script, it should report
"'Phone Cam' webcam available at /dev/video10".

### 4. (Optional) Load it automatically at boot

Without this, run step 2 again after every reboot.

```bash
echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf
echo 'options v4l2loopback devices=1 video_nr=10 card_label="Phone Cam" exclusive_caps=1' | sudo tee /etc/modprobe.d/v4l2loopback.conf
```

### If step 2 fails with "Key was rejected by service"

Secure Boot is rejecting the driver because its signing key isn't trusted
yet. Enroll the key that Ubuntu signed it with. You only need to do this once:

```bash
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
```

`mokutil` asks you to choose a one-time password. Reboot, and a blue **MOK
management** screen appears. Press a key quickly, because it times out after
about 10 seconds. Choose **Enroll MOK → Continue → Yes**, enter the password,
and reboot. Then run step 2 again.

### Removing the driver

```bash
sudo modprobe -r v4l2loopback
sudo rm -f /etc/modules-load.d/v4l2loopback.conf /etc/modprobe.d/v4l2loopback.conf
sudo apt remove v4l2loopback-dkms
```

### Other Linux distributions

The commands above are for Ubuntu and Debian. Fedora gets the driver from
RPM Fusion (`akmod-v4l2loopback`), and Arch has `v4l2loopback-dkms` plus
`linux-headers`. These haven't been tested yet.

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

### The mic turns on automatically

After you tap **Start mic** once, the phone shows **Ready**. It switches to
**Live** by itself whenever an app on the computer starts recording from
Phone Mic, for example a browser's mic button, a voice chat or a recorder.
It goes back to **Ready** about 3 seconds after the last app stops. While
**Ready**, the phone sends no audio.

The one tap is needed because phone browsers only allow mic access after the
user taps something on the page.

To stream the mic all the time instead, run with `--mic-always-on`. The
script also falls back to always-on if it can't watch PulseAudio.

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
| "Your PC has no virtual camera set up" | Follow [Camera setup](#camera-setup-the-webcam-driver), then restart the script. |
| `Camera disabled: no v4l2loopback device named 'Phone Cam'` | The driver isn't loaded, or was loaded with a different `card_label`. Check with step 3 of [Camera setup](#camera-setup-the-webcam-driver). If the name is wrong, unload it with `sudo modprobe -r v4l2loopback` and repeat step 2. |
| Camera not listed in Chrome | The driver was loaded without `exclusive_caps=1`. Unload it and repeat step 2. |
| Mic is still "Phone Mic" after a crash | Choose your normal microphone in Sound settings. |
| Phone goes **Live** with no call or recording | Something is still reading the mic, such as an open Sound settings input meter or a browser tab that kept mic access. Close it, or check which apps are using the mic with `pactl list source-outputs`. |
| The first word gets cut off | The phone needs a moment to switch on. Start talking about a second after you press the app's mic button, or use `--mic-always-on`. |

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
