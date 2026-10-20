# Phone Mic Cam

Use your phone as your computer's **microphone** and **webcam** over Wi-Fi.
There's no app to install on the phone: scan a QR code, and the phone's
browser streams its mic and camera to the computer.

While it runs, the computer gets:

- a microphone called **Phone Mic**, set as the default input, so every app
  (browser, Zoom, Discord, OBS…) uses it;
- a webcam called **Phone Cam** that apps can choose like any other camera.

When you stop it, your previous default microphone is restored.

> **Platform support:** the computer side runs on **Ubuntu 22.04 or newer and
> Debian 12 or newer** (and distributions based on them), with PulseAudio or
> PipeWire. Every change is tested on Ubuntu 22.04, 24.04 and 26.04 and on
> Debian 12 and 13. Ubuntu 20.04 and older aren't supported. Windows and macOS
> aren't supported yet. Testing so far has used an iPhone with Safari. Android
> with Chrome should work, since it uses the same browser features, but hasn't
> been tested.

## Install

1. Download the latest `phone-mic-cam_<version>_all.deb` from the
   [Releases page](https://github.com/khadgachy07/phone-mic-cam/releases).
2. Install it from the folder you downloaded it to:

   ```bash
   sudo apt install ./phone-mic-cam_*_all.deb
   ```

That's everything. The package installs what it needs from Ubuntu's own
repositories, including ffmpeg and the **v4l2loopback** webcam driver, sets
the driver up as **Phone Cam**, and adds **Phone Mic Cam** to your apps.

**Secure Boot:** if your computer uses Secure Boot (most recent PCs do), it
won't load the new webcam driver until you approve the driver's signing key
once. If that's needed, Phone Mic Cam shows the steps on its control page.
They take one command and a restart. The microphone works either way.

## Use

1. Open **Phone Mic Cam** from your apps. You can pin it to the dock. A
   control page opens in your browser.
2. Scan the QR code on that page with your phone's camera and open the link.
   Your phone needs to be on the same Wi-Fi.
3. Accept the certificate warning. In Safari, tap **Show Details → visit this
   website**. The warning is expected, because the certificate is
   self-signed.
4. Tap **Start mic** and/or **Start camera**, and allow access. **Flip**
   switches between the front and back cameras.
5. In your apps, choose **Phone Mic** and **Phone Cam** if they don't use
   them automatically.

The control page shows whether your phone is connected and whether the mic
and camera are live. While they stream, it also shows live numbers: the
camera's frames per second, data rate and frames skipped, and the mic's data
rate and lag. To finish, press **Stop Phone Mic Cam**. If you close the page,
open the app again and it reopens the page.

Keep the page open on the phone and its screen on. The page tries to stop the
screen from locking.

### The mic turns on automatically

After you tap **Start mic** once, the phone shows **Ready**. It switches to
**Live** by itself whenever an app on the computer starts recording from
Phone Mic, for example a browser's mic button, a voice chat or a recorder.
It goes back to **Ready** about 3 seconds after the last app stops. While
**Ready**, the phone sends no audio.

The one tap is needed because phone browsers only allow mic access after the
user taps something on the page.

To stream the mic all the time instead, start it from a terminal with
`phone-mic-cam --mic-always-on`. It also falls back to always-on if it can't
watch PulseAudio.

## Uninstall

```bash
sudo apt remove phone-mic-cam
```

This removes the app and its webcam-driver settings. ffmpeg and the driver
package stay installed; remove them with `sudo apt autoremove` if nothing else
uses them. Your settings folder, `~/.local/share/phone-mic-cam`, is kept.
Delete it to remove the certificate too.

## How it works

```
 Phone browser                              Computer (phone_mic_cam)
 ─────────────                              ────────────────────────
 mic  ──► 48 kHz PCM ───── wss /ws ──────►  PulseAudio pipe source "Phone Mic"
 cam  ──► JPEG frames ──── wss /video ───►  ffmpeg ──► v4l2loopback "Phone Cam"
```

The app runs a small HTTPS server on port **8443**. Browsers only allow mic
and camera access over HTTPS, so the app creates a self-signed certificate on
first run. The phone page records audio and video and sends them back over
WebSockets.

The webcam needs **v4l2loopback**, a free Linux kernel driver that creates a
*virtual webcam*: a camera device that apps can select like a real one but
that has no hardware behind it. Linux has no built-in way for a program to
create a webcam, which is why the driver is needed.

## Security

- The link in the QR code contains a **random pairing code that changes every
  time the app starts**. Requests without it, including the page and both
  audio and video streams, are refused. This stops other devices on the
  network from sending audio or video into your mic or camera. After a
  restart, scan the new QR code.
- The control page is served only to your own computer (`127.0.0.1`). It
  needs the same pairing code and rejects requests made under other host
  names, so other websites open in your browser can't read it or press Stop.
- The certificate's private key is stored in `~/.local/share/phone-mic-cam`,
  readable only by you. **Never share it.**

## Troubleshooting

| Problem | Fix |
|---|---|
| The camera shows **Not available** | The control page explains why and how to fix it, for example the Secure Boot step. |
| Nothing happens when you open the app | Look at the log from the last run: `~/.local/state/phone-mic-cam/last-run.log`. The run before that is kept as `last-run.previous.log`. |
| "Port 8443 is already in use by another program" | Another program uses that port. Close it and open Phone Mic Cam again. |
| Phone can't open the page | Check that both devices are on the same Wi-Fi. If you use `ufw`, run `sudo ufw allow 8443/tcp`. |
| "Reconnecting… scan the new QR code" | The app was restarted, so the old link's pairing code is no longer valid. Scan the new QR code. |
| Camera not listed in Chrome, or the control page says the driver is loaded but not as Phone Cam | Another app loaded the webcam driver with its own settings. Restart your computer. |
| Mic is still "Phone Mic" after a crash | Choose your normal microphone in Sound settings. |
| Phone goes **Live** with no call or recording | Something is still reading the mic, such as an open Sound settings input meter or a browser tab that kept mic access. Close it, or check which apps are using the mic with `pactl list source-outputs`. |
| The first word gets cut off | The phone needs a moment to switch on. Start talking about a second after you press the app's mic button, or use `--mic-always-on`. |

## Known issues

- On iPhone, **Start camera** is sometimes refused with "The request is not
  allowed by the user agent or the platform in the current context", and
  works when tried again. The microphone isn't affected. Since 0.4.1, when
  Safari grants the camera but pauses its preview, the phone asks for one more
  tap instead of failing, and the computer's log records which step was
  refused (`at permission` or `at preview`). If it happens to you, please open
  an issue with the matching "Phone camera refused" lines from
  `~/.local/state/phone-mic-cam/last-run.log` (or `last-run.previous.log` if
  you restarted the app since).

## Limitations

- Ubuntu 22.04+ and Debian 12+ only for now.
- iOS stops the mic and camera when Safari goes into the background or the
  screen locks.
- Video is sent at 1280×720 landscape. A phone held upright appears with
  black bars at the sides.
- Audio is mono, 48 kHz, with about 40–150 ms of buffering.

## Development

### Running from source

```bash
git clone https://github.com/khadgachy07/phone-mic-cam.git
cd phone-mic-cam
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m phone_mic_cam
```

Options: `--no-browser` (don't open the control page), `--mic-always-on`,
`--log-file PATH` (write the output there; the installed launcher uses this
when there's no terminal), `--version`. When started from a terminal, Ctrl+C
stops it too.

For the camera without the package, set up the webcam driver by hand:

```bash
sudo apt install ffmpeg v4l2loopback-dkms linux-headers-$(uname -r)
sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="Phone Cam" exclusive_caps=1
cat /sys/class/video4linux/video10/name    # should print: Phone Cam
```

| Option | Meaning |
|---|---|
| `devices=1` | Create one virtual webcam. |
| `video_nr=10` | Use `/dev/video10`, which keeps it clear of real cameras. |
| `card_label="Phone Cam"` | The name apps show. It must be exactly this, because that's how the app finds the device. |
| `exclusive_caps=1` | Needed for Chrome and other browsers to list the camera. |

Run the `modprobe` command again after every reboot. The package sets this up
permanently instead.

If `modprobe` fails with `Key was rejected by service`, Secure Boot is
rejecting the driver because its signing key isn't trusted yet. Enroll the
key that Ubuntu signed it with. You only need to do this once:

```bash
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
```

`mokutil` asks you to choose a one-time password. Reboot, and a blue **MOK
management** screen appears. Press a key quickly, because it times out after
about 10 seconds. Choose **Enroll MOK → Continue → Yes**, enter the password,
and reboot. Then run the `modprobe` command again.

Other distributions: Fedora gets the driver from RPM Fusion
(`akmod-v4l2loopback`), and Arch has `v4l2loopback-dkms` plus `linux-headers`.
These haven't been tested yet.

### Building the package

```bash
packaging/build-deb.sh
```

This writes `dist/phone-mic-cam_<version>_all.deb`. The version comes from
`__version__` in `phone_mic_cam/__init__.py`. The `packaging/` folder holds
the launcher, app entry, icon, driver settings and install script.

### Code layout

```
phone_mic_cam/
├── __init__.py        version
├── __main__.py        entry point: python3 -m phone_mic_cam
├── app.py             startup, shutdown, command-line options, certificate, log file
├── audio.py           Phone Mic: virtual mic, audio writer, automatic on/off
├── camera.py          Phone Cam: ffmpeg, frame handling, driver diagnosis
├── phone_server.py    what the phone talks to: page, mic, camera, error reports
├── control_page.py    the control page on this computer: status, Stop, reopening
├── stats.py           live numbers shown on the control page
└── web/
    ├── phone.html  phone.js      the page on the phone
    └── control.html  control.js  the control page on this computer
```

The pages are plain HTML and JavaScript with no build step. Each page's
script is inlined into its HTML when the app starts, so it's served as one
document behind the pairing code, as before.

### Running the tests

```bash
python3 -m unittest discover -s tests -v
```

The tests need only the app's own Python dependencies and use only
`127.0.0.1`, without touching PulseAudio or the webcam driver. The page
script check needs `node`, the camera check needs `ffmpeg` and the package
check needs `dpkg-deb`; each is skipped when its tool isn't installed.

GitHub Actions runs the tests on every push and pull request in Docker
containers of each supported system (Ubuntu 22.04, 24.04 and 26.04, Debian 12
and 13), using that system's own packages, and checks that the built `.deb`
installs there. It keeps the `.deb` as a download on each run. To repeat one
system's run locally:

```bash
docker run --rm -it -v "$PWD":/src:ro ubuntu:22.04 bash
# then, inside: apt-get update && apt-get install -y python3-aiohttp python3-qrcode ffmpeg nodejs dpkg-dev
# cp -r /src /w && cd /w && python3 -m unittest discover -s tests -v
```

## Contributing

Issues and pull requests are welcome. Windows and macOS support, and packages
for other Linux distributions, are the most wanted features. Please test
changes on a real phone, and describe what you tested in the pull request.

## License

[MIT](LICENSE)
